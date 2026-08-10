# 方案文档：校招 Agent 当前实现

> **这份文档的职责：记录当前已确认的产品与技术事实，必须与代码一致。**
> 不记决策过程、不记调研弯路、不记「打算做」—— 那些在 Wiki（`docs/WIKI.md`）。
> 不作为 Agent 检索源 —— 那是知识库（`docs/kb/`）。
>
> 校对于 `2026-08-10` · 测试基线 `uv run pytest -q` → **464 passed**
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
| M3 匹配 | 按 `profile.yaml` 判命中/信息不全/不命中，可按维度放宽 | `cli jobs --matched [--allow-missing <维度>]` |
| M4 事件 | 开放/关闭/重开/族首现/批次启动 | `cli digest` |
| M5 摘要 | 按分数排序输出日报 | `cli digest --mark` |
| M6 代投 | 浏览器填表 → 人工确认 → 提交 | `cli apply <id>` |
| M7 投递出口 | 读 `applications`：投了什么、卡在哪、截图在哪 | `cli applications [--funnel]` |

CLI 全部命令：`init` / `sync` / `jobs` / `digest` / `status` / `apply` /
`applications` / `refresh-grad-year` / `repair-apply-url`。

> 这张清单漏过两次（`refresh-grad-year` 从 007 起、`repair-apply-url` 从 010 起），
> 都是新命令落地时没回来改。核对办法是**拿代码当准**：
> `uv run python -m jobagent.cli --help` 的输出才是全集。

**M7 是只读的，且故意不提供任何改状态的开关。** 状态变更必须走 `apply` 的
prepare/execute 两阶段闸门（§7 那条硬约束），从一个查看命令里改终态等于开后门。

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

| source_key | 公司 | 系统 | 主库开放条数 | 其中 campus / intern | 届别填充（2026-08-10）|
|---|---|---|---|---|---|
| `tencent_join` | 腾讯 | 自建 | **807** | 368 / 439 | 807/807，但是**推断的**（`projectId` 分桶，见下）。届别值是 `26` 461 / `不限` 348（含已关闭，全表 809 行）|
| `feishu:bytedance:campus` | 字节跳动 | feishu | **7368** | 2073 / 5295 | **2073/7368（28%）**，招聘项目名推出的（plan 011）。恰好等于它的 campus 行 —— 5295 条实习的项目名不含「届」|
| `feishu:nio:campus` | 蔚来 | feishu | **634** | 218 / 416 | **313/634（49%）**，同上。**不是按 `recruit_type` 分的**：campus 196/218 + intern 117/416 |
| `feishu:xiaopeng:campus` | 小鹏汽车 | feishu | **431** | 312 / 119 | 0/431，它的届别写在**标题**里（54 条「27届」，走通道二，不落这一列）|
| `feishu:sensetime:edu` | 商汤科技 | feishu | **161** | 92 / 69 | **73/161（45%）**，同上 |

合计 **9401** 条开放岗位（`closed_at IS NULL`，2026-08-10 那一轮；全表 9403 行，
腾讯 2 条已关闭）。腾讯 805→807 是重测漂移，不是丢数据。

`sources` 表有 6 行而不是 5 行：`feishu:nio` 是上一版两段键的遗留登记，名下 0 条岗位，
`cli sync --source` 不用它。留着是因为删源登记会牵动 `jobs.source_key` 的外键语义，
而它现在不影响任何计数。

**腾讯的届别是推断的，不是抓到的**：`adapters/tencent_join.py:50` 的 `_parse_grad_year()`
按 `recruitLabelName` 分桶推导（已落地 2026-08-09）。`searchPosition` 响应的 13 个字段里
没有任何毕业时间字段（2026-08-09 实打接口核实），所以那个 100% 填充率不是观测结果。

推导规则（已核实 2026-08-09）：
- 应届桶（`projectId in {1, 14}`）→ `"26"`（站点当前入口年份）
- 实习桶（`projectId in {4, 5, 12, 20}`）→ `"不限"`（站点实习入口明说「不限毕业时间」）
- `projectId == 2`（应届实习）→ `"26"`（本地例外：站点对它返回 null，我们按标签推断）

**用 `projectId` 而不是 `recruitLabelName`**：站点自己在 `renderProjectMeta` 里按 `projectId`
分派届别声明，这是站点的键。标签字符串匹配会错分项目：pid=12 的项目名「项目实习生」、
标签「日常实习」，字符串匹配认不出它是另一个项目。详见 `docs/plans/009-按projectId分桶.md`（已落地）。

