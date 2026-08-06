"""ATS 识别层的回归墙。

这里钉死的是一类特定 bug：**自信地判错**。

判不出来（unknown）是安全的，人工看一眼就补上了。危险的是判出了一个错答案：
route_key 变成 greenhouse:boards，几十家公司挤进同一个适配器条目，
或者把自建的字节判成北森，然后拿北森的采集逻辑去打一个根本不是北森的页面。

下面每条用例背后都有一次真实误判，是 scripts/probe_ats.py 实测出来的，
不是想象出来的边界。改 VENDORS 表的时候如果这里红了，先想清楚是不是又把
某个裸品牌词放回 dom_hints 了。
"""

from __future__ import annotations

import pytest

from jobagent import ats


class TestDomainDetection:
    """域名命中是唯一能直接拿来路由的信号。"""

    @pytest.mark.parametrize(
        "url,system",
        [
            ("https://app.mokahr.com/campus_apply/someco/123", "mokahr"),
            ("https://talent.beisen.com/campus/acme/job/1", "beisen"),
            ("https://www.dayee.com/", "dayee"),
            ("https://acme.wd3.myworkdayjobs.com/en-US/careers", "workday"),
            ("https://career5.successfactors.com/career?company=x", "successfactors"),
            ("https://acme.taleo.net/careersection/x/joblist.ftl", "taleo"),
            ("https://boards.greenhouse.io/acme/jobs/1", "greenhouse"),
            ("https://jobs.lever.co/acme/abc", "lever"),
            ("https://xiaopeng.jobs.feishu.cn/index", "feishu"),
            ("https://youzan.zhiye.com/", "beisen"),
            ("https://join.qq.com/post.html?pid=1", "tencent_join"),
        ],
    )
    def test_known_domains_route(self, url: str, system: str) -> None:
        d = ats.detect(url)
        assert d.system == system
        assert d.confidence == ats.DOMAIN
        assert d.routable, "域名命中必须够格路由，否则整条链路没法自动化"

    def test_greenhouse_dot_com(self) -> None:
        """greenhouse 改名了，www.greenhouse.io 现在 301 到 www.greenhouse.com。

        只写 .io 的话新域名要靠标记兜底，判定降级成 markup 就不可路由了。
        实测踩到过，两个域名都得在表里。
        """
        d = ats.detect("https://www.greenhouse.com/")
        assert d.system == "greenhouse"
        assert d.confidence == ats.DOMAIN

    def test_unknown_domain_is_unknown(self) -> None:
        d = ats.detect("https://careers.some-random-corp.com/jobs")
        assert d.system == ats.UNKNOWN
        assert not d.routable
        assert d.evidence, "判不出来也要留证据，不然没法查为什么没认出"

    def test_garbage_url_does_not_crash(self) -> None:
        for bad in ("", "not a url", "http://", "javascript:void(0)"):
            assert ats.detect(bad).system == ats.UNKNOWN


