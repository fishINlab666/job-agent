"""测试全局守卫：不许往真库写。

**为什么有这个文件。** 2026-08-11 真库里被发现一条 fixture 残留
（`external_id='12345'`、`fingerprint='fp1'`、腾讯「内容运营（校招）」）。
它不是提交进去的代码造成的 —— `tests/test_checkup_tencent.py::cli_db`
提交时就带着 `monkeypatch.setattr(db, "DB_PATH", path)`。
泄漏发生在**加那句 monkeypatch 之前**：我先跑了一遍测试，那行就落进了真库。

也就是说这类污染的形状是：**写测试的过程中间态**，不是提交后的代码。
所以靠 review 提交的 diff 抓不到它，靠「我记得要 monkeypatch」也抓不到 ——
需要一个在跑的时候就拦住的东西。

**判据分两级，不能合。**

- `db.connect()`（可写）指向真库 → **拦**。这是泄漏的唯一入口。
- `db.connect_readonly()`（`mode=ro`）指向真库 → **放行**。
  plan 013 的形状检查故意读真库：`EXPECTED_SHAPE` 要和库里的实际值对账，
  拿 fixture 造的库对账等于对着自己写的答案打勾。

合成一级的后果是二选一：要么把 013 那批检查废掉（失去唯一能发现
「全库一致地坏掉」的东西），要么把守卫关掉。

**为什么比对的是导入时快照，不是 `db.DB_PATH` 当前值。** 正常的测试会
monkeypatch `db.DB_PATH` 到 tmp，那时候当前值已经不是真库了 ——
拿当前值比会永远相等、永远放行。所以在任何 monkeypatch 生效之前
（conftest 导入时）把真路径存下来，之后一律和它比。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from jobagent import db

#: 真库的绝对路径，**在任何 monkeypatch 生效之前**快照下来。
#: `db.DB_PATH` 是 `ROOT / "data" / "jobagent.db"`，`ROOT` 从 `__file__` 解析，
#: 所以它和 cwd 无关、也不受测试里改 cwd 影响。
REAL_DB = Path(db.DB_PATH).resolve()


@pytest.fixture(autouse=True)
def _no_writes_to_the_real_db(monkeypatch):
    """任何测试拿可写连接打开真库时当场抛。

    autouse：不需要每个测试记得要它 —— 「需要人记得」正是这次失手的原因。
    """
    real_connect = db.connect

    def guarded(path=None):
        # path=None 时取模块级 DB_PATH，而它可能已经被测试 monkeypatch 到 tmp 了。
        # 所以这里要解析出「这次实际会打开哪个文件」，再和真库快照比。
        effective = Path(path if path is not None else db.DB_PATH).resolve()
        if effective == REAL_DB:
            raise AssertionError(
                f"测试试图用可写连接打开真库：{effective}\n"
                "真库不是 fixture。要么 monkeypatch.setattr(db, 'DB_PATH', tmp_path/'t.db')，\n"
                "要么显式传 db.connect(tmp_path/'t.db')。\n"
                "只读地对账真库是允许的，用 db.connect_readonly()。"
            )
        return real_connect(path)

    monkeypatch.setattr(db, "connect", guarded)
