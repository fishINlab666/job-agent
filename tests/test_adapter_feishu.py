"""飞书适配器测试。全部假 transport，不打网络。

钉的是 `docs/plans/002-飞书招聘采集.md` §3 那些实测结论，以及三类
「不许悄悄发生」的事：判不出族被兜底成 other、城市不过归一、
空结果被无条件放过。
"""
from __future__ import annotations

import httpx
import pytest

from jobagent.adapters.feishu import FeishuAdapter
from jobagent.normalize import family_from_title


# 必须在打补丁之前把真类抓住。工厂里直接写 httpx.Client 会调到被 patch 的
# 那个符号，也就是它自己 —— RecursionError，而不是一个看得懂的失败。
_REAL_CLIENT = httpx.Client


def _mock_client(handler):
    """把 FeishuAdapter 里的 httpx.Client 换成走 MockTransport 的。

    适配器在 fetch() 内部自己建 client（跟腾讯那个一样），所以只能从
    模块符号这一层拦。返回一个 factory，签名要吃掉 timeout/headers。
    """

    def factory(*_args, **kwargs):
        kwargs.pop("transport", None)
        return _REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    return factory


# 抛什么都算过的话，连 RecursionError 都能让「该抛」的用例变绿 ——
# 第一版就是这么假绿的。只认这两类：适配器自己抛的和 HTTP 层抛的。
EXPECTED_ERRORS = (RuntimeError, httpx.HTTPStatusError)


def _post(post_id: str = "1", title: str = "后端开发工程师", **over) -> dict:
    row = {
        "id": post_id,
        "title": title,
        "city_list": [{"name": "北京"}],
        "recruit_type": {"name": "全职", "parent": {"name": "社招"}},
        "job_category": None,
        "job_function": None,
        "description": "岗位职责若干",
        "requirement": "任职要求若干",
    }
    row.update(over)
    return row


def _body(posts: list[dict], count: int | None = None) -> dict:
    return {
        "code": 0,
        "data": {"count": count if count is not None else len(posts),
                 "job_post_list": posts},
    }


