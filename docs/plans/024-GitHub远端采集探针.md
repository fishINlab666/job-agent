# 方案：验证 GitHub Actions 能否完整读取四家飞书岗位

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` and follow the tasks in order.

**Goal:** 用一次、无密钥、无云端写入的 GitHub Actions 运行，验证四家飞书完整清单是否可达。

**Architecture:** Probe Module 复用 `OBSERVATION_SOURCES`、现有 Adapter 和公开载荷哈希；
Workflow 只是固定运行容器，不包含源规则。远端结果只保留闭合摘要，任一失败即停止路线。

**Tech Stack:** Python 3.13、httpx、pytest、GitHub Actions、uv。

> 编号 `024` · 日期 `2026-08` · 状态：进行中
> 涉及文件：`jobagent/remote_probe.py`、`tests/test_remote_probe.py`、`.github/workflows/remote-feishu-probe.yml`

---

## 0. 当前进度（边做边回写，不是写完方案就不管了）

| 步骤 | 状态 | 核实命令 / 实际偏差 |
|---|---|---|
| 固定 Cloudflare 失败事实与新路线 | 已核实 | D1 readback：腾讯 845 success；四家飞书 HTTPStatusError；`wrangler.jsonc` 为 `crons: []` |
| 建立隔离分支与干净基线 | 已核实 | `git switch -c feat/remote-feishu-runner origin/main && uv run --frozen pytest -q -p no:cacheprovider` → `1029 passed, 2 skipped` |
| 实现本地可测的只读 Probe Module | 未开始 | 先运行 `uv run --frozen pytest tests/test_remote_probe.py -q -p no:cacheprovider` 观察 RED |
| 发布 draft PR 并触发一次远端探针 | 未开始 | 只允许 PR opened 事件；无 secret、无 D1 写、无 schedule |
| 四源结果 Gate | 未开始 | 四源全部 success、count>0、digest=64 hex 才进入方案 025；任一失败即停止 |

---

## 一、产品设计

### 1. 为什么现在做

Cloudflare 数据库与同步代码已经落地，但四家飞书在 Cloudflare 运行环境全部返回 HTTP 405。
继续在同一环境堆请求头、重试或反爬猜测不会证明路线可行，只会增加不可解释的代码。

现在只回答一个问题：**GitHub Actions 这台远端机器，能否用现有 Adapter 完整读取四家
飞书公开岗位？** 这一步不写云数据库，也不启动正式定时任务。

### 2. 用户会看到什么

```text
创建 draft PR
  → GitHub 只运行一次四源只读探针
  → 每家公司只输出 success/failure、岗位数、清单哈希、耗时和错误类别
  → 四家全过：自动进入下一份正式接入方案
  → 任一家失败：停止 GitHub 路线，改评估小型云主机
