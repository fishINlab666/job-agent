"""复现方案 016 §9 的命令 B / E / G。只读真库 —— 所有试算都在临时副本上。

三条各自回答一个问题：
    count        存量有多少行「指纹与列不同步」，且这个数能不能被解释
    dry-sync     修完之后这批行真的一条事件都不发吗
    before-after 改前会发多少条、其中多少条是空的（不然「修好了」只是我说的）

`before-after` 用源码文本替换重建一个「改前」的模块。这样做而不是 git stash：
stash 会动工作区，跑一半被打断就留下一个不知道是改前还是改后的仓库。
"""
from __future__ import annotations

import collections
import importlib.util
import json
import pathlib
import shutil
import sys
import tempfile

from jobagent import db, ingest
from jobagent.adapters.base import RawJob

SRC = "feishu:bytedance:campus"   # 最大的一个源，7368 行


def _mk(r: dict) -> RawJob:
    """按库里的列重建 RawJob —— 模拟「源站没变，只是指纹过期」。

    必须是真 dataclass：`sync` 里 `asdict(j)` 对普通对象会抛。
    """
    return RawJob(
        external_id=r["external_id"], title=r["title"], raw_json={},
        job_family=r["job_family"],
        cities=json.loads(r["cities"]) if r["cities"] else [],
        recruit_type=r["recruit_type"], grad_year=r["grad_year"],
        apply_url=r["apply_url"], raw_category=r["raw_category"],
        raw_location=r["raw_location"], country=r["country"],
        department=r["department"], apply_system=r["apply_system"],
        description=r["description"],
    )


def _adapter(jobs: list[RawJob]):
    class A:
        source_key = SRC
        company = "字节跳动"
        system = "feishu"
        entry_url = "https://example.test"
        tenant = "bytedance"

        def fetch(self):
            return jobs
    return A()


def _copy_db() -> tuple[pathlib.Path, object]:
    d = pathlib.Path(tempfile.mkdtemp())
    p = d / "copy.db"
    shutil.copy(db.DB_PATH, p)
    return d, db.connect(p)


def _live_jobs(con, source: str = SRC) -> list[RawJob]:
    return [_mk(dict(r)) for r in con.execute(
        "SELECT * FROM jobs WHERE source_key=? AND closed_at IS NULL", (source,))]


def count() -> None:
    """存量有多少行指纹与列不同步，以及这个数能不能被解释。"""
    con = db.connect_readonly()
    rows = [dict(r) for r in con.execute("SELECT * FROM jobs")]
    bad, per = [], collections.Counter()
    for r in rows:
        if ingest._fp(_mk(r)) != r["fingerprint"]:
            bad.append(r)
            per[r["source_key"]] += 1
    print(f"全库 {len(rows)} 行，指纹与列不同步 {len(bad)} 行（{len(bad) / len(rows):.1%}）")
    print(f"其中在架：{sum(1 for r in bad if r['closed_at'] is None)}")
    print(f"按源：{dict(per)}")

    # 两个数恰好相等（8594 = 飞书四源在架行全部）本该先当算错看。
    # 验法是反向重建：剥掉 repair_apply_url 加的 /detail 后缀重算指纹再比。
    explained = 0
    unexplained = []
    for r in bad:
        u = r["apply_url"] or ""
        old = u[: -len("/detail")] if u.endswith("/detail") else u
        probe = dict(r, apply_url=old)
        if ingest._fp(_mk(probe)) == r["fingerprint"]:
            explained += 1
        else:
            unexplained.append(r)
    print(f"\n能被「/detail 后缀」解释：{explained} / {len(bad)}，解释不了 {len(unexplained)}")
    for r in unexplained[:5]:
        print(f"  {r['source_key']} {r['title']!r} {r['apply_url']!r}")

    # 反证我的 _fp 重建没有系统性错误：腾讯没被 repair 过，必须全对得上。
    # 少了这一条，「飞书全不同步」也可能只是我重建错了。
    tx = [r for r in rows if r["source_key"] == "tencent_join"]
    ok = sum(1 for r in tx if ingest._fp(_mk(r)) == r["fingerprint"])
    print(f"\n对照组 tencent_join（没被 repair 过）：{ok} / {len(tx)} 指纹对得上")
    if ok != len(tx):
        print("  **对照组也不同步** —— 先别信上面的数，可能是 _fp 重建错了")


