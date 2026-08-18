# job-agent 稳定化收口与产品闭环设计

> 日期：2026-08-17
>
> 状态：2026-08-18 已按用户选择的验收方案 1 修订并获批准；Wave 0 的 PR 顺序、CI/#32 顺序、强制门禁、merge-commit 策略，以及 strict/up-to-date 下的逐层主线同步方案均已由用户批准，进入协调计划编制
>
> 对账基准：`fix/plan-021-package-never-installed`，`c2be0c2`
>
> 原始材料：仓库外 `0815归档/校招Agent-MVP方案计划书.md`、`0815归档/校招Agent-结项报告.md`
>
> 实施原则：WIP=1、一个 Issue 一份方案、一个 Issue 一组可独立回滚的改动

## 1. 目标

先把当前工程收成一个可安装、可验证、可安全试用的基线，再完成原 MVP 的主动发现和真人验收闭环。

本设计采用“稳定化收口，再完成产品闭环”，不采用以下两条路线：

- 不继续逐 Issue 打局部补丁，因为岗位身份、画像读取和投递状态已经分别出现成组的同源问题。
- 不先上调度和推送，因为当前仍有“源失败却显示没有新增”“画像缺失却肯定命中”等失败形状；主动推送会放大而不是消除它们。

## 2. 已确认的产品口径

### 2.1 “完成”分四级

以后所有状态声明都使用以下四级，不再用一个“已完成”混写：

1. **代码已实现**：目标行为存在于当前工作分支。
2. **自动测试已验证**：相关测试和全量测试通过。
3. **目标环境已验证**：干净交付副本、真实客户端或真实页面按适用范围通过。本文的“干净交付副本”指本地 `git archive` 隔离目录和 CI 对精确候选 commit 的 clean checkout；两者都不能读取工作树中的未跟踪文件。
4. **产品验收已通过**：原 MVP 的真人、连续运行和时间节省指标通过。

`README.md` 的“M1–M7 已完成”需要按这四级重写。当前可以声称工程模块已建立，不能声称原 MVP 已完成。

### 2.2 原 MVP 验收仍是最终判据

产品闭环必须回到原计划书的验收，而不是用测试条数替代：

- 三家公司连续三天增量无漏报、无重大误报。
- 用户不再需要手动巡检官网。
- 未覆盖公司是一份短而可枚举的名单。
- 至少两家公司的受监督真实代投跑通；用户只处理确认和验证码。
- 从收到推送到完成投递不超过三分钟。
- 3–5 名外部试用者中有人主动要求继续使用或扩公司。

“本仓库作者正在使用”不等于“外部产品验证已完成”。

## 3. 当前事实与根因

### 3.1 仓库与交付状态

- `main` 尚未承载当前 0.2.0 实现；当前能力位于 10 个线性叠加 PR。
- GitHub 当前有 17 个开放 Issue；其中 #9、#11、#14 已在当前叠加分支实现，但尚未进入目标主线并关闭。
- 本地工作目录执行 `uv run pytest -q` 全量通过；当前用例数仍只以 README 的唯一出处和命令输出为准。
- 2026-08-18 在 `cca2123`（相对 `c2be0c2` 只新增本设计文档）执行 `git archive HEAD`，在干净目录运行 `uv sync --frozen` 与 `uv run pytest -q`，结果为 4 个失败、859 个通过和 2 个错误。结项报告中的“5 条红”是更早一次零克隆实验的摘要，不作为本设计的当前数字。
- 干净环境失败分别来自真实数据库、未提交画像以及画像缺失改变 MCP 测试语义。
- 仓库没有 CI，因此本机全绿不是可移植基线。

### 3.2 MVP 模块状态

