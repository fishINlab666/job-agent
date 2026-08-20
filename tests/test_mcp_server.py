"""MCP 只读层的守卫测试。

这个文件守的不是「工具算得对不对」（那是 test_queries.py 的活），是**边界**：
写动词不许进注册表、连接不许写库、身份数据不许过边界、代投侧事件不许漏出来。

四条都是「破了不会报错，只会静默变成另一种东西」的那类。所以每条都逐条改坏
验过能红，包括守卫自己。
"""
from __future__ import annotations

import ast
import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from jobagent import cli, db, ingest, mcp_server


IDENTITY_TOOL_CASES = [
    ("list_jobs", {}),
    ("list_jobs", {"matched": True}),
    ("explain_match", {"external_id": "J1"}),
    ("list_sources", {}),
    ("list_sync_runs", {}),
    ("job_changes", {}),
]


def call(tool: str, args: dict | None = None) -> dict:
    """经**真的 MCP 协议**调一次工具，返回解开的 JSON。

    不直接调 Python 函数：直接调等于绕开注册表和序列化，而哨兵测试要证明的正是
    「模型那一侧看到的字节里没有身份数据」。序列化环节要算进来。
    """
    from mcp import Client

    async def go():
        async with Client(mcp_server.mcp) as c:
            r = await c.call_tool(tool, args or {})
            text = r.content[0].text
            if r.is_error:
                raise AssertionError(f"工具报错：{text}")
            return json.loads(text)

    return asyncio.run(go())


def tool_names() -> list[str]:
    """真实注册表里的工具名。不是读源码里的装饰器，是问 server 自己。"""
    return sorted(t.name for t in asyncio.run(mcp_server.mcp.list_tools()))


@pytest.fixture
def db_with_data(tmp_path, monkeypatch):
    """临时库 + 一条岗位 + 一条代投侧事件。

    代投侧那条是**故意**放进去的：白名单守卫要有东西可挡，不然它测的是
    「库里恰好没有」而不是「代码挡住了」。
    """
    path = tmp_path / "m.db"
    c = db.connect(path)
    db.init(c)
    db.register_source(c, "tencent_join", "腾讯", "tencent_join",
                       "https://join.qq.com/post.html")
    c.execute(
        """INSERT INTO jobs(source_key, external_id, company, title, job_family,
               cities, recruit_type, grad_year, apply_url, apply_system,
               fingerprint, first_seen_at, last_seen_at)
           VALUES('tencent_join','J1','腾讯','产品运营','operations',
               '["深圳"]','campus','26','https://x','tencent_join','fp',?,?)""",
        (db.now(), db.now()),
    )
    job_id = c.execute("SELECT id FROM jobs WHERE external_id='J1'").fetchone()["id"]
    db.add_event(c, "job_opened", source_key="tencent_join", company="腾讯",
                 job_id=job_id, payload={"title": "产品运营"})
    db.add_event(c, "apply_blocked", source_key="tencent_join", company="腾讯",
                 job_id=job_id,
                 payload={"blocker": "截图在 screenshots/form_LEAKCANARY.png"})
    c.commit()
    c.close()

    monkeypatch.setattr(db, "DB_PATH", path)
    return path


