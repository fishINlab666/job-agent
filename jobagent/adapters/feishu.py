"""飞书招聘（`<tenant>.jobs.feishu.cn`，也支持自定义域名）适配器 —— 第一个多租户源。

接口是租户招聘页自己调的 XHR，免鉴权，POST JSON：
  POST https://<host>/api/v1/search/job/posts
       website-path: campus        ← 决定返回哪一批岗位，不带 = 第三个池
  {"keyword":"","limit":200,"offset":0}
  → {"code":0,"data":{"count":627,"job_post_list":[...]}}

跟腾讯那个适配器的关键区别：**这是一个类摊多家公司，还摊每家的多个门户**。
route_key 是 `feishu:<tenant>` 或 `feishu:<tenant>:<portal>`，
租户和门户都是实例参数不是类常量（`docs/ATS_RESEARCH.md` 结论二）。

已知怪癖，都是实测踩出来的（002 §3、`docs/plans/003-校招门户采集.md` §3/§4）：

1. **UA 是承重的。** 见下面 `UA` 那个常量的注释。简化它 = 整个源挂掉。
2. **`website-path` 请求头是选门户的唯一开关。** 同一个租户同一个接口，
   带 `campus` 是校招池、带 `index` 是社招池、不带是第三个池（三者两两互不
   包含，nio 上 627 / 2077 / 2249）。`portal_type` / `portal_entrance` /
   `website_id` 这些参数全是陪跑的，改了 count 不变 —— 上一轮把力气全花在
   猜路径和猜 body 参数上，一次都没试过换请求头。
3. **`code=-9000003` 是「这个租户没有这个门户」**，跟 `count=0` 长得像但含义
   相反：前者是配置错，后者是事实。见 `fetch()` 里那个分支。
4. **源站分类两个字段是按门户的，不是按租户，也不互斥**：`xiaopeng/398875`
   上 `job_category` 和 `job_function` 同时有值（各 335）。两个都读，
   但**只落 `raw_category` 原文，不参与族判定** —— 取值是门户自定义的，
   映射不过来（002 §3 写的「按租户二选一且互斥」已被 003 证伪）。
5. **`count=0` 是合法答案，不是故障**：luckin / horizon 是活租户、接口正常、
   当下就是没在招。所以有 `empty_is_authoritative` 这个标记，见它的注释。
6. **没有届别字段。** `job_post_info` 里有 `experience` / `required_degree`
   （学历整数码），都对不上届别。标题里有（xiaopeng 校招池 349/436 带「27届」），
   但抠标题是另一份方案，见 003 §8。
"""
from __future__ import annotations

import httpx

from ..normalize import family_from_title, grad_years_from_title, normalize_city
from .base import RawJob

# UA 门：这个串能过，换掉就是 HTTP 405 + 空 body。**不要简化它。**
#
# 实测（002 §3「UA 门」那节，每组都紧挨着跑一次好 UA 做对照）：
#   这个串                                  → 200
#   版本号改成 Chrome/999.0                  → 200
#   平台段换成 Windows / Linux / iPhone      → 405
#   平台段截短成 "Macintosh; Intel Mac OS X" → 405
#   "Mozilla/5.0" 裸串 / curl / 不带 UA      → 405
#
# 排掉了限流（间隔 20 秒重测同样，405 后立刻换好 UA 就是 200，连测四轮稳定）、
# 排掉了其他请求头（除 UA 外逐个删掉仍是 200）、排掉了简单子串规则
# （把 10_15_7 塞进垃圾串或拼在 Windows UA 尾巴上，全是 405）。
# **没**定位到它具体按哪一项判，也没拿真浏览器复核 —— 所以能立的结论只到
# 「这个串能过，那些不能」。正好和腾讯适配器的 UA 是同一个串。
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# offset 硬上限，防翻页死循环。真实租户最大 2265 条，两万条留足余量。
MAX_OFFSET = 20000


