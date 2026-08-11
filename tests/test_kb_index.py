"""`docs/kb/README.md` 的索引表必须和各文件 frontmatter 对得上。

为什么值得写测试：这个漂移已经发生过一次 —— 两份文件 2026-08-10 升到 v5，
索引表还写着 v1 / 2026-08-05，四天没人发现。它不会报错，只会让读索引的人
以为自己看的是最新口径。和 `CLAUDE.md` 里「用例数不写死在文档里」同一个形状：
需要人手同步两处的事实，早晚会有一处过期。

这里测的是**一致性**，不是「版本号是几」。写死 v5 的话每次升版都要改测试，
改测试的人会顺手把断言改成新值 —— 那就等于没有断言。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

KB = Path(__file__).resolve().parent.parent / "docs" / "kb"
README = KB / "README.md"

#: frontmatter 里必须有的键。缺一项就不算入库（README「硬规则」那节的要求）。
REQUIRED_KEYS = ("来源", "版本", "生效时间", "权限范围", "更新负责人", "审核状态")


def frontmatter(path: Path) -> dict[str, str]:
    """读 `---` 包起来的头信息。

    没有 frontmatter 时抛而不是返回 `{}`：「这份文件没头信息」和「头信息是空的」
    得分得开 —— 后者会让下面每条断言都变成拿 None 比 None，静默全绿。
    """
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        raise AssertionError(f"{path.name} 没有 frontmatter，按 README 硬规则它不算入库")
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def index_rows() -> dict[str, tuple[str, str]]:
    """解析索引表，返回 {文件名: (版本, 生效)}。

    只认 `| [x.md](x.md) | 说明 | vN | 日期 |` 这个形状。解析不出任何行时抛 ——
    表格哪天被改成别的写法，这里静默返回 `{}`，下面的循环一条都不跑、测试全绿，
    那正是「守卫悄悄消失」。
    """
    rows = {}
    for line in README.read_text(encoding="utf-8").splitlines():
        m = re.match(
            r"\|\s*\[(?P<name>[\w.-]+\.md)\]\([^)]+\)\s*\|[^|]*\|"
            r"\s*(?P<ver>v\d+)\s*\|\s*(?P<date>[\d-]+)\s*\|",
            line,
        )
        if m:
            rows[m.group("name")] = (m.group("ver"), m.group("date"))
    assert rows, "索引表一行都没解析出来。表格形状变了？这条守卫已经形同不存在"
    return rows


def kb_files() -> list[Path]:
    return sorted(p for p in KB.glob("*.md") if p.name != "README.md")


def test_there_are_kb_files_to_check() -> None:
    """前置：目录里真的有文件。

    没有这条，下面几条 parametrize 在目录空了的时候会**一条都不收集**，
    pytest 报「no tests ran」而不是失败 —— 在 CI 里看起来和通过没区别。
    """
    assert kb_files(), "docs/kb/ 下没有条目文件，下面的检查全都会静默跳过"


@pytest.mark.parametrize("path", kb_files(), ids=lambda p: p.name)
class TestEachEntry:
    def test_frontmatter_has_every_required_key(self, path: Path) -> None:
        fm = frontmatter(path)
        missing = [k for k in REQUIRED_KEYS if k not in fm]
        assert not missing, f"{path.name} 缺头信息字段：{missing}"

    def test_it_is_listed_in_the_index(self, path: Path) -> None:
        """新加的条目必须进索引表。

        不进的后果是「知识库里有，但没人知道有」—— 而这个目录存在的意义就是被检索。
        """
        assert path.name in index_rows(), (
            f"{path.name} 在 docs/kb/ 里但索引表没列它"
        )

    def test_index_version_matches_the_file(self, path: Path) -> None:
        ver, _ = index_rows()[path.name]
        assert ver == frontmatter(path)["版本"], (
            f"{path.name}：索引表说 {ver}，文件 frontmatter 说 "
            f"{frontmatter(path)['版本']}。升版时两处要一起改"
        )

    def test_index_date_matches_the_file(self, path: Path) -> None:
        _, date = index_rows()[path.name]
        assert date == frontmatter(path)["生效时间"], (
            f"{path.name}：索引表说 {date}，文件说 {frontmatter(path)['生效时间']}"
        )


def test_index_lists_no_file_that_does_not_exist() -> None:
    """反向：索引表里不许有已经删掉的条目。

    README 写了被证伪的条目要**从本目录删除**。删了文件忘了删索引行，
    Agent 会去检索一个不存在的文件 —— 而「链接点不开」比「结论是错的」难归因。
    """
    have = {p.name for p in kb_files()}
    listed = set(index_rows())
    assert not (listed - have), f"索引表列了不存在的文件：{sorted(listed - have)}"


def test_versions_are_plain_integers() -> None:
    """版本号形状统一 `vN`。

    `v5.1` / `v05` 这类混进来，上面两条比的是字符串，会出现「肉眼一样但不相等」。
    """
    for p in kb_files():
        v = frontmatter(p)["版本"]
        assert re.fullmatch(r"v\d+", v), f"{p.name} 版本号形状不对：{v!r}"