| 模块 | 当前状态 | 未完成的闭环 |
|---|---|---|
| M1 采集 | 工程实现覆盖 2 种 ATS、5 家公司；默认文档路径只让用户直接到达 1 家 | 补齐源登记路径；连续三天漏报/误报验收 |
| M2 归一增量 | 工程能力超过初版 | partial/failed 未进入用户事件流 |
| M3 用户档案 | 身份、教育、部分 intent | 公司池、折叠段、统一加载语义 |
| M4 匹配订阅 | 匹配已实现 | 订阅、调度、主动输出 |
| M5 交互调度 | CLI/MCP 查询 | 定时触发和通知通道 |
| M6 代投 | 单岗位两阶段原型 | 批量、开放题、折叠段、真实 execute 验证 |
| M7 投递记录 | 已有只读出口 | 上游重复、额度和状态机仍有缺陷 |

### 3.3 架构根因

1. **岗位身份的 interface 与数据库约束不一致。** 数据库身份是 `(source_key, external_id)`，CLI/MCP 却用裸 `external_id`。
2. **画像读取没有单一 interface。** `profile.py`、`match.py`、CLI 和 MCP 对扁平/嵌套格式、文件缺失和空 intent 的语义不同。
3. **投递状态机没有单一所有者。** 配额、重复投递、中间态、警告和确认分散在 CLI、DB 与 Submitter。
4. **MCP 的只读声明与浏览器体检实现冲突。** `check_form_selectors` 可启动带登录态浏览器并短暂创建可提交 token。
5. **运行成功与用户可见事件没有闭合。** `runs` 能记录 partial/failed，但事件消费者会把它解释为“没有新增”。

## 4. 目标架构

不做一次性全仓重构。每个新 Module 只在对应 Issue 被处理时提取，并让旧调用方迁移到同一个 interface。

### 4.1 `JobRef` Module

**职责**：表示并解析唯一岗位引用。

**Interface**：

```text
JobRef(source_key, external_id)
resolve_job(conn, JobRef) -> Job
resolve_external_id(conn, external_id, optional_source) -> Job | JobNotFoundError | AmbiguousJobError
JobRef.cli_args() -> ["--source", source_key, "--job", external_id]
```

规则：

- 持久化和内部调用一律使用 `(source_key, external_id)`。
- 用户只给 `external_id` 且库内唯一时可以兼容。
- 零行明确返回 `JobNotFoundError`；出现多行时必须要求 `--source`，并按 `(source_key, internal_job_id)` 稳定排序列出候选源，不得由 SQLite 随意选一行。
- MCP 始终返回结构化 `{source_key, external_id}`；CLI 和投递记录始终同时输出两个字段及经过 shell escaping 的 `--source … --job …` 参数对，不把可能含冒号的 `source_key` 再拼成一种歧义字符串。

它收口 #24 和 #33，不改变 ATS Adapter 的注册方式。

### 4.2 `ProfileContext` Module

**职责**：成为画像格式、加载状态和隐私投影的唯一解释者。

**Interface**：

```text
load_profile(path) -> ProfileContext.loaded | incomplete | missing | malformed
ProfileContext.intent() -> 只含允许筛选字段
ProfileContext.form() -> 仅投递路径可用的表单资料
```

规则：

- 新示例统一使用嵌套 `intent:`，存量扁平格式在迁移期兼容并给出提醒。
- 缺画像时不得把岗位判成确定命中。
- 画像存在但 `intent` 缺失或为空时返回 `incomplete`；匹配和 digest 不得把它解释为“命中全部”。无筛选浏览只能通过明确的非 matched 路径进行，并标注“未按画像筛选”。
- CLI 给出可执行的建档指引；MCP 返回明确的 missing/unknown 和 notes。
- MCP 永远拿不到 identity/form 内容，只有白名单 intent 能过 seam。

它收口 #25 和 #26，并为后续公司池扩展提供唯一入口。

### 4.3 `ApplicationWorkflow` Module

**职责**：拥有从选岗到投递终态的完整状态机；CLI 只负责输入和渲染。

**Interface**：