```

### 3. 哪些是已核实的，哪些是我猜的

| 事实 | 怎么核实的 |
|---|---|
| 现有 `FeishuAdapter.fetch()` 在本机可读取四源，并校验分页声明数量 | 2026-08-24 本机真实只读采集与 `tests/test_adapter_feishu.py` |
| Cloudflare 技术试运行中四家飞书都是 HTTP 405，腾讯成功 845 | 远端 D1 `source_runs` 只读查询 |
| 当前 GitHub 只有 CI Workflow，仓库 Secret 列表为空 | `gh api .../actions/workflows`、`gh secret list` |
| 现有源清单唯一位于 `jobagent.targets.OBSERVATION_SOURCES` | `tests/test_cloud_collector.py` 与源码 readback |

| 假设 | 如果错了会怎样 | 打算怎么验 |
|---|---|---|
| GitHub hosted runner 的网络出口不会被飞书拒绝 | 正式远端方案不可用 | 同一次 Workflow 顺序读取四源，任一失败即 Gate BLOCK |
| Linux runner 发送现有固定 UA 时能得到与本机同形响应 | 可能收到 405、空清单或截断分页 | 复用现有 Adapter，不在 Workflow 另拼 HTTP；要求 count>0、ID 唯一、无 skipped row |
| 一次顺序读取不会触发源站限流 | 远端探针部分失败 | 不并发、不重试；保留首次错误后停止该路线 |

---

## 二、具体实现

### 4. 数据长什么样，空值怎么办

`ProbeReport` 只允许这些字段：

| 字段 | 有值时 | 取不到时 | 分开了吗 |
|---|---|---|---|
| `source_key` | 固定四源键 | 不允许为空，构造即失败 | 是 |
| `status` | `success` / `failed` | 不存在第三种隐含成功 | 是 |
| `fetched_count` | success 时正整数 | failed 时 `null` | 是 |
| `snapshot_sha256` | success 时 64 位小写 hex | failed 时 `null` | 是 |
| `elapsed_ms` | 非负整数 | 计时器失败则整个探针失败 | 是 |
| `error_kind` | failed 时异常类型名 | success 时 `null` | 是 |

报告禁止包含岗位正文、岗位清单、原始响应、请求头、环境变量、token、cookie、个人资料或
本机路径。清单哈希继续使用 `jobagent.collection.snapshot_digest()`；Workflow 不复制序列化规则。

### 5. 硬约束在哪一行

- Workflow 仅响应同仓库 draft PR 的 `opened` 事件；没有 `schedule`、`workflow_dispatch`、
  `push` 或可重复触发的 `synchronize`。
- `permissions: contents: read`，Workflow 中不得出现 `secrets.`、Cloudflare、D1、通知或投递命令。
- Probe Module 顺序运行四源，任一失败仍先写安全摘要，再以非零退出码结束整个 job。
- 不自动重试真实网络请求；失败结果不能通过重开 PR 或改分支名自动洗成成功。

### 6. 判据的粒度

判据是“来源完整清单级”，不是“HTTP 200 级”：

1. Adapter 自己验证 HTTP、业务 code、声明 count 和分页上限。
2. Probe 再验证岗位数大于 0、external_id 非空且唯一、`skipped_no_id == 0`。
3. 规范公开载荷能生成稳定 64 位 snapshot digest。
4. 四个固定 `source_key` 全部 success，才是 Gate PASS。

### 7. 数字的口径

| 数字 | 分子/内容 | 分母/范围 | 会不会随时间动 |
|---|---|---|---|
| `fetched_count` | Adapter 返回且通过身份检查的岗位数 | 该来源本次完整分页 | 会，不能写成固定验收数 |
| `elapsed_ms` | 从该来源 fetch 开始到完整验证结束 | 单来源一次请求链 | 会，只作诊断 |
| `4/4` | success 的固定飞书来源数 | 当前观察池四个飞书来源 | 源池不变时不动 |

### 8. 这次明确不做什么

- 不写 D1，不读取或创建任何 Cloudflare/GitHub secret，因为本轮只验证网络可达性。
- 不采腾讯，因为 Cloudflare 已证明腾讯可达，本轮只验证未知的飞书出口。
- 不新增 Cron、正式远端采集、上传 Interface、本机 cloud-check 或通知。
- 不把探针成功称为云端接管或三工作日验收完成。
- 不在失败后调 UA、代理、TLS、重试次数继续碰运气；失败直接转小型云主机路线评估。

---

## 三、怎么验

### 9. 验证命令

```bash
uv run --frozen pytest tests/test_remote_probe.py -q -p no:cacheprovider
uv run --frozen pytest -q -p no:cacheprovider
git diff --check
```

远端只读验收从 PR 的 `Remote Feishu Network Probe` job 读取，不在本机伪造：

```bash
gh run list --repo fishINlab666/job-agent --workflow remote-feishu-probe.yml --limit 1
run_id="$(gh run list --repo fishINlab666/job-agent \
  --workflow remote-feishu-probe.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