def _serve(pages: list[dict], record: list | None = None):
    """按调用次序依次返回 pages 里的响应体。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        i = min(calls["n"], len(pages) - 1)
        calls["n"] += 1
        return httpx.Response(200, json=pages[i])

    return handler


def _fetch(monkeypatch, handler, tenant: str = "nio", **kw) -> tuple:
    ad = FeishuAdapter(tenant, **kw)
    monkeypatch.setattr("jobagent.adapters.feishu.httpx.Client", _mock_client(handler))
    return ad, ad.fetch()


class TestFamilyNotBackfilled:
    """判不出族写 None，不许兜底成 "other"。

    腾讯那行 `family_from_title(title) or FAMILY_MAP.get(fam_id, "other")`
    成立是因为 positionFamily 是站点级固定枚举。飞书这个是**租户级自由分类**
    （nio 50 个取值 / xiaopeng 53 个，交集只有 6 个），映射不过来。
    照抄就会把 1099/2265 条打成「其他」—— 那不是其他族，是没判出来。
    """

    # 这四条是 nio 全量里真实判不出的标题，不是编的。
    #
    # 【挑选判据换过一次，别再按「当前判不出」挑】
    # 这批例子过期过两轮：017 加 `顾问` 撞掉 `ONVO-乐道行销顾问-重庆`，
    # 018 加 `售后` 撞掉 `实习-NSC售后服务代表` 和 `售后服务-增值服务（西安）`。
    # 两次红的都是这个文件，而坏的是词表扩容 —— 排查方向被带偏了两次。
    # 所以判据从「跑一遍现在判不出」换成「差一点命中的那个词在**明确不加**的
    # 否决表里」（`scripts/measure_family_gaps.py` 的 REJECTED_018）：
    #   计划  → 否决理由「`培养计划` 里是「项目」的意思：救 9 个错 6 个」
    #   分析  → 否决理由「跨族：数据分析/商业分析/财务分析」
    #   整车 / 大使 / 机电 → 域词和身份词，不是职能，方案 017 §6 的口径
    # 词表下次扩容时这四条仍然不该动；真被撞了，是那次扩容越过了否决表，
    # 该看的是 normalize.py 而不是这里。下面 test_fixtures_are_still_undecidable
    # 会先替你把这件事说清楚。
    UNDECIDABLE = [
        "蔚来AGI超星计划-敢想敢研，自成课题",
        "实习-AI代码分析",
        "提前批-整车音响调音师",
        "实习-蔚来2027届校园大使",
    ]

    @pytest.mark.parametrize("title", UNDECIDABLE)
    def test_undecidable_title_stays_none(self, monkeypatch, title):
        _, jobs = _fetch(monkeypatch, _serve([_body([_post(title=title)])]))
        assert jobs[0].job_family is None, "判不出就该是 None，兜底成 other 会让用户按族筛不到"

    @pytest.mark.parametrize("title", UNDECIDABLE)
    def test_fixtures_are_still_undecidable(self, title):
        """前提独立成一条：这些标题在**规则层**必须仍然判不出。

        上面那条用例只有在 `family_from_title(title) is None` 时才在测适配器；
        前提一旦不成立，它就变成一条恒绿的空用例 —— 或者像 017/018 那样红在
        适配器上，让人去查一个没坏的东西。把前提单独钉出来，红的时候消息直接
        指向词表。
        """
        assert family_from_title(title) is None, (
            f"`{title}` 现在判得出来了 —— 坏的不是适配器，是这条 fixture 过期了。"
            "词表扩容撞到了 REJECTED_018 里的词，先确认那次扩容对不对；"
            "确实该加就换 fixture，从 `measure_family_gaps.py by-source` 里"
            "挑一条近似命中词仍在否决表上的。"
        )

    def test_source_category_does_not_decide_family(self, monkeypatch):
        """源站分类**存在也不参与判定** —— 这条是这次最要紧的反向用例。

        谁图省事写 `or raw_category` 或 `or "other"`，这条立刻红。
        """
        # 刻意挑 raw_category 里含**职能词**的一条：`项目管理` 就在 TITLE_RULES 的
        # product 组里。写 `or raw_category` 或者拿 raw_category 再过一遍规则，
        # 这条会判成 product 而不是 None —— 错得看得见。
        # 标题 `实习-PMO` 本身没有中文职能词，所以命中只可能来自 raw_category。
        row = _post(title="实习-PMO", job_function={"name": "项目管理", "id": "x"})
        _, jobs = _fetch(monkeypatch, _serve([_body([row])]))
        assert jobs[0].job_family is None
        assert jobs[0].raw_category == "项目管理", "原文要留着，供以后扩 TITLE_RULES 看分布"

    def test_decidable_title_still_works(self, monkeypatch):
        _, jobs = _fetch(monkeypatch, _serve([_body([_post(title="后端开发工程师")])]))
        assert jobs[0].job_family == "tech"

    def test_bytedance_department_tail_does_not_override_role(self, monkeypatch):
        title = "AI产品经理（商业化系统方向） - 飞书商业运营与策略"
        _, jobs = _fetch(
            monkeypatch,
            _serve([_body([_post(title=title)])]),
            tenant="bytedance",
            portal="campus",
        )
        assert jobs[0].job_family == "product"


class TestBothCategoryFields:
    """两个分类字段互斥，哪个有值按租户不同 —— 都得认。

    实测：nio 走 job_function（2263/2265，job_category 一条没有），
    xiaopeng 反过来（job_category 1630/1630）。只读一个字段就会在另一个
    租户上把 raw_category 全丢成 None，而这个字段是以后扩词的唯一依据。
    """

    def test_job_function_shape(self, monkeypatch):
        row = _post(job_function={"name": "用户与服务"})
        _, jobs = _fetch(monkeypatch, _serve([_body([row])]))
        assert jobs[0].raw_category == "用户与服务"

    def test_job_category_shape(self, monkeypatch):
        row = _post(job_category={"name": "生产/制造/加工", "depth": 2})
        _, jobs = _fetch(monkeypatch, _serve([_body([row])]), tenant="xiaopeng")
        assert jobs[0].raw_category == "生产/制造/加工"

    def test_neither_gives_none(self, monkeypatch):
        _, jobs = _fetch(monkeypatch, _serve([_body([_post()])]))
        assert jobs[0].raw_category is None, "两个都没有就是 None，不编"


class TestEmptyIsAuthoritative:
    """count=0 是事实，不是故障 —— 但只有接口明说时才算。"""

    def test_count_zero_returns_empty_and_flags(self, monkeypatch):
        """luckin/horizon 实测就是这样：活租户、接口正常、当下没在招。"""
        ad, jobs = _fetch(monkeypatch, _serve([_body([], count=0)]), tenant="luckin")
        assert jobs == []
        # 这一条是「做一半」的守卫：光让 fetch 不抛不够，ingest 那侧凭这个标记
        # 才敢不抛。忘了置它，适配器单测全绿而 sync 照样 failed。
        assert ad.empty_is_authoritative is True

    def test_flag_starts_false(self):
        assert FeishuAdapter("nio").empty_is_authoritative is False

    def test_flag_resets_between_fetches(self, monkeypatch):
        """先 count=0 再有岗位，标记必须回落 —— 否则第二轮的空结果会被误放过。"""
        ad = FeishuAdapter("nio")
        monkeypatch.setattr(
            "jobagent.adapters.feishu.httpx.Client",
            _mock_client(_serve([_body([], count=0)])),
        )
        ad.fetch()
        assert ad.empty_is_authoritative is True
        monkeypatch.setattr(
            "jobagent.adapters.feishu.httpx.Client",
            _mock_client(_serve([_body([_post()])])),
        )
        ad.fetch()
        assert ad.empty_is_authoritative is False


class TestEmptyWithoutReasonRaises:
    """空但说不清原因 → 照旧抛，且标记不许为真。反向对照。

    少了这一组，上面那条会退化成「什么空都放过」，
    而空结果被放过的后果是 diff 把全部岗位判成关闭。
    """

    def _expect_raise(self, monkeypatch, handler, tenant="nio"):
        ad = FeishuAdapter(tenant)
        monkeypatch.setattr(
            "jobagent.adapters.feishu.httpx.Client", _mock_client(handler)
        )
        with pytest.raises(EXPECTED_ERRORS):
            ad.fetch()
        assert ad.empty_is_authoritative is False
        return ad

    def test_code_not_zero(self, monkeypatch):
        def handler(_):
            return httpx.Response(200, json={"code": 500, "msg": "internal"})

        self._expect_raise(monkeypatch, handler)

    def test_non_200(self, monkeypatch):
        def handler(_):
            return httpx.Response(503, text="upstream down")

        self._expect_raise(monkeypatch, handler)

    def test_fake_tenant_400_non_json(self, monkeypatch):
        """假租户实测是 400 + 非 JSON（lixiang / qwerasdfzxcv0000）。"""

        def handler(_):
            return httpx.Response(400, text="<html>Bad Request</html>")

        self._expect_raise(monkeypatch, handler, tenant="lixiang")

    def test_200_but_not_json(self, monkeypatch):
        """UA 门那种情况的近亲：HTTP 层没炸，body 不是 JSON。"""

        def handler(_):
            return httpx.Response(200, text="")

        self._expect_raise(monkeypatch, handler)

    def test_truncated_paging_raises(self, monkeypatch):
        """count 说 300，第二页给空批次 → 抛，不许静默返回半截。

        静默截断比整轮失败危险得多：没拿到的那批会被 diff 判成已关闭。
        """
        page1 = _body([_post(str(i)) for i in range(200)], count=300)
        page2 = _body([], count=300)
        self._expect_raise(monkeypatch, _serve([page1, page2]))


class TestCityNormalization:
    """城市必须过 normalize_city。

    方案第一版写「不用改归一」，实测 xiaopeng 上有「中国香港」——
    不归一就和「香港」分成两个城市，用户按香港筛漏掉一批。
    """

    def test_china_hongkong_rewritten(self, monkeypatch):
        row = _post(city_list=[{"name": "中国香港"}])
        _, jobs = _fetch(monkeypatch, _serve([_body([row])]), tenant="xiaopeng")
        assert jobs[0].cities == ["香港"]

    def test_multi_city_all_kept(self, monkeypatch):
        row = _post(city_list=[{"name": "北京"}, {"name": "上海"}, {"name": "合肥"}])
        _, jobs = _fetch(monkeypatch, _serve([_body([row])]))
        assert jobs[0].cities == ["北京", "上海", "合肥"]

    def test_raw_location_keeps_original(self, monkeypatch):
        row = _post(city_list=[{"name": "中国香港"}, {"name": "深圳总部"}])
        _, jobs = _fetch(monkeypatch, _serve([_body([row])]))
        assert jobs[0].raw_location == "中国香港/深圳总部", "原文不归一，归一只作用于 cities"
        assert jobs[0].cities == ["香港", "深圳"]

    @pytest.mark.parametrize("empty", [None, [], [{"name": ""}]])
    def test_empty_city_list(self, monkeypatch, empty):
        row = _post(city_list=empty)
        _, jobs = _fetch(monkeypatch, _serve([_body([row])]))
        assert jobs[0].cities == [], "空就是 []，交给 match 的三态判断，采集层不判否"


class TestFieldMapping:
    def test_recruit_type_intern(self, monkeypatch):
        row = _post(recruit_type={"name": "实习", "parent": {"name": "社招"}})
        _, jobs = _fetch(monkeypatch, _serve([_body([row])]))
        assert jobs[0].recruit_type == "intern"

    @pytest.mark.parametrize("leaf", ["全职", "劳务", "顾问", "外包"])
    def test_recruit_type_social(self, monkeypatch, leaf):
        row = _post(recruit_type={"name": leaf, "parent": {"name": "社招"}})
        _, jobs = _fetch(monkeypatch, _serve([_body([row])]))
        assert jobs[0].recruit_type == "social"

    def test_grad_year_always_none(self, monkeypatch):
        """这个口子没有届别字段（job_post_info 里 required_degree 是学历，不是届别）。

        None = 信息不足，parse_grad_years 那侧已经分了三态，不许在这儿编一个。
        """
        row = _post(job_post_info={"required_degree": 6, "experience": 3})
        _, jobs = _fetch(monkeypatch, _serve([_body([row])]))
        assert jobs[0].grad_year is None

    def test_department_none_not_invented(self, monkeypatch):
        row = _post(department_id="7123456789")
        _, jobs = _fetch(monkeypatch, _serve([_body([row])]))
        assert jobs[0].department is None, "接口只给 id 没给名字，不编"

    def test_apply_url_and_system(self, monkeypatch):
        """不带门户的老源退到 `index`，且必须带 `/detail` 后缀。

        2026-08-10 实测（nio/xiaopeng/bytedance/sensetime 四家一致），形状是从各
        租户列表页的 `<a href>` 上读出来的：只有 `/<portal>/position/<id>/detail`
        渲染岗位正文，少 `/detail` 或少门户段都渲染「页面不存在」。
        见 `_position_url()` 的注释。
        """
        _, jobs = _fetch(monkeypatch, _serve([_body([_post("777")])]))
        assert jobs[0].apply_url == "https://nio.jobs.feishu.cn/index/position/777/detail"
        assert jobs[0].apply_system == "feishu"

    def test_description_joins_requirement(self, monkeypatch):
        _, jobs = _fetch(monkeypatch, _serve([_body([_post()])]))
        assert "岗位职责若干" in jobs[0].description
        assert "任职要求若干" in jobs[0].description

    def test_no_id_skipped_and_counted(self, monkeypatch):
        """没 id 就跳过并计数，**不许 fallback 到 title**。

        title 会重复，撞 UNIQUE(source_key, external_id)，
        一条脏数据能挡掉后面整批。
        """
        rows = [_post("1"), _post("", title="无 id 的岗"), _post("2")]
        ad, jobs = _fetch(monkeypatch, _serve([_body(rows)]))
        assert [j.external_id for j in jobs] == ["1", "2"]
        assert ad.skipped_no_id == 1


class TestMultiTenantContract:
    """多租户契约：一个类摊多家，键里必须带租户。"""

    def test_source_key_carries_tenant(self):
        assert FeishuAdapter("nio").source_key == "feishu:nio"
        assert FeishuAdapter("xiaopeng").source_key == "feishu:xiaopeng"

    def test_system_is_the_registry_key(self):
        """system 必须等于注册键，不能写成 "self_built" 那种形容词。

        腾讯那个适配器踩过：按形容词去注册表里找永远找不到，
        而且所有自建公司会挤在同一个键上。
        """
        assert FeishuAdapter.system == "feishu"

    def test_tenant_required(self):
        """空租户会拼出 https://.jobs.feishu.cn/，构造时就该炸。"""
        with pytest.raises(ValueError):
            FeishuAdapter("")
        with pytest.raises(ValueError):
            FeishuAdapter("   ")

    def test_company_defaults_to_tenant(self):
        assert FeishuAdapter("nio").company == "nio"
        assert FeishuAdapter("nio", company="蔚来").company == "蔚来"

    def test_entry_url_per_tenant(self):
        assert FeishuAdapter("nio").entry_url == "https://nio.jobs.feishu.cn/"