```text
prepare(JobRef, ProfileContext) -> SubmissionPlan
confirm(confirm_token) -> SubmissionResult
abandon(confirm_token) -> ApplicationRecord
reconcile(application_id, ReconciliationEvidence | None) -> ApplicationRecord
StatusProbe.observe(JobRef) -> ReconciliationObservation
```

状态和约束：

- 状态机主路径是 `reserved → prefilled → submitting → submitted | duplicate | closed | blocked | unknown`；`reserved/prefilled` 还可进入 `closed | blocked | abandoned | expired`，`unknown` 只能经 `reconcile` 进入有证据支持的终态。
- `prepare` 前在同一数据库事务中完成同岗位重复闸门、公司额度预留和 `reserved` 记录；并发调用只能有一个获得有效预留。
- `prefilled` 是等待人工确认的真实占用状态，必须有 TTL、崩溃恢复和可观察结果。
- 用户确认后，必须先以 `expected_state=prefilled` 原子转换到持久化的 `submitting`，再点击外部提交。状态不匹配时不得继续。
- Adapter 只返回页面观测，并独立标注外部动作证据 `not_attempted | attempted | indeterminate`；由 Workflow 统一映射业务状态，不能仅凭 `closed/failed` 等页面文案推断外部动作没有发生。
- `submitting → blocked/closed` 只允许在 `not_attempted` 证据证明外部提交动作尚未发生时使用；点击后、动作证据为 `attempted/indeterminate`，或进程崩溃后无法证明“未发送”的结果一律进入 `unknown`，不能叫“提交失败”，并禁止自动重投。
- `reconcile` 可调用 `StatusProbe` 检查源站投递记录，或接收人工 `ReconciliationEvidence(conclusion, actor, observed_at, source_url, artifact_ref)`；`conclusion` 只能是 `application_exists | duplicate_exists | no_application`，必须附操作者与可追溯证据，不能裸传目标状态。`application_exists/duplicate_exists` 分别映射为 `submitted/duplicate`；只有 `no_application` 才可结合岗位现状映射为 `closed/blocked`。单独看到“岗位已关闭”不能证明未投递，仍保持 `unknown`。
- 每次启动以及任何新的 `prepare/confirm` 前扫描超过租约的 `submitting`，原子转成 `unknown` 并进入待对账队列；恢复流程无证据时不得猜测为未提交。
- `submitted/duplicate` 消耗公司额度；`unknown` 在对账完成前继续占用预留；只有外部动作证据为 `not_attempted` 或对账证明确未产生投递的 `blocked/closed/abandoned/expired` 才释放预留。状态转换和额度变化必须原子提交。
- 所有未填字段和页面校验进入 `warnings`；人工确认界面必须渲染。
- 额度、重复、确认 token 和状态转换由该 Module 统一判断，Submitter Adapter 不自行解释业务状态。
- application 状态转换和对应 event 必须在同一事务提交，或通过同一事务写入 outbox；不得先提交状态、再尝试追加事件。
- 每次转换都记录旧状态、触发事件、操作者、时间和证据；非法转换明确失败，不静默覆盖。

它收口 #27、#28、#29；具体 Issue 仍分开实现和回滚。

### 4.4 `SyncWorkflow` Module

现有 `ingest.sync()` 是这个 Module 的主体，不为了改名新建一层。

新增的 interface 约束：

- 调用方选择的 `source_key` 必须等于 Adapter 最终生成的 `source_key`。
- 已登记源的 company/tenant/entry_url 不能被一次同步静默改成另一家公司。
- `sync()` 对所有结束路径返回稳定的 `SyncResult(status, fetched, changes, warnings, error, run_id)`；单源失败不得只靠异常或终端文案表达。
- `ok`、`partial`、`failed` 都产生明确、可消费的结果；后两者进入事件流并在 digest/通知中置顶，批量 CLI/调度只要任一源失败就返回非零总体状态。
- `run` 终态和对应的 `source_degraded/source_sync_failed` event 必须在同一事务提交，或在同一事务写入 outbox；不得先结束 run 再尝试追加故障事件。即使收尾失败，调用方仍必须收到稳定 `SyncResult`，不能退化成只有异常。
- 主业务提交成功但 `run` 收尾失败时，必须留下可识别的 stale/running 记录；后续状态检查按租约或进程证据完成对账，不得继续展示上一轮 `ok`。
- “没有岗位变化”和“本轮没有可信数据”必须是两个不同输出。

