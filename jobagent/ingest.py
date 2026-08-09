"""M1 采集 + M2 归一与增量。

两个必须在代码里防住的坑：

坑一：上游半残返回。
  如果接口只返回了一半数据（分页挂了、限流、改版），朴素 diff 会把另一半
  判成「已关闭」，瞬间生成几百条假 job_closed，用户直接失去信任。
  防法：消失比例超过 CLOSE_GUARD_RATIO 就拒绝关闭任何岗位，
  把 run 标成 partial 等人工确认。宁可漏报关闭，不可误报关闭。

坑二：首次抓取的事件洪水。
  第一次跑，795 个岗位全是「新增」。用户不需要 795 条通知。
  防法：首轮识别为 bootstrap，只落库不发单条事件，
  改发一条汇总的 source_bootstrapped。
"""
from __future__ import annotations

import json
from dataclasses import asdict

from . import db
from .adapters.base import Adapter, RawJob
from .normalize import fingerprint

# 消失比例超过这个值就不执行关闭，判定为上游异常
CLOSE_GUARD_RATIO = 0.4
# 但消失数量低于这个绝对值时不启用比例守卫。
# 原因：小源（某家 AI 公司可能只有 3 个校招岗）关掉 2 个是正常的季节性行为，
# 比例却高达 67%，会被守卫误挡，导致岗位永远关不掉。
# 比例守卫防的是「上游半残返回导致的批量假关闭」，那类故障必然伴随较大的绝对量。
CLOSE_GUARD_MIN_COUNT = 5
# 单轮新增超过这个数，额外发一条「批次启动」事件
BATCH_THRESHOLD = 15


def _cities(value: str | list[str] | None) -> list[str]:
    """把 cities 归一成排好序的 list，两种形态都接：库里是 JSON 串，内存里是 list。

    排序是**指纹和 diff 必须共用**的口径。指纹算的是排序后的值，而下面
    insert/update 两处存的是适配器给的原始顺序（`json.dumps(j.cities, ...)`）——
    真库 9401 行里有 1090 行本身没排序。所以比较前不排序的话，同一个岗位会出现
    「指纹说没变、diff 说城市变了」的自相矛盾。
    `_fp` 和下面建 diff 的地方都走这个函数，是为了让这个口径没法被改跑偏。
    这里不写行号：这个函数本身就把下面的行号推移过一次。
    """
    if isinstance(value, str):
        value = json.loads(value or "[]")
    return sorted(value or [])


def _fp(job: RawJob) -> str:
    """只覆盖「变了就该通知」的字段，description 刻意不含。"""
    return fingerprint(
        {
            "title": job.title,
            "family": job.job_family,
            "cities": _cities(job.cities),
            "recruit_type": job.recruit_type,
            "department": job.department,
            "apply_url": job.apply_url,
        }
    )


def _is_bootstrap(conn, source_key: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE source_key=?", (source_key,)
    ).fetchone()
    return int(row["n"]) == 0


def _open_families(conn, company: str) -> set[tuple[str, str]]:
    """当前该公司有开放岗位的 (岗位族, 招聘类型) 集合。"""
    rows = conn.execute(
        """SELECT DISTINCT job_family, recruit_type FROM jobs
           WHERE company=? AND closed_at IS NULL""",
        (company,),
    ).fetchall()
    return {(r["job_family"], r["recruit_type"]) for r in rows}