class TestUaGateIsLoadBearing:
    """UA 是承重的：这个串能过，`Mozilla/5.0` 裸串返回 405 + 空 body。

    没定位到它具体按哪一项判（见模块注释），所以这里钉的不是「规则」，
    是「别把这个串简化掉」—— 方案第一版那条复现命令就是因为简化了 UA
    而跑不起来的。
    """

    def test_ua_sent_on_every_request(self, monkeypatch):
        seen: list[httpx.Request] = []
        page1 = _body([_post(str(i)) for i in range(200)], count=250)
        page2 = _body([_post(str(i)) for i in range(200, 250)], count=250)
        _fetch(monkeypatch, _serve([page1, page2], record=seen))
        assert len(seen) == 2
        for req in seen:
            ua = req.headers["User-Agent"]
            assert "Macintosh; Intel Mac OS X 10_15_7" in ua
            assert "Chrome/" in ua
            assert req.headers["Referer"] == "https://nio.jobs.feishu.cn/"

    def test_paging_walks_offset(self, monkeypatch):
        seen: list[httpx.Request] = []
        page1 = _body([_post(str(i)) for i in range(200)], count=250)
        page2 = _body([_post(str(i)) for i in range(200, 250)], count=250)
        _, jobs = _fetch(monkeypatch, _serve([page1, page2], record=seen))
        assert len(jobs) == 250
        import json as _json
        assert _json.loads(seen[0].content)["offset"] == 0
        assert _json.loads(seen[1].content)["offset"] == 200


