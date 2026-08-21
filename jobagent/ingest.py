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
from pathlib import Path

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


class RefreshUnsupported(RuntimeError):
    """这个源的适配器没有重算届别的规则。

    和「刷新失败」分开：飞书四家 8594 条 `grad_year` 全是 NULL，因为源站
    没有这个字段 —— 那不是坏了，是没有可重算的东西。混成一个错会让调用方
    以为要去修飞书适配器。
    """


def refresh_grad_year(conn, adapter, *, apply: bool = False) -> dict:
    """按适配器当前的推导规则，重算存量岗位的 `grad_year`。

    为什么需要这条命令：`grad_year` 不在 `_fp()` 里（上面那个函数），所以
    换季改了常量之后 `sync` 不更新存量 —— 指纹没变就走「只动 last_seen_at」
    那条分支。实测 2026-08-09 真库：807 行里 804 行会被静默跳过，只有 1 行
    因为别的字段也变了而搭上便车。表现是「改了代码、sync 说 updated=0、
    库里没动」，而那 1 行会让人以为生效了。

    输入取 `snapshots.raw_json` 的最新一条，**不联网**。快照是已经落库的原始
    观测，用它重算就不必信任「现在的源站和当初抓的是同一批岗位」——
    已下架的岗位在源站上取不到，但它的届别照样该修对。

    三条硬约束（各有一条测试守着，见 tests/test_cli.py）：

    1. **只写 `grad_year` 一列，不碰 `fingerprint`。** 顺手重算指纹会让全部
       9401 行的哈希都变（实测：加一个键就全不等），造出 9401 条 diff 为空的
       假 `job_updated` —— 那是 plan 006 问题 1 的同型放大。
    2. **幂等。** 值相同的行不进 UPDATE，跑第二次报 0。
    3. **有值不许被静默改成 NULL。** 源站改了 `recruitLabelName` 的字面量时，
       重算会掉成 `None`；此时保留旧值并计入 `skipped_would_null` 报出来。
       `ingest.py` 开头写着「宁可漏报，不可误报」，静默把 807 个好值抹成 NULL
       是反方向。这条是本函数唯一一处「明知新值却不写」的地方。
    """
    recompute = getattr(type(adapter), "grad_year_from_raw", None)
    if recompute is None:
        raise RefreshUnsupported(
            f"{type(adapter).__name__} 没有 grad_year_from_raw()，"
            f"这个源的届别不是推导出来的（源站没有这个字段），没有可重算的东西"
        )

    src = adapter.source_key
    # 每个 external_id 取最新一条快照。id 自增单调，所以 MAX(id) 就是最新。
    # 不按 captured_at：同一轮里所有快照共用一个 ts，比不出先后。
    snaps = {
        r["external_id"]: r["raw_json"]
        for r in conn.execute(
            """SELECT s.external_id, s.raw_json FROM snapshots s
               JOIN (SELECT external_id, MAX(id) AS mid FROM snapshots
                     WHERE source_key=? GROUP BY external_id) t
                 ON s.id = t.mid""",
            (src,),
        )
    }

    stats = {
        "source": src,
        "examined": 0,
        "changed": 0,
        "unchanged": 0,
        "no_snapshot": 0,
        "skipped_would_null": 0,
        # (旧值, 新值) → 条数。报给用户看的是这个，不是一个总数 ——
        # 「改了 441 行」不如「'27'→'不限' 348 条」能让人当场判断对不对。
        "transitions": {},
        "applied": apply,
    }

    # 已关闭的岗位也一起刷。它们的届别同样是错的，而 reopen 走的是
    # sync 的 UPDATE 分支、不归这里管 —— 留着错值等 reopen 是碰运气。
    rows = conn.execute(
        "SELECT id, external_id, grad_year FROM jobs WHERE source_key=?", (src,)
    ).fetchall()

    for row in rows:
        stats["examined"] += 1
        raw = snaps.get(row["external_id"])
        if raw is None:
            stats["no_snapshot"] += 1
            continue

        old = row["grad_year"]
        new = recompute(json.loads(raw))

        if old == new:
            stats["unchanged"] += 1
            continue
        if new is None and old is not None:
            stats["skipped_would_null"] += 1
            continue

        stats["changed"] += 1
        stats["transitions"][(old, new)] = stats["transitions"].get((old, new), 0) + 1
        if apply:
            # 只有这一列。fingerprint / last_seen_at 都不动：本命令不是一次观测，
            # 它没有「又见到这个岗位」的语义。
            conn.execute("UPDATE jobs SET grad_year=? WHERE id=?", (new, row["id"]))

    if apply:
        conn.commit()
    return stats