**不能按 `recruit_type` 分桶**：「应届实习」(93条) 的 type 是 `intern` 但届别跟应届走，
按 type 分会把它错分成 `"27"` 或 `"不限"`。站点那两个日期字符串
（`Project_CampusGraduationDate` / `Project_TraineeGraduationDate`）**已推翻（2026-08-09）**：
在语言包定义文件里数引用是错的方法，实际在 `renderProjectMeta` 里各被引用 2 次。
但站点对 `应届实习`(projectId=2) 返回 null，届别是我按标签推断的。
详见 `docs/plans/007-届别窗口区间.md`（已落地）。

**`grad_year` 不在指纹字段里**（`ingest._fp()`），所以改了推导规则之后 `sync` 刷不动
存量：指纹没变就落到「只动 `last_seen_at`」那条分支。实测 2026-08-09，807 行里 804 行
会被静默跳过，只有 1 行因为别的字段也变了而搭上便车 —— 那 1 行最坏，它让人以为生效了。
表现是「改了代码、`sync` 说 `updated=0`、库里没动」。

所以换季改了 `CURRENT_CAMPUS_YEAR` 之后要显式跑一次：

```bash
uv run python -m jobagent.cli refresh-grad-year --source tencent_join           # 预演
uv run python -m jobagent.cli refresh-grad-year --source tencent_join --apply    # 真写
```

它只写 `grad_year` 一列，不碰 `fingerprint`、不发事件、不动 `last_seen_at`，幂等。
输入取 `snapshots.raw_json` 的最新一条，**不联网**（已实测：805 个共有 id 上，
用快照重算与实时 fetch 零分歧）。见 `docs/plans/008-届别换季刷新与过期告警.md`
（刷新已落地 2026-08-09；届别过期告警仍是草稿）。

**飞书四家现在指的是校招门户**（`website-path`，键第三段就是门户）。
上一版这四家采的是社招池、键是两段的，用户核对时 5 个入口里 4 个对不上 —— 见
`docs/plans/003-校招门户采集.md`。海底捞没有校招门户（`code=-9000003`），
换成了字节跳动。

**`apply_url` 的形状**（这一列的唯一用途是给人点开核对，所以它错等于核对手段本身坏了）：

| 系统 | 形状 | 实测 |
|---|---|---|
| feishu | `https://<host>/<portal>/position/<id>/detail` | 渲染岗位正文（2026-08-10，四租户一致）|
| feishu（无门户的老源）| 退到 `index` 段，同样带 `/detail` | 同上 |
| ~~`https://<host>/<portal>/position/<id>`~~ | **渲染「页面不存在」** | 少 `/detail`。2026-08-10 前的口径，库里 8594 条链接因此全坏，见 `plans/010` |
| ~~`https://<host>/position/<id>`~~ | **404，body 9 字节** | 少门户段。曾被 `003 §4` 当口径写着，核对库 4810 条链接因此全坏 |

**判死活的判据是渲染后的正文，不是 HTTP 状态码。** 这是 2026-08-10 补的一课：
这些页面是客户端渲染 SPA，**不存在的路由照样回 200 而且 body 有 200KB**
（实测 nio 209298 字节，正文却是「您正在寻找的页面不存在」）。
少 `/detail` 那个错能活四天没被发现，就是因为当初的验证只到状态码。

**仍然没验到的那一步**：任何 id（含 `0`/`abc`/乱填）配上门户段和 `/detail`
会不会也渲染出正文，没试过 —— 所以「校招 id 必须配校招门户前缀」
依旧只是按门户构造，**没有独立验证**。

**两个库的分工**（都按 `closed_at IS NULL` 计数）：

- `data/jobagent.db` 主库 —— 飞书四家已于 2026-08-06 17:07 落库（在此之前主库只有腾讯）。
  这是 `cli sync` / `jobs` / `digest` 读写的那一个。
- `data/jobagent-5.db` 核对库 —— `scripts/run_five.py --fresh` 的落点，可以反复重跑。
  核对是要反复做的动作，反复往主库写会搅乱 `first_seen_at` 和 `snapshots`，
  所以 `--fresh` 拒绝主库。

**两个库现在报同一个 9399，这是巧合不是同一个文件。** 核对库跑在 16:50–16:52、
主库跑在 17:07–17:09，隔 17 分钟，岗位池没动。两者是不同文件（inode 不同、
体积 85MB vs 144MB、核对库多 4 轮历史）：

```bash
stat -f '%N inode=%i size=%z' data/jobagent.db data/jobagent-5.db
```

