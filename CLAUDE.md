# job-agent 仓库约定

方案规则在上一层 `../CLAUDE.md`（**改代码前先写方案**）。
这里只放跑这个仓库需要知道的事。

## 命令一律带 `uv run` 前缀

```bash
uv run pytest -q                          # 全量
uv run python -m jobagent.cli sync        # 同步岗位
uv run python scripts/probe_ats.py        # ATS 探测
```

裸 `python` 在这个环境里不存在，只有 `python3`，而它看不见 `.venv`——
装在虚拟环境里的 httpx / playwright / pytest 一个都 import 不到。
文档里出现的每条命令都得是能直接粘出去跑的，
否则「下面每个数字都能复现」这种话第一步就断了。

## 用例数不写死在这里

想知道多少个就跑 `uv run pytest -q`，看最后一行。

故意不在这个文件里记数字：每加一次测试就得改一处，
改漏了就变成一个假的回归信号——看到数字不对，先怀疑的会是自己少收集了。
「当前有多少个用例」这种会动的事实只写在 `../README.md` 的现状那一节，
数字和产生它的命令放在一起。

## 模块地图

| 文件 | 管什么 |
|---|---|
| `jobagent/ingest.py` | 采集编排、去重、写库 |
| `jobagent/adapters/` | 各站点适配器（自建站一个一个写） |
| `jobagent/ats.py` | ATS 厂商识别（飞书 / 北森 / Moka） |
| `jobagent/routing.py` | 路由分层：`domain` 可投，`markup` 只做线索 |
| `jobagent/normalize.py` | 清洗：届别、城市、职能族 |
| `jobagent/match.py` | 画像筛选 |
| `jobagent/submitters/` | 代投。`base.py` 是闸门本体 |
| `jobagent/db.py` + `schema.sql` | 建表与迁移 |

## 改 schema 时注意 `init()` 的顺序

`db.init()` 是**先** `executescript(schema.sql)`，**再** `migrate()`。

所以任何索引只要引用了 `APPLICATION_COLUMNS` / `SOURCE_COLUMNS` 里的列
（那些是 `migrate()` 补的），就**不能写在 `schema.sql` 里**。
老库缺那一列，索引语句直接报 `no such column`，
挂掉的是整个 `init()`，不是「索引没建上」而已。

这类索引只在 `db.migrate()` 里建，补列之后。
`tests/test_db_migrate.py::TestIndexesBuiltAfterColumns` 守着这条归属——
包括一条读 `schema.sql` 源码的检查，因为两边都写的时候新库照样全绿。

## 空值先想清楚是 NULL 还是空串

这个仓库的头号 bug 来源，两次都是同一个形状：

- `grad_year=None` → `None not in ["26","27"]` → 岗位被静默丢掉
- `confirm_token=""` → 空串是**非 NULL** → 参与唯一索引去重 → 第二条 blocked 撞约束

写库的地方，取不到值就显式写 `NULL`（`x or None`），
不要图省事写空串。判断的地方，「没有值」要当成一个正经状态来分支，
不能靠真值判断顺手带过。

## 不要提交的东西

`.gitignore` 已经挡住：`profile.yaml`（真实个人信息）、`data/`（含数据库）、
`.browser-profile/`（登录态 cookie）、`screenshots/` 里的存证。

加新文件时先过一遍：里面有没有姓名、手机号、邮箱、cookie。
`profile.yaml.example` 是可以提交的那一份，改字段时两份一起改。