def _grad_year_from_subject(post: dict) -> str | None:
    """招聘项目名（`job_subject`）→ 届别。**第三条观测通道**（plan 011）。

    这个源的 `grad_year` 那一列确实不存在（4 租户 × 12 门户全量核实过），
    但届别一直躺在招聘项目名里，从采集第一天起就在 `snapshots.raw_json` 中：

        "2027届校园招聘"                1852 条（bytedance）
        "2027届校园招聘-技术提前批"      197 条（nio）
        "27届校园招聘"                    73 条（sensetime）

    2026-08-10 实测覆盖率：bytedance 2073/7368、nio 313/634、
    sensetime 73/161、xiaopeng **0/431**（小鹏的项目名全是 `None`，
    它的届别写在标题里，通道二已经覆盖 —— 对它本通道是 0 增量，这是预期不是遗漏）。

    **为什么复用 `grad_years_from_title()` 而不新写解析**：它要求必须出现「届」字，
    这道门刚好挡住不含届别的项目名 —— `ByteIntern`（2656）、`日常实习`（2438）、
    `营销暑期实习生招募`（233）、`Shine校园招聘计划`（40）全部返回 `None`。
    实习岗**不许兜底成「不限」**：腾讯那边实习是「不限」，但那是 projectId 分桶
    实测出来的（plan 009），这里没有等价证据，编一个「不限」等于把 5295 条实习岗
    洗成「任何届别都命中」。

    三层嵌套每层都可能是 `None`（小鹏是整个 `job_subject` 为 `None`，
    商汤有 7 条也是），所以逐层用 `or {}` 兜。

    **这条判据是项目名字符串级的，换季会失效。** 飞书的 `job_subject` 只给
    `id` + `name`，而 id 是各租户自己的雪花号、跨租户没有稳定语义，没法像腾讯
    `projectId` 那样分桶（plan 009）。所以只能认字符串，代价是换季要复核。
    """
    name = ((post.get("job_subject") or {}).get("name") or {}).get("zh_cn")
    years = grad_years_from_title(name)
    # 取第一个：`grad_year` 列是单值。实测 21 个真实取值全部解析成单元素，
    # 所以当前不会截断；写成显式取首而不是假装它一定单元素 ——
    # 下个招聘季出现「26/27届」时这里不能静默丢。
    return years[0] if years else None


def _recruit_type(post: dict) -> str | None:
    """`recruit_type` 那棵两层树 → 归一。**先看 parent，再看叶子。**

    上一版只看叶子，注释写「这个口子里没有校招」。那句话是在**社招池**里核实的
    （不带 `website-path` / 带 `index`），一换成校招门户就假了：校招池的叶子是
    `正式` / `实习`，而 `正式` 不含「实习」二字，会掉进结尾那句 `return "social"`
    —— **把校招岗位标成社招**。这比判不出更糟：判不出用户还能靠 `--loose` 看到，
    标错就静默进了另一个类，按 `--recruit-type campus` 筛永远筛不到。见 003 §4。

    实测的两棵树（003 §4，全量对比不是抽样）：
        社招(parent.id=1) → 全职101 / 劳务103 / 顾问 / 外包 / 实习301
        校招(parent.id=2) → 正式201 / 实习202

    `校招+实习 → intern` 而不是新造一个 `campus_intern`：`match.classify()` 的
    `rtypes` 和 `cli jobs --recruit-type` 现在只认三个值，加第四个要同时改两处，
    而收益只是把「校招实习」和「社招实习」分开 —— 用户画像里两者都要。
    """
    node = post.get("recruit_type") or {}
    parent = ((node.get("parent") or {}).get("name") or "").strip()
    leaf = (node.get("name") or "").strip()
    if not parent and not leaf:
        return None

    if "校招" in parent:
        # 校招池里 `正式` 才是「校招正式岗」，也就是用户要的那批。
        return "intern" if "实习" in leaf else "campus"
    if "社招" in parent:
        return "intern" if "实习" in leaf else "social"

    # parent 认不出：**不许兜底 social**。飞书今天只有这两棵树，冒出第三棵
    # 说明源站改了分类体系，那时候「静默按社招处理」会把一整批岗位标错。
    # 写 None = 判不出，用户按类型筛看不到，但 `--loose` 能看到，且填充率报告
    # 里 recruit_type 会掉下来，是个能被发现的信号。
    if not parent:
        # 只有叶子没有 parent：叶子名带「实习」还认得出，其余不猜。
        return "intern" if "实习" in leaf else None
    return None