class TestBeisenTenantDomain:
    """zhiye.com 这条是 2026-08 实测加进来的，把「北森一个真实租户都没抓到」推翻了。

    证据链（`data/ats_probe_midsize_v2.json`）：

        job.youzan.com  --302-->  youzan.zhiye.com    页面自称：有赞招聘
        静态资源  tcdn.bstatics.com / acdn.bstatics.com/ux/beisen-common/
        图片      stcms.beisen.com          ← 已核实的北森域名
        i18n      i18n.italent.cn           ← 已核实的北森域名

    跟字节那次误判的区别是**资源链路落在厂商已核实的域名上**，不只是页面里
    出现了品牌串——字节全程只在自家 CDN 上，只是提到了 beisen 这个词。

    反向对照才是这条能进 domains 的关键：zhiye.com 有通配 DNS + 兜底响应，
    非租户子域名一律返回 200（不是 404），所以「200」本身在这个域上什么都不证明，
    只有内容不同才算。三个非租户子域名返回的是字节完全相同的软 404。
    """

    def test_tenant_page_routes_at_domain_level(self) -> None:
        d = ats.detect("https://youzan.zhiye.com/")
        assert (d.system, d.tenant, d.confidence) == ("beisen", "youzan", ats.DOMAIN)
        assert d.routable, "这是采集/代投唯一能自动化的前提"

    def test_bstatics_is_a_lead_not_a_verdict(self) -> None:
        """北森的静态资源域出现在别人家页面上，只能立案。

        它是 dom_hints 而不是 domains：一个自建页面完全可能引一个北森资源
        （字节引了北森的在线考试就是这么回事），照着它路由就会拿北森的逻辑
        去打一个不是北森的页面。
        """
        d = ats.detect(
            "https://careers.unknown-corp.com/",
            '<script src="//acdn.bstatics.com/ux/beisen-common/x.js"></script>',
        )
        assert d.system == "beisen"
        assert not d.routable, "资源域出现在正文里只够立案，不够路由"

    def test_zhiye_notes_record_what_was_not_verified(self) -> None:
        """核实的是「页面由北森的前端和资源链路提供」，不是「域名归北森所有」。

        白标/代理的可能性从外部排除不了。notes 必须把这个口径写下来，
        否则下一个人会拿它当所有权结论用。
        """
        notes = ats.BY_KEY["beisen"].notes
        assert "zhiye.com" in notes
        assert "n=1" in notes, "租户页格式只有一个样本，不能外推"


class TestMarkupIsOnlyALead:
    """页面标记只是线索。这条分级是防误判的主要闸门。"""

    def test_markup_hit_is_not_routable(self) -> None:
        d = ats.detect("https://careers.unknown-corp.com/", "<html>powered by mokahr</html>")
        assert d.system == "mokahr"
        assert d.confidence == ats.MARKUP
        assert not d.routable, "标记命中只够立案，不够直接拿去路由"

    def test_bytedance_feature_flags_do_not_look_like_beisen(self) -> None:
        """字节自建页面上有 FeatureDailyExamUseBeisen 这类自家灰度开关。

        （它自建 ATS，只是接了北森的在线考试。）
        dom_hints 里放裸词 "beisen" 就会把字节判成北森——实测真的判错了。
        """
        html = (
            '{"ats.common.organization_display_optimization":1,'
            '"FeatureDailyExamUseBeisen":1,"BeisenExamAccount":"x",'
            '"ats.interview.coding_asset_write_sup":1}'
        )
        assert ats.detect("https://jobs.bytedance.com/campus", html).system == ats.UNKNOWN

    def test_competitor_names_on_marketing_page(self) -> None:
        """厂商官网会互相提名字。裸品牌词会让一个页面同时命中三家。

        实测 www.lever.co 的页面里同时有 workday 和 greenhouse。
        """
        html = "<html>compare us with Workday and Greenhouse and Lever</html>"
        assert ats.detect("https://blog.some-hr-media.com/x", html).system == ats.UNKNOWN

    def test_leverage_is_not_lever(self) -> None:
        html = "<html>leverage your data</html>"
        assert ats.detect("https://www.example.com/", html).system == ats.UNKNOWN

    def test_domain_beats_markup(self) -> None:
        """域名和标记打架时，域名说了算。"""
        d = ats.detect("https://jobs.lever.co/acme/x", "<html>greenhouse.io</html>")
        assert d.system == "lever"
        assert d.confidence == ats.DOMAIN


