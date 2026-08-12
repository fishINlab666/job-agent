"""巡检 `apply_url` 还活着。方案见 `docs/plans/013-apply_url健康巡检.md`。

为什么有这个模块：2026-08-10 发现库里 8594 条飞书 `apply_url` 全是死链，
少了 `/detail` 后缀，从采集第一天就坏，三个月没人发现。它坏得很安静 ——
HTTP 回 200、body 有 200KB，只有**渲染出来**才是「您正在寻找的页面不存在」。

这里分两层，分工是刻意的：

- **形状层**（`EXPECTED_SHAPE` / `normalize_url` / `check_shapes`）不联网，
  跑在 pytest 里，提交时就能红。它钉的是「链接拼成什么样」。
- **判据层**（`classify`）只吃一个正文字符串，不碰网络。真开浏览器那部分在
  `probe.py` / CLI 里，这样判据本身可以进 CI，不会因为源站抖动假红。
"""
from __future__ import annotations

import re
import time
from collections import Counter

#: 每个源的 `apply_url` 归一化后必须长这样。**是字面量，不是「形状只有一种」。**
#:
#: 这个区别在事故当天被验过：事故期的库里每个源也只有 1 种形状，
#: 只不过那一种是 `/campus/position/<ID>`（少 `/detail`）。
#: 所以 `len(shapes) == 1` 这种不变量在它唯一要防的那次事故里是**绿的**。
#: 必须钉字面量，才能把「全库一致地坏掉」这件事分出来。
EXPECTED_SHAPE: dict[str, str] = {
    "feishu:bytedance:campus": "https://bytedance.jobs.feishu.cn/campus/position/<ID>/detail",
    "feishu:nio:campus": "https://nio.jobs.feishu.cn/campus/position/<ID>/detail",
    # 自定义域名，不是 `sensetime.jobs.feishu.cn`。飞书租户可以绑自己的域，
    # 所以源名里的 tenant 推不出主机名 —— 这一行必须照库里的实际值写。
    "feishu:sensetime:edu": "https://hr-jobs.sensetime.com/edu/position/<ID>/detail",
    "feishu:xiaopeng:campus": "https://xiaopeng.jobs.feishu.cn/campus/position/<ID>/detail",
    "tencent_join": "https://join.qq.com/post.html?pid=<ID>",
}

#: query 参数里的 id 值（`?pid=-6`、`?pid=104437`）。
#: **必须认负号** —— 腾讯源站自己就有 `pid=-2`~`-6` 五条占位卡片。
#: 不认负号的写法（`[0-9]{4,}`）会让每条负 pid 自成一形状，腾讯报 6 种。
_QUERY_ID = re.compile(r"(?<==)-?\d+")

#: 路径里的整段数字（`/position/7123.../detail` 里的那段）。
#: 用 `(?=/|$)` 而不是 `\d+` 裸匹配：否则 `/campus/` 这种纯字母段不会动，
#: 但 `/edu2/` 这类含数字的门户名会被吃掉半截，归一结果就不再能指认门户。
_PATH_ID = re.compile(r"/-?\d+(?=/|$)")


def normalize_url(url: str) -> str:
    """把 URL 里的岗位 id 换成 `<ID>`，留下形状。

    只换 id，不换门户段 —— `/campus/` 和 `/edu/` 的区别是要保住的，
    那是「这批岗位从哪个门户采的」，混了就没法指认是哪个源坏了。
    """
    return _QUERY_ID.sub("<ID>", _PATH_ID.sub("/<ID>", url))


#: 判定的取值域。**白名单，不是「不是 healthy 就算死」。**
#:
#: 库里的源是 f-string 拼出来的（`feishu:<tenant>:<portal>`），数不出全集；
#: 判据是字符串级的，会随源站改版失效。所以「判不出来」必须是一个显式取值：
#: 并进 `dead` 则慢租户每轮假红，并进 `healthy` 则判据失效那天巡检安静地全绿 ——
#: 后者正是 2026-08-10 那次事故的形状。
VERDICTS = ("healthy", "broken_route", "gone", "unknown", "error")

#: 判成「我们的 bug」的那一类。只有这一类值得报警。
OUR_BUG = ("broken_route",)

#: 正文里认的标记。**顺序即优先级**，见 `classify`。
BROKEN_ROUTE_MARK = "您正在寻找的页面不存在"
CLOSED_MARK = "该职位已下线"
GONE_MARK = "undefined"