它收口 #22 和 #30。#23 的迁移幂等先于该 Module 的任何写操作修复。

### 4.5 保留的 seam

- `routing.py` 继续按 ATS 系统注册 Adapter；租户和门户是实例参数。
- `queries.py` 继续作为 CLI/MCP 共用的只读查询 Module，但改为消费 `JobRef` 和统一后的 intent。
- Submitter Adapter 只负责页面交互，不拥有额度、重复投递或产品状态定义。

## 5. MCP 安全边界

MCP 的目标边界是严格只读：

- 不提供 `prepare`、`execute`、画像 identity 更新或任何状态写入。
- `check_form_selectors` 从 MCP 注册表移除；体检暂时保留在 CLI。
- 后续若重新开放浏览器体检，必须先有一个独立 `check()` interface：不能 mint token、不能写 `SESSIONS`、不能接受模型给出的任意目录，只能使用人工配置的目录键。
- 所有工具拒绝未知参数；不存在的 `grad_year` 等筛选能力不得出现在文档里。
- 返回值携带数据新鲜度、缺画像和排除范围，禁止用“正常空结果”掩盖降级。

在 #32 落地前，不从 MCP 调用 `check_form_selectors`，也不把当前六工具状态描述成“严格只读”；代码边界只有在该工具移出注册表并通过守卫测试后才成立。

这一定义优先于结项报告中“把可撤销 prepare 提到 MCP”的建议；该建议保留为待真人验证的产品假设，不进入当前主线。

## 6. 实施波次与依赖

本设计是项目级路线，不允许作为一个大改动实施。用户复核后，先写 Wave 0 协调计划，只记录顺序、门槛和子 Issue；每个可写改动继续单独使用 `docs/plans/NNN-*.md`，协调计划本身不承载实现。W0-00 文档 PR 统一版本化本规格、协调计划、六份逐一批准的子计划和逐 PR 审计台账，任何业务代码、workflow 或工作区规则实现仍进入各自执行载体，不混入 W0-00。

### Wave 0：基线治理

Wave 0 是协调波次，不是一个大 PR。每个可写改动仍绑定一个 Issue、一份方案和一组可独立回滚的提交，活动 WIP 始终为 1。已经到达 `fixed-on-branch`、因硬门槛等待验证/合入的冻结分支不占活动 WIP，但在重新取得唯一活动槽位前不得继续修改；协调计划必须显式列出所有冻结项，避免用“暂停”暗中并行开发。

