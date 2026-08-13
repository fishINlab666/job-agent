"""文档和代码对不上就红（方案 020）。

守的是三处已经漂移过的地方：SPEC 的 CLI 清单（漏过**三次**）、SPEC 的模块覆盖、
以及「当前用例数」在几个文件里各写一份（`CLAUDE.md` 明文禁止，但那条规则只管住了
它自己）。

锚点一律取**代码侧**，不取文档侧：拿文档里的表当锚点只查得出「表里有不存在的东西」，
查不出「东西没进表」—— 而这三处的漂移全是后者。
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs" / "SPEC.md"
README = ROOT / "README.md"
CHANGELOG = ROOT / "CHANGELOG.md"


def cli_commands() -> set[str]:
    """`cli --help` 里的命令名集合。**这是唯一来源**，不读源码里的装饰器。

    读装饰器等于换一个地方重建一份清单，而重建的那份会和真实注册的那份分叉
    —— 这份文件要防的就是这类分叉。
    """
    r = subprocess.run(
        [sys.executable, "-m", "jobagent.cli", "--help"],
        cwd=ROOT, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
             "PYTHONPATH": str(ROOT), "COLUMNS": "200"},
    )
    assert r.returncode == 0, f"`cli --help` 跑不起来：{r.stderr[-500:]}"
    # Typer 的命令区是 `│ 名字   描述  │`，取每行第一个 token。
    out: set[str] = set()
    in_cmds = False
    for line in r.stdout.splitlines():
        if "Commands" in line:
            in_cmds = True
            continue
        if in_cmds and line.startswith("╰"):
            break
        if in_cmds and line.startswith("│"):
            tok = line.lstrip("│").strip().split()
            if tok and re.fullmatch(r"[a-z][a-z-]*", tok[0]):
                out.add(tok[0])
    return out


def spec_cli_list() -> set[str]:
    """SPEC §2 那句「CLI 全部命令：...」里列的命令名。"""
    text = SPEC.read_text(encoding="utf-8")
    # 允许「CLI 全部命令」和冒号之间夹一段（比如「（**12 条**）」）——
    # 但不允许跨行找，免得匹配到别处的冒号。
    m = re.search(r"CLI 全部命令[^：:\n]*[：:]\s*(.+?)(?:\n\n|\n>)", text, re.S)
    assert m, "SPEC 里找不到「CLI 全部命令：」那一句 —— 是不是改了措辞？"
    return set(re.findall(r"`([a-z][a-z-]*)`", m.group(1)))


class TestSpecCliList:
    """SPEC 的 CLI 清单必须等于 `cli --help` 的实际命令集。

    这张清单已经漏过**三次**：`refresh-grad-year`（007 起）、
    `repair-apply-url`（010 起）—— 这两次 SPEC 自己记着 ——
    以及 `source-add` / `checkup` / `health`（013/014 起），第三次。
    SPEC §2 甚至写了核对办法「拿代码当准」，但那是一句给人看的话，没人执行。
    """

    def test_help_parser_finds_commands(self) -> None:
        """解析器真的解析到了东西。**这条必须在比对之前。**

        `--help` 的排版是 Typer 给的，换个版本就可能变。解析器数出 0 个时，
        下面那条比对是「空集 == 空集」…… 不，是「空集 != SPEC 的 9 条」会红 ——
        但红的理由会被读成「SPEC 多写了 9 条」，指向错的那一层。
        所以先单独断言解析到了，且数量在一个合理范围内。
        """
        cmds = cli_commands()
        assert cmds, "解析 `--help` 得到 0 个命令 —— 解析器坏了，不是文档错了"
        assert "sync" in cmds and "apply" in cmds, \
            f"解析结果里连 sync/apply 都没有，解析器认错了区块：{sorted(cmds)}"
        assert len(cmds) >= 10, f"只解析到 {len(cmds)} 个命令，疑似漏了一段：{sorted(cmds)}"

    def test_spec_cli_list_matches_help(self) -> None:
        """两边集合相等 —— 双向查，缺一个方向就漏一类漂移。"""
        real, doc = cli_commands(), spec_cli_list()
        missing = sorted(real - doc)
        stray = sorted(doc - real)
        assert not missing, (
            f"这些命令能跑但 SPEC 没列：{missing}。\n"
            f"清单漏第四次了。SPEC §2 写着「拿代码当准」—— 现在这条测试就是那句话。"
        )
        assert not stray, (
            f"SPEC 列了但 `cli --help` 里没有：{stray}。\n"
            f"命令被删/改名了，或者清单里拼错了 —— 后者更糟，它是个永远跑不通的承诺。"
        )


class TestSpecKnowsEveryModule:
    """SPEC 自称「必须与代码一致」，那它至少要知道每个模块存在。

    2026-08-13 的实际状态：SPEC 正文**一次都没引用过** `jobagent/*.py`，
    而 `health.py` / `mcp_server.py` / `queries.py` 三个模块它完全不知道。
    """

    #: 不要求 SPEC 提到的模块，写清每一个的理由（不许写「其他」）。
    #: 白名单而不是黑名单：新增模块默认**要求**进 SPEC，漏的方向是「多要求一个」，
    #: 看得见（测试红了去加一行）。反过来漏的方向是新模块静默不进文档。
    NOT_REQUIRED = {
        "__init__.py": "空的包标记",
    }

    def test_spec_mentions_every_module(self) -> None:
        """锚点是 `ls jobagent/*.py`，不是 SPEC 里的表。

        拿表当锚点只查得出「表里有不存在的模块」，查不出「模块没进表」,
        而这次的漂移正是后者。
        """
        text = SPEC.read_text(encoding="utf-8")
        mods = sorted(p.name for p in (ROOT / "jobagent").glob("*.py"))
        assert len(mods) >= 10, f"只找到 {len(mods)} 个模块，锚点可疑：{mods}"
        missing = [m for m in mods
                   if m not in self.NOT_REQUIRED and m[:-3] not in text and m not in text]
        assert not missing, (
            f"SPEC 完全没提到这些模块：{missing}。\n"
            f"SPEC 页首自称「必须与代码一致」—— 一份宣称与代码一致的文档说错话，"
            f"比一份没人信的文档危险，因为 §9 的验收命令是从它抄出去跑的。"
        )

    def test_exemptions_are_only_for_empty_modules(self) -> None:
        """豁免只许给**真的没内容**的模块，理由字符串不算理由。

        改坏验出来的：把 `mcp_server.py` 加进 `NOT_REQUIRED`、理由写「懒得写」，
        上面那条判据一声不响就绿了。一个能靠写字绕开的判据，在它被绕开的那天
        和不存在没区别 —— 而绕开它的动作恰好发生在「新模块没进文档」的时候，
        也就是这条判据唯一有用的时刻。

        所以豁免的依据换成文件自己的属性：没有任何 `def` / `class`。
        `__init__.py` 满足；任何有实现的模块都不满足，写什么理由都不行。
        """
        for name in self.NOT_REQUIRED:
            src = (ROOT / "jobagent" / name)
            assert src.exists(), f"{name} 在豁免表里但文件不存在，表该清一清"
            tree = ast.parse(src.read_text(encoding="utf-8"))
            defs = [n.name for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
            assert not defs, (
                f"{name} 被豁免了，但它有实现：{defs}。\n"
                f"豁免只给空模块。有实现的模块必须进 SPEC —— 理由写得再好，"
                f"读 SPEC 的人也还是不知道它存在。"
            )


class TestReadmeTableCoversEveryTestFile:
    """README 的分文件表必须覆盖 `tests/test_*.py` 全部文件。

    这张表烂过一次，而且烂得很有教育意义：在发布 commit `9d6ffb1` 上跑分文件收集，
    它点名的 9 行数字 **98/69/64/63/54/39/31/24/23 全对**，「其余」桶的 39 也对，
    **合计正好等于当时真实的 504**。它不是写错了 —— 是写完之后新增的 8 个测试文件
    从没进过它。

    顺带那天它也确实错了一处：「其余 **6** 个文件」实际是 **5** 个。
    数字列合计对上给了整张表虚假的可信度，而错的是**文件数**那一列 ——
    另一个维度。所以这条判据两个方向都查。
    """

    @staticmethod
    def _named() -> set[str]:
        text = README.read_text(encoding="utf-8")
        assert "| 文件 | 覆盖 |" in text, (
            "README 里找不到分文件表的表头「| 文件 | 覆盖 |」—— 是不是改了列？"
        )
        body = text.split("| 文件 | 覆盖 |")[1].split("\n\n")[0]
        return set(re.findall(r"^\|\s*`(test_[a-z0-9_]+\.py)`", body, re.M))

    @staticmethod
    def _actual() -> set[str]:
        got = {p.name for p in (ROOT / "tests").glob("test_*.py")}
        assert len(got) >= 20, f"只找到 {len(got)} 个测试文件，锚点可疑：{sorted(got)}"
        return got

    def test_every_test_file_is_in_the_table(self) -> None:
        """锚点是 `ls tests/test_*.py`，不是表本身。

        拿表当锚点只查得出「表里有不存在的文件」，查不出「文件没进表」——
        而这次烂掉的正是后者，8 个文件。
        """
        missing = sorted(self._actual() - self._named())
        assert not missing, (
            f"这些测试文件不在 README 的表里：{missing}。\n"
            f"表以前是靠一个「其余 N 个文件」的桶兜着的，新文件进不进桶没人知道 ——"
            f"于是 8 个文件在表外待了三个月。现在一个都不许兜。"
        )

    def test_table_has_no_file_that_does_not_exist(self) -> None:
        """反方向：表里不许有已经删掉/改名的文件。"""
        stale = sorted(self._named() - self._actual())
        assert not stale, (
            f"README 的表里这些文件已经不存在了：{stale}。\n"
            f"删测试文件时顺手改表 —— 指向空气的行比没有行更误导。"
        )


class TestTestCountHasOneHome:
    """「当前有多少个用例」在文档里只许有一个出处。

    `CLAUDE.md` 明文写着这条（「用例数不写死在这里……改漏了就变成一个假的回归信号」），
    但那条规则只管住了 `CLAUDE.md` 自己。实际发生过的：发布 commit `9d6ffb1` 上
    README 写 504、SPEC 页首写 464，**同一个 commit 两个数打对台**，
    而当时真实收集数是 504（`git worktree` + `--collect-only` 核过）。

    判据是：**裸的当前数**不许出现在 SPEC / CHANGELOG 里。
    带日期的历史快照可以留 —— 那是历史，不是当前值，两者能区分开的唯一标志就是日期。
    """

    #: 三位数以上的「N passed」/「N 个测试用例」在这些文件里一律算裸当前数。
    #: README 不在列内：它的「现状」那一节是唯一出处（数字 + 日期 + 命令放在一起）。
    NO_PINNED_COUNT = ("docs/SPEC.md",)

    @pytest.mark.parametrize("rel", NO_PINNED_COUNT)
    def test_no_pinned_current_count(self, rel: str) -> None:
        """这些文件里不许出现「当前基线 = N passed」这种钉法。"""
        text = (ROOT / rel).read_text(encoding="utf-8")
        # 只查页首那一段和验收标准那一节 —— 变更记录里的历史快照是对的。
        head = text.split("## 1.")[0]
        acceptance = ""
        if "## 9. 验收标准" in text:
            acceptance = text.split("## 9. 验收标准")[1].split("\n## ")[0]
        for name, chunk in (("页首", head), ("§9 验收标准", acceptance)):
            hits = re.findall(r"\*\*(\d{3,})\s*passed\*\*|(\d{3,})\s*passed", chunk)
            flat = [h for pair in hits for h in pair if h]
            assert not flat, (
                f"{rel} 的{name}里钉着当前用例数 {flat}。\n"
                f"这个数每加一次测试就过期，而它旁边写着「必须与代码一致」——"
                f"于是它变成一个假的回归信号：看到数字不对，先怀疑的会是自己少收集了。\n"
                f"改成「跑 `uv run pytest -q` 看最后一行」。"
            )

    def test_readme_count_travels_with_its_command(self) -> None:
        """README 是唯一许写当前数的地方，但数字必须和产生它的命令挨在一起。

        `CLAUDE.md` 的原话是「数字和产生它的命令放在一起」。
        一个没有命令的数字没法被验证，只能被相信 —— 而它一定会过期。
        """
        text = README.read_text(encoding="utf-8")
        m = re.search(r"\*\*(\d{3,})\s*个测试用例\*\*", text)
        assert m, "README 里找不到「**N 个测试用例**」—— 唯一出处不该消失"
        window = text[max(0, m.start() - 120):m.end() + 240]
        assert "pytest" in window, (
            "README 的用例数附近没有 pytest 命令。"
            "数字和产生它的命令要放在一起，否则它只能被相信、没法被验证。"
        )
        # 日期必须**紧跟**这个数（同一行、30 字内），不是「附近有个日期」。
        # 改坏验出来的：拿 600 字窗口找日期时，判据被正文里另一处引用的
        # `2026-08-13` 满足了 —— 日期被拿掉它照样绿。窗口越宽，判据越容易
        # 被无关内容喂饱。
        assert re.search(r"\*\*\d{3,}\s*个测试用例\*\*[^\n]{0,30}?20\d\d-\d\d-\d\d", text), (
            "README 的用例数后面 30 字内没有日期。没有日期的数字是在宣称"
            "「现在就是这个数」，而它只在写下的那一刻为真。"
        )

    def test_readme_count_is_actually_true(self) -> None:
        """README 的数必须等于真实收集数。

        上面两条只管「数字带着命令和日期」—— 那是格式。504 当年**格式是合规的**
        （有日期，附近有 `pytest -xvs`），它烂在没人核对内容。
        只查形状的守卫会一直绿着看它烂掉。

        代价是自觉的：加一条测试就得改一次 README。这正是「唯一出处」的意思 ——
        要么这个数是真的，要么套件红。没有第三种状态。
        """
        text = README.read_text(encoding="utf-8")
        m = re.search(r"\*\*(\d{3,})\s*个测试用例\*\*", text)
        assert m, "README 里找不到「**N 个测试用例**」—— 唯一出处不该消失"
        claimed = int(m.group(1))

        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "--collect-only", str(ROOT / "tests")],
            capture_output=True, text=True, cwd=ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        collected = re.search(r"(\d+)\s+tests?\s+collected", proc.stdout)
        assert collected, (
            "没能从 --collect-only 的输出里读到收集数，锚点自己坏了。\n"
            f"stdout 末尾：{proc.stdout[-500:]}"
        )
        actual = int(collected.group(1))
        assert claimed == actual, (
            f"README 写 {claimed}，实际收集 {actual}。\n"
            f"改 README 的那个数，日期也一起改。"
        )
