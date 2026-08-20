# job-agent 稳定化收口与产品闭环设计

> 日期：2026-08-17
>
> 状态：2026-08-18 的稳定化方向、merge-commit、strict/up-to-date 与强制门禁已获批准；2026-08-19 用户进一步批准路线 A：冻结 Transaction Gate 候选，先核验 MCP 实际暴露，暴露时把 #32 紧急注销移到 CI 前，并在 W0-03 后转为发现闭环优先。本次精确文字仍须固定 SHA Review Gate 后方可应用
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

MCP 的目标边界是严格只读，并以运行时注册表而不是提示词或工具名作为安全 seam：

- 不提供 `prepare`、`execute`、画像 identity 更新或任何状态写入。
- `check_form_selectors` 从 MCP 注册表和面向用户的工具文档移除；CLI `checkup` 的浏览器体检 Implementation 保留。
- MCP Module 不再导入 `routing` 或 Submitter，不接受 `user_data_dir`、cookie、浏览器 profile、截图目录等登录态输入；五个保留工具只读本地数据库与白名单 intent。
- 后续若重新开放浏览器体检，必须另立安全设计和独立 `check()` Interface：不能 mint token、不能写 `SESSIONS`、不能接受模型给出的任意目录，只能使用人工配置的目录键。
- 所有工具拒绝未知参数；不存在的 `grad_year` 等筛选能力不得出现在文档里。
- 返回值携带数据新鲜度、缺画像和排除范围，禁止用“正常空结果”掩盖降级。

2026-08-19 的只读核验已确认：本机 Claude 的项目配置与 Desktop 配置都注册了 `job-agent`，Claude 同时保持两组 `python -m jobagent.mcp_server` stdio 进程；运行时注册表仍有六个工具，且 `check_form_selectors` 强制接收 `user_data_dir`。因此“禁止调用”的文字禁令已被证实不是物理隔离。

在 #32 进入 `main` 并完成真实客户端五工具 readback 前执行两层收口：

1. **本机临时隔离**：先由 W0-00 把唯一 active 写租约交给 W0-02 并 readback，再记录并完整退出 Claude，确认旧 app PID 和后代全部消失；只在 app 关闭态从项目配置和 Desktop 配置同时移除 `job-agent` 条目。每份写后立即校验全文 SHA/JSON pointer，两份均通过才可重开 Claude；新 app PID 必须不同于旧 PID，两客户端列表无该 server，连续三次结构化进程树/全局 argv 扫描均无 `jobagent.mcp_server`。只改一处、app 未退出就写、写失败后重开，或仍看到任一模块进程，都不算隔离成功；W0-02 必须以 `active/local-isolation-partial` 持续占槽。若重开后 readback 失败，同一批准包只允许再退出新 app 一次并确认相关进程为零，随后停线。
2. **仓库永久收口**：#32 从注册表删除工具与 Submitter seam，并用注册表、不可调用、敏感入参和导入图守卫阻止回归。本机隔离不代签代码修复；代码合入 release 也不自动授权恢复客户端。

恢复 MCP 必须等 #32 随 PR #1 进入并验证于 `main`，再对一个明确客户端、一个已验证 clean checkout 单独设计和批准；不得自动恢复原来的双配置，也不得重新暴露第六个工具。

这一定义优先于结项报告中“把可撤销 prepare 提到 MCP”的建议；该建议保留为待真人验证的产品假设，不进入当前主线。

## 6. 实施波次与依赖

本设计是项目级路线，不允许作为一个大改动实施。用户复核后，先写 Wave 0 协调计划，只记录顺序、门槛和子 Issue；每个可写改动继续单独使用 `docs/plans/NNN-*.md`，协调计划本身不承载实现。W0-00 文档 PR 统一版本化本规格、协调计划、六份逐一批准的子计划和逐 PR 审计台账，任何业务代码、workflow 或工作区规则实现仍进入各自执行载体，不混入 W0-00。

### Wave 0：安全与可交付基线

Wave 0 是协调波次，不是一个大 PR。每个可写改动仍绑定一个 Issue、一份方案和一组可独立回滚的提交，活动 WIP 始终为 1。已经到达 `fixed-on-branch`、因硬门槛等待验证/合入的冻结分支不占活动 WIP，但在重新取得唯一活动槽位前不得继续修改；只读审计、测试矩阵和后续依赖调查可以并行，不得并行修改第二个分支、计划或远端状态。

2026-08-19 的实际暴露核验触发以下批准顺序；稳定 Key 不改名，但执行序列改为 **W0-02 → W0-01 → W0-03**：

本次路线修订本身必须先由 W0-00 独占 active：规格、协调计划与 Plan 023 三个固定 SHA 经 Review Gate/批准后形成 allowlisted 文档 commit，普通 push 与 PR #35 live-state readback 分别批准并完成。只有 W0-00 随后 frozen，才允许用另一条精确 live-state 更新把唯一写租约持久交给 W0-02；不得先改客户端配置再补文档真值。