def repair_apply_url(conn, *, source_prefix: str = "feishu", apply: bool = False) -> dict:
    """给存量飞书 `apply_url` 补上漏掉的 `/detail` 后缀。

    为什么需要这条命令（而不是等 `sync` 自己修）：`apply_url` **在** `_fp()` 里
    （见上面那个函数）。走 `sync` 的话 8594 行指纹全变，走 UPDATE 分支，
    造出 8594 条 `job_updated` 事件，每条 diff 都是
    `apply_url: 老形状 → 新形状`。那是**噪声不是信号** —— 用户订阅「岗位有变化」
    是想知道岗位变了，不是想知道我们修了个 bug。

    这和 `refresh_grad_year` 是镜像关系，值得写清楚免得下次搞混：

        refresh_grad_year : 字段**不在**指纹里 → sync **不会**修 → 需要这条命令
        repair_apply_url  : 字段**在**指纹里   → sync **会**修但会造假事件 → 需要这条命令

    两个方向相反，但结论一样：单列原地改，不碰指纹。

    **不联网、不读快照。** 修的是 URL 的拼接形状，老值里已经有全部素材
    （host、门户段、id），加个后缀就行。读快照反而会把「源站现在还有没有这个
    岗位」这个无关的问题混进来。

    三条硬约束照抄 `refresh_grad_year`（各有测试守着，见 tests/test_ingest.py）：

    1. **只写 `apply_url` 一列，不碰 `fingerprint` / `last_seen_at`。**
       这条命令不是一次观测，它没有「又见到这个岗位」的语义。
    2. **幂等。** 判据是逐行看结尾（`/detail` 结尾的跳过），不是按 source_key
       整批拼。按批拼会把已修好的行拼成 `/detail/detail`，跑第二次就烂。
    3. **形状不对的行保留原值并报出来，不写 NULL 也不硬拼。**
       老源那批 `/index/position/<id>` 也是正常形状，照修；
       但压根不含 `/position/` 的（将来源站改版）算不认识，计入
       `skipped_unknown_shape`，宁可留个死链让人看见，也不静默编一个新的。

    已关闭的岗位一起修：它们的链接同样是死的，而人工核对历史投递时照样要点开。
    """
    stats = {
        "source_prefix": source_prefix,
        "examined": 0,
        "changed": 0,
        "already_ok": 0,
        "skipped_unknown_shape": 0,
        "skipped_empty": 0,
        "applied": apply,
    }

    rows = conn.execute(
        "SELECT id, apply_url FROM jobs WHERE source_key LIKE ?",
        (f"{source_prefix}%",),
    ).fetchall()

    for row in rows:
        stats["examined"] += 1
        old = row["apply_url"]

        if not old:
            # 空值本来就没链接可修。不编一个出来。
            stats["skipped_empty"] += 1
            continue
        if old.endswith("/detail"):
            stats["already_ok"] += 1
            continue
        if "/position/" not in old:
            stats["skipped_unknown_shape"] += 1
            continue

        new = f"{old.rstrip('/')}/detail"
        stats["changed"] += 1
        if apply:
            # 只有这一列。fingerprint 不动 —— 理由见 docstring。
            conn.execute("UPDATE jobs SET apply_url=? WHERE id=?", (new, row["id"]))

    if apply:
        conn.commit()
    return stats


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


class _NoCommit:
    """dry-run 用：记账照写主连接，但把 commit 吞掉，留给结尾那句 rollback。

    去掉 `commit=` 形参之后 register_source/start_run 变成无条件提交，
    dry-run 下如果直接把 side 设成 conn，这两句 commit 就落在主连接上，
    结尾那句 rollback() 无事可回 —— 库里留下 sources 一行 + runs 一行
    永远 running，正是 db.start_run 注释里记的「cli status 说假话」那个 bug。
    """

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def commit(self):
        pass


