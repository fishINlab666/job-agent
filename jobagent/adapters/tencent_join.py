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

# 届别推导：按站点自己的 projectId 分桶（已核实 2026-08-09）
#   应届桶 {1, 14}             → 26 届（站点当前入口年份）
#   实习桶 {4, 5, 12, 20}      → 不限（站点实习入口明说「不限毕业时间」）
#   projectId=2（应届实习）    → 26 届（本地例外：站点对它返回 null，我们按标签推断）
#   其余                       → None（站点未声明，我们不猜）
#
# 站点在 renderProjectMeta 里按 projectId 分派届别声明，这是站点自己的键。
# 007 用的是 recruitLabelName 字符串匹配，重建站点逻辑，但标签会错分项目：
# pid=12 的项目名「项目实习生」、标签「日常实习」，字符串匹配认不出它是另一个项目。
#
# 每个招聘季入口年份会变，换季时核对 Project_CampusSubtitle（见 plan 008）。
CURRENT_CAMPUS_YEAR = "26"


def _recruit_type(label: str) -> str | None:
    if "应届毕业生" in label:
        return "campus"
    if "实习" in label:
        return "intern"
    return None


def _parse_grad_year(project_id: int | None) -> str | None:
    """按 projectId 推导届别。用站点自己的分派键，不是重建的标签映射。

    站点语义（核实日期 2026-08-09，renderProjectMeta 函数体）：
    - 应届桶 {1, 14} → 当届（当前入口年份）
    - 实习桶 {4, 5, 12, 20} → "不限"（站点实习入口明说「不限毕业时间」）
    - projectId=2（应届实习）→ 当届（本地例外，见下方注释）
    - 其余 → None（站点未声明，我们不猜）

    返回值：
    - "26" / "27" 等具体届别
    - "不限" 表示知道不限毕业年份（parse_grad_years 会解析成 []，匹配时命中）
    - None 表示未知（需 --allow-missing 才能看到，带 ? 标记）
    """
    if project_id in {1, 14}:           # 应届桶
        return CURRENT_CAMPUS_YEAR
    if project_id in {4, 5, 12, 20}:    # 实习桶
        return "不限"
    if project_id == 2:                 # 应届实习（本地例外）
        # 站点 renderProjectMeta 对 pid=2 返回 null，但标签原文是「应届实习」，
        # 我们推断它跟应届生走。如果它其实是「在读即可」，93 条会漏报（选漏报
        # 方向：宁可让它对当届可见，也不放宽成不限）。
        return CURRENT_CAMPUS_YEAR
    return None


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

    @staticmethod
    def grad_year_from_raw(raw: dict) -> str | None:
        """从源站原文重算届别，和 fetch 走的是同一条规则（`_parse_grad_year`）。

        为什么要单独暴露这个入口：`grad_year` 不在指纹里（见 `ingest._fp`），
        换季改了 `CURRENT_CAMPUS_YEAR` 之后 `sync` 不会更新存量岗位 ——
        指纹没变就落到「只动 last_seen_at」那条分支。`refresh-grad-year`
        靠这个方法重算，输入取 `snapshots.raw_json`，**不联网**。

        必须和 fetch 同源。两处各写一遍推导，换季时只改一处就是静默分裂：
        新抓的岗位一个届别、刷新过的存量另一个届别，而两边都说自己是对的。

        实测 2026-08-09：805 个共有 id 上，用快照重算与实时 fetch 的结果零分歧。
        """
        return _parse_grad_year(raw.get("projectId"))

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
                # 先检查是否拿够，拿够了就不管 batch 是否为空
                if len(rows) >= total:
                    break
                # 还没拿够但遇到空批次 = 截断，必须抛异常
                if not batch:
                    raise RuntimeError(
                        f"tencent_join: page={page} 返回空批次，"
                        f"但 count={total} 只拿到 {len(rows)} 条，拒绝返回半截数据"
                    )
                rows.extend(batch)
                # 防止无限循环
                if page > 50:
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
            grad_year=_parse_grad_year(row.get("projectId")),
            apply_url=f"https://join.qq.com/post.html?pid={post_id}",
            apply_system="tencent_join",
            description=None,   # 详情要单独打 jobDetails 接口，MVP 先不拉
        )