1. **先做本机临时隔离。** W0-00 先通过批准/readback 的 live-state 更新把唯一 active 写租约交给 W0-02；再完整退出 Claude，记录旧 app PID/后代并确认它们全部退出。只在 app 已停止时，对项目配置和 Desktop 配置使用同一精确 allowlist 事务。写前锁定两文件 SHA，每份写后立即解析 JSON/核对候选 SHA；只有两份均通过才可重开 Claude。重开后断言新 app PID 不同于旧 PID、两个客户端 registry 无 `job-agent`、连续观测无 `jobagent.mcp_server` 后代或孤儿进程。任一失败都让 W0-02 以 `active/local-isolation-partial` 持续占槽；重开后失败只允许再退出新 app 一次并读回进程为零，随后只读报告，不得自动重试、补偿、恢复或继续实现。
2. **#32 紧急注销先行。** 从刷新后且彼此相等的 PR #1 head / release head 建 `fix/issue-32-mcp-read-only`，目标仍是 `release/0.2.0-two-phase-apply`。exact base 只改已存在的 `jobagent/mcp_server.py` 与 `tests/test_mcp_server.py`，从 MCP 注册表和守卫测试移除 `check_form_selectors`；保留 CLI `checkup`，不从后续叠加栈复制文档，不设计新浏览器 Interface，不改 Submitter、`SESSIONS` 或投递状态机。
3. **#32 使用 pre-CI 手工差分门槛。** 因 required check 尚不存在，不能伪称 CI 通过。精确 base 与 candidate 都在独立 `git archive` 中执行 `uv sync --frozen` 和默认全量测试；candidate 不得新增任何失败/错误，新增和保留的 MCP 安全边界 nodeid 必须零失败。整份 `tests/test_mcp_server.py` 在 clean archive 中已知有一个属于 W0-01 的画像缺失基线红，只允许该精确 nodeid 在 base/candidate 中保持同形失败，不得由 #32 越界修复。再锁定 base/head/merge SHA、diff allowlist、隐私扫描和人工 merge 批准，使用 **Create a merge commit** 合入 release。#32 保持 open，本机隔离保持有效，直到该修复随 PR #1 进入并验证于 `main`。
4. **随后 W0-01 建立可移植测试与 CI。** 从已包含 #32 的最新 PR #1/release head 建 `chore/wave0-clean-ci`：真实数据库/真实页面检查改成显式 opt-in；默认测试不得依赖 gitignored 文件。GitHub Actions 只使用 `pull_request`，覆盖 `opened/synchronize/reopened/edited`，目标包含 release 与 `main`，不使用路径过滤；验证平台生成的 merge candidate 并记录 base/head/merge SHA。禁止 `pull_request_target`、secrets 和写权限，顶层 `permissions` 只能是 `contents: read`，外部 Action 固定到完整 commit SHA。
5. CI PR 首次产生稳定 check 名后，对 release 与 `main` 启用 required status check，要求 strict/up-to-date、管理员不得绕过。CI 变更自身必须在本地干净归档和其 PR merge candidate 的 workflow 上通过，才可使用 **Create a merge commit** 合入 release；没有远端强制规则时只能称人工门禁。
6. **W0-03 全链合入。** 先按真实 base/head 依赖只读审计 `#1 → #15 → #10 → #12 → #16 → #17 → #18 → #19 → #20 → #21`。PR #1 的新候选必须同时包含已验证的 #32 与 CI。PR #15 首次引入 `docs/MCP_SETUP.md`；处理该层前必须单独展示/批准只把当前工具清单同步为五工具的兼容补丁，禁止临场顺手修改。各层一律使用 **Create a merge commit**；前序合入后验证其获批 head 已成为 `main` 祖先，再 retarget 下一层。strict/up-to-date 下继续执行“锁定 main → merge main 到 topic → 单独批准普通 push → 清空旧证据 → 重建候选”；冲突、main/topic 移动或非 fast-forward 都立即停线。
7. **W0-03 后立即建立快速产品反馈检查点。** 从干净 clone 安装，连接真实 MCP 客户端，只验证五个严格只读工具；不调用浏览器、登录态或真实投递，也不把演示成功冒充连续三天或 Wave 5 验收。
8. W0-04–W0-06 继续作为治理 lane，但不再阻塞产品纵向切片。它们仍受唯一写 WIP 约束、各自单独审批和目标验证；W0-00 可保持 frozen/open，待治理 lane 完成后再收口自身文档 PR。

安全 tranche（本机隔离、W0-02、W0-01、W0-03、五工具 checkpoint）未完成前，不开发 Wave 1–5。安全 tranche 通过后，产品工作不再等待 W0-04–W0-06。

