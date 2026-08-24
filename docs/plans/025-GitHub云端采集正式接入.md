# 方案：GitHub 采集、Cloudflare 存储、本机兜底

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` and follow the tasks in order.

**Goal:** 将已验证可达的 GitHub Actions 变成五源远端主采集器，把完整公开快照安全写入
Cloudflare D1，同时保留本机观察任务作为断网或远端故障时的兜底。

**Architecture:** GitHub runner 先在内存中完整读取五源并做来源身份、空清单、缺失 ID、重复 ID、
数量和摘要校验；全部读取成功后，再通过独立上传凭证把每个来源分块送到 Cloudflare Worker。
Worker 只接收批准字段，重算分块和整份清单摘要，复用现有 D1 发布、关闭守卫、变化游标和本机
同步模块。Cloudflare 不再主动访问招聘门户。

**Tech Stack:** Python 3.13、httpx、Cloudflare Python Workers、D1、GitHub Actions、pytest、uv。

> 编号 `025` · 日期 `2026-08` · 状态：进行中
> 依赖：方案 024 的 GitHub 四源探针 `4/4 PASS`

---

## 0. 当前进度

| 步骤 | 状态 | 完成判据 |
|---|---|---|
| 四家飞书远端网络可达性 | 已完成 | run `32753445274`：4/4 success |
| 正式上传协议与迁移 | 未开始 | 本地 RED→GREEN；半份快照、重放、漂移、越权全部 fail-closed |
| 五源远端采集客户端 | 未开始 | 五源先全部采完再产生首个写请求；报告不含岗位正文或凭证 |
| 定时 Workflow | 未开始 | 工作日北京时间 09:30、14:30、20:30；同一时刻最多一个执行 |
| PR / CI / Worker 部署 | 未开始 | required check 通过；迁移与 Worker readback 一致 |
| 技术试运行 | 未开始 | 独立 technical-trial 窗口五源 success；本机同步可读到变化 |
| 正式定时启用 | 未开始 | 技术试运行通过后才保留 schedule；本机旧观察任务继续兜底 |

---

## 一、产品闭环

```text
GitHub 工作日定时启动
  → 五家公司全部读取并在内存校验
  → 任一家失败：零云端写入，任务红灯，本机兜底不受影响
  → 五家全过：逐来源建立上传批次并分块发送
  → Cloudflare 逐块校验、整份重算、关闭守卫、发布变化
  → 本机 cloud-check 只读取状态与变化
  → 既有 ingest / 匹配 / 通知继续工作