历史数会随重跑变，因为**岗位池自己在动**：核对库 00:56 那轮是 9419，16:50 重跑成 9399；
腾讯主库 08-04 是 795，08-06 重跑成 805。同一个源两次不同的数不等于丢数据。

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
| 届别字段缺失但标题写了「27届」 | 退到标题解析（`grad_years_from_title`），**判定时读、不落库** | 标题上写着的不算「没写」。标题是自由文本，写回 `jobs.grad_year` 会让填充率的分子说不清是哪条通道 |
| 届别字段缺失但**招聘项目名**写了「27届」 | 采集时就由 `feishu._grad_year_from_subject()` 推出并**写回** `grad_year` 列 | 项目名是结构化字段（`job_subject.name.zh_cn`），语义比标题稳，够格落库。判据仍是「必须含「届」字」「永不返回不限」，和标题通道同一档严格度 |
| 标题里出现「不限 / 任意 / any」 | 标题通道**永不返回**「不限届别」 | 撞上过 `Anyscale 平台研发`、`全部业务线-数据分析`。判成「不限」等于任何届别都命中，是把「不知道」洗成「确定命中」 |
| 信息不全的岗位要不要给用户看 | 按维度放宽（`--allow-missing=grad_year`），不是一个布尔开关 | 「只差届别」和「连岗位族都判不出」可信度差得远，混在一个开关里等于逼用户在 612 条和 2533 条之间二选（2026-08-10：放开届别 1952 / 放开族 789）|
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
| `--allow-missing` 到不了代投 | 结构性：`match` 只被 `cli.py` 和测试 import，`jobagent/submitters/` 一个都没有 | 「放宽看什么」被当成「放宽投什么」。放宽只影响可见性，代投的闸门仍是 `confirm_token` |

最后一条是既存结构，不是这次加的守卫，核实命令：

```bash
awk '/match/ && (/import/ || /from/) {print FILENAME":"FNR": "$0}' jobagent/submitters/*.py
```

输出为空即成立。

## 8. 评测指标

| 指标 | 口径 | 当前值（2026-08-09）|
|---|---|---|
| 岗位池宽度 | 库内 `closed_at IS NULL` 条数 | **9401**（主库，2026-08-09 刷新后）。核对库同为 9401 |
| **校招覆盖** | 校招门户抓到条数 / 已接公司的校招岗位总数 | **8594 / 8594**（飞书四家全量）+ 腾讯 807（含 2 条已关闭）。分母是「已接公司」的，不是市场的 |
| 族判不出率 | `job_family IS NULL` / 该源开放条数 | 按源报，**6%~45%**（字节 6 / 商汤 9 / 小鹏 36 / 蔚来 45）。**这是公司属性不是系统属性** |
| 届别填充率 | `grad_year IS NOT NULL` / 该源开放条数 | **必须按源报**（2026-08-10）：腾讯 807/807（`projectId` 分桶**推断的**），飞书 2459/8594（**28.6%**，招聘项目名推出的）—— 拆开是 bytedance 2073/7368、nio 313/634、sensetime 73/161、xiaopeng 0/431（它的届别在标题里，不落这一列）。合成「全库 34.7%」会同时盖掉「腾讯那份是推的」「飞书那份是项目名推的」「小鹏那份在标题里」三件事 |
| 匹配四档 | `classify` 判定 + 按维度放宽后的可见条数 | 严格 **612** / 放开届别 **1952** / 放开岗位族 **789** / 全放开 **2533**（2026-08-10）。三态分布 `hit` 612 / `unknown` 1921 / `miss` 6868，共 9401 条 |
| 误关闭 | 被判关闭但官网仍在招的条数 | 未测量（守卫拦住的次数可从 `runs.status='partial'` 数）|
| 字段填充率 | 非空条数 / 该源条数 | 见 `scripts/run_five.py` 输出 |

**「严格 612 条」的届别是三条通道拼出来的，报数时别当成一件事**（2026-08-10）：
558 条的 `grad_year` 列有值（腾讯是 `projectId` 分桶推的、飞书是招聘项目名推的），
另 **54 条列是空的、靠标题读出来**（全在 `feishu:xiaopeng:campus`，全解析成 27 届）。
按源拆：bytedance 477 / xiaopeng 54 / tencent 41 / nio 31 / sensetime 9。

这一档的分子因此依赖**每家公司把届别写在哪**，换一家公司可能就是 0 —— 字节
7368 条里标题能解析出届别的是 0 条，它的 477 条全靠项目名通道。不许把任何单条
通道的贡献折成全库百分比往外报。

**口径规则（踩过八次）**：任何计数都带测量日期；任何区间都带样本来源
（是哪几个租户/门户）；**报数前先算交集**——两个键不等于两批数据。

## 9. 验收标准

```bash
uv run pytest -q
```

464 passed（2026-08-10，012 落地后；011 落地时是 450）。新功能的验收 =
这个数变大且全绿。