def _campus(leaf: str = "正式", **over) -> dict:
    """校招池的 recruit_type 形状（实测：parent.id=2，leaf 201=正式 / 202=实习）。"""
    return _post(recruit_type={"name": leaf, "id": "201",
                               "parent": {"name": "校招", "id": "2"}}, **over)


class TestCampusRecruitType:
    """校招池的 `正式` 必须判成 campus，不是 social。

    上一版只看叶子名、结尾无条件 `return "social"`，注释写「这个口子里没有校招」——
    那句话是在社招池里核实的。一换成校招门户，`正式` 不含「实习」二字，
    就掉进那句 return，**把校招岗位标成社招**。比判不出更糟：判不出用户靠
    `--loose` 还能看到，标错就静默进了另一个类。见 003 §4。
    """

    def test_campus_regular_is_campus(self, monkeypatch):
        _, jobs = _fetch(monkeypatch, _serve([_body([_campus("正式")])]))
        assert jobs[0].recruit_type == "campus"

    def test_campus_intern_stays_intern(self, monkeypatch):
        """反向对照：别把校招实习一起洗进 campus。

        `campus+实习 → intern` 是有意的（003 §4）：`match.classify()` 和
        `cli jobs --recruit-type` 只认三个值，加第四个要同时改两处，
        而收益只是把「校招实习」和「社招实习」分开——用户画像里两者都要。
        """
        _, jobs = _fetch(monkeypatch, _serve([_body([_campus("实习")])]))
        assert jobs[0].recruit_type == "intern"

    def test_social_unaffected(self, monkeypatch):
        """社招池不许被这次改动带跑偏 —— 库里 4810 条都是这一支。"""
        rows = [_post(str(i), recruit_type={"name": leaf, "parent": {"name": "社招"}})
                for i, leaf in enumerate(("全职", "劳务", "顾问", "外包"))]
        _, jobs = _fetch(monkeypatch, _serve([_body(rows)]))
        assert {j.recruit_type for j in jobs} == {"social"}

    def test_unknown_parent_is_none_not_social(self, monkeypatch):
        """认不出的 parent 写 None。谁加 `or "social"` 兜底，这条红。

        飞书今天只有社招/校招两棵树。冒出第三棵说明源站改了分类体系，
        那时候「静默按社招处理」会把一整批岗位标错，而 None 是能被发现的信号
        （填充率报告里 recruit_type 会掉下来）。
        """
        row = _post(recruit_type={"name": "正式", "parent": {"name": "外星人招"}})
        _, jobs = _fetch(monkeypatch, _serve([_body([row])]))
        assert jobs[0].recruit_type is None

    def test_missing_recruit_type_is_none(self, monkeypatch):
        _, jobs = _fetch(monkeypatch, _serve([_body([_post(recruit_type=None)])]))
        assert jobs[0].recruit_type is None