def dry_sync() -> None:
    """在临时副本上跑两轮 sync，数事件。验「修完真的静默」。"""
    d, con = _copy_db()
    try:
        base = con.execute("SELECT MAX(id) i FROM events").fetchone()["i"] or 0
        jobs = _live_jobs(con)
        st = ingest.sync(con, _adapter(jobs))
        print(f"喂进 {len(jobs)} 行（列没变，只有指纹过期）")
        print(f"  updated={st['updated']}  fingerprint_desync={st['fingerprint_desync']}")
        print(f"  本轮新增事件：{_events_since(con, base) or '无'}")

        mid = con.execute("SELECT MAX(id) i FROM events").fetchone()["i"] or 0
        st2 = ingest.sync(con, _adapter(jobs))
        print(f"\n第二轮（指纹已重算）：updated={st2['updated']}  "
              f"desync={st2['fingerprint_desync']}  新增事件：{_events_since(con, mid) or '无'}")
        if st2["fingerprint_desync"]:
            print("  **第二轮还非 0** —— 指纹没重算成功，这个信号会永久失真")
    finally:
        con.close()
        shutil.rmtree(d)


def _events_since(con, after: int) -> dict:
    return {r["kind"]: r["c"] for r in con.execute(
        "SELECT kind, COUNT(*) c FROM events WHERE id>? GROUP BY kind", (after,))}


def before_after() -> None:
    """改前会发多少条事件、其中多少条空 diff。对照组，不然「修好了」只是我说的。"""
    src = pathlib.Path("jobagent/ingest.py").read_text()
    old = src.replace("if not bootstrap and not fp_desync:", "if not bootstrap:")
    if old == src:
        print("**剥离没生效** —— 守卫那行的写法变了，这个对照现在是假的")
        return 1

    # 用真 package 名建模块，否则 ingest.py 里的相对 import 找不到 parent。
    tmp = pathlib.Path(tempfile.mkdtemp()) / "ingest_before.py"
    tmp.write_text(old)
    spec = importlib.util.spec_from_file_location("jobagent.ingest_before", tmp)
    before = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = before
    spec.loader.exec_module(before)

    d, con = _copy_db()
    try:
        base = con.execute("SELECT MAX(id) i FROM events").fetchone()["i"] or 0
        before.sync(con, _adapter(_live_jobs(con)))
        print(f"【改前】新增事件：{_events_since(con, base)}")
        rows = con.execute(
            "SELECT payload FROM events WHERE kind='job_updated' AND id>?", (base,)
        ).fetchall()
        empty = sum(1 for r in rows if json.loads(r["payload"])["diff"] == {})
        print(f"  其中 diff 为空：{empty} / {len(rows)}")
    finally:
        con.close()
        shutil.rmtree(d)
        shutil.rmtree(tmp.parent)
    return None


COMMANDS = {"count": ("B", count), "dry-sync": ("E", dry_sync),
            "before-after": ("G", before_after)}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        print("子命令（括号里是方案 016 §9 的编号）：")
        for name, (tag, fn) in COMMANDS.items():
            print(f"  {name:<14} [{tag}]  {(fn.__doc__ or '').splitlines()[0]}")
        return 1 if len(sys.argv) != 1 else 0
    tag, fn = COMMANDS[sys.argv[1]]
    print(f"=== {sys.argv[1]}  （方案 016 §9 命令 {tag}）===\n")
    return fn() or 0


if __name__ == "__main__":
    raise SystemExit(main())