def _raw_category(post: dict) -> str | None:
    """源站分类原文。两个字段都读，取到就行。

    原来这里写「两个字段互斥，哪个有值按租户不同」—— **按门户，且不互斥**：
    `xiaopeng/398875` 上两个同时有值（各 335）。所以「随便读一个就够」的理由
    是错的，代码不变但理由要对：读两个是因为不同门户填的字段不一样。

    **只留原文，不参与族判定。** 拿它兜底会有两个后果：门户自定义的取值
    （`蔚来顾问`/`用户与服务`）映射不到我们的族，以及以后想知道
    「哪些词该加进 TITLE_RULES」时就没有干净样本了。
    """
    for key in ("job_category", "job_function"):
        node = post.get(key)
        if isinstance(node, dict):
            name = (node.get("name") or "").strip()
            if name:
                return name
    return None


def _city_names(post: dict) -> list[str]:
    return [
        (c.get("name") or "").strip()
        for c in (post.get("city_list") or [])
        if isinstance(c, dict) and (c.get("name") or "").strip()
    ]


# 门户不存在时接口回这个码。**必须抛，不许当空**（003 §5）。
# 它跟 count=0 长得像但含义相反：count=0 是「这家现在没岗位」（事实），
# 这个是「我打错了门户」（配置错）。后者要是被 empty_is_authoritative 放过，
# 表现就是**校招门户改个名，我们静默把 627 条全部判成关闭**，而 run 记 ok。
PORTAL_NOT_FOUND = -9000003