def shapes_by_source(conn) -> dict[str, Counter[str]]:
    """按源统计开放岗位 `apply_url` 的归一化形状。

    `apply_url` 为空**抛**而不是跳过：今天真库里是 0 条空值，
    真出现了说明采集侧漏了拼装 —— 那正是这个巡检要抓的事故，
    静默跳过等于把它藏起来（方案 §4）。
    """
    out: dict[str, Counter[str]] = {}
    for row in conn.execute(
        "SELECT source_key, external_id, apply_url FROM jobs WHERE closed_at IS NULL"
    ):
        url = row["apply_url"]
        if not url:
            raise AssertionError(
                f"{row['source_key']} 的 {row['external_id']} 没有 apply_url。"
                f"空值不是「这条跳过」，是采集侧漏了拼装"
            )
        out.setdefault(row["source_key"], Counter())[normalize_url(url)] += 1
    return out


def check_shapes(conn) -> list[str]:
    """比对真库形状与 `EXPECTED_SHAPE`，返回人能读的问题列表（空 = 全对）。

    两类问题都算失败：

    - **形状不等于登记的字面量** —— 8594 条死链那次事故就是这一类。
    - **源没登记** —— 源名是 f-string 拼出来的（`feishu:<tenant>:<portal>`，
      `adapters/feishu.py`），数不出全集，所以只能白名单。
      漏登记的方向必须是「报错」而不是「不检查」。
    """
    problems = []
    actual = shapes_by_source(conn)
    for source, shapes in sorted(actual.items()):
        if source not in EXPECTED_SHAPE:
            problems.append(
                f"{source}：库里有 {sum(shapes.values())} 条，但 EXPECTED_SHAPE 没登记它。"
                f"新源要显式登记，否则它的链接形状没人看着"
            )
            continue
        want = EXPECTED_SHAPE[source]
        for shape, n in shapes.most_common():
            if shape != want:
                problems.append(
                    f"{source}：{n} 条的形状是 {shape}，登记的是 {want}。"
                    f"源站改版了还是我们拼错了？别逐条改库，先看适配器"
                )
    return problems


def classify(body_text: str) -> str:
    """按渲染后的正文判死活。返回 `VERDICTS` 里的一个。

    **判据必须是渲染后的正文，不能是 HTTP 状态码。** 死链回的也是 200。

    判定顺序是固定的，先判最明确的：

        1. 「您正在寻找的页面不存在」 → broken_route   我们的 bug
        2. 「该职位已下线」           → gone           源站业务事件
        3. 「undefined」              → gone           形状对但 id 不存在
        4. 正文非空且不含以上          → healthy
        5. 正文空                     → unknown

    第 1 条必须在第 3 条前面：坏路由页正文也可能带 `undefined`，
    顺序颠倒就把我们自己的 bug 归成了源站的业务事件 —— 那条不会有人去修。

    三个不用的判据，都是被实测打死的（方案 §6）：

    - **不看 title。** 上一版打算按 title 的「岗位名段为空」判死。
      实测 `- 蔚来校招` 前面**没有空格**，` - ` 不是它的子串，
      `split(" - ")[0]` 恒等于原串 —— 这条判据是 100% 空操作，
      写下去会得到一个永远绿的巡检。
    - **不拿「职位描述」当健康标记。** 假 id 页也渲染这几个**标签**（实测 4/4），
      只是标签底下没内容。拿它判健康 = 拿「页面框架加载出来了」判「岗位存在」。
    - **不看正文长度。** 分得开（正常 294–1299 字 vs 假 id 46–127 字），
      但阈值定在哪都是编的，xiaopeng 正常页只有 294 字。
      这种判据坏起来是渐进的，没有报警点。
    """
    if BROKEN_ROUTE_MARK in body_text:
        return "broken_route"
    if CLOSED_MARK in body_text:
        return "gone"
    if GONE_MARK in body_text:
        return "gone"
    if not body_text.strip():
        return "unknown"
    return "healthy"


#: 轮询预算。正常页最慢实测 15.9s 才首现标记（nio），坏路由 1.4s。
#: **固定等待是竞态**：上一版方案的 3.5s 会把 8 条正常页里的 6 条判成 unknown。
POLL_BUDGET_SEC = 20.0

#: 每轮读一次正文的间隔。
POLL_INTERVAL_SEC = 0.5