def sync(conn, adapter: Adapter, *, dry_run: bool = False) -> dict:
    """跑一次采集 + 增量判定，返回统计摘要。"""
    # dry-run 靠函数结尾那句 conn.rollback() 把整轮抹掉，所以这一路上**任何**
    # 写动作都不许自己 commit —— commit 过的东西 rollback 不回来。
    # 这两个原来是无条件 commit 的，于是 `sync --dry-run` 往真库里留下了
    # sources 一行 + runs 一行永远 running 的记录，后者让 cli status 说假话。
    write = not dry_run
    db.register_source(
        conn, adapter.source_key, adapter.company, adapter.system, adapter.entry_url,
        # 多租户 ATS 的适配器才有 tenant（feishu:xiaopeng 的 xiaopeng）。
        # 自建的没有这个属性，取到 None，register_source 会保留库里原值。
        tenant=getattr(adapter, "tenant", None),
        commit=write,
    )
    run_id = db.start_run(conn, adapter.source_key, commit=write)
    stats = {
        "source": adapter.source_key,
        "bootstrap": False,
        "fetched": 0,
        "opened": 0,
        "updated": 0,
        "closed": 0,
        "guard_tripped": False,
        "families_first_seen": [],
        # 这一批里 job_family 判不出的条数。**当场数，不跟任何基线比** ——
        # 飞书上这个比例是 48.5%(nio) / 33.1%(xiaopeng)，按租户差 15 个百分点，
        # 而且岗位池自己在动，所以写死一个阈值必然错。见 002 §7。
        "family_unknown": 0,
    }

    try:
        jobs = adapter.fetch()
    except Exception as exc:
        db.finish_run(conn, run_id, "failed", 0, str(exc), commit=write)
        if dry_run:
            conn.rollback()
        raise

    # 空结果**默认**不是合法状态：招聘站不可能一个岗位都没有。
    # 这个检查必须在 ingest 层，不能指望每个适配器作者都记得写 ——
    # 一旦漏掉，diff 会把全部岗位判成关闭，是本项目最危险的静默故障。
    #
    # 唯一的例外：适配器能**说清空的原因**。飞书那个接口会明确回
    # code=0 + count=0（真租户、当下没在招，实测 luckin/horizon 就是这样），
    # 那是一个事实，不是故障。区分交给适配器做，ingest 不猜：
    #   适配器返回空且说不清为什么 = 「我没拿到数据」→ 抛
    #   接口明确回了 count=0        = 「这家现在没岗位」→ 正常走完，opened=0
    # getattr 带默认 False：没声明这个属性的适配器（腾讯）行为完全不变，
    # 新语义默认关闭，只有显式声明的适配器才享受。
    if not jobs:
        if not getattr(adapter, "empty_is_authoritative", False):
            msg = f"{adapter.source_key} 返回 0 条，判定为上游异常，拒绝按「全部关闭」处理"
            db.finish_run(conn, run_id, "failed", 0, msg, commit=write)
            # 抛之前也要回滚：不然 start_run 那条未提交的 INSERT 会挂在事务里，
            # 被下一个源的 commit 顺手带进库。
            if dry_run:
                conn.rollback()
            raise RuntimeError(msg)

    stats["fetched"] = len(jobs)
    # 分母是「这一次抓到的条数」，和 fetched 同一个粒度 —— 不是新增的条数。
    # 判不出族的岗位照样入库，只是按族筛不到，所以要报的是全量占比。
    stats["family_unknown"] = sum(1 for j in jobs if j.job_family is None)
    bootstrap = _is_bootstrap(conn, adapter.source_key)
    stats["bootstrap"] = bootstrap
    families_before = _open_families(conn, adapter.company)
    ts = db.now()

    # 落原始快照
    for j in jobs:
        conn.execute(
            """INSERT INTO snapshots(run_id, source_key, external_id, fingerprint, raw_json, captured_at)
               VALUES(?,?,?,?,?,?)""",
            (
                run_id,
                adapter.source_key,
                j.external_id,
                _fp(j),
                json.dumps(j.raw_json, ensure_ascii=False),
                ts,
            ),
        )

    existing = {
        r["external_id"]: r
        for r in conn.execute(
            "SELECT * FROM jobs WHERE source_key=?", (adapter.source_key,)
        ).fetchall()
    }
    seen_ids = {j.external_id for j in jobs}

    # 坑一的守卫：先算消失比例，再决定要不要关闭
    live_before = {k: v for k, v in existing.items() if v["closed_at"] is None}
    disappeared = [k for k in live_before if k not in seen_ids]
    guard = (
        len(disappeared) >= CLOSE_GUARD_MIN_COUNT
        and bool(live_before)
        and (len(disappeared) / len(live_before)) > CLOSE_GUARD_RATIO
    )
    stats["guard_tripped"] = guard

    for j in jobs:
        fp = _fp(j)
        prev = existing.get(j.external_id)
        payload = asdict(j)
        payload.pop("raw_json", None)

        if prev is None:
            cur = conn.execute(
                """INSERT INTO jobs(source_key, external_id, company, title, job_family,
                       raw_category, cities, raw_location, country, department,
                       recruit_type, grad_year, apply_url, apply_system, description,
                       fingerprint, first_seen_at, last_seen_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    adapter.source_key, j.external_id, adapter.company, j.title,
                    j.job_family, j.raw_category, json.dumps(j.cities, ensure_ascii=False),
                    j.raw_location, j.country, j.department, j.recruit_type,
                    j.grad_year, j.apply_url, j.apply_system, j.description,
                    fp, ts, ts,
                ),
            )
            stats["opened"] += 1
            if not bootstrap:
                db.add_event(
                    conn, "job_opened", source_key=adapter.source_key,
                    company=adapter.company, job_id=int(cur.lastrowid),
                    payload=payload, run_id=run_id,
                )
        else:
            reopened = prev["closed_at"] is not None
            if prev["fingerprint"] != fp or reopened:
                diff = {
                    k: {"from": prev[k], "to": getattr(j, k)}
                    for k in ("title", "job_family", "recruit_type", "department", "apply_url")
                    if prev[k] != getattr(j, k)
                }
                # cities 单独比，不能塞进上面那个推导式：库里存的是 JSON 字符串，
                # 内存里是 list，直接比永远不等 —— 会把每个岗位都判成城市变了。
                # 两边都过 _cities() 归一，和指纹同口径。
                # 这个字段原来不在 diff 里，而指纹里有它（见 _fp）—— 于是
                # 「只有城市变了」会触发 job_updated 但 diff 是空的，用户收到
                # 「岗位 XXX 有更新」后面什么都没有。真库里这样的事件有 16 条，
                # 源站原文里变的字段全是 workCities。见方案 006 问题 1。
                prev_cities, now_cities = _cities(prev["cities"]), _cities(j.cities)
                if prev_cities != now_cities:
                    diff["cities"] = {"from": prev_cities, "to": now_cities}
                conn.execute(
                    """UPDATE jobs SET title=?, job_family=?, raw_category=?, cities=?,
                           raw_location=?, department=?, recruit_type=?, grad_year=?,
                           apply_url=?, apply_system=?, fingerprint=?, last_seen_at=?,
                           closed_at=NULL
                       WHERE id=?""",
                    (
                        j.title, j.job_family, j.raw_category,
                        json.dumps(j.cities, ensure_ascii=False), j.raw_location,
                        j.department, j.recruit_type, j.grad_year, j.apply_url,
                        j.apply_system, fp, ts, prev["id"],
                    ),
                )
                stats["updated"] += 1
                if not bootstrap:
                    db.add_event(
                        conn, "job_reopened" if reopened else "job_updated",
                        source_key=adapter.source_key, company=adapter.company,
                        job_id=int(prev["id"]), payload={"diff": diff, **payload},
                        run_id=run_id,
                    )
            else:
                conn.execute(
                    "UPDATE jobs SET last_seen_at=? WHERE id=?", (ts, prev["id"])
                )

    # 关闭判定
    if disappeared and not guard:
        for ext_id in disappeared:
            prev = existing[ext_id]
            conn.execute(
                "UPDATE jobs SET closed_at=?, last_seen_at=last_seen_at WHERE id=?",
                (ts, prev["id"]),
            )
            stats["closed"] += 1
            if not bootstrap:
                db.add_event(
                    conn, "job_closed", source_key=adapter.source_key,
                    company=adapter.company, job_id=int(prev["id"]),
                    payload={"title": prev["title"]}, run_id=run_id,
                )

    # family_first_seen：某公司某岗位族从 0 变非 0，最有价值的信号
    if not bootstrap:
        for fam, rtype in _open_families(conn, adapter.company) - families_before:
            stats["families_first_seen"].append(f"{fam}/{rtype}")
            db.add_event(
                conn, "family_first_seen", source_key=adapter.source_key,
                company=adapter.company,
                payload={"job_family": fam, "recruit_type": rtype}, run_id=run_id,
            )
        if stats["opened"] >= BATCH_THRESHOLD:
            db.add_event(
                conn, "batch_started", source_key=adapter.source_key,
                company=adapter.company, payload={"count": stats["opened"]},
                run_id=run_id,
            )
    else:
        db.add_event(
            conn, "source_bootstrapped", source_key=adapter.source_key,
            company=adapter.company,
            payload={"count": stats["opened"]}, run_id=run_id,
        )

    status = "partial" if guard else "ok"
    err = (
        f"关闭守卫触发：{len(disappeared)}/{len(live_before)} 个岗位消失，"
        f"超过阈值 {CLOSE_GUARD_RATIO:.0%}，本轮不执行关闭"
        if guard else None
    )
    if dry_run:
        conn.rollback()
    else:
        db.finish_run(conn, run_id, status, len(jobs), err)
        conn.commit()
    return stats