```bash
uv run python -m jobagent.cli jobs --matched --limit 1                              # 612
uv run python -m jobagent.cli jobs --matched --allow-missing=grad_year --limit 1    # 1952
uv run python -m jobagent.cli jobs --matched --allow-missing=job_family --limit 1   # 789
uv run python -m jobagent.cli jobs --matched --loose --limit 1                      # 2533
```

（`--allow-missing` 要带 `=`。）四档条数。**放开届别档和全放开档的数不该随
「加一条观测通道」变**：通道只该把行在档之间搬，不该扩大岗位池（末档涨）也不该
错杀（末档跌）。这个不变量已经兑现两次 —— 004 落地时严格档 41→95、末两档
1952/2533 不动；011 落地时严格档 95→**612**、放开族档 198→**789**，而
1952/2533 **仍然纹丝不动**。

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
| 2026-08-06 | 基线 320→**351**。§5 飞书四家「未落」→已落主库，源表补 campus/intern 拆分与届别填充列、写明腾讯届别是 `GRAD_WINDOW` 推断的、解释 `sources` 6 行与两库同为 9399 的巧合；§2 M3 补 `--allow-missing`；§6 加标题回退/标题不认「不限」/按维度放宽三条；§7 加「`--allow-missing` 到不了代投」；§8 岗位池 9419→**9399**、校招覆盖 8618→**8594**、族判不出率 6%~44%→**6%~45%**、新增届别填充率与匹配三档；§9 补三档命令与不变量 | `plans/004` 已复盘 |
| 2026-08-09 | 基线 351→**400**。腾讯届别改按 `recruitLabelName` 推导（应届类 `26`、在读实习类 `不限`），并补 `refresh-grad-year` 刷新存量。§5 源表补刷新后的届别分布、把「`intern → 27` 是错的」换成已落地的推导规则、写明 `grad_year` 不在指纹里导致 807 行里 804 行静默跳过；§9 验收数 351→**400**。真库 441 行届别刷新后，三档条数 95/1952/2533 纹丝不动（真画像同收 26+27，实习岗从「27 命中」变「不限命中」，一进一出抵平） | `plans/007` 已落地；`plans/008` 刷新已落地、告警仍草稿 |
| 2026-08-10 | **补记两轮只改了页首基线、没留变更行的编辑**：`plans/009`（腾讯届别判据从标签字符串换成 `projectId` 分桶）把基线带到 **401**，`plans/010`（飞书代投 + `apply_url` 少 `/detail` 的 P0）带到 **435**。§5 `apply_url` 形状、§6 登录门那两条当时已同步，漏的是这张表本身 | `plans/009`、`plans/010` 均已复盘 |
| 2026-08-10 | 基线 435→**450**。届别第三通道（招聘项目名 `job_subject`）落地。§5 源表届别填充列整列重写（飞书四家 0/8594 → **2459/8594**，按租户 28/49/45/0%，并写明 nio 的填充**不是**按 `recruit_type` 分的）、腾讯 805→**807** 重测漂移、合计 9399→**9401**；§6 新增「项目名写了届别 → 采集时写回」一条，与标题通道「判定时读、不落库」并列；§8 届别填充率与匹配四档按 2026-08-10 重测（严格 95→**612**、放开族 198→**789**），「严格 95 条里 54 条靠标题」改写成「612 条里 558 条列有值 + 54 条靠标题」并补按源拆分；§9 验收数 435→**450**、补 `--allow-missing=job_family` 一档、`--allow-missing` 标注要带 `=`、不变量补记 011 这一次的兑现 | `plans/011` 已复盘 |
| 2026-08-10 | 基线 450→**464**。`applications` 表开出 CLI 出口（`cli applications`，M7）。库里 14 条投递记录此前**没有任何命令能读**，也就是说「代投卡在哪」的全部证据（拦截原因 + 截图路径）是黑的。§2 命令清单补 `applications` / `refresh-grad-year` 两条（后者是 007 起就有、清单一直没跟上）并写明 M7 只读、故意不给改状态的开关。**同时更正两处此前的事实错误**：①「14 条全部卡在登录门」是错的，实际 **10 条登录门 + 4 条「未找到申请按钮」**，而后者比同一岗位的登录记录更早，是**已修掉的历史**不是现存卡点；②14 次尝试只覆盖 **7 个岗位**（腾讯一个岗位重试 7 次），把 14 读成 14 个岗位是翻倍高估覆盖面。所以 `--funnel` 报分层条数 + 按原因分组 + 每档「最近一次」时间，**不报成功率**（0/14 的分母全是「还没试到那一步」）| `plans/012` 已复盘 |
