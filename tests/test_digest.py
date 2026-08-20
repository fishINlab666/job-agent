"""digest 的 job_updated 渲染测试。

这个分支原来零真实覆盖 —— `test_e2e.py` 里那句是 `print("  - digest: ✓")`，
一句 print，不是断言。于是「生产端写 from/to、消费端读 old/new」这个
KeyError 活了下来（方案 006 问题 0）。

**硬要求：负载不许手搓。**
如果测试自己写 `{"title": {"from": …, "to": …}}` 再断言渲染成功，那它测的是
「我以为生产端写什么」—— 而这次的 bug 恰恰是两端各以为对方写什么。
所以事件一律走 `ingest.sync()` 造、从 events 表读出来，契约两端都在测试里，
改任何一端都会红。
"""
from __future__ import annotations

import inspect
import json

import pytest

from jobagent import cli, db, ingest, match
from jobagent.adapters.base import RawJob


class FakeAdapter:
    source_key = "fake_src"
    company = "测试公司"
    system = "self_built"
    entry_url = "https://example.test"

    def __init__(self, jobs: list[RawJob]) -> None:
        self._jobs = jobs

    def fetch(self) -> list[RawJob]:
        return self._jobs


def make_job(
    ext_id: str = "1",
    title: str = "产品运营",
    family: str = "operations",
    cities: list[str] | None = None,
) -> RawJob:
    # None 是哨兵，不是默认值：要能区分「没传」和「传了空列表」——
    # 城市变空要走到渲染的「未写」分支。
    return RawJob(
        external_id=ext_id,
        title=title,
        raw_json={"id": ext_id, "title": title},
        job_family=family,
        cities=["北京"] if cities is None else cities,
        recruit_type="campus",
        grad_year="27",
        apply_url=f"https://example.test/{ext_id}",
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    """把 digest 的两个全局依赖指到临时目录。

    `digest()` 自己 `db.connect()`（无参 → db.DB_PATH）+ `match.load_profile()`
    （无参 → match.PROFILE_PATH），两个都指向真库和真档案。不改这两个，
    测试会读到 data/jobagent.db 和带真实姓名手机邮箱的 profile.yaml。
    """
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(
        match,
        "PROFILE_PATH",
        tmp_path / "profile.yaml",
    )
    (tmp_path / "profile.yaml").write_text(
        json.dumps(
            {
                "intent": {
                    # 两个族都要写：改族那条用例从 operations 变到 marketing，
                    # 只写一个的话变更后的岗位过不了 worth_showing，用例就假绿了
                    "families": ["operations", "marketing"],
                    "recruit_types": ["campus"],
                    "grad_years": ["27"],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # rich 默认按终端宽度折行，窄终端下断言会莫名其妙地失败
    monkeypatch.setattr(cli.console, "width", 200)
    conn = db.connect(tmp_path / "t.db")
    db.init(conn)
    conn.commit()
    yield conn
    conn.close()


def make_update_event(conn, **changes) -> dict:
    """跑两轮 sync 造一条真的 job_updated，返回它的 payload。

    第一轮是 bootstrap（不发单条事件），第二轮改字段才产生 job_updated。
    """
    ingest.sync(conn, FakeAdapter([make_job()]))
    ingest.sync(conn, FakeAdapter([make_job(**changes)]))
    row = conn.execute(
        "SELECT payload FROM events WHERE kind='job_updated'"
    ).fetchone()
    assert row is not None, "两轮 sync 没造出 job_updated，用例本身坏了"
    return json.loads(row["payload"])


class TestDiffContract:
    """契约本身：生产端写的内层键是什么。"""

    def test_producer_writes_from_to(self, env) -> None:
        """diff 的内层键是 from/to。

        这条是消费端那几行的依据。谁把生产端改成 old/new，这条先红。
        """
        payload = make_update_event(env, title="产品运营（高级）")
        diff = payload["diff"]
        assert diff, "改了标题却没进 diff"
        for field, change in diff.items():
            assert set(change) == {"from", "to"}, f"{field} 的内层键不是 from/to"

    def test_consumer_reads_the_keys_producer_writes(self, env) -> None:
        """渲染读的键和生产端写的键是同一套。

        直接拿真 payload 的键去反查渲染代码里出现过哪些键名 ——
        **只改了三处渲染中的一两处**时这条红。
        """
        payload = make_update_event(env, title="产品运营（高级）", family="marketing")
        producer_keys = {
            k for change in payload["diff"].values() for k in change
        }
        src = inspect.getsource(cli.digest)
        rendering = src.split("岗位变更（影响你的画像）")[1]
        for stale in ("['old']", '["old"]', "['new']", '["new"]'):
            assert stale not in rendering, f"渲染里还留着旧键 {stale}"
        for key in producer_keys:
            assert f"'{key}'" in rendering or f'"{key}"' in rendering, (
                f"生产端写了 {key}，渲染里没读"
            )


class TestRenderRealPayload:
    """端到端：真事件喂给真 digest，不许崩。"""

    def test_title_change_renders(self, env, capsys) -> None:
        """改标题的真实负载能渲染出来，不 KeyError。

        修之前这条抛 KeyError('old')。
        """
        make_update_event(env, title="产品运营（高级）")
        cli.digest(mark=False)
        out = capsys.readouterr().out
        assert "产品运营（高级）" in out
        assert "标题" in out

    def test_family_change_renders_chinese_label(self, env, capsys) -> None:
        """改岗位族的真实负载能渲染，且族名走 FAM_ZH 翻译。"""
        make_update_event(env, family="marketing")
        cli.digest(mark=False)
        out = capsys.readouterr().out
        assert cli.FAM_ZH["operations"] in out
        assert cli.FAM_ZH["marketing"] in out

    def test_digest_survives_every_stored_update_event(self, env, capsys) -> None:
        """一次改多个字段，渲染分支同时走 title 和 job_family。

        反向价值：只修了 title 那一行、没修 job_family 那两行时这条红。
        """
        payload = make_update_event(env, title="高级产品运营", family="marketing")
        assert set(payload["diff"]) >= {"title", "job_family"}, "用例没造出多字段 diff"
        cli.digest(mark=False)
        out = capsys.readouterr().out
        assert "高级产品运营" in out
        assert cli.FAM_ZH["marketing"] in out


class TestRenderCities:
    """城市变更的渲染。方案 006 问题 1 之前这个分支是死代码 —— cities 不在
    diff 的字段清单里，`cli.py:253` 的 `if "cities" in diff` 永远为假。
    """

    def test_cities_change_renders(self, env, capsys) -> None:
        """城市从三地缩到一地，日报上要能看见变了什么。"""
        payload = make_update_event(env, cities=["深圳"])
        assert "cities" in payload["diff"], "生产端没把 cities 写进 diff"

        cli.digest(mark=False)
        out = capsys.readouterr().out

        assert "城市" in out
        assert "北京" in out and "深圳" in out

    def test_cities_render_as_prose_not_python_lists(self, env, capsys) -> None:
        """打印的是「北京 → 上海、深圳」，不是「['北京'] → ['上海', '深圳']」。

        负载里是 list，直接塞进 f-string 就会漏出中括号和引号。
        谁把 `_fmt_cities` 摘掉，这条红。
        """
        make_update_event(env, cities=["上海", "深圳"])
        cli.digest(mark=False)
        out = capsys.readouterr().out

        assert "上海、深圳" in out, f"城市没按中文顿号拼：{out}"
        assert "[" not in out and "'" not in out, f"Python 列表表示法漏进输出：{out}"

    def test_empty_cities_render_as_unwritten(self, env, capsys) -> None:
        """城市变空显示「未写」，不是「不限」。

        「不限」在本仓是**真实的城市值**（`normalize.CITY_WILDCARDS`）——
        源站写「工作地点不限」时它作为一个元素出现在 cities 里，`any_city_ok`
        认它。拿「不限」表示空列表，会让 `["不限"]`（源站说哪都行）和 `[]`
        （我们没拿到）打印成同一句话。方案 §4 的表格写的是「不限」，是错的。
        """
        make_update_event(env, cities=[])
        cli.digest(mark=False)
        out = capsys.readouterr().out

        assert "未写" in out
        assert "不限" not in out


class TestNoCrashMeansNoWedge:
    """崩溃会卡住整批 —— 这条钉的是「不崩」的下游后果。"""

    def test_mark_lands_after_successful_render(self, env) -> None:
        """渲染成功后 --mark 能把 notified_at 写进去。

        原来的 bug 是渲染在 `--mark` 的 UPDATE 之前崩掉，notified_at 一行都
        写不进去，下次 digest 同一批重新扫、同一行重新崩，永远卡住。
        """
        make_update_event(env, title="产品运营（高级）")
        pending_before = env.execute(
            "SELECT COUNT(*) n FROM events WHERE notified_at IS NULL"
        ).fetchone()["n"]
        assert pending_before > 0

        cli.digest(mark=True)

        pending_after = db.connect(db.DB_PATH).execute(
            "SELECT COUNT(*) n FROM events WHERE notified_at IS NULL"
        ).fetchone()["n"]
        assert pending_after == 0, "渲染没崩，但 notified_at 没写进去"
