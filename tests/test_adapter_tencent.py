"""腾讯适配器测试。全部假 transport，不打网络。

钉的是 `docs/plans/006-高优bug修复.md` §4 问题 3：recruit_type 认不出时
返回 None，不兜底成 campus。飞书已经有同样的回归测试守着同样的规矩
（`test_adapter_feishu.py:410`），这次是把腾讯对齐过去。
"""
from __future__ import annotations

import httpx
import pytest

from jobagent.adapters.tencent_join import TencentJoinAdapter


# 必须在打补丁之前把真类抓住。工厂里直接写 httpx.Client 会调到被 patch 的
# 那个符号，也就是它自己 —— RecursionError，而不是一个看得懂的失败。
_REAL_CLIENT = httpx.Client


def _mock_client(handler):
    """把 TencentJoinAdapter 里的 httpx.Client 换成走 MockTransport 的。

    适配器在 fetch() 内部自己建 client（跟飞书那个一样），所以只能从
    模块符号这一层拦。返回一个 factory，签名要吃掉 timeout/headers。
    """

    def factory(*_args, **kwargs):
        kwargs.pop("transport", None)
        return _REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    return factory


# 抛什么都算过的话，连 RecursionError 都能让「该抛」的用例变绿 ——
# 第一版就是这么假绿的。只认这两类：适配器自己抛的和 HTTP 层抛的。
EXPECTED_ERRORS = (RuntimeError, httpx.HTTPStatusError)


def _position(post_id: str = "1", title: str = "后端开发工程师", **over) -> dict:
    """造一条岗位记录，可以用 **over 覆盖任何字段。"""
    row = {
        "postId": post_id,
        "positionTitle": title,
        "workCities": "深圳",
        "categoryName": "技术",
        "positionFamily": 40001,
        "projectId": 1,  # 默认应届桶
        "recruitLabelName": "应届毕业生",
        "bgs": "腾讯云与智慧产业事业群",
    }
    row.update(over)
    return row


def _body(positions: list[dict], count: int | None = None) -> dict:
    """造一个完整的响应体，searchPosition 接口的形状。"""
    return {
        "status": 0,
        "data": {
            "count": count if count is not None else len(positions),
            "positionList": positions,
        },
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


def _fetch(monkeypatch, handler) -> tuple:
    ad = TencentJoinAdapter()
    monkeypatch.setattr("jobagent.adapters.tencent_join.httpx.Client", _mock_client(handler))
    return ad, ad.fetch()


class TestRecruitType:
    """recruit_type 和 grad_year：认不出的标签/projectId 写 None，不兜底。

    飞书那边 `test_adapter_feishu.py:410` 的 docstring 原话：
    > 认不出的 parent 写 None。谁加 `or "social"` 兜底，这条红。
    > ……那时候「静默按社招处理」会把一整批岗位标错，而 None 是能被发现的信号。

    腾讯原来写的 `return "campus"` 就是那条 docstring 点名的兜底，
    只是没人给腾讯写这条测试。现在补齐。
    """

    def test_known_project_ids_still_work(self, monkeypatch):
        """已知的 projectId 正确识别届别。

        应届桶（pid=1, 14）→ 26（当前入口年份）
        实习桶（pid=4, 5, 12, 20）→ "不限"（在读即可）
        """
        campus_pos = _position("1", projectId=1, recruitLabelName="应届毕业生")
        intern_pos = _position("2", projectId=20, recruitLabelName="实习生 青云计划")
        _, jobs = _fetch(monkeypatch, _serve([_body([campus_pos, intern_pos])]))
        assert len(jobs) == 2
        assert jobs[0].recruit_type == "campus"
        assert jobs[0].grad_year == "26"
        assert jobs[1].recruit_type == "intern"
        assert jobs[1].grad_year == "不限"

    def test_unknown_label_returns_none(self, monkeypatch):
        """认不出的标签不兜底成 campus。谁加 `return "campus"` 兜底，这条红。

        源站今天只有应届/实习两类。冒出第三类说明改了分类体系，
        那时候「静默按应届处理」会把一整批岗位标错，而 None 是能被发现的信号
        （填充率报告里 recruit_type 会掉下来）。
        """
        row = _position(projectId=1, recruitLabelName="外星人招聘")
        _, jobs = _fetch(monkeypatch, _serve([_body([row])]))
        assert jobs[0].recruit_type is None

    def test_unknown_project_id_returns_none(self, monkeypatch):
        """认不出的 projectId 不兜底。谁加 `return "26"` 兜底，这条红。

        站点今天有 6 个 projectId（1, 2, 4, 5, 14, 20），pid=2 是本地例外。
        冒出第七个说明站点加了新项目，那时候静默兜底会把一整批岗位标错，
        而 None 是能被发现的信号（填充率报告里 grad_year 会掉下来）。
        """
        row = _position(projectId=999, recruitLabelName="博士后")
        _, jobs = _fetch(monkeypatch, _serve([_body([row])]))
        assert jobs[0].recruit_type is None
        assert jobs[0].grad_year is None

    def test_pid_2_is_local_exception_returns_campus_year(self, monkeypatch):
        """应届实习（projectId=2）站点返回 null，我们按标签推断是应届。

        站点 renderProjectMeta 对 pid=2 返回 null（不在应届桶也不在实习桶），
        但标签原文是「应届实习」，我们推断它跟应届生走。这是唯一一处本地例外。
        如果它其实是「在读即可」，93 条会漏报（选漏报方向：宁可让它对当届可见，
        也不放宽成不限）。详见 009 §3。
        """
        row = _position(projectId=2, recruitLabelName="应届实习")
        _, jobs = _fetch(monkeypatch, _serve([_body([row])]))
        assert jobs[0].recruit_type == "intern"
        assert jobs[0].grad_year == "26"


class TestPagingTruncation:
    """分页遇空批次且还有剩余，必须抛异常。

    静默截断比整轮失败危险得多：40% 关闭守卫能兜住大截断，
    但 30% 的截断会穿过，把 200+ 条在招岗位误判成已关闭。
    """

    def test_truncated_paging_raises(self, monkeypatch):
        """count 说 300，第二页给空批次 → 抛，不许静默返回半截。"""
        page1 = _body([_position(str(i)) for i in range(200)], count=300)
        page2 = _body([], count=300)
        ad = TencentJoinAdapter()
        monkeypatch.setattr("jobagent.adapters.tencent_join.httpx.Client", _mock_client(_serve([page1, page2])))
        with pytest.raises(EXPECTED_ERRORS) as exc:
            ad.fetch()
        # 错误信息里要能看出是分页的事，以及拿到了多少条
        assert "200" in str(exc.value)
        assert "300" in str(exc.value)

    def test_paging_ends_cleanly_when_complete(self, monkeypatch):
        """拿够了才空批次 → 正常结束，不抛异常。

        这是反向用例：防止把条件写成「只要空批次就抛」。
        腾讯的 pageSize 上限很宽，服务端可能一次给全量，第二页自然是空的。

        场景：count=50，第一页给全部 50 条，第二页空批次 ——
        此时 len(rows)==total，应该正常结束不抛异常。
        """
        page1 = _body([_position(str(i)) for i in range(50)], count=50)
        page2 = _body([], count=50)
        _, jobs = _fetch(monkeypatch, _serve([page1, page2]))
        assert len(jobs) == 50
