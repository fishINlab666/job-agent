"""腾讯国内校招 / 实习（join.qq.com）适配器。

接口是从 post 页的 JS bundle 里挖出来的，无鉴权，POST JSON：
  POST https://join.qq.com/api/v1/position/searchPosition
  {"projectId":2,"pageIndex":1,"pageSize":200,"keyword":""}

已知怪癖：
1. projectId 参数被服务端忽略（传 1/2/14 返回完全一样的全量）。
   真正的分类维度是响应里的 positionFamily 和 recruitLabelName。
2. 一行 = 「岗位类型 × 招聘项目」，workCities 是空格分隔的多城市字符串，
   不是一行一个岗位。所以 external_id 用 postId，同名岗位在不同项目下是不同行。
3. 静态资源和接口都要求带 Referer，不带会返回空 body。
"""
from __future__ import annotations

import httpx

from ..normalize import family_from_title, split_cities
from .base import RawJob

API = "https://join.qq.com/api/v1/position/searchPosition"
REFERER = "https://join.qq.com/post.html?query=p_2"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# positionFamily → 归一族。只是兜底默认值，最终以标题关键词优先。
FAMILY_MAP = {
    2: "tech",
    3: "product",      # 但运营岗也在这个族里，靠标题关键词纠正
    4: "design",
    5: "marketing",
    6: "other",        # 职能：财经/人力/法律/行政，靠标题细分
    7: "tech",         # AI 大类，本质是技术
}

FAMILY_LABEL = {2: "技术", 3: "产品", 4: "设计", 5: "市场", 6: "职能", 7: "AI"}

# 届别推导来自站点自身的项目配置（V2Index bundle）：
#   应届生招聘  毕业时间 2025-01-01 ~ 2026-12-31  → 26 届
#   实习生招聘  毕业时间 2026-09-01 ~ 2027-12-31  → 27 届
# 每个招聘季这两个窗口都会变，换季时必须重新核对，别当常量信一年。
GRAD_WINDOW = {"campus": "26", "intern": "27"}


def _recruit_type(label: str) -> str:
    if "应届毕业生" in label:
        return "campus"
    if "实习" in label:
        return "intern"
    return "campus"


class TencentJoinAdapter:
    source_key = "tencent_join"
    company = "腾讯"
    # 这里原来写的是 "self_built"，那不是一个厂商 key，是一类厂商的形容词——
    # 按它去注册表里找永远找不到，而且所有自建公司会挤在同一个键上。
    # 自建系统的 system 就是它自己（ats.py 里 tencent_join.self_built=True）。
    system = "tencent_join"
    entry_url = "https://join.qq.com/post.html"

    def __init__(self, timeout: float = 20.0, page_size: int = 200) -> None:
        self.timeout = timeout
        self.page_size = page_size

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": UA,
            "Referer": REFERER,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
        }

    def fetch(self) -> list[RawJob]:
        rows: list[dict] = []
        with httpx.Client(timeout=self.timeout, headers=self._headers()) as client:
            page, total = 1, None
            while True:
                resp = client.post(
                    API,
                    json={
                        "projectId": 2,
                        "pageIndex": page,
                        "pageSize": self.page_size,
                        "keyword": "",
                    },
                )
                resp.raise_for_status()
                body = resp.json()
                if body.get("status") != 0:
                    raise RuntimeError(f"searchPosition 返回异常: {body.get('message')!r}")
                data = body.get("data") or {}
                batch = data.get("positionList") or []
                if total is None:
                    total = int(data.get("count") or 0)
                rows.extend(batch)
                # 服务端一次可能就给全量（pageSize 上限很宽），拿够或拿空就停
                if not batch or len(rows) >= total or page > 50:
                    break
                page += 1

        if not rows:
            raise RuntimeError("searchPosition 返回 0 条，疑似接口变更，拒绝当成空结果")
        return [self._to_raw_job(r) for r in rows]

    def _to_raw_job(self, row: dict) -> RawJob:
        title = (row.get("positionTitle") or "").strip()
        fam_id = row.get("positionFamily")
        label = (row.get("recruitLabelName") or "").strip()
        rtype = _recruit_type(label)

        # 标题关键词优先，源站族兜底：修正「产品运营」被归进 product 的问题
        family = family_from_title(title) or FAMILY_MAP.get(fam_id, "other")

        post_id = str(row.get("postId") or row.get("id") or "")
        return RawJob(
            external_id=post_id,
            title=title,
            raw_json=row,
            job_family=family,
            raw_category=FAMILY_LABEL.get(fam_id, str(fam_id)),
            cities=split_cities(row.get("workCities")),
            raw_location=row.get("workCities"),
            country="中国",
            department=(row.get("bgs") or "").strip() or None,
            recruit_type=rtype,
            grad_year=GRAD_WINDOW.get(rtype),
            apply_url=f"https://join.qq.com/post.html?pid={post_id}",
            apply_system="tencent_join",
            description=None,   # 详情要单独打 jobDetails 接口，MVP 先不拉
        )