class FeishuAdapter:
    """一个租户 × 一个门户 = 一个实例。

    `system` 必须等于注册键，也必须在 `ats.VENDORS` 里。
    `portal` 决定 `website-path` 请求头，也就决定这个实例采哪一批岗位。
    """

    company = ""
    system = "feishu"

    # fetch() 返回空列表时，这个标记回答「空是不是一个可信的答案」。
    # 接口明确回了 code=0 + count=0 才置 True（真租户、当下没在招）；
    # 网络失败、非 200、code!=0、翻页中断一律不置 —— 那些情况 fetch() 直接抛。
    # ingest.sync() 用 getattr 读它，默认 False，所以腾讯适配器行为不变。
    empty_is_authoritative: bool = False

    def __init__(
        self,
        tenant: str,
        company: str = "",
        timeout: float = 20.0,
        page_size: int = 200,
        portal: str | None = None,
        host: str | None = None,
    ) -> None:
        tenant = (tenant or "").strip()
        if not tenant:
            # 多租户系统没有租户就没有源。空租户会拼出 https://.jobs.feishu.cn/，
            # 与其让它去打一个不存在的主机，不如在构造时就炸。
            raise ValueError("FeishuAdapter 必须给 tenant，它决定打哪个租户的接口")
        self.tenant = tenant
        self.company = company or tenant
        self.timeout = timeout
        self.page_size = page_size
        # 空串归一成 None：`portal=""` 要和「没给门户」表现一致，否则会发出
        # `website-path: ` 这么个空头。实测不带头是第三个池（nio 2249），
        # 带 index 是社招池（2077）—— 发空头等于赌接口怎么处理空值，不赌。
        self.portal = (portal or "").strip() or None
        # 自定义域名（hr-jobs.sensetime.com）。**只从 sources 行里取**，
        # 不从岗位链接里现推 —— 那等于放宽路由判据，会把无关站点判成飞书。
        self.host = (host or "").strip().lower() or None
        if self.host and self.host.endswith(".jobs.feishu.cn"):
            # 落在飞书自己域名上时，租户就在子域名里，能核对 —— 那就必须核对。
            # 不核对的失败方式是静默的：`sources.entry_url` 抄错一行
            # （复制上一家忘了改子域名），我们会拿着 tenant='蔚来那行的配置'
            # 去打小鹏的接口，然后把小鹏的岗位落在蔚来名下。宁可构造时炸。
            # 自定义域名抠不出租户，所以只在这个后缀下检查。
            in_host = self.host.split(".", 1)[0]
            if in_host != tenant:
                raise ValueError(
                    f"host={self.host} 里的租户是 {in_host!r}，与 tenant={tenant!r} 对不上。"
                    f"先核实是 sources.entry_url 抄错了还是 sources.tenant 过期了 —— "
                    f"赌一个方向就是把别家的岗位落在这家名下。"
                )
        self.skipped_no_id = 0

    # 腾讯那个是类常量，因为它只有一个租户。这里必须是属性 —— 键里带租户和门户。
    @property
    def source_key(self) -> str:
        """`feishu:<tenant>` 或 `feishu:<tenant>:<portal>`。

        **不带门户时仍是两段，不许补个默认门户凑成三段。** 库里那 4810 条
        （核对库）的键就是两段的，改了它们全变成孤儿：diff 找不到旧行，
        一轮 sync 下来「全部新增 + 全部关闭」。

        带门户是为了让关闭守卫的分母分开。合在一个键里，校招门户整个下线时
        消失比例是 627/2704=23%，低于 0.4 的守卫线，**627 条会被静默关闭**；
        分开之后同一件事是 627/627=100%，守卫必然触发，run 记 partial 等人看。
        见 003 §6。
        """
        if self.portal:
            return f"{self.system}:{self.tenant}:{self.portal}"
        return f"{self.system}:{self.tenant}"

    @property
    def base(self) -> str:
        return f"https://{self.host or f'{self.tenant}.jobs.feishu.cn'}"

    @property
    def entry_url(self) -> str:
        # 门户路径放进 entry_url：这一列是给人点开核对的，指到租户首页
        # 会让核对的人看到社招列表，然后以为采错了。
        return f"{self.base}/{self.portal}/" if self.portal else f"{self.base}/"

    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": UA,          # 承重，见模块顶部
            "Referer": f"{self.base}/",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
        }
        if self.portal:
            # 这一行就是全部开关。没有它 nio 返回 2249 条社招，有它返回 627 条校招。
            headers["website-path"] = self.portal
        return headers

    @staticmethod
    def grad_year_from_raw(raw: dict) -> str | None:
        """从源站原文重算届别，和 fetch 走的是同一条规则（`_grad_year_from_subject`）。

        为什么要单独暴露这个入口：`grad_year` 不在指纹里（见 `ingest._fp`），
        所以加了这条通道之后 `sync` 不会更新存量岗位 —— 指纹没变就落到
        「只动 last_seen_at」那条分支。`refresh-grad-year` 靠这个方法重算，
        输入取 `snapshots.raw_json`，**不联网**。

        **必须是类可取的**（`@staticmethod` 或 `@classmethod`）：
        `ingest.refresh_grad_year()` 用 `getattr(type(adapter), "grad_year_from_raw", None)`
        取它。写成实例方法取不到，会被当成「这个源不支持刷新」抛
        `RefreshUnsupported` —— 那是静默跳过，不是报错。

        必须和 fetch 同源。两处各写一遍推导，换季时只改一处就是静默分裂：
        新抓的岗位一个届别、刷新过的存量另一个届别，而两边都说自己是对的。
        """
        return _grad_year_from_subject(raw)

    def _position_url(self, post_id: str) -> str:
        """岗位详情页链接。形状是 `/<portal>/position/<id>/detail`。

        2026-08-10 实测（nio / xiaopeng / bytedance / sensetime 四个租户一致），
        形状从各租户列表页的 `<a href>` 上直接读出来的，不是猜的：

            /position/<id>                  → 渲染「页面不存在」
            /<portal>/position/<id>         → 渲染「页面不存在」  ← 曾经用的
            /<portal>/position/<id>/detail  → 渲染岗位正文        ← 现在用的

        **`/detail` 是 2026-08-10 补上的，补之前库里 8594 条飞书链接全是死的。**
        当初为什么漏：这些页面是客户端渲染的 SPA，**404 发生在渲染层，HTTP 照样
        200 而且 body 有 200KB**（实测 nio 209298 字节，正文却是「您正在寻找的
        页面不存在」）。当初的验证只看到 HTTP 状态码就收了。

        教训写在这儿免得下一个人再踩：**对 SPA，「200」不是页面存在的证据，
        得看渲染后的正文。** 判死活的判据是正文里有没有「页面不存在」。

        不带门户的老源退到 `index`。门户段仍然是必须的 ——
        `apply_url` 的唯一用途是「点开就是官网那一页」拿去人工核对。
        """
        return f"{self.base}/{self.portal or 'index'}/position/{post_id}/detail"

    def fetch(self) -> list[RawJob]:
        self.empty_is_authoritative = False
        self.skipped_no_id = 0
        rows: list[dict] = []
        total: int | None = None
        offset = 0

        with httpx.Client(timeout=self.timeout, headers=self._headers()) as client:
            while True:
                resp = client.post(
                    f"{self.base}/api/v1/search/job/posts",
                    json={"keyword": "", "limit": self.page_size, "offset": offset},
                )
                # 假租户返回 400 + 非 JSON。raise_for_status 先把它变成异常，
                # 不然下面 .json() 抛的是 JSONDecodeError，看不出是「这个租户不存在」。
                resp.raise_for_status()
                try:
                    body = resp.json()
                except ValueError as exc:
                    raise RuntimeError(
                        f"{self.source_key}: 响应不是 JSON（HTTP {resp.status_code}），"
                        f"疑似租户不存在或接口变更"
                    ) from exc

                code = body.get("code")
                if code == PORTAL_NOT_FOUND:
                    # 单独一条分支只为了把错误消息说清楚：这不是「上游挂了」，
                    # 是「这个租户没有这个门户」，处理动作是改配置不是重试。
                    # 注意它落在 code != 0 的抛出路径上，**不置**
                    # empty_is_authoritative —— 详见 PORTAL_NOT_FOUND 的注释。
                    raise RuntimeError(
                        f"{self.source_key}: 门户 {self.portal!r} 在 {self.base} 上不存在"
                        f"（code={code}）。这是配置错，不是「当下没岗位」——"
                        f"要么门户改名了，要么这家压根没这个门户。"
                        f"用 docs/kb/company-portals.md 里的命令重新确认门户路径。"
                    )
                if code != 0:
                    raise RuntimeError(
                        f"{self.source_key}: 接口返回 code={code!r} "
                        f"msg={body.get('msg')!r}"
                    )

                data = body.get("data") or {}
                if total is None:
                    total = int(data.get("count") or 0)
                    if total == 0:
                        # 真租户 + 当下没岗位。这是事实，不是故障 —— 立标记，
                        # ingest 那侧凭它决定不抛。
                        self.empty_is_authoritative = True
                        return []

                batch = data.get("job_post_list") or []
                if not batch:
                    # count 说还有，却给了空批次 = 半残返回。宁可整轮失败，
                    # 也不能静默截断：截断后 diff 会把没拿到的那批判成已关闭。
                    raise RuntimeError(
                        f"{self.source_key}: offset={offset} 返回空批次，"
                        f"但 count={total} 只拿到 {len(rows)} 条，拒绝返回半截数据"
                    )
                rows.extend(batch)
                offset += self.page_size
                if len(rows) >= total or offset > MAX_OFFSET:
                    break

        jobs = []
        for row in rows:
            job = self._to_raw_job(row)
            if job is None:
                self.skipped_no_id += 1
                continue
            jobs.append(job)
        return jobs

    def _to_raw_job(self, row: dict) -> RawJob | None:
        # 没 id 就跳过并计数。**不许 fallback 到 title** —— title 会重复，
        # 撞 UNIQUE(source_key, external_id)，一条脏数据会挡掉后面的整批。
        post_id = str(row.get("id") or "").strip()
        if not post_id:
            return None

        title = (row.get("title") or "").strip()
        raw_names = _city_names(row)

        desc_parts = [
            (row.get("description") or "").strip(),
            (row.get("requirement") or "").strip(),
        ]
        description = "\n\n".join(p for p in desc_parts if p) or None

        return RawJob(
            external_id=post_id,
            title=title,
            raw_json=row,
            # 判不出就是 None，**不兜底成 "other"**。那不是「其他族」，
            # 那是「没判出来」—— 混进 other 之后用户按族筛永远看不到这批，
            # 而 other 里真正的职能岗和没判出来的也分不开了。见 002 §4。
            job_family=family_from_title(title),
            raw_category=_raw_category(row),
            # 必须过归一：xiaopeng 上有「中国香港」，不归一就和「香港」分成两个城市。
            cities=[c for c in (normalize_city(n) for n in raw_names) if c],
            raw_location="/".join(raw_names) or None,
            country="中国",
            department=None,        # 接口只给 department_id，没有名字，不编
            recruit_type=_recruit_type(row),
            # 结构化 grad_year 那一列确实不存在（全量核实过），但届别写在招聘项目名
            # 里 —— 这是通道三，见 _grad_year_from_subject（plan 011）。
            # 这里必须和 grad_year_from_raw() 同源，否则新抓的和刷新的会分裂。
            grad_year=_grad_year_from_subject(row),
            apply_url=self._position_url(post_id),
            apply_system=self.system,
            description=description,
        )