1. 在 #32 落地前，把“不调用 `check_form_selectors`、不把当前 MCP 描述成严格只读”作为临时运行禁令；该禁令不等于代码边界已修复。
2. 按真实 base/head 依赖只读审计 PR：#1 → #15 → #10 → #12 → #16 → #17 → #18 → #19 → #20 → #21。此时不得向 `main` merge，先记录每个 PR 的目标分支、对应 Issue、`base SHA + head SHA`、平台生成的 merge candidate SHA 和未验证项；任何 retarget、前序合入或 head 变化都会产生新候选，必须重新运行门槛验证。
3. **CI 先行。** 为测试自包含与 CI 建独立 Issue/方案，并从 PR #1 head 建分支、以 `release/0.2.0-two-phase-apply` 为父目标：真实数据库/真实页面检查改成显式 opt-in 集成测试；默认测试不得依赖 gitignore 文件。GitHub Actions 只使用 `pull_request`，覆盖 `opened/synchronize/reopened/edited`，目标包含该 release 父分支和 `main`，不使用路径过滤；验证平台生成的 merge candidate，并记录 base/head/merge SHA。禁止 `pull_request_target`、secrets 和写权限，顶层 `permissions` 只能是 `contents: read`，外部 Action 固定到完整 commit SHA。
4. CI PR 首次产生稳定 check 名后，对 `release/0.2.0-two-phase-apply` 和 `main` 启用 required status check，要求分支保持最新，管理员不得绕过。CI 变更自身必须在本地干净归档和其 PR merge candidate 的 workflow 上通过，才可使用 **Create a merge commit** 合入 release 父分支；没有远端强制规则时只能称人工门禁，不得称硬门槛。
5. CI 生效后处理 #32：从更新后的 PR #1 head 建修复分支，以 `release/0.2.0-two-phase-apply` 为父目标；从 MCP 注册表和面向用户的工具文档移除 `check_form_selectors`，增加注册表守卫测试，不重新设计浏览器体检。#32 候选通过本地干净归档与 required check 后，使用 **Create a merge commit** 合入父分支；此时 PR #1 的新候选同时包含 CI 与安全收口。
6. **全链合入硬门槛**：本地干净归档和 CI clean checkout 在精确候选 commit 上全部通过之前，任何 PR 都不得进入 `main`。PR #1 及后续各层一律使用 **Create a merge commit**；前序合入后先运行 `git merge-base --is-ancestor <前序获批 head SHA> origin/main`，返回 0 才能把下一层 retarget 到 `main`。由于 required check 使用 strict/up-to-date，retarget 后还必须锁定最新 `origin/main` SHA，在隔离 worktree 中把这个精确 SHA 以普通 merge commit 合入当前 topic head；禁止 rebase、force-push 和冲突自动裁决。验证锁定的 main 与原 topic head 均为新 head 的祖先后，展示同步 commit、影响范围和回退方式，取得该次普通 push 的单独批准。push 后同时 readback main/topic；若 main 在窄竞态中已移动，push 已发生但证据立即作废，从新 head 重新同步，不得继续候选 Gate。只有 readback 一致时才清空旧 base/head/merge/check/archive/merge-approval 证据，再为新 head 生成候选并逐个合入。若 topic 远端 head 改变、发生冲突或 push 非 fast-forward，立即停线；不得降低 strict 门禁绕过。W0-00 文档 PR 最终 retarget `main` 时遵循同一同步协议。
7. 为技术反馈台账与文档规则分别建立小范围 Issue/方案，依次落地完成四级、WIP、模板、README、索引和交接；区分“开放未修”“已在未合并分支修复”“已进入 main 并验证”。

Wave 0 完成前不开发调度、批量投递或新 ATS。

### Wave 1：数据、身份和画像 Interface

顺序：#23 → #22 → #24 → #25 → #26。

- #23 先做，因为每个 `db.init()` 都可能重复迁移并污染额度。
- #22 再封住整源写错公司的路径。
- #24 再统一 `JobRef`，为投递和查询提供稳定身份。
- #25/#26 随后统一 `ProfileContext`、缺失/空 intent 语义和建档路径；这是 `ApplicationWorkflow` 的前置 Interface，不得延后到投递状态机之后。

### Wave 2：投递安全

顺序：#27 → #28 → #29。

- 先阻止重复投递和错误诱导。
- 再实现额度预留、并发和崩溃恢复。
- 再保证确认界面展示 warnings。
- #32 的最小禁用已经在 Wave 0 完成；若以后恢复浏览器体检，另写安全设计，不纳入当前 MVP。

### Wave 3：用户可达性

顺序：#31 → #33 → 目标公司池与源登记可达性。

- 严格拒绝 MCP 未知参数。
- 让 CLI/MCP 都交付可复制的 `JobRef`。
- 为目标公司池建立独立 Issue/方案，通过 `ProfileContext` 的唯一入口落地。
- 补齐飞书公司源的可复制登记步骤和验证，使“工程覆盖 5 家”转为默认用户路径可达；未验证的公司不得计入产品覆盖。

