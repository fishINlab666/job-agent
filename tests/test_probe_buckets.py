"""探测脚本的分桶口径。

这个文件钉的不是 ATS 识别，是**汇总口径**——即「一个探测结果该算进哪一档」。
调研过程里这类错犯了七次（见 docs/ATS_RESEARCH.md「还有七次是我自己的口径错」），
而且它们比判错厂商更危险：判错厂商会被 MARKUP 闸门挡住，口径错没有闸门，
它直接变成正文里的一个数字，而且**看着很正常**。

所以每个「不是关于那家公司」的状态都得有用例：
  - starved  连接池饥饿  → 关于本机调度
  - blocked  412/403 那档 → 关于我的 HTTP 客户端
  - tombstone 200 的墓碑页 → 关于页面内容
这三个混进「探不通/入口不存在」，读出来就是「这家没有招聘入口 → 大概自建」。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_probe():
    """按路径加载 scripts/probe_ats.py。

    它不是包的一部分（是个 typer 脚本），但里面的分桶逻辑是承重的，
    不能因为「不好 import」就不测。

    `sys.modules` 那行是必须的，不是保险：`@dataclass` 处理注解时会拿
    `cls.__module__` 去 `sys.modules` 里查所属模块，执行中的模块还没登记进去，
    查出来是 None，直接炸在 `Probe` 的定义上。所以先登记、再 exec。
    """
    spec = importlib.util.spec_from_file_location(
        "probe_ats", ROOT / "scripts" / "probe_ats.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


probe_ats = _load_probe()


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


class TestBlockedIsNotAbsent:
    """412/403 是「站点拒了我」，不是「入口不存在」。

    实测背景：B站 那两个入口在 httpx 下连续三轮 ConnectTimeout，另一轮回了
    412（1.5 秒），同一时刻 curl 两个都是 200，后来又自己通了。
    不解析的主机不会自己复活，会时通时不通的只能是中间那层在做判决。

    不把这一档摘出去，它就会被算进 dead（4xx/空页），进而被读成
    「这家没有招聘入口 → 大概自建」——拿我客户端的遭遇给公司下结论。
    """

    @pytest.mark.parametrize("status", [403, 406, 412, 429])
    def test_refusal_statuses_are_blocked_not_dead(self, status: int) -> None:
        """412/406 对一个无条件 GET 在语义上根本不该出现，回了就是有人拦了。"""
        with _client(lambda req: httpx.Response(status, text="<html>blocked</html>")) as c:
            p = probe_ats.probe_one(c, "B站", "https://jobs.bilibili.com/campus")
        assert p.ok, "有响应就是 ok，这一档不是网络异常"
        assert not p.alive, "被拦的页面不算一个活着的入口"
        assert p.blocked, f"HTTP {status} 必须落进 blocked"

    def test_real_404_is_still_dead_not_blocked(self) -> None:
        """反向钉一次：404 是真的没这个页面，不能也算成「被挡」。

        `blocked` 收得太宽就等于把「入口不存在」洗成「可能是被挡的」，
        那 25 个构造出来的 campus.<公司>.com 会全被洗白。
        """
        with _client(lambda req: httpx.Response(404, text="not found")) as c:
            p = probe_ats.probe_one(c, "旷视", "https://campus.megvii.com/")
        assert p.ok and not p.alive
        assert not p.blocked, "404 是关于那个站的事实，不是关于我的客户端"

    def test_blocked_row_does_not_print_as_absent(self) -> None:
        """输出那一行也得分开。

        `report()` 里 blocked 的分支必须排在 `elif p.ok` 前面，否则 412 会被打成
        「入口不存在 412」——字段立对了、打出来还是错的，等于 blocked 白立。
        这里直接读源码断言顺序，因为 report() 是闭在 main() 里的局部函数。
        """
        src = (ROOT / "scripts" / "probe_ats.py").read_text(encoding="utf-8")
        body = src[src.index("def report(p: Probe)"):]
        body = body[: body.index("console.print(f\"  {mark}")]
        assert body.index("elif p.blocked") < body.index("elif p.ok"), (
            "blocked 的分支要排在 p.ok 之前，不然 412 会被打成「入口不存在」"
        )


class TestStarvedIsNotUnreachable:
    """连接池饥饿是本机调度的事，不是那个站连不上。"""

    def test_pool_timeout_is_starved_and_counted_nowhere(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.PoolTimeout("pool")

        with _client(handler) as c:
            p = probe_ats.probe_one(c, "某厂", "https://campus.example.com/")
        assert p.starved, "PoolTimeout 必须单独标出来，交给串行补探那一轮"
        # ok/alive 都是 False：两个统计口径都不收它,既不算「探通」也不算「探不通」。
        assert not p.ok and not p.alive
        assert not p.blocked, "饥饿跟被挡是两件事，别混"


class TestTombstoneIsNotAlive:
    """200 + 有正文 + 页面自称「已关停」，HTTP 层看不出来。

    实测：zhiye.com 的通配子域名兜底回 200 / 1228 字节，比 alive 那道 500 字节的
    门槛高，于是「租户页命中第三方 ATS」多报了一家（涂鸦那个 Moka 页）。
    """

    def test_soft_404_with_body_is_flagged(self) -> None:
        html = "<html><head><title>当前网页已关停</title></head><body>" + "x" * 1200 + "</body></html>"
        with _client(lambda req: httpx.Response(200, text=html)) as c:
            p = probe_ats.probe_one(c, "涂鸦智能", "https://tuya.mokahr.com/campus")
        assert p.alive, "HTTP 层确实是 2xx + 有正文，alive 只管这一层"
        assert p.tombstone, "内容层自称关停，证据口径要靠 alive and not tombstone 排掉它"