class TestPortalHeader:
    """`website-path` 是选门户的唯一开关，必须真的发出去。

    钉的是「加了参数但没传」这种半成品：不带头时 nio 返回 2249 条社招，
    带 `campus` 返回 627 条校招 —— 参数收下了却没进请求头，
    表现是「校招源采到了一堆社招岗」，而两个数都是三位数以上，肉眼看不出。
    """

    def test_header_sent_when_portal_given(self, monkeypatch):
        seen: list[httpx.Request] = []
        _fetch(monkeypatch, _serve([_body([_campus()])], record=seen), portal="campus")
        assert seen[0].headers["website-path"] == "campus"

    def test_header_absent_without_portal(self, monkeypatch):
        """不带门户时**必须没有这个键**，不许发空串。

        发空串等于赌接口怎么处理空值。实测不带头是第三个池（2249）、
        带 index 是社招池（2077），两者互不包含 —— 赌错就换了一整个池子。
        """
        seen: list[httpx.Request] = []
        _fetch(monkeypatch, _serve([_body([_post()])], record=seen))
        assert "website-path" not in seen[0].headers

    def test_blank_portal_same_as_none(self, monkeypatch):
        seen: list[httpx.Request] = []
        _fetch(monkeypatch, _serve([_body([_post()])], record=seen), portal="   ")
        assert "website-path" not in seen[0].headers

    def test_header_on_every_page(self, monkeypatch):
        """翻页的每一页都要带。少带一页就是把两个池的数据混进一个源。"""
        seen: list[httpx.Request] = []
        page1 = _body([_post(str(i)) for i in range(200)], count=250)
        page2 = _body([_post(str(i)) for i in range(200, 250)], count=250)
        _fetch(monkeypatch, _serve([page1, page2], record=seen), portal="edu")
        assert len(seen) == 2
        assert all(r.headers["website-path"] == "edu" for r in seen)