### Wave 4：主动发现闭环

1. #30：partial/failed 进入事件流和用户输出。
2. 增加单机调度；MVP 只选一个最小本地通知通道。
3. 在观察开始前冻结候选版本、三个公司源和目标岗位判定规则；另建独立真值台账，由非试用用户的验收人员通过官网人工走查或官方数据快照记录岗位 URL、源站 ID 和时间戳，不复用 job-agent 的采集或匹配结果。
4. 连续三天运行，把每轮输出与同期独立真值逐项对照，记录每个源的成功、降级、漏报、误报和新鲜度；没有真值记录的日期不计入三天窗口。
5. #8/#13 只处理三天验收中真实影响目标岗位的分类缺口，不为追求全库百分比继续堆词表。任何会改变漏报或误报结果的修复都使旧观察失效，修复后的候选版本必须重新连续运行三天。
6. 观察期结束后，由主验收用户确认是否已无需手动巡检官网，并列出仍关心但未覆盖的公司；不能形成短而可枚举的名单即不通过。独立验收人员建立真值不算主验收用户的日常巡检。

### Wave 5：受监督产品验收

1. 选择两家公司的真实岗位，用户逐字段确认并明确授权最终提交；可以来自同一 ATS，也可以来自不同 ATS。
2. 计时验收前，所选两家公司必须已覆盖其必填折叠段和开放题，或经真实页面确认不存在这些要求；用户除确认和验证码外仍需手填，即判定本轮不通过并先建立独立 Issue 修复。
3. 记录从收到推送到投递结束的完整墙钟时间、人工补填项、验证码耗时和失败状态；三分钟覆盖这段完整用户旅程，不排除验证码等待时间。只有页面证据与持久化状态都确认 `submitted` 的投递才计为本项通过；任何非 `submitted` 状态即使更快结束也只记失败证据，不得计入两家公司或三分钟指标。
4. 让 3–5 名外部试用者完成建档、发现和至少一次投递或明确放弃，并记录其是否主动要求继续使用或扩公司；至少一人出现该主动信号才通过产品验证。
5. 只有验收通过后，才设计批量队列、非验收公司所需的开放题泛化、更多 ATS 和外部 IM 通道。

## 7. 错误处理原则

| 场景 | 必须输出 | 禁止退化成 |
|---|---|---|
| 缺画像 | 建档指引或 `profile=missing` | “命中全部岗位” |
| 未知筛选参数 | 明确参数错误 | 忽略参数后返回正常结果 |
| 岗位引用歧义 | 候选源和 `--source` 指引 | 任取一行 |
| 同步 partial/failed | 置顶告警和原因 | “今天没有新增” |
| 提交结果无法判断 | 状态未知、先去源站核实 | “提交失败，请重试” |
| 人工确认前有缺项 | warnings 明文展示 | ready 但静默隐藏 |
| 干净环境缺真实数据 | 跳过显式集成测试并说明命令 | 默认测试报错 |

## 8. 验证策略

每个 Issue 都必须同时有以下证据：

1. 最小复现测试在修复前失败。
2. 修复后目标测试通过。
3. 相关反向用例和半修测试通过。
4. `uv run pytest -q` 在当前工作目录通过。
5. 任何 PR 合入 `main` 前，精确候选 commit 必须在 CI 中完成 `uv sync --frozen` + 自包含全量测试并通过。
6. 涉及真实页面、MCP 客户端或连续运行的结论，必须另附人工验收；单元测试不能代签。
7. 连续运行验收期间，只要修改会影响漏报、误报、新鲜度或通知行为，旧观察窗口作废，候选版本从零重新累计三天。

Issue 状态遵循：

```text
open/unfixed
  → fixed-on-branch
  → merged-to-target
  → verified-on-target
  → closed
```

不得从“本地通过”直接跳到“已关闭”。

## 9. 文档与流程规则落位