def sync(conn, adapter: Adapter, *, dry_run: bool = False) -> dict:
    """跑一次采集 + 增量判定，返回统计摘要。"""
    write = not dry_run
    # 侧连接必须跟随主连接的库文件，不能写 bare db.connect()（会打到真库）。
    dbfile = conn.execute("PRAGMA database_list").fetchone()[2]
    side = db.connect(Path(dbfile)) if write else _NoCommit(conn)
    db.register_source(
        side, adapter.source_key, adapter.company, adapter.system, adapter.entry_url,
        # 多租户 ATS 的适配器才有 tenant（feishu:xiaopeng 的 xiaopeng）。
        # 自建的没有这个属性，取到 None，register_source 会保留库里原值。
        tenant=getattr(adapter, "tenant", None),
    )
    run_id = db.start_run(side, adapter.source_key)
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
        # 指纹变了但六个字段全等的行数 = 有人绕过 sync 只改了列。
        # 正常恒为 0，非 0 说明刚跑过修复命令或者有 bug。见方案 016。
        # 这个数**必须打到 CLI 输出里**：只判空不报出来，等于把不一致状态藏起来，
        # 换个字段又会以别的形式复发（那正是这个 bug 第三次出现的原因）。
        "fingerprint_desync": 0,
    }

    try:
        jobs = adapter.fetch()

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
                    # 指纹变了但六个字段逐个比全等 = 库里存着一个不一致状态：
                    # 有人只改了列没重算指纹（repair_apply_url 按自己的硬约束 1
                    # 就是这么干的，8594 行，实测 2026-08-13）。这时候 diff 是空的，
                    # 发出去就是一条「岗位 XXX 有更新」点开什么都没有。
                    #
                    # 判据刻意用 `diff == {}` 而不是「apply_url 变了但值相同」这种
                    # 具体成因：上面那段注释记的 cities 是同一个症状的第一次，
                    # 这是第三次。按成因修了两回还是复发，所以这次守的是形状 ——
                    # diff 是发事件那一刻手上唯一的事实，空的就没有可通知的东西。
                    #
                    # reopened 不受这条守卫管（见下面发事件那行的条件）：下线又
                    # 原样上线时 diff 确实是空的，但 closed_at 从有值变 NULL 压根
                    # 不在这六个字段里，判空会吃掉一个真实信号。
                    fp_desync = not diff and not reopened
                    if fp_desync:
                        stats["fingerprint_desync"] += 1
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
                    # UPDATE 照旧执行（在上面），只是不发事件 —— 指纹必须重算，
                    # 否则每轮 sync 都会重新进这个分支，fingerprint_desync 永久非 0，
                    # 「不同步」这个信号就失真了。
                    if not bootstrap and not fp_desync:
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
        if guard:
            db.add_event(
                conn,
                "source_degraded",
                source_key=adapter.source_key,
                company=adapter.company,
                payload={
                    "disappeared": len(disappeared),
                    "live_before": len(live_before),
                    "error": err,
                },
                run_id=run_id,
            )
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception as exc:
        # 业务数据先退，再写痕迹 —— 顺序是硬的：主连接还持着写事务时
        # 侧连接写 runs 会撞 database is locked。
        conn.rollback()
        error = str(exc)
        if write:
            try:
                db.add_event(
                    side,
                    "source_sync_failed",
                    source_key=adapter.source_key,
                    company=adapter.company,
                    payload={"error": error},
                    run_id=run_id,
                )
                db.finish_run(side, run_id, "failed", 0, error)
            except Exception as record_exc:
                # 告警链自己坏了不能遮住真正的采集错误。先撤掉可能只写了一半的
                # 告警，再尽力把 run 收成 failed；无论二次留痕是否成功，最后都
                # 重新抛最初的 exc，而不是 record_exc。
                side.rollback()
                fallback_error = f"{error}；失败告警未写入：{record_exc}"
                try:
                    db.finish_run(side, run_id, "failed", 0, fallback_error)
                except Exception:
                    side.rollback()
        else:
            db.finish_run(side, run_id, "failed", 0, error)
        raise
    else:
        db.finish_run(side, run_id, status, len(jobs), err)
    finally:
        if write:
            side.close()
    return stats