class TestPortalNotFound:
    """`code=-9000003` 必须抛，且不许置 empty_is_authoritative。

    关键反向用例。少了它，校招门户改个名 → 接口回这个码 → 被当成「空结果可信」
    → diff 把 627 条全部判成关闭，而 run 记 ok。不报错，只是数据没了。
    """

    def test_raises(self, monkeypatch):
        def handler(_):
            return httpx.Response(200, json={"code": -9000003, "msg": "website not found"})

        ad = FeishuAdapter("haidilao", portal="campus")
        monkeypatch.setattr("jobagent.adapters.feishu.httpx.Client", _mock_client(handler))
        with pytest.raises(EXPECTED_ERRORS) as exc:
            ad.fetch()
        assert ad.empty_is_authoritative is False, "配置错不是「当下没岗位」"
        # 消息里要能看出是门户的事。只说「code=-9000003」的话，
        # 下一个人会去查上游是不是挂了，而实际动作是改配置。
        assert "campus" in str(exc.value)

    def test_count_zero_still_authoritative(self, monkeypatch):
        """对照：同一个门户下 count=0 仍然是可信的空（这家现在没在招）。

        两条放一起才说明问题被理解了：不是「带门户就一律抛」，
        是「门户不存在才抛」。
        """
        ad, jobs = _fetch(monkeypatch, _serve([_body([], count=0)]), portal="campus")
        assert jobs == []
        assert ad.empty_is_authoritative is True


class TestPortalSourceKey:
    """`source_key` 带门户，不带门户时保持两段不变。"""

    def test_key_with_portal(self):
        assert FeishuAdapter("nio", portal="campus").source_key == "feishu:nio:campus"

    def test_key_without_portal_unchanged(self):
        """**老源不许改键。** 核对库里 4810 条挂的是两段的键，
        改成三段它们全变孤儿：diff 找不到旧行，一轮下来「全部新增 + 全部关闭」。
        """
        assert FeishuAdapter("nio").source_key == "feishu:nio"
        assert FeishuAdapter("nio", portal="").source_key == "feishu:nio"

    def test_entry_url_points_at_portal(self):
        """entry_url 是给人点开核对的。指到租户首页，核对的人会看到社招列表，
        然后以为我们采错了。"""
        assert FeishuAdapter("nio", portal="campus").entry_url == \
            "https://nio.jobs.feishu.cn/campus/"
        assert FeishuAdapter("nio").entry_url == "https://nio.jobs.feishu.cn/"


class TestApplyUrlCarriesPortal:
    """`apply_url` 必须带门户段**和** `/detail` 后缀。这是修 bug，不是加功能。

    2026-08-10 实测（nio / xiaopeng / bytedance / sensetime 四个租户一致），
    形状从各租户列表页的 `<a href>` 上直接读出来：

        /position/<id>                  → 渲染「页面不存在」
        /<portal>/position/<id>         → 渲染「页面不存在」  ← 2026-08-10 前用的
        /<portal>/position/<id>/detail  → 渲染岗位正文

    `apply_url` 的唯一用途就是「点开就是官网那一页」拿去人工核对，
    而库里 8594 条飞书岗位的链接在补 `/detail` 之前**全部**打不开。

    **为什么上一轮验漏了**：页面是 SPA，404 发生在渲染层，HTTP 照样 200 而且
    body 有 200KB。只看状态码看不出死活，判据得是渲染后的正文里有没有
    「页面不存在」。
    """

    def test_portal_in_path(self, monkeypatch):
        _, jobs = _fetch(monkeypatch, _serve([_body([_campus(post_id="42")])]),
                         portal="campus")
        assert jobs[0].apply_url == "https://nio.jobs.feishu.cn/campus/position/42/detail"

    def test_detail_suffix_always_present(self, monkeypatch):
        """反向用例：谁把 `/detail` 去掉，这条立刻红。

        单独一条而不是并进上面那条，因为这两个段是**两个独立的错法**：
        门户段管「id 落在哪个门户下」，`/detail` 管「路由到详情页而不是空壳」。
        少任何一个都是死链，但修法不同。
        """
        for portal in ("campus", "edu", None):
            _, jobs = _fetch(monkeypatch, _serve([_body([_post("777")])]),
                             portal=portal)
            assert jobs[0].apply_url.endswith("/position/777/detail"), (
                f"portal={portal!r} 时少了 /detail 后缀"
            )

    def test_bare_position_path_never_produced(self, monkeypatch):
        """反向用例：谁把门户段去掉图省事，这条立刻红。

        断言的是「不等于那个死链形状」而不是「等于某个形状」—— 上面已经钉了
        正形状，这条专门盯住回退。
        """
        for portal in ("campus", "edu", None):
            _, jobs = _fetch(monkeypatch, _serve([_body([_post("777")])]),
                             portal=portal)
            assert jobs[0].apply_url != "https://nio.jobs.feishu.cn/position/777"
            assert "/position/777" in jobs[0].apply_url
            # 门户段就在 /position/ 前面那一节，且不是空的（`//position/` 也是死链）
            prefix = jobs[0].apply_url.split("/position/")[0]
            assert prefix.rsplit("/", 1)[1], f"portal={portal!r} 时门户段是空的"