#: 能提前结束轮询的判定 —— **只有「坏」算数**。
#:
#: `healthy` 不在这里面，理由见 `probe_one`：它不是一个能被观测到的正标记，
#: 而是「等满预算也没等到坏标记」。把它当提前退出条件，等于拿加载中途当结论。
_EARLY_EXIT = ("broken_route", "gone")

#: 对照组用的假 id。飞书真 id 也是 19 位数字，所以形状是对的、岗位是不存在的。
#: 它必须判出 `gone` —— 判不出说明 `undefined` 这条判据已经失效（方案 §6）。
SENTINEL_ID = "9" * 19


def probe_one(page, url: str, budget: float = POLL_BUDGET_SEC) -> tuple[str, float]:
    """真打开一个 URL，轮询正文判死活。返回 `(判定, 耗时秒)`。

    **只有「坏」的判定能提前返回；判成 `healthy` 必须等满预算。** 这条不对称
    是实测逼出来的，不是保守。第一版一见 `healthy` 就返回，结果 4 条对照组
    （形状对、id 不存在）里 3 条被判成活的。把 nio 那条的时间线摊开就看明白了：

        2.2s  正文 0 字            → unknown
        6.2s  正文 20 字           → healthy   ← 第一版在这里就返回了
       11.3s  正文 49 字 undefined → gone      ← 真判据 5 秒后才到

    页面框架（导航栏、登录按钮）先渲染，`undefined` 后到。而 `healthy` 的定义是
    「正文非空且**没有**坏标记」—— 一个还没渲染完的页面天然满足它。
    拿「暂时还没看到坏标记」当「它是好的」，就是把加载中途当成结论。

    换句话说 `healthy` 不是一个能被观测到的正标记，它是「等满了也没等到坏的」。
    坏标记来得快（1.4–2.1s）、活的来得慢（2.7–15.9s），所以这个不对称也不贵：
    坏页照样秒返回，只有真活着的页面才会拖满预算。

    超时判 `healthy` 而不是 `unknown`：正文里有内容、等满了也没有任何坏标记，
    这就是「活着」的全部证据。反过来正文一直是空 → `unknown`，
    **判不出来不等于坏了。**
    """
    t0 = time.time()
    try:
        page.goto(url, wait_until="commit", timeout=45000)
    except Exception:
        return "error", time.time() - t0
    last = "unknown"
    while time.time() - t0 < budget:
        try:
            text = page.inner_text("body", timeout=5000)
        except Exception:
            # 导航中途读 body 会抛（元素还没有、或者整页超时），等下一轮
            time.sleep(POLL_INTERVAL_SEC)
            continue
        verdict = classify(text)
        if verdict in _EARLY_EXIT:
            return verdict, time.time() - t0
        last = verdict
        time.sleep(POLL_INTERVAL_SEC)
    return last, time.time() - t0


#: 腾讯判不出死活，别浪费 6.9s/条去打。
#:
#: 已复验到底：真 pid / 负 pid / 假 pid 三条渲染出**逐字节相同**的正文
#: （875 字，title `岗位投递 | 腾讯校招`，无 `职位描述`、无 `不存在`）。
#: `post.html?pid=` 是列表页，详情不登录不渲染。
#: 腾讯侧唯一被守着的是上面的 URL 形状不变量。
UNJUDGEABLE = ("tencent_join",)


def sample_urls(conn, source: str, n: int) -> list[tuple[str, str, str]]:
    """随机抽 n 条开放岗位，返回 `[(source_key, external_id, apply_url)]`。

    只抽 `closed_at IS NULL`：已知下架的岗位打开当然是死的，
    算进死链数是自己制造噪音。

    随机而不是 `LIMIT n`：固定取前 n 条的话，每轮巡检看的是同一批岗位，
    覆盖率永远停在 n / 总数，而且**坏在别处的批次永远抽不到**。
    """
    rows = conn.execute(
        "SELECT source_key, external_id, apply_url FROM jobs "
        "WHERE source_key = ? AND closed_at IS NULL AND apply_url IS NOT NULL "
        "ORDER BY RANDOM() LIMIT ?",
        (source, n),
    ).fetchall()
    return [(r["source_key"], r["external_id"], r["apply_url"]) for r in rows]


def sentinel_url(url: str, external_id: str) -> str:
    """把真 id 换成 `SENTINEL_ID`，造一条「形状对、岗位不存在」的对照 URL。"""
    return url.replace(external_id, SENTINEL_ID)