```

用户最终感受到的是：电脑合盖、离线或外出时，云端仍按三个宽时间窗发现岗位；电脑再次联网后
同步云端变化。本机原有三次观察不被删除，作为远端任务缺失时的独立兜底，但同一份云端事实
只由 GitHub→Cloudflare 链路发布。

### 不做的事

- 不让 GitHub 或 Cloudflare 触碰登录态、简历、个人资料或真实投递。
- 不上传招聘门户原始响应、请求头、cookie、token 或本机路径。
- 不把半份分页结果、空清单或大规模岗位消失当成成功。
- 不把技术试运行冒充三个工作日产品验收。
- 不新建展示后台；继续用现有 CLI、MCP 只读工具和通知。

---

## 二、信任边界与数据契约

### 1. 两把钥匙分开

| 凭证 | 允许 | 禁止 |
|---|---|---|
| `JOBAGENT_INGEST_TOKEN` | 仅新建上传批次、上传分块、提交批次 | 读取状态/变化、确认游标、触发投递或其他写入 |
| `JOBAGENT_SYNC_TOKEN` | 现有本机状态、变化、ack | 上传岗位快照 |

上传 token 由本机一次性生成，通过不回显管道分别写入 Cloudflare Worker secret 和 GitHub
Actions secret。仓库、计划、日志、PR 与测试夹具都不得出现真实值。

### 2. 只允许五个来源和公开字段

来源必须精确等于 `OBSERVATION_SOURCES`。每个岗位只允许 `to_public_payload()` 产生的键；
Worker 拒绝额外键、来源身份不一致、空 `external_id`、重复 ID、非 JSON 值和越界体积。

### 3. 分块事务

1. `POST /v1/ingest/sessions`：固定来源、模式、期望岗位数、整份 SHA-256 和请求 ID。
2. `PUT /v1/ingest/sessions/{session_id}/chunks/{index}`：每块最多 100 个岗位，固定块摘要。
3. `POST /v1/ingest/sessions/{session_id}/commit`：Worker 要求块号从 0 连续、合计数量精确、
   staged 身份唯一，并重算整份摘要；随后复用 `finalize_snapshot()` 发布。

同一块相同摘要可只读确认；同一块不同摘要、过期租约或来源/窗口漂移必须 409。网络响应不明时
客户端不自动重发写请求；下一次独立调度通过 D1 的已成功来源和摘要判定是否已完成。

### 4. 体积限制

- 每来源 1–20,000 个岗位；五源合计不超过 30,000。
- 每块最多 100 个岗位、编码后最多 2 MiB。
- session 元数据最多 4 KiB；commit body 必须是空 object。
- 错误报告只保存异常类型和闭合状态，不返回原始响应或岗位正文。

---

## 三、时间与兜底

GitHub cron 使用 UTC `30 1,6,12 * * 1-5`，对应北京时间工作日 09:30、14:30、20:30。
这三个时间处在现有早/午/晚宽窗口内部，不依赖招聘门户恰好在某一分钟更新；每轮是完整快照，
晚发布的岗位会被后续轮次发现。

GitHub 调度可能延迟，因此 Worker 以收到首个 session 的服务端时间选择“当前窗口或刚结束一小时
内的上一窗口”，而不是相信客户端时间。周末没有正式窗口，直接拒绝正式写入。

本机兜底保持：原有 LaunchAgent 继续独立观察。`cloud-check` 只读取云端状态、同步变化和通知，
不再请求 Cloudflare 自己抓门户；因此不会把已知 405 路径重新带回主线。

---

## 四、实施任务

### Task 1：把完整快照校验下沉到共用模块

**文件：** `jobagent/collection.py`、`jobagent/remote_probe.py`、
`cloud/collector/src/collector.py` 与对应测试。

- 新增单一 `validate_snapshot()`；本机探针、正式采集和旧 Collector 共用。
- 新增从公开 payload 重算发布 fingerprint 的函数，Worker 不信任客户端 fingerprint。
- RED：空清单、skipped row、空/重复 ID、额外字段、身份漂移。

### Task 2：D1 上传 session 与分块协议

**文件：** `cloud/collector/migrations/0002_remote_ingest.sql`、
`cloud/collector/src/repository.py`、`cloud/collector/src/main.py` 和 cloud tests。

- session 固定 window/source/run/owner/generation/count/digest/expiry/status。
- chunk 固定 index/digest/row_count；相同重放只确认，不同内容拒绝。
- commit 复用现有 source head、关闭守卫和 `finalize_snapshot()`；失败不推进 success/head/changes。
- 上传路由使用独立 auth，现有 sync token 无法调用。
- 移除 `/v1/catch-up` 与 `/v1/technical-trial` 的 Cloudflare 主动抓取语义；scheduled 不再访问门户。

### Task 3：五源远端采集客户端

**文件：** `jobagent/remote_collect.py`、`tests/test_remote_collect.py`。

- 顺序读取五源，全部在本地内存校验后才允许第一次 HTTP 写。
- 上传分块固定、无自动重试；安全 summary 只输出来源、数量、摘要、上传状态和异常类型。
- token 只从环境变量读取，不进入 argv、文件、日志或报告。

### Task 4：正式 Workflow

**文件：** `.github/workflows/cloud-collection.yml` 与静态契约测试。

- `schedule` + 手工 technical-trial；`permissions: contents: read`。
- 固定 action commit、`uv sync --frozen`、并发组不取消正在执行的采集。
- secret 只注入采集步骤；PR 工作流拿不到正式上传 token。

### Task 5：发布与试运行

1. 全量测试、diff check、secret scan；创建 draft PR。
2. CI 通过后 merge commit；应用 D1 migration，部署 Worker。
3. 生成独立上传 token，不回显地写入 Worker secret 与 GitHub secret；设置公开 Worker URL variable。
4. 手工运行一次 technical-trial；五源全部 success 后，从 D1 和本机 cloud sync 双路径 readback。
5. 技术试运行失败：暂停 schedule、保留本机任务、只读诊断；不得把部分结果称成功。
6. 成功：正式 schedule 生效；三个工作日验收从首个完整正式工作日重新计数。

---

## 五、完成定义

- GitHub 五源采集、分块上传、D1 发布和本机同步的自动测试全绿。
- 未授权来源、额外字段、半份清单、摘要不符、块漂移、过期 session、错误 token 全部 fail-closed。
- technical-trial 五源 success，D1 数量/摘要与 GitHub runner 一致。
- 本机能同步变化，首次基线不发送“845+ 条新岗位”噪音；后续真实变化才通知。
- Cloudflare cron 仍为空且 Worker 不再主动访问招聘门户。
- 本机观察任务保持 installed/loaded，云端故障不阻断本机兜底。
- 上述通过只代表远端技术闭环；产品仍需完成三个工作日独立官网真值与用户通知验收。
