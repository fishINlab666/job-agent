"""路由层测试。钉死的是「按系统注册、按租户配置」这套键的选法。

这里的用例分两类：
  一类来自 docs/ATS_RESEARCH.md 的实测结论——feishu 一个类要服务多个租户，
  自建的一家一份，租户能从链接里取到就取、取不到才靠配。
  另一类是**不许悄悄发生**的事：租户取错、租户没传进去、两处证据对不上、
  注册表还没加载就说「没写这个代投器」。

这一类 bug 的共同点是不报错，只是投到别人家公司去。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from jobagent import ats, routing

ROOT = Path(__file__).resolve().parent.parent
FEISHU_URL = "https://xiaopeng.jobs.feishu.cn/campus/position/7123"


@pytest.fixture(autouse=True)
def _restore_registry():
    """注册表是模块级可变状态，用例往里塞假类，跑完必须还原。

    先真实加载一次再快照：importlib 不会重复执行模块级副作用，如果快照到的是
    空表，还原之后 _LOADED 里已经有记录了，_ensure 不会再导入，后面的用例就
    永远看不到真实注册——错误还原比不还原更难查。
    """
    routing.registered_submitters()
    routing.registered_adapters()
    subs, adps, loaded = (
        dict(routing._SUBMITTERS), dict(routing._ADAPTERS), set(routing._LOADED)
    )
    yield
    routing._SUBMITTERS.clear()
    routing._SUBMITTERS.update(subs)
    routing._ADAPTERS.clear()
    routing._ADAPTERS.update(adps)
    routing._LOADED.clear()
    routing._LOADED.update(loaded)


class FakeFeishu:
    """多租户实现的最小形状：构造函数收 tenant。"""

    system = "feishu"

    def __init__(self, tenant: str | None = None, headless: bool = True) -> None:
        self.tenant, self.headless = tenant, headless


class TenantBlindFeishu:
    """多租户系统，但构造函数漏了 tenant——本项目最危险的那类写法。"""

    system = "feishu"

    def __init__(self, headless: bool = True) -> None:
        self.headless = headless


class FakeFeishuAdapter:
    """采集器的最小形状：收 tenant，company 缺省退到租户名。

    缺省值故意设成租户名而不是空串——真适配器就是这么写的，
    这样「没传 company」的表现是挂在 `nio` 底下，跟实测踩到的现象一致。
    """

    system = "feishu"

    def __init__(
        self,
        tenant: str | None = None,
        company: str = "",
        portal: str | None = None,
        host: str | None = None,
    ) -> None:
        self.tenant = tenant
        self.company = company or (tenant or "")
        self.portal = portal
        self.host = host


class TestResolveOrder:
    """判定依据从可靠到不可靠：库里存的 > 域名识别 > 源站配置 > 老 source_key。"""

    def test_apply_system_wins_over_url(self):
        # 采集时写进库的值是明确判定，不该被链接再推翻一次
        r = routing.resolve({"apply_system": "tencent_join", "apply_url": FEISHU_URL})
        assert r.system == "tencent_join"

    def test_url_domain_hit(self):
        r = routing.resolve({"apply_url": FEISHU_URL})
        assert (r.system, r.tenant, r.key) == ("feishu", "xiaopeng", "feishu:xiaopeng")

    def test_source_system_config(self):
        r = routing.resolve({"source_key": "nio_campus"}, {"system": "feishu", "tenant": "nio"})
        assert r.key == "feishu:nio"

    def test_legacy_source_key(self):
        r = routing.resolve({"source_key": "tencent_join"})
        assert r.system == "tencent_join" and r.ok

    def test_legacy_self_built_string_falls_through(self):
        """老库 sources.system 存的是 "self_built"。

        那是一类系统的形容词，不是某个系统。第 3 步认不出它是对的，
        得由 source_key 兜住——不然升级完老库就整个路由不出去了。
        """
        r = routing.resolve({"source_key": "tencent_join"}, {"system": "self_built"})
        assert r.system == "tencent_join"

    def test_nothing_to_go_on(self):
        r = routing.resolve({})
        assert not r.ok and r.key == ats.UNKNOWN

    def test_unregistered_source_key_is_not_a_system(self):
        """source_key 不在厂商表里就不能当系统用，否则等于凭空造一个系统名。"""
        r = routing.resolve({"source_key": "some_company_campus"})
        assert not r.ok


class TestMarkupNeverRoutes:
    """页面标记只是线索。实测过一次：字节自建页里有 FeatureDailyExamUseBeisen
    （它自建 ATS，只接了北森的在线考试），按标记就会拿北森的逻辑去打字节的页面。
    """

    def test_markup_level_evidence_does_not_route(self, monkeypatch):
        monkeypatch.setattr(
            ats, "detect",
            lambda url, html="": ats.Detection("beisen", None, ats.MARKUP, ["假标记"]),
        )
        r = routing.resolve({"apply_url": "https://careers.example.com/x"})
        assert not r.ok
        # 线索要留着给人看，但不能变成判定
        assert r.lead == "beisen" and r.system is None

    def test_lead_shows_up_in_the_error(self, monkeypatch):
        monkeypatch.setattr(
            ats, "detect",
            lambda url, html="": ats.Detection("beisen", None, ats.MARKUP, ["假标记"]),
        )
        with pytest.raises(routing.RouteError, match="疑似 beisen"):
            routing.get_submitter({"apply_url": "https://careers.example.com/x"})


class TestTenantReconciliation:
    """租户有两个来源：链接里抠出来的（证据）和 sources.tenant（人工配的）。"""

    def test_derived_from_url(self):
        assert routing.resolve({"apply_url": FEISHU_URL}).tenant == "xiaopeng"

    def test_config_fills_in_when_url_cannot(self):
        """公司自有域名（CNAME 过去的）抠不出租户，这才是 sources.tenant 的用途。"""
        r = routing.resolve(
            {"apply_system": "feishu", "apply_url": "https://campus.xiaopeng.com/jobs/1"},
            {"tenant": "xiaopeng"},
        )
        assert r.tenant == "xiaopeng" and r.conflict is None

    def test_no_tenant_invented_off_vendor_domain(self):
        """非厂商域名不许抠租户。

        ats.tenant_from_url 只管抠第一段子域名，不校验归属。拿一个跟厂商无关的
        链接去问，会从 acme.example.com 里抠出 "acme" 当租户——凭空造一个。
        """
        r = routing.resolve(
            {"apply_system": "feishu", "apply_url": "https://acme.example.com/jobs/1"}
        )
        assert r.tenant is None

    def test_agreement_is_silent(self):
        r = routing.resolve({"apply_url": FEISHU_URL}, {"tenant": "xiaopeng"})
        assert r.conflict is None

    def test_conflict_is_recorded_not_raised(self):
        """resolve 是纯判断，不抛异常：CLI 要能把冲突显示出来。"""
        r = routing.resolve({"apply_url": FEISHU_URL}, {"tenant": "nio"})
        assert r.conflict and "nio" in r.conflict and "xiaopeng" in r.conflict
        # 证据优先于配置——链接里那个是实测核实过的
        assert r.tenant == "xiaopeng"

    def test_conflict_blocks_construction(self):
        """到了要造实例这一步就必须停。

        下一步是不可逆的投递。两处对不上说明有一个是错的（配置过期、复制粘贴串行），
        挑哪个都是赌，赌错就是投到别人家公司去。
        """
        routing.register_submitter("feishu", FakeFeishu)
        with pytest.raises(routing.RouteError, match="对不上"):
            routing.get_submitter({"apply_url": FEISHU_URL}, {"tenant": "nio"})

    def test_self_built_has_no_tenant(self):
        assert routing.resolve({"source_key": "tencent_join"}).tenant is None


class TestMultiTenantGuards:
    """多租户系统缺租户不会报错，只会拿别的租户的页面填表。所以要主动拦。"""

    def test_class_must_accept_tenant(self):
        routing.register_submitter("feishu", TenantBlindFeishu)
        with pytest.raises(routing.RouteError, match="不接 tenant"):
            routing.get_submitter({"apply_url": FEISHU_URL})

    def test_tenant_value_required(self):
        routing.register_submitter("feishu", FakeFeishu)
        with pytest.raises(routing.RouteError, match="没取到租户"):
            routing.get_submitter({"apply_system": "feishu"})

    def test_tenant_gets_injected(self):
        routing.register_submitter("feishu", FakeFeishu)
        s = routing.get_submitter({"apply_url": FEISHU_URL}, headless=False)
        assert s.tenant == "xiaopeng" and s.headless is False

    def test_one_class_serves_many_tenants(self):
        """这就是调研测出来的杠杆：4 个活租户，1 份实现。"""
        routing.register_submitter("feishu", FakeFeishu)
        made = {
            routing.get_submitter(
                {"apply_url": f"https://{t}.jobs.feishu.cn/campus/position/1"}
            ).tenant
            for t in ("xiaopeng", "nio", "luckin")
        }
        assert made == {"xiaopeng", "nio", "luckin"}

    def test_self_built_needs_no_tenant(self):
        s = routing.get_submitter({"source_key": "tencent_join"}, headless=True)
        assert type(s).__name__ == "TencentJoinSubmitter"

    def test_unknown_kwargs_filtered(self):
        """按构造函数实际签名过滤：不是每个实现都收 user_data_dir。"""
        routing.register_submitter("feishu", FakeFeishu)
        s = routing.get_submitter({"apply_url": FEISHU_URL}, user_data_dir="/tmp/x")
        assert not hasattr(s, "user_data_dir")


class TestCompanyComesFromSourcesRow:
    """公司名要从 sources 行传进采集器，不许让适配器自己编。

    实测踩到的：多租户适配器手里只有租户名（`nio`），公司名（`蔚来`）只在
    sources 行里。`get_adapter` 不传的话 2265 条岗位全落在 company='nio' 底下，
    `jobs --company 蔚来` 一条都查不到 —— 看着像采集没跑，其实跑了但挂错名字。
    不报错，所以只能靠测试钉。
    """

    SRC = {
        "source_key": "feishu:nio",
        "company": "蔚来",
        "system": "feishu",
        "tenant": "nio",
    }

    def test_company_injected_from_source(self):
        routing.register_adapter("feishu", FakeFeishuAdapter)
        a = routing.get_adapter({"source_key": "feishu:nio"}, self.SRC)
        assert a.company == "蔚来", "落库的 company 用的就是这个值"
        assert a.tenant == "nio"

    def test_explicit_kwarg_wins(self):
        routing.register_adapter("feishu", FakeFeishuAdapter)
        a = routing.get_adapter({"source_key": "feishu:nio"}, self.SRC, company="蔚来汽车")
        assert a.company == "蔚来汽车"

    def test_blank_company_not_injected(self):
        """空白公司名不许盖掉适配器自己的默认值（它会退到租户名）。"""
        routing.register_adapter("feishu", FakeFeishuAdapter)
        a = routing.get_adapter({"source_key": "feishu:nio"}, {**self.SRC, "company": "  "})
        assert a.company == "nio"

    def test_self_built_adapter_unaffected(self):
        """腾讯那个适配器的 company 是类常量、构造函数不收这个参数。

        按签名过滤那一步得兜住它，否则加这个功能会把现有唯一的源打挂。
        """
        a = routing.get_adapter(
            {"source_key": "tencent_join"},
            {"source_key": "tencent_join", "company": "别的名字"},
        )
        assert a.company == "腾讯"


class TestPortalFromSourceKey:
    """门户从 `source_key` 第三段取。`feishu:nio:campus` → `campus`。

    门户放进键而不是另开一列：这个键**就是**判据。`feishu:nio` 和
    `feishu:nio:campus` 是两个源、两条 run、两套关闭守卫分母，区别只有门户。
    另开一列则要多回答一个问题：同一个 key 配了两个门户时哪个算数。
    """

    SRC = {
        "source_key": "feishu:nio:campus",
        "company": "蔚来",
        "system": "feishu",
        "tenant": "nio",
        "entry_url": "https://nio.jobs.feishu.cn/campus/",
    }

    def test_portal_parsed(self):
        assert routing.portal_of("feishu:nio:campus") == "campus"
        assert routing.portal_of("feishu:sensetime:edu") == "edu"

    def test_two_segment_key_has_no_portal(self):
        """老源不带门户。这里返回非 None 就等于给老源凭空加了个门户，
        采到的池子当场换掉。"""
        assert routing.portal_of("feishu:nio") is None
        assert routing.portal_of("tencent_join") is None
        assert routing.portal_of("") is None

    def test_portal_with_colon_not_truncated(self):
        """门户名里再有冒号也不切。宁可把奇怪的门户名原样传给接口
        （接口会回 -9000003，是个响亮的失败），也不要静默截断成另一个门户
        （那会安静地采到另一批岗位）。"""
        assert routing.portal_of("feishu:nio:a:b") == "a:b"

    def test_portal_injected_into_adapter(self):
        routing.register_adapter("feishu", FakeFeishuAdapter)
        a = routing.get_adapter({"source_key": "feishu:nio:campus"}, self.SRC)
        assert a.portal == "campus"
        assert a.tenant == "nio", "多出来的第三段不许把租户也带跑偏"

    def test_no_portal_for_legacy_key(self):
        routing.register_adapter("feishu", FakeFeishuAdapter)
        a = routing.get_adapter(
            {"source_key": "feishu:nio"},
            {"source_key": "feishu:nio", "company": "蔚来", "system": "feishu",
             "tenant": "nio", "entry_url": "https://nio.jobs.feishu.cn/"},
        )
        assert a.portal is None

    def test_host_injected_from_entry_url(self):
        """自定义域名只从 sources.entry_url 取，不从岗位链接里现推 ——
        那等于放宽域名判据，会把无关站点判成飞书。"""
        routing.register_adapter("feishu", FakeFeishuAdapter)
        a = routing.get_adapter(
            {"source_key": "feishu:sensetime:edu"},
            {"source_key": "feishu:sensetime:edu", "company": "商汤科技",
             "system": "feishu", "tenant": "sensetime",
             "entry_url": "https://hr-jobs.sensetime.com/edu/"},
        )
        assert a.host == "hr-jobs.sensetime.com"
        assert a.portal == "edu"

    def test_missing_entry_url_leaves_host_none(self):
        """取不到就是 None，让适配器退到 `<tenant>.jobs.feishu.cn`，不编一个。"""
        routing.register_adapter("feishu", FakeFeishuAdapter)
        a = routing.get_adapter({"source_key": "feishu:nio"},
                                {"source_key": "feishu:nio", "tenant": "nio",
                                 "system": "feishu"})
        assert a.host is None


class TestLazyLoad:
    """查表前得先确保实现包被 import 过，否则报出来的是一句很确定的错话。"""

    def test_registry_is_empty_until_asked(self):
        """在**干净的解释器**里验：import routing 时表是空的，查一次就填上。

        必须开子进程。本进程里 jobagent.submitters 早被 import 过了，
        importlib 不会重复执行模块级副作用，在这儿测不出加载行为。
        """
        code = (
            "from jobagent import routing\n"
            "assert routing._SUBMITTERS == {}, '导入时就非空，说明有人在模块级抢跑'\n"
            "assert 'tencent_join' in routing.registered_submitters()\n"
            "print('ok')\n"
        )
        r = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True
        )
        assert r.returncode == 0, r.stderr
        assert "ok" in r.stdout

    def test_no_playwright_needed_to_route_adapters(self):
        """采集层不该被代投层的依赖拖下水。

        代投要 playwright，采集不要。两个包合成一个加载动作的话，光是查采集器
        也会把 playwright 拽进来，环境里没装就直接 ImportError——采集本来能跑。
        """
        code = (
            "import sys\n"
            "sys.modules['playwright'] = None\n"      # 一碰就炸
            "sys.modules['playwright.sync_api'] = None\n"
            "from jobagent import routing\n"
            "assert 'tencent_join' in routing.registered_adapters()\n"
            "print('ok')\n"
        )
        r = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True
        )
        assert r.returncode == 0, r.stderr

    def test_not_implemented_message_names_what_is_registered(self):
        """「还没写」和「没加载」得能分开。所以错误里要带上已注册的列表。"""
        with pytest.raises(routing.RouteError, match="已注册：tencent_join"):
            routing.get_submitter({"apply_url": FEISHU_URL})


class TestRegistryInvariants:
    def test_every_registered_system_is_a_known_vendor(self):
        """注册用的键必须在 ats.VENDORS 里。

        resolve 只认 ats.BY_KEY 里的键。注册一个表里没有的系统名，那个类永远
        路由不到——它不会报错，只是静静地躺着不被调用。
        """
        for kind, table in (
            ("代投器", routing.registered_submitters()),
            ("采集器", routing.registered_adapters()),
        ):
            for key in table:
                assert key in ats.BY_KEY, f"{kind} 注册了 {key}，但 ats.VENDORS 里没有这家"

    def test_multi_tenant_implementations_accept_tenant(self):
        """在测试期就把「多租户系统的类不收 tenant」拦掉，不要等运行时。"""
        import inspect

        for table in (routing.registered_submitters(), routing.registered_adapters()):
            for key, cls in table.items():
                v = ats.BY_KEY[key]
                if not v.self_built:
                    assert "tenant" in inspect.signature(cls).parameters, (
                        f"{cls.__name__} 服务多租户系统 {key}，构造函数必须收 tenant"
                    )

    def test_declared_system_matches_registration_key(self):
        """类上写的 system 和注册时用的键要一致，否则两处会各自漂移。"""
        for table in (routing.registered_submitters(), routing.registered_adapters()):
            for key, cls in table.items():
                if declared := getattr(cls, "system", None):
                    assert declared == key, f"{cls.__name__}.system={declared}，却注册在 {key}"
