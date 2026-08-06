# 方案文档：校招 Agent 当前实现

> **这份文档的职责：记录当前已确认的产品与技术事实，必须与代码一致。**
> 不记决策过程、不记调研弯路、不记「打算做」—— 那些在 Wiki（`docs/WIKI.md`）。
> 不作为 Agent 检索源 —— 那是知识库（`docs/kb/`）。
>
> 校对于 `2026-08-06` · 测试基线 `uv run pytest -q` → **320 passed**
> 状态口径同 `CLAUDE.md`：这里每一条都能被下面 §9 的命令验证；验不出来的属于 Wiki。

---

## 1. 目标用户与问题定义

**用户**：26/27 届找产品/运营岗的学生（当前只有一个真实用户：本仓库作者）。
画像配在 `profile.yaml`（`profile.yaml.example` 是样例）。

**问题**：校招岗位散在几十家公司的招聘站上，开放窗口短、下架无声。
手工刷的失败模式不是「累」，是**漏**——岗位放出来那几天没刷到就没了。

**产品因此只解决一件事**：把「哪些岗位新出现了」变成一条可推送的事件流，
并在投递环节保留人工确认。

**明确不解决**：不做投递代理商（不代替用户判断投不投）、不做简历优化、
不做面试辅导。

## 2. 功能范围（M1–M6，已实现）

| 模块 | 做什么 | 入口 |
|---|---|---|
| M1 采集 | 按源抓岗位列表，落 `snapshots` 原文 | `cli sync` |
| M2 归一与增量 | 字段归一 + diff 出开/关/变更，落 `jobs` | 同上 |
| M3 匹配 | 按 `profile.yaml` 判命中/信息不全/不命中 | `cli jobs --matched` |
| M4 事件 | 开放/关闭/重开/族首现/批次启动 | `cli digest` |
| M5 摘要 | 按分数排序输出日报 | `cli digest --mark` |
| M6 代投 | 浏览器填表 → 人工确认 → 提交 | `cli apply <id>` |

CLI 全部命令：`init` / `sync` / `jobs` / `digest` / `status` / `apply`。

## 3. Agent 工作流

```
sync ──► adapters.fetch() ──► normalize ──► ingest.diff ──► jobs + events
                                                              │
                                              digest ◄────────┘
                                                              │
                                       apply ──► prepare ──► 人工确认 ──► execute
```

采集与投递**共用一套路由**（`routing.py`）：同一个 `source_key` 决定用哪个
adapter 抓、用哪个 submitter 投。路由拒绝的五种情况见 §7。

## 4. 工具与权限

| 能力 | 实现 | 权限边界 |
|---|---|---|
| HTTP 抓取 | `httpx`，各 adapter 自带 UA | 只读公开接口，不带用户 cookie |
| 浏览器投递 | Playwright（`submitters/tencent_join.py`）| 需要用户已登录的 `user_data_dir` |
| 本地库 | SQLite（`data/jobagent.db`）| 唯一持久化，不上云 |
| 用户资料 | `profile.yaml` | 不进 git（`profile.yaml.example` 才进）|

**不可逆动作只有一个：投递提交。** 它的闸门是 API 形状的，见 §7。

## 5. 数据流与库结构

六张表（`jobagent/schema.sql`）：

| 表 | 存什么 | 关键约束 |
|---|---|---|
| `sources` | 源登记（含 `tenant`）| `UNIQUE(source_key)` |
| `runs` | 每轮采集的状态 | `ok` / `partial` / `failed` / `running` |
| `snapshots` | 每轮抓到的原文 JSON | 漏报排查的唯一依据，每轮都落 |
| `jobs` | 归一后的岗位 | `UNIQUE(source_key, external_id)` |
| `events` | 事件流 | `notified_at` 区分已推未推 |
| `applications` | 投递记录 | `prefilled`→终态，`confirm_token` 唯一索引 |

**已接的源**（`cli sync --source` 可用的键）：