class TestTenantExtraction:
    """租户取错比取不到更坏：route_key 会静默把多家公司并到一条。"""

    @pytest.mark.parametrize(
        "url,tenant",
        [
            ("https://acme.wd3.myworkdayjobs.com/en-US/x", "acme"),
            ("https://xiaopeng.jobs.feishu.cn/index", "xiaopeng"),
            ("https://boards.greenhouse.io/acme/jobs/1", "acme"),
            ("https://jobs.lever.co/acme/abc-def", "acme"),
            ("https://app.mokahr.com/campus_apply/someco/1", "someco"),
            ("https://talent.beisen.com/campus/acme/job/1", "acme"),
            ("https://youzan.zhiye.com/", "youzan"),
        ],
    )
    def test_tenant_from_url(self, url: str, tenant: str) -> None:
        assert ats.detect(url).tenant == tenant

    def test_generic_subdomain_is_not_a_tenant(self) -> None:
        """boards.greenhouse.io 的 boards 是厂商自己的主机名。

        实测取出过 tenant="boards"，于是所有 greenhouse 公司的 route_key
        都是 greenhouse:boards——共用一个适配器条目，还看不出错在哪。
        """
        d = ats.detect("https://boards.greenhouse.io/")
        assert d.tenant is None

    def test_self_built_has_no_tenant(self) -> None:
        """自建系统天生只有一个租户，多出来的那半截是纯噪声。

        实测 join.qq.com 取出过 tenant="join"。
        """
        for url in ("https://join.qq.com/post.html", "https://careers.tencent.com/x"):
            d = ats.detect(url)
            assert d.tenant is None
            assert ":" not in d.route_key

    def test_missing_tenant_returns_none_not_guess(self) -> None:
        """取不到就是 None。宁可留空等 probe 补，不要编一个。"""
        d = ats.detect("https://app.mokahr.com/")
        assert d.system == "mokahr"
        assert d.tenant is None
        assert d.route_key == "mokahr"


class TestRouteKey:
    def test_route_key_shape(self) -> None:
        assert ats.detect("https://xiaopeng.jobs.feishu.cn/").route_key == "feishu:xiaopeng"

    def test_same_vendor_different_tenants_differ(self) -> None:
        """同一个 ATS 上的两家公司必须是两条 route_key，否则串号。"""
        a = ats.detect("https://acme.wd3.myworkdayjobs.com/x").route_key
        b = ats.detect("https://other.wd3.myworkdayjobs.com/x").route_key
        assert a != b

    def test_unknown_route_key_is_unknown(self) -> None:
        assert ats.detect("https://x.example.com/").route_key == ats.UNKNOWN


class TestFeishuScopeIsJobsOnly:
    """飞书只收 jobs.feishu.cn 这一个后缀。这条是 2026-08 实测收窄的。

    原来 domains 里有裸 feishu.cn，后果是一篇飞书文档被判成一家公司的招聘入口：

        abcde.feishu.cn/docx/HxKtdM2f0oL1s
          → system=feishu  tenant=abcde  domain  routable=True

    这是 domain 级的判错，比 markup 级严重得多：markup 只是立案，domain 会直接
    交给采集/代投去跑。飞书是一整套办公套件（文档、表格、IM），feishu.cn 底下
    绝大多数子域名跟招聘无关，所以品牌域名本身够不上判据。
    """

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.feishu.cn/",                        # 厂商官网，不是岗位源
            "https://abcde.feishu.cn/docx/HxKtdM2f0oL1s",    # 一篇文档
            "https://bytedance.feishu.cn/sheets/xyz",        # 一张表
        ],
    )
    def test_non_ats_feishu_urls_refuse_to_route(self, url: str) -> None:
        """飞书套件里非招聘的域名一律不认。宁可 unknown 等人补，不要判错。"""
        d = ats.detect(url)
        assert d.system == ats.UNKNOWN, f"{url} 不该被判成招聘系统"
        assert d.tenant is None, "更不该从里面编出一个租户名"
        assert not d.routable

    @pytest.mark.parametrize("url", [
        "https://xiaopeng.jobs.feishu.cn/campus",
        "https://nio.jobs.feishu.cn/index",
    ])
    def test_real_tenants_still_route(self, url: str) -> None:
        """收窄不能把实测命中的那两家弄丢——后缀匹配照样吃子域名。"""
        d = ats.detect(url)
        assert (d.system, d.confidence) == ("feishu", ats.DOMAIN)
        assert d.routable and d.tenant

    def test_feishucdn_is_not_a_marker(self) -> None:
        """feishucdn.com 不能当判据：它是字节所有产品共用的 CDN。

        实测在 jobs.bytedance.com（字节自建招聘页）上命中过，把字节报成
        「命中第三方 ATS」。而飞书本来就是字节自己的产品——那是第一方依赖，
        不是「采用了第三方 ATS」。这跟裸 "beisen" 误命中字节是同一个毛病。
        """
        d = ats.detect(
            "https://jobs.bytedance.com/campus",
            '<script src="//lf-package-cn.feishucdn.com/x.js"></script>',
        )
        assert d.system == ats.UNKNOWN, "自家 CDN 不构成第三方 ATS 的证据"


