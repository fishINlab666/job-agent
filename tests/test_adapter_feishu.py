"""飞书适配器测试。全部假 transport，不打网络。

钉的是 `docs/plans/002-飞书招聘采集.md` §3 那些实测结论，以及三类
「不许悄悄发生」的事：判不出族被兜底成 other、城市不过归一、
空结果被无条件放过。
"""
from __future__ import annotations

import httpx
import pytest

from jobagent.adapters.feishu import FeishuAdapter


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

    # 这四条是 nio 全量里真实判不出的标题，不是编的
    UNDECIDABLE = [
        "ONVO-乐道行销顾问-重庆",
        "门店店总-蔚来天津区域公司",
        "区域用户增长（活动方向）",
        "售后服务-增值服务（西安）",
    ]

    @pytest.mark.parametrize("title", UNDECIDABLE)
    def test_undecidable_title_stays_none(self, monkeypatch, title):
        _, jobs = _fetch(monkeypatch, _serve([_body([_post(title=title)])]))
        assert jobs[0].job_family is None, "判不出就该是 None，兜底成 other 会让用户按族筛不到"

    def test_source_category_does_not_decide_family(self, monkeypatch):
        """源站分类**存在也不参与判定** —— 这条是这次最要紧的反向用例。

        谁图省事写 `or raw_category` 或 `or "other"`，这条立刻红。
        """
        row = _post(title="ONVO-乐道行销顾问-重庆",
                    job_function={"name": "蔚来顾问", "id": "x"})
        _, jobs = _fetch(monkeypatch, _serve([_body([row])]))
        assert jobs[0].job_family is None
        assert jobs[0].raw_category == "蔚来顾问", "原文要留着，供以后扩 TITLE_RULES 看分布"

    def test_decidable_title_still_works(self, monkeypatch):
        _, jobs = _fetch(monkeypatch, _serve([_body([_post(title="后端开发工程师")])]))
        assert jobs[0].job_family == "tech"


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
        """不带门户的老源退到 `index`。**不是 `/position/<id>`** —— 那个是硬 404。

        2026-08-06 实测（nio/xiaopeng/bytedance 一致）：`/position/<id>` 回 404、
        body 9 字节；`/<portal>/position/<id>` 回 200。老源那批岗位在
        `/index/position/<id>` 下能开。见 `_position_url()` 的注释。
        """
        _, jobs = _fetch(monkeypatch, _serve([_body([_post("777")])]))
        assert jobs[0].apply_url == "https://nio.jobs.feishu.cn/index/position/777"
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
    """`apply_url` 必须带门户段。这是修 bug，不是加功能。

    2026-08-06 实测（nio / xiaopeng / bytedance 三个租户一致）：

        /position/<id>          → 404，body 只有 9 字节
        /<portal>/position/<id> → 200

    `apply_url` 的唯一用途就是「点开就是官网那一页」拿去人工核对，
    而核对库里 4810 条飞书岗位的链接在修之前全是 404。

    这里钉的是**构造形状**，不是归属：页面是 SPA，任何 id 都回同一个 200 外壳，
    所以「校招 id 配校招门户」这条至今没有独立验证（见 `_position_url()` 注释）。
    """

    def test_portal_in_path(self, monkeypatch):
        _, jobs = _fetch(monkeypatch, _serve([_body([_campus(post_id="42")])]),
                         portal="campus")
        assert jobs[0].apply_url == "https://nio.jobs.feishu.cn/campus/position/42"

    def test_bare_position_path_never_produced(self, monkeypatch):
        """反向用例：谁把门户段去掉图省事，这条立刻红。

        断言的是「不等于那个 404 形状」而不是「等于某个形状」—— 上一条已经钉了
        正形状，这条专门盯住回退。
        """
        for portal in ("campus", "edu", None):
            _, jobs = _fetch(monkeypatch, _serve([_body([_post("777")])]),
                             portal=portal)
            assert jobs[0].apply_url != "https://nio.jobs.feishu.cn/position/777"
            assert "/position/777" in jobs[0].apply_url
            # 门户段就在 /position/ 前面那一节，且不是空的（`//position/` 也是 404）
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
        assert jobs[0].apply_url == "https://hr-jobs.sensetime.com/edu/position/9"

    def test_request_goes_to_custom_host(self, monkeypatch):
        seen: list[httpx.Request] = []
        _fetch(monkeypatch, _serve([_body([_campus()])], record=seen),
               tenant="sensetime", portal="edu", host="hr-jobs.sensetime.com")
        assert seen[0].url.host == "hr-jobs.sensetime.com"
        assert seen[0].headers["Referer"] == "https://hr-jobs.sensetime.com/"

    def test_default_host_unchanged(self, monkeypatch):
        _, jobs = _fetch(monkeypatch, _serve([_body([_post("777")])]))
        assert jobs[0].apply_url == "https://nio.jobs.feishu.cn/index/position/777"

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