### Wave 1：数据、身份和画像 Interface

顺序：#23 → #22 → #24 → #25 → #26。

- #23 先做，因为每个 `db.init()` 都可能重复迁移并污染额度。
- #22 再封住整源写错公司的路径。
- #24 再统一 `JobRef`，为投递和查询提供稳定身份。
- #25/#26 随后统一 `ProfileContext`、缺失/空 intent 语义和建档路径；这是 `ApplicationWorkflow` 的前置 Interface，不得延后到投递状态机之后。

### Wave 2：发现准备与用户可达性

顺序：#31 → #33 → 目标公司池与源登记可达性 → #30。

- 严格拒绝 MCP 未知参数，避免调用方以为筛选已生效。
- 让 CLI/MCP 都交付可复制的 `JobRef`。
- 为目标公司池建立独立 Issue/方案，通过 `ProfileContext` 的唯一入口落地。
- 补齐飞书公司源的可复制登记步骤；未验证的公司不得计入产品覆盖。
- 让 partial/failed 进入事件流和用户输出；“没有变化”与“没有可信数据”必须可区分。

### Wave 3：主动发现闭环

1. 增加单机调度；MVP 只选一个最小本地通知通道。
2. 在观察开始前冻结候选版本、三个公司源和目标岗位判定规则；另建独立真值台账，由非试用用户的验收人员通过官网人工走查或官方数据快照记录岗位 URL、源站 ID 和时间戳，不复用 job-agent 的采集或匹配结果。
3. 连续三天运行，把每轮输出与同期独立真值逐项对照，记录每个源的成功、降级、漏报、误报和新鲜度；没有真值记录的日期不计入三天窗口。
4. #8/#13 只处理三天验收中真实影响目标岗位的分类缺口。任何会改变漏报、误报、新鲜度或通知行为的修复都使旧观察失效，修复后的候选版本必须重新连续运行三天。
5. 观察期结束后，由主验收用户确认是否已无需手动巡检官网，并列出仍关心但未覆盖的公司；不能形成短而可枚举的名单即不通过。

### Wave 4：投递安全

在写真实投递状态前，先经用户明确授权，对两家目标公司的真实表单做“不提交”的字段走查，确认必填折叠段、开放题、验证码和成功/重复观测点；登录态和页面内容不得进入 Git 或日志。

实现顺序：#27 → #28 → #29。

- 先阻止重复投递和错误诱导。
- 再实现额度预留、并发、崩溃恢复和 `unknown → reconcile`。
- 再保证确认界面展示 warnings。
- #32 的最小禁用已经在 Wave 0 完成；若以后恢复浏览器体检，另写安全设计，不纳入当前 MVP。

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

本次路线修订生效后，协调计划必须明确：

1. 本机 Claude 双配置隔离的前置 SHA、typed pointer、先退出后写入的精确顺序、旧/新 app PID 与后代证据、两客户端 registry readback、partial-success 保持 app 关闭的停止条件和单独恢复批准；仓库候选只保存 redacted pointer/SHA。审批展示可以包含且只包含目标 `job-agent` object 的 exact target-only hunk（其中的本机 command/cwd 不得复制进仓库或其他持久化产物），不得带出相邻配置、secret 或其他 MCP object。
2. 稳定 Key 保持 W0-01=CI、W0-02=#32，但执行顺序改为 W0-02 → W0-01 → W0-03；#32 的 branch/target/base 与 pre-CI 手工差分门槛必须完整。
3. #32 合入 release 前不能声称 CI 通过；W0-01 必须从已经包含 #32 的 release 新 head 创建，并让随后 PR #1 候选同时通过 clean archive、required check 和 exact SHA Gate。
4. W0-03 后的 clean-clone/真实 MCP 客户端五工具 checkpoint；它只提供技术反馈，不代签连续三天、真实投递或外部用户验收。
5. W0-04–W0-06 仍按 WIP=1 独立执行，但不作为 Wave 1–4 产品纵向切片的入口 Gate；W0-00 可冻结等待其最终文档收口。
6. 产品波次改为：数据/身份/画像 → 发现准备 → 连续三天主动发现 → 投递安全 → 受监督真实投递与外部用户。
7. Transaction Gate 完整 schema/executor 不进入当前 critical path；若未来重启，须在产品首个真实证据后作为独立工具项目重新立项。

协调计划固定 SHA 获批后，第一个可写子计划只处理 **#32 从 MCP 注册表紧急移除**；本机临时隔离是另一个非 Git 安全事务，必须先完成。#32 在 pre-CI 手工差分门槛下进入 release 后，第二个可写计划才处理 **测试自包含与 CI**。两者不得同时修改。安全 tranche 与五工具 checkpoint 通过前，Wave 1–5 只保留依赖顺序而不实施；通过后按“发现闭环优先”启动产品 lane，不等待 W0-04–W0-06。