本节全部是 Wave 0 待落地项，不是当前文件状态声明；只有对应文件进入目标分支并按命令验证后，才能改记为“已落地”。

### 9.1 工作区 `../CLAUDE.md`

新增长期协作规则：

- 每次恢复工作先声明当前主线、唯一活动 Issue、分支、未验证项。
- WIP=1；不得因相邻问题擅自改主线。
- 五类决策必须用户拍板：不可逆外部动作、安全/权限边界、业务口径、与既有 Issue/文档冲突、多方案选型。
- 【等我拍板】和【只有我能做的事】必须进入方案和交接。
- 同类问题第二次出现时，先解释旧修法为何没挡住，再升级模板或硬守卫。
- 不得用总结或“更好的交付物”替代用户明确要求的原始产物。

### 9.2 仓库 `CLAUDE.md`

新增仓库级规则：

- 完成状态使用代码/测试/目标环境/产品验收四级。
- 安装、默认路径、画像和测试基线变更必须跑本文定义的干净交付副本验证。
- 岗位内部身份统一为 `(source_key, external_id)`。
- MCP 严格只读，不得创建可提交会话或接受任意登录态目录。
- Issue 在目标分支验证前不得关闭；“已在叠加分支修复”单独标记。

### 9.3 模板和清单

更新 `docs/plans/_TEMPLATE.md` 与 `_VERIFICATION_CHECKLIST.md`：

- 主线对齐与 WIP 检查。
- 关键决策审批记录。
- 命令、预期结果、偏差含义三件套。
- 干净交付副本/目标环境/人工验收。
- 重复问题的旧修法、漏守方向和根因升级。
- 【等我拍板】与【只有我能做的事】。

### 9.4 反馈台账和交接

- 新增 `docs/reviews/2026-08-15-technical-feedback.md`，记录来源、证据、状态、Issue/Plan 和下一门槛。
- `docs/README.md` 索引该台账。
- 修订并纳入 `AGENT_HANDOFF.md`，修复乱码，避免复制动态测试数作为永久事实。
- 原始 HTML 和归档 Markdown 保留在仓库外，不提交 Git。

## 10. 明确不做

- 不在 Wave 0 顺手修任何业务 Issue。
- 不一次性重写 `cli.py` 或 Submitter。
- 不把 MCP 扩成可写或可 prepare 的 Agent。
- 不先做云端、计费、多用户、邮件解析、微信或小程序。
- 不用批量投递绕过逐个人工确认。
- 不把结项报告中的商业化和工具重切建议写成当前事实。

## 11. 下一份实施计划的范围

用户复核本规格后，下一份文档只写 **Wave 0 协调计划**。它必须列出：

1. WIP=1 的子 Issue 顺序、负责人、目标分支、`base/head/merge candidate` 组合和停止条件；#32 的目标分支与接入顺序必须明确。
2. #32 最小禁用、干净测试/CI、逐 PR 合入、规则与台账分别对应哪一份独立方案。
3. “本地干净归档 + CI clean checkout 在精确候选 commit 上通过”这一不可跳过的合入门槛。
4. 每个子 Issue 的完成状态只按 `fixed-on-branch → merged-to-target → verified-on-target` 迁移。
5. strict/up-to-date 下每层 retarget 后的“锁定 main → merge main 到 topic → 单独批准普通 push → 清空旧证据 → 重建候选”协议，以及冲突、main/topic 移动和非 fast-forward 时的停止条件。
6. 六份子计划和逐 PR 审计台账统一由 W0-00 文档 PR 版本化；每份计划批准并完成远端 readback 后才能创建对应实现载体，W0-00 最终 diff 不含实现文件。

协调计划获批后，第一个可写计划只处理 **测试自包含与 CI**；CI required check 和远端门禁生效后，第二个可写计划才处理 **#32 从 MCP 注册表移除**。两者不得同时修改。Wave 1–5 只保留依赖顺序，不在 Wave 0 提前实现。