class TestCustomHost:
    """自定义域名：`hr-jobs.sensetime.com` 和 `sensetime.jobs.feishu.cn` 是同一个
    租户（实测同样 159 条、id 交集 159），但投递链接必须用配的那个 host。

    写死成 `<tenant>.jobs.feishu.cn` 的后果是商汤的 apply_url 指向另一个站点 ——
    页面能打开（同一个租户），所以核对时不一定看得出来。
    """

    def test_apply_url_uses_custom_host(self, monkeypatch):
        _, jobs = _fetch(monkeypatch, _serve([_body([_campus(post_id="9")])]),
                         tenant="sensetime", portal="edu", host="hr-jobs.sensetime.com")
        assert jobs[0].apply_url == "https://hr-jobs.sensetime.com/edu/position/9/detail"

    def test_request_goes_to_custom_host(self, monkeypatch):
        seen: list[httpx.Request] = []
        _fetch(monkeypatch, _serve([_body([_campus()])], record=seen),
               tenant="sensetime", portal="edu", host="hr-jobs.sensetime.com")
        assert seen[0].url.host == "hr-jobs.sensetime.com"
        assert seen[0].headers["Referer"] == "https://hr-jobs.sensetime.com/"

    def test_default_host_unchanged(self, monkeypatch):
        _, jobs = _fetch(monkeypatch, _serve([_body([_post("777")])]))
        assert jobs[0].apply_url == "https://nio.jobs.feishu.cn/index/position/777/detail"

    def test_feishu_host_tenant_must_match(self):
        """飞书自己域名下租户在子域名里，能核对就必须核对。

        失败方式是静默的：`sources.entry_url` 抄错一行（复制上一家忘了改子域名），
        我们会拿 nio 的配置去打 xiaopeng 的接口，把小鹏的岗位落在蔚来名下。
        """
        with pytest.raises(ValueError):
            FeishuAdapter("nio", host="xiaopeng.jobs.feishu.cn")
        # 对得上就放过
        assert FeishuAdapter("nio", host="nio.jobs.feishu.cn").base == \
            "https://nio.jobs.feishu.cn"


def _subject(name: str | None) -> dict:
    """招聘项目名的真实嵌套形状：job_subject.name.zh_cn。"""
    return {"job_subject": {"name": {"zh_cn": name}}}