class TestNoWriteVerbInTheRegistry:
    """主约束：模型调不到不存在的工具。

    另两条（只读连接、哨兵）都是兜底 —— 它们管的是「我写错了」，
    这一条管的是「模型想投递」。
    """

    def test_no_write_verb_is_registered(self) -> None:
        """每个工具的**头动词**必须是读动词。

        管头词而不是「名字里不许出现写动词」：`list_sync_runs` 读采集历史，
        名字里有 sync 但它不采集。按「出现即禁」会把它拦下来，然后逼人把守卫
        放松掉 —— 一条会误报的守卫最终会被改松或删掉，那才是真的把门拆了。
        """
        names = tool_names()
        assert names, "注册表是空的，这条测试什么都没测到"
        for name in names:
            if name in mcp_server.NOUN_PHRASE_TOOLS:
                continue
            head = name.split("_")[0]
            assert head not in mcp_server.WRITE_VERBS, (
                f"工具 {name!r} 以写动词 {head!r} 打头。只读层不许注册会改东西的"
                "工具 —— 代投留在 CLI，因为提交不可逆、必须人工逐字段确认。"
            )
            assert head in mcp_server.READ_VERBS, (
                f"工具 {name!r} 的头动词 {head!r} 既不在读动词清单里、也不在"
                "名词短语白名单里。先答一句：它是只读的吗？是就把动词加进 "
                "READ_VERBS，或把名字加进 NOUN_PHRASE_TOOLS。"
            )

    def test_the_verb_lists_do_not_overlap(self) -> None:
        """读动词和写动词不许有交集。

        交集意味着同一个头词既算读又算写，那时上面那条的判定取决于两个 assert
        的先后顺序 —— 一条守卫的结论依赖代码行序，就是随时会翻的。
        """
        assert not (mcp_server.READ_VERBS & mcp_server.WRITE_VERBS)

    def test_the_guard_would_catch_a_write_tool(self) -> None:
        """守卫自己能不能红。

        真往一个临时 server 上注册 `execute_apply`，跑同一套判据，断言它被拦下。
        少了这条，「全绿」既可能是「没有写工具」，也可能是「判据压根不生效」。
        """
        from mcp.server import MCPServer

        probe = MCPServer(name="probe")

        @probe.tool()
        def execute_apply(job_id: str) -> dict:
            """假装要投递。"""
            return {}

        names = sorted(t.name for t in asyncio.run(probe.list_tools()))
        assert names == ["execute_apply"]
        head = names[0].split("_")[0]
        assert head in mcp_server.WRITE_VERBS, "判据认不出 execute 是写动词"
        assert head not in mcp_server.READ_VERBS

    def test_registry_is_exactly_the_five_readonly_tools(self) -> None:
        """钉住全集。

        上一条是按动词黑名单查的，一个叫 `do_the_thing` 的写工具能绕过去。
        这条钉死名单，多一个少一个都会红 —— 加工具时**必须**顺手改这里，
        那一刻就是「你确认它是只读的吗」的检查点。
        """
        assert tool_names() == [
            "explain_match", "job_changes", "list_jobs", "list_sources",
            "list_sync_runs",
        ], "注册表变了。新增工具时先答一句：它是只读的吗？"

    def test_removed_tool_has_no_definition_or_registration_surface(self) -> None:
        """旧工具要从源码、模块属性和注册表一起消失。"""
        source = Path(mcp_server.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        definitions = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        assert "check_form_selectors" not in definitions
        assert "check_form_selectors" not in tool_names()
        assert not hasattr(mcp_server, "check_form_selectors")

        from mcp import Client

        async def go():
            async with Client(mcp_server.mcp) as client:
                return await client.call_tool("check_form_selectors", {})

        assert asyncio.run(go()).is_error

    def test_registered_tools_have_no_login_or_browser_path_inputs(self) -> None:
        """MCP 输入面不接受登录态、浏览器目录或其他凭据。"""
        def schema_keys(value):
            if isinstance(value, dict):
                for key, nested in value.items():
                    yield str(key)
                    yield from schema_keys(nested)
            elif isinstance(value, list):
                for nested in value:
                    yield from schema_keys(nested)

        forbidden_parts = (
            "user_data", "profile", "browser", "cookie", "screenshot",
            "session", "token", "secret", "credential",
        )
        for tool in asyncio.run(mcp_server.mcp.list_tools()):
            for key in schema_keys(tool.input_schema):
                lowered = key.lower()
                assert not lowered.endswith(("_path", "_dir")), (
                    f"工具 {tool.name!r} 暴露了路径输入 {key!r}"
                )
                assert not any(part in lowered for part in forbidden_parts), (
                    f"工具 {tool.name!r} 暴露了登录态/浏览器输入 {key!r}"
                )

    def test_mcp_module_has_no_submitter_routing_import(self) -> None:
        """MCP 模块只能依赖只读查询链，不得触达投递/浏览器栈。"""
        source = Path(mcp_server.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        local_deps: set[str] = set()
        external_roots: set[str] = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                external_roots.update(
                    alias.name.split(".")[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    if node.module:
                        local_deps.add(node.module.split(".")[0])
                    else:
                        local_deps.update(
                            alias.name.split(".")[0] for alias in node.names
                        )
                elif node.module:
                    external_roots.add(node.module.split(".")[0])

        assert local_deps == {"db", "match", "queries"}
        assert external_roots == {"__future__", "sqlite3", "typing", "mcp"}

        forbidden = {
            "routing", "submitter", "playwright", "selenium", "httpx",
            "requests", "subprocess", "socket",
        }
        assert not (local_deps | external_roots) & forbidden
        assert not [name for name in forbidden if hasattr(mcp_server, name)]

    def test_prepare_and_execute_are_not_callable(self) -> None:
        """点名要 execute 会失败，不是「被拒绝」而是「没有这个东西」。"""
        from mcp import Client

        async def go():
            async with Client(mcp_server.mcp) as c:
                return await c.call_tool("execute", {"job_id": "J1"})

        r = asyncio.run(go())
        assert r.is_error, "居然有一个叫 execute 的工具能调"

    def test_module_defines_no_write_helpers(self) -> None:
        """模块里不该有 commit/insert 这类字样。

        守的是「工具没注册但模块里塞了个会写库的辅助函数」，下一步就是有人把它
        接上去。
        """
        import inspect
        src = inspect.getsource(mcp_server)
        for bad in ("commit()", "INSERT ", "UPDATE ", "DELETE ", "db.connect()"):
            assert bad not in src, f"mcp_server.py 里出现了 {bad!r}"


class TestReadOnlyConnection:
    """兜底：就算工具体里写错一句 SQL，SQLite 自己拒绝。"""

    def test_readonly_connection_refuses_insert(self, db_with_data) -> None:
        c = db.connect_readonly(db_with_data)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            c.execute("INSERT INTO events(kind, occurred_at) VALUES('x','y')")

    @pytest.mark.parametrize("sql", [
        "UPDATE jobs SET title='改了' WHERE external_id='J1'",
        "DELETE FROM jobs WHERE external_id='J1'",
        "CREATE TABLE zzz(a int)",
        "DROP TABLE jobs",
    ])
    def test_readonly_connection_refuses_every_write_shape(
        self, db_with_data, sql
    ) -> None:
        """四种写法逐个试。

        只测 INSERT 会留一个洞：`mode=ro` 万一只挡插入，UPDATE 照样能把库改花，
        而那种改动不报错、只是数据不对了。
        """
        c = db.connect_readonly(db_with_data)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            c.execute(sql)

    def test_it_can_still_read(self, db_with_data) -> None:
        """反向对照。

        少了这条，上面几条可以靠「连接压根连不上」假绿 —— 连不上时任何 SQL 都抛，
        看起来一样，但那时 MCP 是全瘫而不是只读。
        """
        c = db.connect_readonly(db_with_data)
        assert c.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 1

    def test_missing_db_raises_instead_of_creating_one(self, tmp_path) -> None:
        """库不存在时抛，不悄悄造个空库。

        造空库的后果是所有工具答「0 条岗位」——一个看起来正常的错答案，
        而真实原因是路径指错了。
        """
        with pytest.raises(FileNotFoundError):
            db.connect_readonly(tmp_path / "nope.db")

    def test_tools_use_the_readonly_connection(self, db_with_data, monkeypatch) -> None:
        """归属：工具确实走 `connect_readonly`，不是 `connect`。

        两个函数都能读，所以功能测试对「用错了哪个」全绿 —— 只有这条能看见。
        """
        def boom(*a, **k):
            raise RuntimeError("SENTINEL_ro")
        monkeypatch.setattr(db, "connect_readonly", boom)
        with pytest.raises(RuntimeError, match="SENTINEL_ro"):
            mcp_server.list_jobs()


class TestIdentityDoesNotCrossTheBoundary:
    """哨兵：往 profile 里塞可识别的假身份值，调每个工具，断言哨兵串不出现。

    这一条不能靠「现在 identity 是空串所以没事」来通过 —— 那是数据碰巧，不是形状。
    所以测试自己把值填满。
    """

    SENTINELS = {
        "name": "哨兵姓名CANARY",
        "gender": "哨兵性别CANARY",
        "phone": "13900000001",
        "email": "canary@sentinel.invalid",
        "id_card": "110101199001011234",
    }

    @pytest.fixture
    def profile_with_identity(self, tmp_path, monkeypatch):
        """一份 intent 有值、identity 全是哨兵的档案。"""
        import yaml
        from jobagent import match

        p = tmp_path / "profile.yaml"
        p.write_text(yaml.safe_dump({
            "intent": {
                "families": ["operations"],
                "recruit_types": ["campus"],
                "grad_years": ["26"],
                "cities": ["深圳"],
            },
            "identity": dict(self.SENTINELS),
            "education": [{"school": "哨兵学校CANARY", "major": "哨兵专业CANARY"}],
            "narrative": {"strengths": "哨兵自述CANARY"},
        }, allow_unicode=True), encoding="utf-8")
        monkeypatch.setattr(match, "PROFILE_PATH", p)
        return p

    def test_intent_helper_hands_over_only_whitelisted_keys(
        self, db_with_data, profile_with_identity
    ) -> None:
        """`_intent()` 交出去的键必须全在白名单里。

        为什么要这条、而不是只靠下面的哨兵扫描：红队验过 ——
        把 `_intent()` 改成 `return match.load_profile()`（整份档案往下传），
        哨兵扫描**照样全绿**，因为 `match.classify` 只读它认识的键，身份数据
        传进去了但没被用到、于是没进返回值。那时「只有 intent 过边界」靠的是
        「下游恰好不读」，不是这一层挡住了 —— 下游哪天多读一个键就静默失效。

        所以这条查的是**交出去的东西本身**，不是它有没有被印出来。
        """
        got = mcp_server._intent()
        assert set(got) <= mcp_server.INTENT_KEYS, (
            f"多交了这些键：{set(got) - mcp_server.INTENT_KEYS}"
        )
        assert "identity" not in got and "education" not in got
        assert got["families"] == ["operations"], "白名单把该过的键也挡掉了"

    def test_an_unknown_intent_key_is_not_handed_over(
        self, tmp_path, monkeypatch
    ) -> None:
        """`intent` 里多一个没见过的键时，默认**不**交出去。

        这条守的是白名单 vs 黑名单的差别，而那个差别在今天的档案上看不出来 ——
        `intent` 里本来就没有 identity，所以「挑白名单」和「排除 identity」给出
        一样的结果，把实现换成黑名单不会有任何测试变红（红队验过）。

        差别只在 profile **将来长出新键**时显现：黑名单默认放行，白名单默认拦下。
        所以这里自己造一个未来的键。它叫 `id_card_backup` 是刻意的 ——
        真出现这种键时，黑名单式实现会把身份证号直接送进对话。
        """
        import yaml
        from jobagent import match

        p = tmp_path / "profile.yaml"
        p.write_text(yaml.safe_dump({
            "intent": {
                "families": ["operations"],
                "id_card_backup": "110101199001011234",
            },
        }, allow_unicode=True), encoding="utf-8")
        monkeypatch.setattr(match, "PROFILE_PATH", p)

        got = mcp_server._intent()
        assert got == {"families": ["operations"]}, (
            "intent 里的未知键被一起交了出去。白名单要默认拦下没见过的键 ——"
            "黑名单式实现在这里会放行。"
        )

    def test_intent_still_crosses(self, db_with_data, profile_with_identity) -> None:
        """先证明匹配真的读到了这份档案。

        少了这条，下面的「哨兵不出现」可以靠「档案压根没被读」假绿 ——
        那时 identity 当然不泄露，因为什么都没读。
        """
        out = call("list_jobs", {"matched": True})
        assert out["total"] == 1, "matched 筛完应该还剩那条 operations 岗位"
        assert mcp_server._intent()["families"] == ["operations"]

    @pytest.mark.parametrize("tool,args", IDENTITY_TOOL_CASES)
    def test_no_identity_value_crosses_the_boundary(
        self, db_with_data, profile_with_identity, tool, args
    ) -> None:
        blob = json.dumps(call(tool, args), ensure_ascii=False)
        for field, value in self.SENTINELS.items():
            assert value not in blob, f"{tool} 的返回里出现了 identity.{field}"
        for other in ("哨兵学校CANARY", "哨兵专业CANARY", "哨兵自述CANARY"):
            assert other not in blob, f"{tool} 的返回里出现了 {other}"

    def test_every_registered_tool_is_covered_by_the_sentinel_sweep(self) -> None:
        """上面那张参数表要覆盖到**每个**注册工具。

        少了这条，将来新增一个工具时哨兵扫不到它，而测试全绿 ——
        「守卫覆盖不全」本身就是静默失效。
        """
        swept = {tool for tool, _args in IDENTITY_TOOL_CASES}
        registered = set(tool_names())
        assert swept == registered, (
            "有工具没进哨兵清单：" f"{swept ^ registered}"
        )

    def test_missing_profile_degrades_instead_of_crashing(
        self, db_with_data, tmp_path, monkeypatch
    ) -> None:
        """档案不存在时匹配退化成「什么都不排除」，不是整个工具挂掉。"""
        from jobagent import match
        monkeypatch.setattr(match, "PROFILE_PATH", tmp_path / "nope.yaml")
        assert mcp_server._intent() == {}
        assert call("list_jobs", {"matched": True})["total"] >= 0


class TestOnlyJobSideEvents:
    """代投侧的事件不许从 job_changes 漏出去。

    那些 payload 里有 `screenshots/` 路径，而截图是填好的表单，画面上有
    姓名手机身份证。
    """

    def test_apply_events_are_filtered_out(self, db_with_data) -> None:
        """不带 kind 时一条 apply_* 都不许出来。**查行为，不查常量。**

        这条和 `test_whitelist_is_a_whitelist_not_a_blacklist` 不重复：
        `queries.job_changes(kind=None)` 会返回**整张表**（`queries.py` 里 kind 为空
        就不加 WHERE 条件），所以「白名单内容对」和「代投侧没漏出去」是两件事，
        中间隔着 `mcp_server` 逐个 kind 查这个动作。把 `kind=None` 直接透传给下一层，
        常量测试全绿而 PII 全出去了 —— 这条是那个「不透传」的唯一守卫。
        整个返回值（不只 events）都过一遍哨兵：字段是我加的，也可能是我漏的那个。
        """
        r = call("job_changes", {"limit": 100})
        kinds = {e["kind"] for e in r["events"]}
        assert "job_opened" in kinds, "采集侧事件被一起挡掉了，那就是挡过头"
        assert not [k for k in kinds if k.startswith("apply")], \
            f"代投侧事件漏出来了：{sorted(k for k in kinds if k.startswith('apply'))}"
        assert "LEAKCANARY" not in json.dumps(r, ensure_ascii=False)

    def test_asking_for_an_apply_kind_is_refused(self, db_with_data) -> None:
        """点名要 apply_blocked 要被拒，不是静默返回空。

        静默返回空会让「这类事件不给你看」和「这类事件没发生」看起来一样。
        """
        from mcp import Client

        async def go():
            async with Client(mcp_server.mcp) as c:
                return await c.call_tool("job_changes", {"kind": "apply_blocked"})

        r = asyncio.run(go())
        assert r.is_error
        assert "不认识的事件种类" in r.content[0].text

    def test_whitelist_is_a_whitelist_not_a_blacklist(self) -> None:
        """白名单里不许出现 apply_*。

        黑名单在这里一定会漏：代投侧的 kind 是 `f"apply_{status}"` 拼出来的，
        grep 数不出全集，将来多一个状态就多一个漏出去的 kind。

        【这条只查一个方向，查不出「少了一个」】它遍历的是白名单自己，
        所以白名单少三种事件的时候它照样绿 —— 019 修的就是那个缺陷。
        管「少了一个」的是下面 `TestWhitelistCoversEveryIngestKind`。
        """
        assert not [k for k in mcp_server.JOB_EVENT_KINDS if k.startswith("apply")]

    def test_docstring_does_not_promise_everything(self) -> None:
        """docstring 不许承诺「省略则全要」。

        这条守的是**文档**，不是行为，因为这个修法会被做一半：把三种事件加进
        白名单之后，`job_changes()` 返回 7 种，看着就对了 —— 但 docstring 还写着
        「省略则全要」，而这一层永远不给代投侧。模型按承诺办事，会把一份被裁过的
        结果当成整张表转述。行为测试查不出这个，因为行为本身没错，是承诺错了。
        """
        doc = mcp_server.job_changes.__doc__ or ""
        assert "省略则全要" not in doc, \
            "docstring 还在承诺「全要」，但这一层永远排除代投侧"
        assert "不是这张表" in doc, \
            "docstring 得说清「全部采集侧事件」≠「events 表的全部」"

    def test_excluded_kinds_is_reported(self, db_with_data) -> None:
        """返回值里要有 `excluded_kinds`，让「没给什么」看得见。

        白名单上面那段注释为自己辩护的理由是「漏的方向是少给一类事件，看得见」。
        那句话只在调用方会去数少了什么的时候成立。返回值里不带这个字段，
        调用方**没有任何依据**能发现结果被裁过。
        """
        r = call("job_changes", {"limit": 5})
        assert "excluded_kinds" in r, "返回值里没有 excluded_kinds，被裁掉的部分看不见"
        assert any("apply" in k for k in r["excluded_kinds"]), \
            f"excluded_kinds 没说清排除的是代投侧：{r['excluded_kinds']}"

    def test_excluded_kinds_is_reported_even_for_one_kind(self, db_with_data) -> None:
        """点名要单个 kind 时也要带 `excluded_kinds`。

        它说的是「这一层永远不给什么」，不是「这一次筛掉了什么」。写成后者的话，
        `kind='job_opened'` 的返回里就没有这个字段了 —— 而那正是模型最常走的路径。
        """
        r = call("job_changes", {"kind": "job_opened", "limit": 5})
        assert r.get("excluded_kinds"), \
            "指定单个 kind 时 excluded_kinds 丢了，等于常见路径上没有这个提示"

    def test_excluded_kinds_is_never_empty(self) -> None:
        """`EXCLUDED_KINDS` 不许是空的，也不许是算出来的差集。

        差集（表里所有 kind 减白名单）在白名单补全之后会变成空 —— 空列表会被读成
        「什么都没排除」，那正是这次要修掉的误读。代投侧一条不给是这一层的固定边界，
        不是某次快照的结果。
        """
        assert mcp_server.EXCLUDED_KINDS, "EXCLUDED_KINDS 空了，等于宣布什么都没排除"


class TestWhitelistCoversEveryIngestKind:
    """白名单必须盖住采集侧发的**每一种**事件 —— 能查出「少了一个」。

    锚点是 `jobagent/ingest.py` 的 AST，**不是白名单自己**。拿白名单当锚点是自证
    （`set(w) <= set(w)` 恒真），遍历被检对象也只查得出「多了一个」；
    而 019 要修的缺陷正是「少了三个」（`job_reopened` / `family_first_seen` /
    `batch_started`），库里当时丢着 8 条事件。

    **这一组只覆盖采集侧，覆盖不了代投侧。** 代投侧的 kind 有一处是
    `f"apply_{result.status}"`（`cli.py:877`）拼出来的，AST 里是个 `JoinedStr`，
    数不出全集 —— 那一侧靠的是「白名单形状」本身，不是这里的枚举。
    不写这句的话，下一个人会以为这一组守住了整张 `events` 表。
    """

    @staticmethod
    def _add_event_kinds(module) -> tuple[set[str], list[tuple[int, str]]]:
        """扫一个模块里所有 `*.add_event(conn, <kind>, ...)`，分成能枚举的和不能的。

        返回 `(字面量 kind 集合, [(行号, 节点类型)])`。第二个是「数不出全集」的调用点
        —— 它们不能进锚点，但**必须被数出来**，否则锚点会悄悄退化成「只覆盖一部分」
        还全绿。
        """
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        literal: set[str] = set()
        dynamic: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_event"):
                continue
            arg = node.args[1] if len(node.args) > 1 else None
            # `"job_reopened" if reopened else "job_updated"` 一处出两个 kind。
            # 不摊开的话它整个是个 IfExp，会被当成「数不出全集」，
            # 而实际上两臂都是字面量。
            branches = ([arg.body, arg.orelse] if isinstance(arg, ast.IfExp)
                        else [arg])
            for b in branches:
                if isinstance(b, ast.Constant) and isinstance(b.value, str):
                    literal.add(b.value)
                else:
                    dynamic.append((node.lineno, type(b).__name__))
        return literal, dynamic

    def test_every_ingest_kind_is_whitelisted(self) -> None:
        """采集侧发的每一种 kind 都要在白名单里。**这条查的是「少了一个」。**"""
        emitted, _ = self._add_event_kinds(ingest)
        missing = sorted(emitted - set(mcp_server.JOB_EVENT_KINDS))
        assert not missing, (
            f"`ingest.py` 会发这些事件，但白名单没有它们：{missing}。\n"
            f"模型调 job_changes() 拿不到它们，而 docstring 承诺的是「全部采集侧事件」"
            f"—— 于是「没给」会被读成「没发生」。"
        )

    def test_whitelist_has_no_kind_ingest_never_emits(self) -> None:
        """反方向：白名单里不许有采集侧根本不发的 kind。

        查这个方向是为了拼错的词。`job_reopend` 少个 `e` 加进白名单，
        上一条测试照样绿（它只查漏），而这个 kind 永远查不到任何事件 ——
        一个「配好了但永远返回空」的选项，比没配更难发现。
        """
        emitted, _ = self._add_event_kinds(ingest)
        # 代投侧的 kind 不该出现在这张表里，但那由
        # `test_whitelist_is_a_whitelist_not_a_blacklist` 管；
        # 这里只比采集侧，免得两条测试红在同一个原因上。
        stray = sorted(k for k in mcp_server.JOB_EVENT_KINDS
                       if k not in emitted and not k.startswith("apply"))
        assert not stray, (
            f"白名单里这些 kind `ingest.py` 从来不发：{stray}。"
            f"拼错了，或者发射点被删了 —— 它们是永远返回空的选项。"
        )

    def test_anchor_sees_every_add_event_site(self) -> None:
        """锚点自己没漏掉调用点 —— 数 AST 数出的调用点，和源码里的行数对齐。

        锚点退化的方向是**假绿**：AST 匹配条件写窄了（比如只认
        `db.add_event` 而 `ingest.py` 改成了 `from .db import add_event`），
        它数出 0 个调用点，于是「采集侧减白名单」是空集，上面两条全绿。
        「一条都没匹配上」和「全都合规」在集合运算里长得一模一样。
        """
        src = Path(ingest.__file__).read_text(encoding="utf-8")
        in_source = src.count("add_event(")
        literal, dynamic = self._add_event_kinds(ingest)
        # 调用点数 ≠ kind 数：`:452` 一处出两个 kind（三元）。所以这里比的是
        # 「AST 找到的调用点」和「源码里的 `add_event(` 次数」，不是 kind 个数。
        tree = ast.parse(src)
        found = sum(
            1 for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "add_event"
        )
        assert found > 0, "锚点一个调用点都没找到 —— 它已经退化成假绿了"
        assert found == in_source, (
            f"AST 找到 {found} 个 add_event 调用点，源码里出现 {in_source} 次。"
            f"不相等说明有调用点不是 `x.add_event(...)` 的形状，锚点漏了它。"
        )
        assert len(literal) > found, (
            f"kind 数 {len(literal)} 应当大于调用点数 {found}："
            f"`ingest.py` 有一处三元表达式一次发两种 kind。"
            f"两个数相等说明那处三元没被摊开。"
        )

    def test_ingest_kinds_are_all_enumerable(self) -> None:
        """采集侧的 kind 必须全是字面量 —— 这是锚点成立的前提。

        前提单独钉一条，因为它会**过期**：将来有人在 `ingest.py` 里写
        `f"job_{state}"`，锚点就数不出全集了，而上面几条会继续绿（它们比的是
        数出来的那部分）。那时候要红的是这条，它会指着「锚点不再可信」这一层，
        而不是让人去查白名单。
        """
        _, dynamic = self._add_event_kinds(ingest)
        assert not dynamic, (
            f"`ingest.py` 这些调用点的 kind 不是字面量：{dynamic}。"
            f"AST 数不出全集了，`test_every_ingest_kind_is_whitelisted` 从此"
            f"只覆盖一部分 —— 它不会因此变红，所以由这条来喊。"
        )

    def test_apply_side_is_the_one_that_cannot_be_enumerated(self) -> None:
        """代投侧确实有数不出全集的调用点 —— 白名单那段注释的理由要站得住。

        注释说「代投侧的 kind 是拼出来的，grep 数不出全集，黑名单一定会漏」。
        这条把那个理由变成可查的：真有一处 `JoinedStr`。哪天代投侧改成枚举了，
        这条会红，提醒去重新读那段注释 —— 那时候黑名单方案重新可行，
        而注释还在拿一个不再成立的理由说话。
        """
        _, dynamic = self._add_event_kinds(cli)
        assert dynamic, (
            "`cli.py` 里已经没有拼出来的 kind 了。白名单注释里"
            "「grep 数不出全集」这个理由不再成立，去重读那段注释。"
        )

    def test_limit_means_the_most_recent_overall(self, db_with_data) -> None:
        """跨 kind 合并后要重新排序再截断。

        各 kind 分别取 limit 条再拼起来，条数是对的，但拿到的不是最近那些 ——
        一个数字对、内容错的答案。
        """
        c = db.connect(db_with_data)
        job_id = c.execute("SELECT id FROM jobs WHERE external_id='J1'").fetchone()["id"]
        # 最新的那条挂在 source_bootstrapped 上 —— 它在白名单里**排最后**。
        # 这个选择是刻意的：不重排时代码是按 sorted(kinds) 逐个取的，
        # 所以最新事件必须落在排序靠后的 kind 上，否则「碰巧先取到它」会让
        # 缺失的重排也通过。（第一版用 job_closed 就是这个毛病：它按字母序
        # 恰好排在 job_opened 前面，去掉重排照样绿。）
        for kind, when in (
            ("job_closed", "2050-01-01T00:00:00"),
            ("source_bootstrapped", "2099-01-01T00:00:00"),
        ):
            c.execute(
                """INSERT INTO events(kind, source_key, company, job_id, payload, occurred_at)
                   VALUES(?,'tencent_join','腾讯',?,'{}',?)""",
                (kind, job_id, when),
            )
        c.commit()
        c.close()

        assert sorted(mcp_server.JOB_EVENT_KINDS)[-1] == "source_bootstrapped", (
            "白名单的字母序变了，这条测试的前提没了 —— 最新事件要挂在排最后的 kind 上"
        )
        evs = call("job_changes", {"limit": 1})["events"]
        assert len(evs) == 1
        assert evs[0]["kind"] == "source_bootstrapped", (
            "拿到的不是全局最近那条。各 kind 分别截 limit 条再拼起来，"
            "条数对，内容错。"
        )


class TestToolContract:

    def test_total_is_not_capped_by_limit(self, db_with_data) -> None:
        """`total` 是筛完的全量，`returned` 才受 limit 限。

        混成一个数的后果是「共 1 条」，而真相是 1069 条里给了你 1 条。
        """
        c = db.connect(db_with_data)
        for i in range(4):
            c.execute(
                """INSERT INTO jobs(source_key, external_id, company, title,
                       job_family, cities, recruit_type, grad_year, apply_url,
                       apply_system, fingerprint, first_seen_at, last_seen_at)
                   VALUES('tencent_join',?,'腾讯','运营','operations','["深圳"]',
                       'campus','26','https://x','tencent_join','fp',?,?)""",
                (f"X{i}", db.now(), db.now()),
            )
        c.commit()
        c.close()
        out = call("list_jobs", {"limit": 2})
        assert out["total"] == 5
        assert out["returned"] == 2
        assert len(out["jobs"]) == 2

    def test_missing_job_says_not_found_instead_of_erroring(self, db_with_data) -> None:
        out = call("explain_match", {"external_id": "没这条"})
        assert out["found"] is False
        assert "hint" in out

    def test_duplicate_job_id_exposes_sources_instead_of_guessing(
        self, db_with_data
    ) -> None:
        c = db.connect(db_with_data)
        db.register_source(
            c, "feishu:nio", "蔚来", "feishu",
            "https://nio.jobs.feishu.cn", tenant="nio",
        )
        c.execute(
            """INSERT INTO jobs(source_key, external_id, company, title,
                   job_family, cities, recruit_type, grad_year, apply_url,
                   apply_system, fingerprint, first_seen_at, last_seen_at)
               VALUES('feishu:nio','J1','蔚来','产品运营','operations','["深圳"]',
                   'campus','26','https://x','feishu','fp',?,?)""",
            (db.now(), db.now()),
        )
        c.commit()
        c.close()

        listed = call("list_jobs")["jobs"]
        assert all("source_key" in job for job in listed)

        ambiguous = call("explain_match", {"external_id": "J1"})
        assert ambiguous["ambiguous"] is True
        assert ambiguous["source_keys"] == ["feishu:nio", "tencent_join"]

        exact = call(
            "explain_match", {"external_id": "J1", "source_key": "feishu:nio"}
        )
        assert exact["found"] is True
        assert exact["source_key"] == "feishu:nio"

    def test_bad_dimension_is_an_error_not_a_silent_ignore(self, db_with_data) -> None:
        from mcp import Client

        async def go():
            async with Client(mcp_server.mcp) as c:
                return await c.call_tool(
                    "list_jobs", {"matched": True, "allow_missing": ["nonsense"]}
                )

        r = asyncio.run(go())
        assert r.is_error
        assert "不认识的维度" in r.content[0].text

    def test_notes_reach_the_caller(self, db_with_data) -> None:
        """`allow_missing` 没生效这件事要传到模型那一侧，不能只留在 Python 里。"""
        out = call("list_jobs", {"allow_missing": ["grad_year"]})
        assert any("只在 --matched 下生效" in n for n in out["notes"])

    def test_unsure_jobs_carry_their_reason(
        self, db_with_data, tmp_path, monkeypatch
    ) -> None:
        """信息不全的岗位要带 why_unsure，别看起来和确定命中的一样。"""
        import yaml
        from jobagent import match

        profile = tmp_path / "profile.yaml"
        profile.write_text(yaml.safe_dump({
            "intent": {
                "families": ["operations"],
                "recruit_types": ["campus"],
                "grad_years": ["26"],
                "cities": ["深圳"],
            },
        }, allow_unicode=True), encoding="utf-8")
        monkeypatch.setattr(match, "PROFILE_PATH", profile)

        c = db.connect(db_with_data)
        c.execute(
            """INSERT INTO jobs(source_key, external_id, company, title, job_family,
                   cities, recruit_type, grad_year, apply_url, apply_system,
                   fingerprint, first_seen_at, last_seen_at)
               VALUES('tencent_join','U1','腾讯','运营','operations','["深圳"]',
                   'campus',NULL,'https://x','tencent_join','fp',?,?)""",
            (db.now(), db.now()),
        )
        c.commit()
        c.close()
        jobs = call("list_jobs", {"matched": True, "allow_missing": ["grad_year"]})["jobs"]
        unsure = [j for j in jobs if j["external_id"] == "U1"]
        assert unsure and unsure[0]["why_unsure"], "放宽进来的岗位没说明缺什么"