| source_key | 公司 | 系统 | 主库 `jobagent.db` | 核对库 `jobagent-5.db` |
|---|---|---|---|---|
| `tencent_join` | 腾讯 | 自建 | **795** | 801（campus 366 @26届 + intern 435 @27届）|
| `feishu:nio:campus` | 蔚来 | feishu | **未落** | 627（campus 218 + intern 409）|
| `feishu:xiaopeng:campus` | 小鹏汽车 | feishu | **未落** | 436（campus 312 + intern 124）|
| `feishu:bytedance:campus` | 字节跳动 | feishu | **未落** | 7395（campus 2048 + intern 5347）|
| `feishu:sensetime:edu` | 商汤科技 | feishu | **未落** | 160（campus 92 + intern 68）|

**飞书四家现在指的是校招门户**（`website-path`，键第三段就是门户）。
2026-08-06 `--fresh` 跑完核对库共 **9419** 条。上一版这四家采的是社招池、
键是两段的，用户核对时 5 个入口里 4 个对不上 —— 见
`docs/plans/003-校招门户采集.md`。海底捞没有校招门户（`code=-9000003`），
换成了字节跳动。

**`apply_url` 的形状**（这一列的唯一用途是给人点开核对，所以它错等于核对手段本身坏了）：

| 系统 | 形状 | 实测 |
|---|---|---|
| feishu | `https://<host>/<portal>/position/<id>` | 200（2026-08-06，四租户一致）|
| feishu（无门户的老源）| 退到 `index` 段 | 同上 |
| ~~`https://<host>/position/<id>`~~ | **404，body 9 字节** | 曾被 `003 §4` 当口径写着，核对库 4810 条链接因此全坏 |

**没验到的那一步**：页面是客户端渲染 SPA，任何 id（含 `0`/`abc`/乱填）都回同一个
200 外壳，所以「校招 id 必须配校招门户前缀」只是按门户构造，**没有独立验证** ——
200 只证明路由存在。要验得跑真浏览器看 network。

**两个库是有意分开的**（`closed_at IS NULL`）：飞书四家只落在
`data/jobagent-5.db`（`scripts/run_five.py` 的核对库），主库仍只有腾讯 795 条。
核对是要反复跑的动作，反复往主库写会搅乱 `first_seen_at` 和 `snapshots`。
腾讯两个库差 6 条是不同日期各跑一轮的结果，不是丢数据——**岗位池自己在动**。

## 6. 异常处理

每一条都对应一个真实故障，不是防御性编程：

| 异常 | 处理 | 为什么不能反过来 |
|---|---|---|
| 抓到 0 条 | **抛**，run 记 `failed` | 否则 diff 把全部岗位判成关闭 |
| 抓到 0 条 **且适配器说得清原因** | 正常走完，`opened=0` | `empty_is_authoritative`，飞书 `count=0` 是真租户没在招 |
| 门户不存在（飞书 `code=-9000003`）| **抛**，且**不置** `empty_is_authoritative` | 它和 `count=0` 长得像、含义相反：一个是「这家没在招」，一个是「我打错了门户」。被当空放过 = 门户改名就静默关闭 627 条，run 还记 `ok` |
| `recruit_type` 认不出的组合 | 写 `None` | **不许兜底 `social`**：把校招岗标成社招比判不出更糟——判不出用户还能靠 `--loose` 看到，标错是静默错到另一个类里 |
| 消失比例 > 40% 且 ≥ 5 条 | 不关任何岗位，run 记 `partial` | 上游半残返回会造成批量假关闭 |
| 翻页拿到空批次 | 整轮抛 | 静默截断 = 半截数据被判关闭 |
| 岗位缺 `external_id` | 跳过并计数 `skipped_no_id` | 不许 fallback 到 title（会撞唯一约束）|
| `job_family` 判不出 | 写 `None` | **不许兜底 `"other"`**，那会让用户按族筛永远看不到 |
| `grad_year` / `cities` 缺失 | 三态：命中 / 信息不全 / 不命中 | 「没有值」被当「不合格」会静默丢岗位 |
| 投递页要登录 / 岗位已关 | 落 `blocked`，不进提交 | —— |