class TestGradYearFromSubject:
    """届别第三通道：招聘项目名（`job_subject`）。plan 011。

    这个源的 `grad_year` 那一列确实不存在（全量核实过），但届别一直躺在招聘
    项目名里，从采集第一天起就在 `snapshots.raw_json` 中。kb v3 那句
    「飞书 0/8594，字段真的不存在」说的是那一列，被误当成了整个数据源的结论。

    2026-08-10 实测覆盖率：bytedance 2073/7368、nio 313/634、
    sensetime 73/161、xiaopeng 0/431。
    """

    def test_grad_year_from_job_subject(self, monkeypatch):
        _, jobs = _fetch(monkeypatch, _serve([_body([
            _campus(**_subject("2027届校园招聘"))
        ])]), portal="campus")
        assert jobs[0].grad_year == "27"

    def test_real_subject_values_all_four_tenants(self):
        """四租户的真实取值逐个过一遍（值抄自 2026-08-10 全量枚举）。"""
        from jobagent.adapters.feishu import _grad_year_from_subject as g

        # 带届别的
        for name in ("2027届校园招聘", "2027届前沿技术领域人才校招",
                     "2027届Seed大模型人才校招", "2027届校园招聘-技术提前批",
                     "蔚来2027届实习生招募", "2027届校园招聘-正式批",
                     "27届校园招聘"):
            assert g(_subject(name)) == "27", name

        # 不带届别的 —— 必须是 None，见下一条
        for name in ("ByteIntern", "日常实习", "前沿技术领域人才实习招聘",
                     "Seed大模型人才实习招聘", "营销暑期实习生招募",
                     "Shine校园招聘计划", "蔚来AGI超星计划",
                     "Super Sparks 招聘计划", "实习生",
                     "「无限原力」顶尖人才计划"):
            assert g(_subject(name)) is None, name

    def test_intern_subject_yields_none_not_unlimited(self):
        """实习项目名返回 None（信息不全），**不许兜底成「不限」**。

        腾讯那边实习是「不限」，但那是 projectId 分桶实测出来的（plan 009）。
        这里没有等价证据，编一个「不限」等于把 5295 条实习岗洗成
        「任何届别都命中」—— 把不知道说成确定命中，方向最错。
        """
        from jobagent.adapters.feishu import _grad_year_from_subject as g

        assert g(_subject("ByteIntern")) is None
        assert g(_subject("日常实习")) is None
        # 关键：不是空串也不是「不限」
        assert g(_subject("日常实习")) != "不限"
        assert g(_subject("日常实习")) != ""

    def test_job_subject_none_at_every_level(self):
        """三层嵌套每层都可能是 None，一层都不许崩。

        小鹏 431 条整个 `job_subject` 是 None，商汤有 7 条也是。
        """
        from jobagent.adapters.feishu import _grad_year_from_subject as g

        assert g({}) is None                                   # 键都没有
        assert g({"job_subject": None}) is None                 # 小鹏 / 商汤
        assert g({"job_subject": {}}) is None
        assert g({"job_subject": {"name": None}}) is None
        assert g({"job_subject": {"name": {}}}) is None
        assert g(_subject(None)) is None                        # zh_cn 是 None

    def test_xiaopeng_shape_yields_none_by_design(self, monkeypatch):
        """小鹏的项目名全是 None → 本通道 0 增量，这是预期不是遗漏。

        它的届别写在标题里（343 条带「27届」），通道二已经覆盖。
        """
        _, jobs = _fetch(monkeypatch, _serve([_body([
            _campus(**{"job_subject": None})
        ])]), portal="campus")
        assert jobs[0].grad_year is None

    def test_fetch_fills_grad_year_from_subject(self, monkeypatch):
        """**守采集路径**：新采的岗位当场就要带届别，不能只靠 refresh 补。

        这条是「做一半」的守门测试。只写了 `grad_year_from_raw()`、刷了存量、
        但没接进 `fetch()` 的话：跑 refresh 全绿、库里数字也对，
        **但下一次 sync 进来的新岗位届别又是 NULL**。而且 `grad_year` 不在指纹里，
        这些新岗位不会触发任何事件 —— 表现是「数字慢慢退回去」，几周后才发现。
        """
        _, jobs = _fetch(monkeypatch, _serve([_body([
            _campus(**_subject("2027届校园招聘")),
            _campus(post_id="2", **_subject("日常实习")),
        ])]), portal="campus")

        assert jobs[0].grad_year == "27", "fetch 没把届别写进去"
        assert jobs[1].grad_year is None, "不带届别的项目名不该被兜底"

    def test_grad_year_from_raw_is_class_accessible(self):
        """`grad_year_from_raw` 必须能从**类**上直取，不经实例。

        `ingest.refresh_grad_year()` 用
        `getattr(type(adapter), "grad_year_from_raw", None)` 取它。
        写成实例方法取不到 → 被当成「这个源不支持刷新」抛 `RefreshUnsupported`
        → **静默跳过，不报错**。这条守的是那个失败模式。
        """
        assert getattr(FeishuAdapter, "grad_year_from_raw", None) is not None
        assert FeishuAdapter.grad_year_from_raw(
            _subject("2027届校园招聘")
        ) == "27"

    def test_raw_and_fetch_agree(self, monkeypatch):
        """两条入口必须同源。各写一遍推导 = 换季时静默分裂：
        新抓的一个届别、刷新过的另一个届别，而两边都说自己是对的。
        """
        row = _campus(**_subject("2027届校园招聘-技术提前批"))
        _, jobs = _fetch(monkeypatch, _serve([_body([row])]), portal="campus")

        assert jobs[0].grad_year == FeishuAdapter.grad_year_from_raw(row)