test -n "$run_id"
gh run view "$run_id" --repo fishINlab666/job-agent --log
```

### 10. 测试钉的是哪几条

| 结论 | 测试 | 做一半会红的那条 |
|---|---|---|
| Probe 只取四家飞书固定源 | `test_probe_uses_exact_four_feishu_sources` | 腾讯或第五源混入时集合不等 |
| 不可信空清单、空 ID、重复 ID、skipped row 均失败 | `test_probe_rejects_incomplete_source_snapshots` | 只检查 HTTP 成功时反例红 |
| 报告不含岗位正文或原始数据 | `test_probe_report_has_a_closed_public_schema` | 新增任意未批准键时 exact-set 红 |
| 任一来源失败使 CLI 非零但仍落安全摘要 | `test_main_writes_report_before_returning_failure` | 直接抛异常、丢报告时红 |
| Workflow 单次、同仓库、只读、无 secret/定时/云写 | `test_remote_probe_workflow_is_one_shot_and_read_only` | 加 schedule/secret/写权限时红 |

---

## 四、实施任务

### Task 1: 建立 Probe Module 的 RED/GREEN

**Files:**
- Create: `jobagent/remote_probe.py`
- Create: `tests/test_remote_probe.py`

- [ ] 写上述五类失败测试；运行聚焦测试，确认因 Module/Workflow 不存在而 RED。
- [ ] 实现 `probe_feishu_sources()`、闭合报告 schema、JSON/Markdown 安全输出和 CLI exit code。
- [ ] 运行聚焦测试，确认 GREEN；不得通过放宽反例实现。

外部 Interface 固定为：

```python
def probe_feishu_sources(
    *,
    specs: tuple[Mapping[str, str | None], ...] = OBSERVATION_SOURCES,
    adapter_builder: Callable = build_observation_adapter,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict:
    """顺序探测四个飞书来源，返回闭合且不含岗位正文的报告。"""


def write_report(report: dict, output: Path, summary: Path | None) -> None:
    """先原子写 JSON，再追加安全 Markdown summary；不得写岗位载荷。"""
```

CLI 固定参数为 `--output PATH` 与可选 `--summary PATH`；先写报告，再按 `status` 返回 0/1。

### Task 2: 建立一次性只读 Workflow

**Files:**
- Create: `.github/workflows/remote-feishu-probe.yml`
- Modify: `tests/test_remote_probe.py`

- [ ] Workflow 仅 `pull_request.types: [opened]`，同仓库 PR，`contents: read`。
- [ ] 使用固定 SHA 的 `actions/checkout` 与 `astral-sh/setup-uv`，运行 `uv sync --frozen`。
- [ ] 执行 `uv run --frozen python -m jobagent.remote_probe`；报告只写 runner 临时目录和 Job Summary。
- [ ] 静态 Workflow 测试与聚焦测试 GREEN。

Workflow 固定结构为：

```yaml
name: Remote Feishu Network Probe

"on":
  pull_request:
    types: [opened]

permissions:
  contents: read

jobs:
  probe:
    if: github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-24.04
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d
        with:
          version: 0.11.29
          python-version: "3.13"
          enable-cache: false
      - run: uv sync --frozen
      - run: >-
          uv run --frozen python -m jobagent.remote_probe
          --output "$RUNNER_TEMP/remote-feishu-probe.json"
          --summary "$GITHUB_STEP_SUMMARY"
```

### Task 3: 本地收口并发布 draft PR

- [ ] 运行全量测试、`git diff --check`、secret/个人字段扫描与 Workflow YAML 解析。
- [ ] 只提交方案、Probe Module、测试和 Workflow；普通 push 后创建 draft PR。
- [ ] PR 创建只触发一次远端探针；不重发失败 job，不通过 `synchronize` 再跑。

### Task 4: 远端 Gate

- [ ] 只读核对 run 的 event、head SHA、Workflow 文件 SHA、四源 source_key/count/digest/status。
- [ ] 四源全过：在本方案 §0 回填事实，创建方案 025 设计分块上传、首次基线静默和本机代理接入，随后继续实施。
- [ ] 任一失败：在 §0 回填首次结果，停止 GitHub runner 路线；不写方案 025 代码，转为评估小型云主机。

---

## Gate 与停止条件

- Workflow 未在 PR opened 时运行：只读诊断事件/权限，禁止改成定时或反复 push 触发。
- 任一飞书来源 HTTP/业务 code/分页/count/身份不完整：Gate BLOCK，不重跑。
- Workflow 输出出现岗位正文、完整清单、secret、cookie、个人字段或绝对本机路径：立即取消 run，Gate BLOCK。
- GitHub 要求付费、额外安全授权或仓库权限升级：停止，交由用户做消费/权限决定。

---

## 五、复盘（探针完成后回填）

### 11. 方案和实现差在哪

探针完成后按实际 Workflow/run SHA 与四源结果回填。

### 12. 实现中踩到的坑

探针完成后只记录可复现的触发条件、错误表现、最小修法和真实测试名。

### 13. 如果重来，方案里该提前写上哪句话

探针完成后判断是否需要改模板；单次环境不兼容不扩散成全项目规则。