## 7. 硬约束（API 形状，不是提示词）

| 约束 | 在哪 | 挡什么 |
|---|---|---|
| 提交必须带 `confirm_token` | `submitters/base.py` `mint_token()` / `TokenError` | 模型自作主张提交 |
| 投递分 prepare / execute 两步 | `cli apply` | 「填完顺手就投了」 |
| 路由五道拒绝 | `routing._build()` | 认不出系统、租户对不上、多租户缺 tenant 等 |
| 域名判据收窄到产品级 | `ats.py` `domains` | 品牌域名（`feishu.cn`）把一篇文档判成招聘入口 |
| dry-run 全程不落盘 | `db.*(commit=False)` + `conn.rollback()` | `--dry-run` 往真库留 `running` 行，让 `status` 说假话 |
| 一个 `source_key` = 一个门户 | `routing.portal_of()`，键第三段 | 社招校招合一个键 → 关闭判定的分母被混：校招门户整个下线时消失比例 627/2704=23%，低于 0.4 守卫，**627 条静默关闭** |
| host 只从 `sources.entry_url` 取 | `routing.get_adapter()` | 从岗位链接现推 host = 放宽域名判据。自定义域名（`hr-jobs.sensetime.com`）靠人工登记那一行 |
| host 与 tenant 对不上就炸 | `FeishuAdapter.__init__` | `entry_url` 抄错一行（复制上一家忘改子域名）→ 拿 A 的配置打 B 的接口，**把 B 的岗位落在 A 名下且不报错** |

## 8. 评测指标

| 指标 | 口径 | 当前值（2026-08-06）|
|---|---|---|
| 岗位池宽度 | 库内 `closed_at IS NULL` 条数 | 9419（核对库）/ 795（主库）|
| **校招覆盖** | 校招门户抓到条数 / 已接公司的校招岗位总数 | **8618 / 8618**（飞书四家全量）+ 腾讯 366。分母是「已接公司」的，不是市场的 |
| 族判不出率 | `job_family IS NULL` / 该源抓到条数 | 按源报，**6%~44%**（四个校招门户：字节 6 / 商汤 9 / 小鹏 37 / 蔚来 44）。**这是公司属性不是系统属性** |
| 误关闭 | 被判关闭但官网仍在招的条数 | 未测量（守卫拦住的次数可从 `runs.status='partial'` 数）|
| 字段填充率 | 非空条数 / 该源条数 | 见 `scripts/run_five.py` 输出 |

**口径规则（踩过八次）**：任何计数都带测量日期；任何区间都带样本来源
（是哪几个租户/门户）；**报数前先算交集**——两个键不等于两批数据。

## 9. 验收标准

```bash
uv run pytest -q
```

320 passed（2026-08-06）。新功能的验收 = 这个数变大且全绿。

```bash
uv run python -m jobagent.cli status
```

不该出现 `running`（出现就是有一轮 sync 没收尾，或 dry-run 泄漏了）。

```bash
uv run python scripts/run_five.py --fresh
```

五个源落进 `data/jobagent-5.db`（**不动主库**），打字段填充率 + 抽样。
这是拿去和官网逐字段核对的入口。

单功能的验收标准写在各自方案的 §9/§10（`docs/plans/NNN-*.md`）。

---

## 变更记录

| 日期 | 改了什么 | 依据 |
|---|---|---|
| 2026-08-05 | 首次成文。补 §5 已知缺口（飞书采的是社招池）| `plans/003` |
| 2026-08-06 | 基线 293→**320**；§5 源表换成三段键（飞书四家指校招门户，海底捞→字节）+ 补 `apply_url` 形状与「没验到那一步」；§6 加门户不存在必抛、`recruit_type` 不兜底；§7 加一键一门户、host 只从 `sources` 取、host/tenant 对不上就炸；§8 校招覆盖 0→**8618**、族判不出率改为按公司 6%~44% | `plans/003` 已复盘 |
