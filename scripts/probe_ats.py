"""探测真实招聘页面的技术特征，回答一个产品问题：采集按公司做，还是按 ATS 做。

评审提出「大厂通常自建，中厂大多用北森/moka 之类的招聘系统，用 ATS 会更省事」，
但不确定，要求实测一批公司的页面特征之后再决定。这个脚本就是那次实测的工具。

它做三件事，都不带判断：
  1. 跟着重定向把 URL 走到底，记下完整主机链。很多公司的「校园招聘」入口会跳到
     ATS 自己的域名上，这一跳本身就是最强的证据。
  2. 把页面里的第三方脚本域名、meta generator、已知厂商标记抠出来。
  3. 跑一遍 ats.detect()，判定和证据一起记下来。

**不猜**：取不到就写 null，网络不通就写 error 并在汇总里单独说明。
这份产出会被用来改 ats.py 里的 dom_hints——如果脚本自己编数据，
那张注册表就建在编的数据上了，比留空危险得多。

用法：
    python scripts/probe_ats.py                     # 内置种子列表
    python scripts/probe_ats.py --urls-file my.txt  # 一行一个「公司,URL」或裸 URL
    python scripts/probe_ats.py --render            # 再用浏览器跑一遍，抓 XHR 域名
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from jobagent import ats  # noqa: E402

console = Console()
app = typer.Typer(add_completion=False)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
# 种子列表：验证「大厂自建、中厂买 SaaS」这个假设需要两头都有样本。
# 只放能确定存在的入口域名。中厂样本天生难找——它们的招聘页不做 SEO，
# 搜索引擎里搜不到（这也正是需要实测而不是靠搜的原因），所以中厂那部分
# 靠 --urls-file 自己补。探不通的会如实记成 error，不会被当成「自建」。
SEED: tuple[tuple[str, str], ...] = (
    # 自建阵营（预期 self_built）
    ("腾讯", "https://join.qq.com/"),
    ("腾讯社招", "https://careers.tencent.com/"),
    ("阿里巴巴", "https://talent.alibaba.com/"),
    ("字节跳动", "https://jobs.bytedance.com/campus"),
    ("美团", "https://zhaopin.meituan.com/"),
    ("百度", "https://talent.baidu.com/"),
    ("京东", "https://campus.jd.com/"),
    ("网易", "https://campus.163.com/"),
    ("小米", "https://hr.xiaomi.com/"),
    ("华为", "https://career.huawei.com/reccampportal/portal5/index.html"),
    # SaaS 厂商自己的站（确认域名和标记，不是租户页）
    ("Moka", "https://www.mokahr.com/"),
    ("北森", "https://www.beisen.com/"),
    ("大易", "https://www.dayee.com/"),
    ("Workday", "https://www.workday.com/"),
    ("Greenhouse", "https://www.greenhouse.io/"),
    ("Lever", "https://www.lever.co/"),
)


@dataclass
class Probe:
    """一次探测的原始记录。字段全部可空——探不到就是探不到。"""

    company: str
    url: str
    ok: bool = False       # 拿到了 HTTP 响应（没有网络异常）
    alive: bool = False    # 而且是 2xx + 有正文，即这个入口真的存在
    # 连接池饥饿，压根没轮到它发请求。跟「探不通」必须分开：
    # 探不通是关于那个站的结论，饥饿是关于本地调度的，把它算进 error
    # 会读成「这家连不上 → 大概自建」，那是拿本机的排队情况在给公司下结论。
    starved: bool = False
    # 200 + 有正文，但页面自己说「没这个页面」/「已关停」。alive 判不出来：
    # zhiye.com 的通配子域名返回 200、1228 字节、标题 Not Found，比 500 字节那道
    # 门槛高，会被算成一个活着的入口。这是同一类口径错的第三次（前两次是把厂商官网
    # 算成用户、把 404 算成命中），所以单独立一个字段，不再靠字节数猜。
    tombstone: bool = False
    # 站点明确拒绝了我这个客户端（412/406/403/429 那一档），不是「入口不存在」。
    # 跟 starved 同一个道理：这是关于我的客户端的事实，不是关于那家公司的事实。
    # 不摘出去就会混进 dead，被读成「这家没有招聘入口 → 大概自建」。
    # 只能兜住「服务端回了个拒绝状态码」这一种；TLS 握手层就被挡掉的（实测 B站）
    # 连状态码都没有，脚本分不出来，得换客户端复测，见 docs 里「WAF」那节。
    blocked: bool = False
    error: str | None = None
    status: int | None = None
    final_url: str | None = None
    host_chain: list[str] = field(default_factory=list)
    redirected_offsite: bool = False      # 最后落在了别的注册域上
    system: str = ats.UNKNOWN
    tenant: str | None = None
    confidence: str = ats.UNKNOWN
    evidence: list[str] = field(default_factory=list)
    third_party_hosts: list[str] = field(default_factory=list)
    vendor_markers: list[str] = field(default_factory=list)
    generator: str | None = None
    page_title: str | None = None   # 页面自称是谁，跟 company 字段可能不是一家
    needs_js: bool = False
    html_bytes: int = 0
    xhr_hosts: list[str] = field(default_factory=list)   # 只有 --render 才有
    vendor_site: bool = False    # 这是厂商自己的官网，不是某家公司的租户页

    @property
    def route_key_str(self) -> str:
        return f"{self.system}:{self.tenant}" if self.tenant else self.system
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_SRC = re.compile(r"""<(?:script|link|iframe)[^>]+(?:src|href)\s*=\s*["']([^"']+)""", re.I)
_GENERATOR = re.compile(r"""<meta[^>]+name\s*=\s*["']generator["'][^>]+content\s*=\s*["']([^"']*)""", re.I)
_SPA_ROOT = re.compile(r"""<div[^>]+id\s*=\s*["'](?:root|app|__next|__nuxt)["']""", re.I)
_TEXT_TAGS = re.compile(r"<(?:script|style)[^>]*>.*?</(?:script|style)>", re.I | re.S)
_TAGS = re.compile(r"<[^>]+>")

# 两段式后缀：这些下面还有一层才是注册域（gov.cn 之外主要是 com.cn 这类）。
_TWO_LEVEL = frozenset({"com.cn", "net.cn", "org.cn", "co.uk", "com.hk", "co.jp"})


def registrable(host: str) -> str:
    """取可注册域，用来判断「跳到站外了没有」。

    talent.alibaba.com 和 alibaba.com 是同一家，jobs.bytedance.com 跳到
    mokahr.com 就不是了——后者才是「买了 SaaS」的铁证。
    这是个近似实现（没引 publicsuffix 依赖），对 .com.cn 这类做了特例。
    """
    parts = (host or "").lower().split(".")
    if len(parts) < 3:
        return host or ""
    if ".".join(parts[-2:]) in _TWO_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def third_party_hosts(html: str, base_host: str) -> list[str]:
    """页面加载的站外资源域名，按出现次数排。

    一个自建系统的页面只会引自己的静态域和 CDN；如果引了 mokahr 的 JS，
    那这页基本就是 Moka 渲染的。
    """
    base = registrable(base_host)
    seen: Counter[str] = Counter()
    for raw in _SRC.findall(html):
        u = raw.strip()
        if u.startswith("//"):
            u = "https:" + u
        if not u.lower().startswith("http"):
            continue          # 相对路径是自家的，不是线索
        h = ats.host_of(u)
        if h and registrable(h) != base:
            seen[h] += 1
    return [h for h, _ in seen.most_common(12)]


def vendor_markers(html: str) -> list[str]:
    """页面里出现的已知厂商特征，域名和 DOM 标记都算。

    这是 ats.py 里 dom_hints 的校准数据源：现在那批 hints 是猜的，
    这里统计出真实出现的字符串之后才好收紧。
    """
    low = html.lower()
    found: list[str] = []
    for v in ats.VENDORS:
        for d in v.domains:
            if d in low:
                found.append(f"{v.key}:域名串 {d}")
        for hint in v.dom_hints:
            if hint.lower() in low:
                found.append(f"{v.key}:标记 {hint}")
    return found


def looks_js_rendered(html: str) -> bool:
    """判断岗位列表是不是要跑 JS 才出来。

    有 SPA 根节点、而且去掉标签后正文很少，就说明服务端没渲染内容。
    这个结论直接决定采集要不要上 Playwright（成本差一个量级）。
    """
    if not _SPA_ROOT.search(html):
        return False
    text = _TAGS.sub(" ", _TEXT_TAGS.sub(" ", html))
    return len(" ".join(text.split())) < 800
def is_vendor_site(system: str, host: str, tenant: str | None) -> bool:
    """这条命中的是厂商官网，还是某家公司的租户页。

    必须分开数。www.mokahr.com 命中 mokahr 只说明「Moka 的官网是 Moka 的」，
    不说明有公司在用它。把这类算进「N 家公司共用 M 个适配器」，那个数就是自己
    骗自己——第一版汇总就是这么报的：7 家 6 个适配器，其中 6 家是厂商官网。
    """
    v = ats.BY_KEY.get(system)
    if v is None or v.self_built or tenant:
        return False
    h = (host or "").lower()
    return any(h == d or h == f"www.{d}" for d in v.domains)


# 页面自称「不存在 / 已关停」的说法。命中就不算证据，哪怕 HTTP 是 200。
_TOMBSTONE_TITLE = re.compile(
    r"已被删除|已删除|不存在|已关停|已关闭|已下线|已停止|暂未开放|not\s*found|404",
    re.I,
)


def looks_tombstone(title: str | None, final_url: str | None) -> bool:
    """这个页面是不是一块墓碑：HTTP 200，但内容说「没有这个页面」。

    两个信号都是实测见过的：
      标题——`当前网页已关停`（涂鸦那个 Moka 租户页）、
            `对不起，你访问的页面已被删除或不存在 - 有赞`（有赞自家的软 404）
      落地 URL——zhiye.com 的通配兜底跳到 `/404?errorpath=/`，
            标题只有干巴巴一个 `Not Found`，但 URL 里写得很清楚

    只挡「页面自己承认」的情况。不吭声的软 404 挡不住，所以这是用来**收窄证据口径**的，
    不是一个完备判据——判不出来的仍旧按活着记，然后靠人看。
    """
    if final_url and re.search(r"/404(?:[/?#]|$)", final_url):
        return True
    return bool(title and _TOMBSTONE_TITLE.search(title))


def probe_one(client: httpx.Client, company: str, url: str) -> Probe:
    """探一个入口。任何异常都收进 error 字段，不让一个探不通的站中断整轮。"""
    p = Probe(company=company, url=url)
    try:
        r = client.get(url)
    except httpx.PoolTimeout as exc:
        # 没排到连接，这一个根本没发出去。单独标出来，交给补探那一轮。
        p.starved = True
        p.error = f"{type(exc).__name__}: 连接池饥饿，未实际探测"
        return p
    except Exception as exc:                       # noqa: BLE001 - 网络异常种类太多
        p.error = f"{type(exc).__name__}: {exc}"
        return p

    p.ok = True
    # 2xx 且有正文才算这个入口真的存在。
    # 只看「有没有异常」会把 404 当成证据：实测编了 11 个假租户名去访问
    # <slug>.jobs.feishu.cn，全部 404 + 0 字节，却被汇总成「12 家公司命中 feishu」。
    # 编出来的名字撞不出真租户，这条线只能靠公司自己的入口跳转拿到。
    p.alive = 200 <= r.status_code < 300 and len(r.text or "") > 500
    p.status = r.status_code
    # 明确的拒绝，跟 4xx「没这个页面」分开。412/406 对一个无条件 GET
    # （没带任何 precondition、Accept: */*）在语义上根本不该出现，回了就是有人
    # 在中间拦了；429 是限流；403 有可能是真的权限控制，但两种解释都不支持
    # 「这个入口不存在」这个结论，所以一起摘出去。判据是状态码，不是猜。
    p.blocked = r.status_code in (403, 406, 412, 429)
    p.final_url = str(r.url)
    # 主机链：每一跳的 host。跳到 ATS 域名上是最强证据，必须留痕。
    p.host_chain = [ats.host_of(str(h.url)) for h in r.history] + [ats.host_of(str(r.url))]
    p.redirected_offsite = registrable(p.host_chain[0]) != registrable(p.host_chain[-1])

    html = r.text or ""
    p.html_bytes = len(html)
    d = ats.detect(p.final_url, html)
    p.system, p.tenant, p.confidence, p.evidence = d.system, d.tenant, d.confidence, d.evidence
    p.third_party_hosts = third_party_hosts(html, p.host_chain[-1])
    p.vendor_markers = vendor_markers(html)
    if m := _GENERATOR.search(html):
        p.generator = m.group(1).strip() or None
    # 页面自称是谁，必须记下来单独看。实测猜的租户名会撞到别人家的真租户：
    # luckin.jobs.feishu.cn 是「加入狂浪俱乐部」、horizon.jobs.feishu.cn 是
    # 「加入汉森」，都不是我按 slug 猜的那家。租户活着 ≠ 归属对得上，
    # 只信输入里那列公司名，就会把张三的租户记成李四的。
    if m := _TITLE.search(html):
        p.page_title = " ".join(m.group(1).split())[:60] or None
    # 放在 page_title 之后：墓碑判定要读标题。
    p.tombstone = looks_tombstone(p.page_title, p.final_url)
    p.needs_js = looks_js_rendered(html)
    p.vendor_site = is_vendor_site(p.system, p.host_chain[-1], p.tenant)
    return p


def render_one(p: Probe) -> None:
    """用真浏览器再跑一遍，抓 XHR 域名。原地补 p.xhr_hosts。

    SPA 页面的 HTML 里什么都没有，岗位数据在 XHR 里。那些请求打到哪个域，
    才说明背后是谁的系统——静态 HTML 看不出来。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        p.xhr_hosts = ["__playwright_missing__"]
        return

    hosts: Counter[str] = Counter()
    base = registrable(p.host_chain[-1] if p.host_chain else ats.host_of(p.url))
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(user_agent=UA)
            page.on(
                "request",
                lambda req: hosts.update([ats.host_of(req.url)])
                if req.resource_type in ("xhr", "fetch")
                else None,
            )
            page.goto(p.url, wait_until="networkidle", timeout=45_000)
            html = page.content()
            browser.close()
    except Exception as exc:                       # noqa: BLE001
        p.xhr_hosts = [f"__error__: {type(exc).__name__}"]
        return

    p.xhr_hosts = [h for h, _ in hosts.most_common(15) if h]
    # 渲染后的 HTML 常常才露出厂商标记，合并进来（去重保序）。
    for mk in vendor_markers(html):
        if mk not in p.vendor_markers:
            p.vendor_markers.append(mk)
    for h in p.xhr_hosts:
        if registrable(h) != base and h not in p.third_party_hosts:
            p.third_party_hosts.append(h)
def load_targets(path: Path | None) -> list[tuple[str, str]]:
    """读目标列表。每行「公司,URL」或裸 URL，# 开头是注释。"""
    if path is None:
        return list(SEED)
    out: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "," in line:
            name, _, u = line.partition(",")
            out.append((name.strip(), u.strip()))
        else:
            out.append((ats.host_of(line) or line, line))
    return out


def summarize(probes: list[Probe]) -> None:
    """把原始记录汇成能拿去做决策的三个数：自建几家、SaaS 几家、几个适配器覆盖得完。"""
    t = Table(title="探测结果", show_lines=False)
    for col in ("公司", "落地域名", "判定", "租户", "置信度", "要JS", "站外资源"):
        t.add_column(col, overflow="fold")
    for p in probes:
        if not p.ok:
            t.add_row(p.company, f"[red]{(p.error or '')[:38]}[/red]", "-", "-", "-", "-", "-")
            continue
        if not p.alive:
            # 判定列故意写「入口不存在」而不是 p.system：这页是 404，
            # 域名却照样能匹配上厂商，摆出判定结果会看成一条命中。
            t.add_row(
                p.company,
                f"[dim]{p.host_chain[-1] if p.host_chain else '?'}[/dim]",
                f"[dim]入口不存在 {p.status}[/dim]", "-", "-", "-", "-",
            )
            continue
        land = p.host_chain[-1] if p.host_chain else "?"
        t.add_row(
            f"{p.company} [dim](厂商官网)[/dim]" if p.vendor_site else p.company,
            f"[yellow]{land}[/yellow]" if p.redirected_offsite else land,
            p.system if p.system != ats.UNKNOWN else "[dim]unknown[/dim]",
            p.tenant or "[dim]-[/dim]",
            p.confidence,
            "是" if p.needs_js else "",
            ", ".join(p.third_party_hosts[:2]) or "[dim]-[/dim]",
        )
    console.print(t)

    # 证据口径：活着**且不是墓碑**。alive 只管 HTTP 层（2xx + 有正文），
    # 墓碑是内容层的事实，两个分开存、在这里合起来用。
    ok = [p for p in probes if p.alive and not p.tombstone]
    tombs = [p for p in probes if p.alive and p.tombstone]
    # blocked 从 dead 里摘出去：412/403 有正文有状态码，不摘就被算成
    # 「4xx/空页 → 这家没有招聘入口」，而那其实是「这个站不接我的客户端」。
    dead = [p for p in probes if p.ok and not p.alive and not p.blocked]
    blocked = [p for p in probes if p.blocked]
    # 饥饿的从 failed 里摘出去。混在一起就是把「本机没排到连接」写成
    # 「这个站连不上」，跟当初把 404 算成命中是同一类口径错。
    starved = [p for p in probes if p.starved]
    failed = [p for p in probes if not p.ok and not p.starved]
    known = [p for p in ok if p.system != ats.UNKNOWN]
    # 厂商官网单独一堆：它们证明域名/标记认得出，但不能算「有公司在用」。
    ref = [p for p in known if p.vendor_site]
    third = [p for p in known if not p.vendor_site and not ats.BY_KEY[p.system].self_built]
    # 按置信度再切一刀。表里的规矩是「domain 才能路由，markup 只够立案」，
    # 汇总却把两档混在一起数，等于用一条自己都不敢路由的线索去报占比。
    # 实测代价：jobs.bytedance.com 因为页面上有 feishucdn.com 被算成
    # 「命中第三方 ATS」，而飞书是字节自己的产品，那是第一方依赖。
    saas = [p for p in third if p.confidence == ats.DOMAIN]
    leads = [p for p in third if p.confidence != ats.DOMAIN]
    built = [p for p in known if not p.vendor_site and ats.BY_KEY[p.system].self_built]
    unknown = [p for p in ok if p.system == ats.UNKNOWN]

    console.print()
    console.print(
        f"入口存在 [bold]{len(ok)}[/bold] / {len(probes)}"
        f"（{len(dead)} 个是 4xx/空页，{len(tombs)} 个是 200 的墓碑页，"
        f"{len(failed)} 个连不上）"
    )
    if blocked:
        # 单独一行，而且不进上面那个「连不上」。这一档是关于我的客户端的，
        # 混进去就会被读成关于那家公司的结论。
        console.print(
            f"  [yellow]另有 {len(blocked)} 个被站点拒了（不是入口不存在）：[/yellow]"
        )
        for p in blocked:
            console.print(f"    [yellow]{p.company:<10}[/yellow] HTTP {p.status} [dim]{p.url}[/dim]")
        console.print(
            "    [dim]412/406 对一个无条件 GET 不该出现，基本是 WAF。"
            "这些站大概率是活的，换个客户端（真浏览器）再探一遍才能定性。[/dim]"
        )
    if tombs:
        # 单独点名。这类页面 HTTP 200、有正文、看统计像个活入口，
        # 实际上写着「已关停」——不点出来就会被当成「这家在用某个 ATS」。
        console.print("[dim]墓碑页（200 但页面自称不存在/已关停，不计入证据）：[/dim]")
        for p in tombs:
            console.print(f"  [dim]· {p.company} → {p.page_title}[/dim]")
    if starved:
        # 补探之后还饥饿，说明这个数字不能用。宁可显眼地报「没探到」，
        # 也不要让它悄悄摊进「连不上」里，那会被读成「这些公司自建」。
        console.print(
            f"[yellow]⚠ {len(starved)} 个补探后仍未探到（连接池饥饿），"
            f"不计入上面任何一档，把 --concurrency 调小重跑：[/yellow]"
            + "、".join(p.company for p in starved)
        )
    console.print(
        f"  租户页命中第三方 ATS：{len(saas)}   自建：{len(built)}   "
        f"认不出：{len(unknown)}   厂商官网（对照组）：{len(ref)}"
    )
    if leads:
        # markup 级的单独报，而且不进上面那一行。表里写明了这一档不可路由，
        # 那它也不能进占比——不然就是拿一条自己都不敢照着跑的线索去下结论。
        console.print(
            f"  [yellow]另有 {len(leads)} 个只到 markup 级（页面上有厂商串，"
            f"但域名不是厂商的），只够立案，不计入上面的占比：[/yellow]"
        )
        for p in leads:
            console.print(
                f"    [yellow]{p.company:<10}[/yellow] 疑似 {p.system} "
                f"[dim]{'、'.join(p.vendor_markers[:2])}[/dim]"
            )
        console.print(
            "    [dim]自建系统引一个厂商的资源是常事（字节自建 ATS 接了北森的在线考试，"
            "页面又挂着自家的 feishucdn）。照着 markup 路由就会拿错厂商的逻辑去打这些页面。[/dim]"
        )
    if dead:
        console.print(
            f"  [dim]4xx/空页的不计入任何一类：编出来的 URL 大多长这样，"
            f"当证据用就是自己造数据。[/dim]"
        )

    if saas:
        vendors = Counter(p.system for p in saas)
        console.print(
            f"  [green]{len(saas)} 家公司只需要 {len(vendors)} 个适配器[/green]："
            + "、".join(f"{k}×{v}" for k, v in vendors.most_common())
        )
        # 只有真的摊开了才谈杠杆。原来这句无条件打，于是「2 家公司 2 个适配器」
        # 后面照样跟一句「这就是杠杆」——1:1 是没有杠杆的，那是拿一句结论去盖数据。
        if len(saas) > len(vendors):
            console.print("  [dim]这就是按 ATS 建适配层的杠杆：一个适配器摊到多家公司。[/dim]")
        else:
            console.print(
                "  [dim]这一轮没摊开（家数 = 适配器数），所以本轮不证明杠杆，"
                "只证明这几家用的是第三方 ATS。杠杆要靠同一个 ATS 上多找到几家。[/dim]"
            )
        console.print()
        console.print("  [dim]逐条核对归属（页面自称的公司，和你填的公司名对不上就别记账）：[/dim]")
        for p in saas:
            console.print(
                f"    {p.company:<10} {p.route_key_str:<22} "
                f"页面自称 [cyan]{p.page_title or '（无标题）'}[/cyan]"
            )
    elif ref:
        console.print(
            "  [yellow]注意：命中的全是厂商官网，没有一条是真实租户页。[/yellow]"
        )
        console.print(
            "  [dim]只能说明识别逻辑认得出这些厂商，不能说明覆盖率。"
            "要验证杠杆，得往 --urls-file 里加中厂的招聘入口。[/dim]"
        )

    # 认不出的分两种，性质完全不同：有厂商线索的是待补 hints，
    # 一条线索都没有的更可能真是自建。
    if unknown:
        with_lead = [p for p in unknown if p.vendor_markers]
        no_lead = [p for p in unknown if not p.vendor_markers]
        console.print()
        if with_lead:
            console.print("[yellow]认不出但有厂商线索（待人工核实，别直接写进 dom_hints）[/yellow]")
            for p in with_lead:
                console.print(
                    f"  {p.company:<12} {p.host_chain[-1] if p.host_chain else '?':<30} "
                    f"{', '.join(p.vendor_markers[:3])}"
                )
            console.print(
                "  [dim]线索可能是误命中：品牌词会出现在自家灰度开关、同行对比页里。[/dim]"
            )
        if no_lead:
            console.print("[dim]认不出且无任何厂商线索（大概率自建，各写一个适配器）[/dim]")
            for p in no_lead:
                console.print(
                    f"  [dim]{p.company:<12} {p.host_chain[-1] if p.host_chain else '?':<30} "
                    f"{', '.join(p.third_party_hosts[:2])}[/dim]"
                )

    if failed:
        console.print()
        console.print("[dim]探不通的不代表自建，只代表这次没探到：[/dim]")
        for p in failed:
            console.print(f"  [dim]{p.company}: {p.error}[/dim]")

    js = [p for p in ok if p.needs_js]
    if js:
        # 「入口」和「家」不是一回事：候选表里一家公司常有多个候选 URL
        # （`小红书` 和 `小红书-join`），5 个要跑 JS 的入口实际只有 3 家公司。
        # 原来这里写的是「N 家」而实际数的是行数——跟把 404 算成命中、
        # 把厂商官网算成用户是同一类口径错，只是这次错在分子上。
        # 按 `-` 前缀归并，这是候选表自己的命名约定。
        firms = {p.company.split("-")[0] for p in js}
        console.print()
        console.print(
            f"[dim]{len(js)} 个入口（{len(firms)} 家公司）的岗位列表要跑 JS 才出来 "
            f"→ 采集必须上浏览器，不能只靠 httpx。[/dim]"
        )


@app.command()
def main(
    urls_file: Path | None = typer.Option(
        None, "--urls-file", help="一行一个「公司,URL」，不给就用内置种子列表"
    ),
    out: Path = typer.Option(Path("data/ats_probe.json"), "--out", help="原始记录落盘位置"),
    render: bool = typer.Option(
        False, "--render", help="再用浏览器跑一遍抓 XHR 域名（慢，但 SPA 页面只有这样才看得到）"
    ),
    timeout: float = typer.Option(20.0, "--timeout"),
    concurrency: int = typer.Option(8, "--concurrency", min=1, max=16),
) -> None:
    """探测招聘页面特征，产出「按公司做还是按 ATS 做」的决策依据。"""
    targets = load_targets(urls_file)
    console.print(f"[dim]探测 {len(targets)} 个入口"
                  + ("，含浏览器渲染" if render else "") + "…[/dim]")

    probes: list[Probe] = []

    def report(p: Probe) -> None:
        """打一行。首轮和补探轮共用，两轮的判定口径必须一样。"""
        if p.alive and p.tombstone:
            mark, tail = "[dim]墓[/dim]", f"200 但页面自称：{p.page_title}"
        elif p.alive:
            mark, tail = "[green]✓[/green]", p.system
        elif p.blocked:
            # 必须排在 p.ok 前面。412 也是「拿到了响应」，落进下面那一档就会打成
            # 「入口不存在 412」——这一行是给人看的，打错了就等于 blocked 白立了。
            mark, tail = "[yellow]挡[/yellow]", f"被站点拒了 {p.status}，不是入口不存在"
        elif p.ok:
            mark, tail = "[yellow]·[/yellow]", f"入口不存在 {p.status}"
        elif p.starved:
            mark, tail = "[yellow]?[/yellow]", "连接池饥饿，未探到"
        else:
            mark, tail = "[dim]✗[/dim]", (p.error or "").split(":")[0]
        console.print(f"  {mark} {p.company} → {tail}")

    with httpx.Client(
        follow_redirects=True,
        # pool 超时要有限。这里一开始写的是 pool=None（「等连接池永不超时」），
        # 用来治 6 个目标吃 PoolTimeout 的问题——那确实是个真问题（PoolTimeout
        # 不是「探不通」，是根本没探，会被误读成「这家自建」），但这个治法是错的：
        # 它把一次会上报的失败换成了死等。faulthandler 抓到的栈就停在
        #     httpcore/_sync/connection_pool.py:35 wait_for_connection
        # 36 个目标跑到 23 个就卡住，0% CPU，挂了 38 分钟没动。
        #
        # 正确的做法是「有限等待 + 把饥饿和探不通分开记 + 补探一轮」：
        # pool 给足单个请求的最坏时长（connect + read）还有余量，
        # 真饥饿了就报 PoolTimeout，由 starved 标记接住，第二轮重试。
        timeout=httpx.Timeout(timeout, connect=timeout, pool=timeout * 4),
        headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
        # 槽位比 worker 多，让重定向换连接时不用排队。上限是 worker 数，
        # 不是这个数——这里只是给握手失败但还占着槽位的连接留出余量。
        limits=httpx.Limits(max_connections=concurrency * 3, max_keepalive_connections=4),
    ) as client:
        # 并发跑。候选列表里大多是构造的域名，DNS 解析不到的会一直挂到超时——
        # 串行的话 36 个目标要十分钟，光等不存在的主机。
        # httpx.Client 本身线程安全，共用一个连接池就行。
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for p in pool.map(lambda t: probe_one(client, t[0], t[1]), targets):
                probes.append(p)
                report(p)

        # 补探被连接池挤掉的。**串行**——饥饿本来就是并发挤出来的，
        # 再并发一遍是同一个坑。这一轮结束还 starved 的，会在汇总里单独点名，
        # 不许悄悄消失（starved 的 ok/alive 都是 False，两个统计口径都不收它）。
        if starved := [i for i, p in enumerate(probes) if p.starved]:
            console.print(f"[yellow]补探 {len(starved)} 个被连接池挤掉的目标（串行）…[/yellow]")
            for i in starved:
                probes[i] = probe_one(client, probes[i].company, probes[i].url)
                report(probes[i])

    if render:
        # 渲染必须串行：playwright 的同步 API 不能跨线程用。
        for p in probes:
            if p.ok:
                render_one(p)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps([asdict(p) for p in probes], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    console.print()
    summarize(probes)
    console.print(f"\n[dim]原始记录：{out}[/dim]")

    if not any(p.ok for p in probes):
        console.print(
            "\n[red]一个都没探通。[/red]这通常是沙箱/网络不通，不是这些站都挂了——"
            "结论不成立，换有网的环境重跑。"
        )
        raise typer.Exit(2)


if __name__ == "__main__":
    app()