class TestRegistryInvariants:
    """表本身的自检。加厂商的时候最容易在这几点上翻车。"""

    def test_keys_unique(self) -> None:
        keys = [v.key for v in ats.VENDORS]
        assert len(keys) == len(set(keys))
        assert len(ats.BY_KEY) == len(ats.VENDORS)

    def test_no_bare_brand_word_hints(self) -> None:
        """dom_hints 里的每条标记，要么带点号/连字符，要么在自造词白名单里。

        判据是「这条串会不会出现在跟这家厂商无关的页面上」。带点号的是域名片段
        和资源前缀，天然安全；剩下的必须是厂商自造词（写进 COINED_HINTS）。
        这条规则是被实测打出来的，不是洁癖。
        """
        offenders = [
            (v.key, h)
            for v in ats.VENDORS
            for h in v.dom_hints
            if not any(c in h for c in "._-") and h.lower() not in ats.COINED_HINTS
        ]
        assert not offenders, (
            f"这些 hint 会误命中无关页面：{offenders}。"
            "确认它是厂商自造词再加进 ats.COINED_HINTS。"
        )

    @pytest.mark.parametrize(
        "word", ["greenhouse", "workday", "lever", "beisen", "moka", "dayee", "feishu"]
    )
    def test_specific_bare_words_stay_out(self, word: str) -> None:
        """这几个裸词是实测确认会误命中的，任何时候都不许作为独立 hint 出现。

        白名单挡不住有人把 "greenhouse" 顺手加进 COINED_HINTS，所以这里逐个点名。
        """
        for v in ats.VENDORS:
            assert word not in [h.lower() for h in v.dom_hints], (
                f"{v.key} 的 dom_hints 里出现了裸词 {word!r}"
            )
        assert word not in ats.COINED_HINTS, f"{word!r} 不是自造词，不该进白名单"

    def test_coined_hints_are_all_in_use(self) -> None:
        """白名单里不该留没人用的条目，否则它只是记录了一次早已删掉的妥协。"""
        used = {h.lower() for v in ats.VENDORS for h in v.dom_hints}
        assert not (ats.COINED_HINTS - used), f"白名单有死条目：{ats.COINED_HINTS - used}"

    def test_self_built_vendors_have_no_tenant_config(self) -> None:
        for v in ats.VENDORS:
            if v.self_built:
                assert v.tenant_seg is None, f"{v.key} 自建，不该配租户路径段"

    def test_every_vendor_has_at_least_one_domain(self) -> None:
        for v in ats.VENDORS:
            assert v.domains, f"{v.key} 没有域名，就永远只能靠标记猜，不可路由"

    def test_domains_are_bare_registrable_names(self) -> None:
        """domains 里写裸域名，不带协议、不带路径、不带前导点。

        detect_from_url 是按 host == d or host.endswith("." + d) 匹配的，
        写成 "https://x.com" 或 ".x.com" 会永远匹配不上，而且不报错。
        """
        for v in ats.VENDORS:
            for d in v.domains:
                assert "/" not in d and not d.startswith("."), f"{v.key}: {d!r}"
                assert d == d.lower(), f"{v.key}: {d!r} 要小写，host 是小写的"
