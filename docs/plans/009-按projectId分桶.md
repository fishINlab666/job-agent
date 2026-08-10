# 方案：腾讯届别改按 projectId 分桶（而不是标签字符串）

> 编号 `009` · 日期 `2026-08` · 状态：**已落地（2026-08-09）**
> 涉及文件：`jobagent/adapters/tencent_join.py`、`tests/test_adapter_tencent.py`、`tests/test_cli.py`
>
> 从 007 §8 观察项核实时发现：站点自己按 `projectId` 分派届别（`renderProjectMeta`），
> 而我的 `_parse_grad_year()` 是按 `recruitLabelName` 字符串匹配重建的。
> 已知会错：pid=12（项目实习生）的标签是「日常实习」，字符串匹配认不出它是另一个项目。

---

## 0. 当前进度（边做边回写，不是写完方案就不管了）

| 步骤 | 状态 | 核实命令 / 实际偏差 |
|---|---|---|
| 站点按 `projectId` 分派届别 | 已核实（2026-08-09）| 007 §9 命令 F，`renderProjectMeta` 的两个列表：应届桶 `{1,14}`、实习桶 `{4,5,12,20}` |
| 标签字符串匹配已知的错误案例 | 已核实（2026-08-09）| pid=12 的 `projectName` 是「项目实习生」而 `recruitLabelName` 是「日常实习」，会被标签匹配错分到 pid=4（日常实习）桶 |
| `searchPosition` 响应里有 `projectId` 字段 | 已核实（2026-08-09）| 007 §9 命令 A，13 个字段里有 `projectId`；命令 C 显示 6 个 projectId：1, 2, 4, 5, 14, 20 |
| 807 行的 projectId 分布 | 已核实（2026-08-09）| 应届 366（1=79, 14=287）、实习 348（4=61, 5=14, 12=5, 20=268）、未声明 93（2=93）|
| 代码实现 | **已完成** | 改 `_parse_grad_year()` 签名为 `(project_id: int | None)`，改 `_to_raw_job()` 和 `grad_year_from_raw()` 调用点 |
| 测试通过 | **已完成** | 401 passed（400→401，新增 `test_pid_2_is_local_exception`）。改了 `_position` fixture 加 `projectId`，改了 `seed()` 函数，改了所有调用点 |
| 真库刷新 | **已完成（逻辑等价重构，0 行改动）** | `refresh-grad-year --source tencent_join` 报「届别已是最新，查了 807 行，无需改动」。真库分布 `26` 459 / `不限` 348 不变 |

---

## 一、产品设计

### 1. 为什么现在做

现在的 `_parse_grad_year(label: str)` 按 `recruitLabelName` 字符串匹配分桶：

```python
def _parse_grad_year(label: str) -> str | None:
    if "应届" in label and "实习" in label:
        return CURRENT_CAMPUS_YEAR
    if "应届毕业生" in label or "应届生" in label:
        return CURRENT_CAMPUS_YEAR
    if "实习" in label:
        return "不限"
    return None
```

这是重建站点的分派逻辑，但站点自己用的键是 `projectId`，不是标签字符串。007 §9 命令 F 核实的 `renderProjectMeta`：

```js
renderProjectMeta: function(p) {
  if (p.projectId === 1 || p.projectId === 14) {           // 应届桶
    return {
      subtitle: Project_CampusSubtitle,                     // "2026应届生招聘"
      graduation: Project_CampusGraduationDate              // 2025-01-01 ~ 2026-12-31
    };
  }
  if (p.projectId === 4 || p.projectId === 5 ||
      p.projectId === 12 || p.projectId === 20) {          // 实习桶
    return {
      graduation: Project_TraineeGraduationDate,            // 2026-09-01 ~ 2027-12-31
      tip: Project_TraineeGraduationTip                     // "不限毕业时间"
    };
  }
  return null;
}
```

**已知的标签匹配错误**：projectId=12（项目实习生，5 条）的 `projectName` 是「项目实习生」，但 `recruitLabelName` 是「日常实习」。标签匹配把它和 pid=4（日常实习，61 条）分到同一桶，丢失了「这是另一个项目」的信息。

**站点对 pid=2 返回 null**：「应届实习」(93 条) 在站点分派表里两个列表都不存在，`renderProjectMeta(2)` 返回 `null`。我现在按标签推断它是 `"26"`，这一处是唯一的本地例外。

**用 projectId 的好处**：
1. 用站点自己的分派键，不是重建的映射 → 更直接
2. 新项目进来时，标签匹配可能猜错（比如「实习生招聘」含「实习」但不一定是不限届别），而 pid 匹配会返回 `None`（明确的「不知道」）→ 更安全
3. pid=12 那类标签与项目名不一致的情况不会被错分

**代价**：
- pid=2 的 93 条，站点返回 `null`，如果照搬会让它从现在的 `"26"`（可见）变成 `None`（要 `--allow-missing` 才看得到），是产品回退
- 正确形状是「按 pid 分桶 + pid=2 作为一处写明理由的本地例外」

### 2. 要解决什么问题，不解决什么

**解决**：
- 用站点自己的分派键 `projectId` 替代重建的标签字符串匹配
- 让 pid=12（项目实习生）不被错认成 pid=4（日常实习）
- 让新项目进来时，认不出的返回 `None` 而不是猜错

**明确不解决**：
- 不改 pid=2（应届实习）的值 —— 它仍是 `"26"`，这一处是本地例外，要在代码和文档里写明理由
- 不改 `CURRENT_CAMPUS_YEAR` 的换季逻辑 —— 那是 008 的告警部分
- 不改 `refresh_grad_year` 的机制 —— 刷新逻辑不变，只是判据键从 `recruitLabelName` 换成 `projectId`

### 3. 哪些是已核实的，哪些是我猜的

| 事实 | 怎么核实的 |
|---|---|
| 站点按 `projectId` 分派（应届桶 `{1,14}`、实习桶 `{4,5,12,20}`） | 007 §9 命令 F，`renderProjectMeta` 函数体 |
| `searchPosition` 响应里有 `projectId` 字段 | 007 §9 命令 A（实打接口，2026-08-09） |
| 真库 807 行的 projectId 分布 | 007 结束前的只读查询 |
| pid=12 的标签是「日常实习」而非「项目实习生」 | 007 结束前的只读查询，`SELECT DISTINCT projectId, projectName, recruitLabelName` |
| pid=2（应届实习）在站点分派表里返回 `null` | 007 §9 命令 F，`renderProjectMeta(2)` 不在两个列表里 |

| 假设 | 如果错了会怎样 | 打算怎么验 |
|---|---|---|
| pid=2 按标签推断是合理的（应届实习 → 跟应届生走） | 如果它其实是「在读即可」，那 93 条对非当届用户不可见（漏报方向） | 标签原文只有「应届实习」四个字，站点没给单独说明。**推理，未实测。** 007 §3 已记录这一假设 |
| pid=12 现在存的就是 `"不限"`（因为标签含「实习」） | 刷新会改它的值 | 上一轮查库时已确认 pid=12 那 5 条的 `grad_year` 是 `"不限"`，和新规则一致，不会变 |
| 新版改后 807 行的值和现在一样（只有逻辑重构，无语义变化） | 刷新会改一批行 | pid 列表和标签规则对 807 行的覆盖是一样的：应届 366、实习 348、未声明 93。刷新后应该 `changed=0` |

### 4. 数据长什么样，空值怎么办

| 字段 | 有值时 | 取不到时 | 「没有值」和「不匹配」分开了吗 |
|---|---|---|---|
| 应届类项目（`projectId in {1, 14}`）→ `grad_year` | `"26"`（站点当前入口年份） | —— | —— |
| 实习类项目（`projectId in {4, 5, 12, 20}`）→ `grad_year` | `"不限"` | —— | —— |
| `projectId == 2`（应届实习，本地例外）→ `grad_year` | `"26"` | —— | —— |
| 认不出的 projectId → `grad_year` | —— | `None` → 落库 `NULL` | 是。`match.py:104` 的 `parse_grad_years(None)` 返回 `None` → 判 `unknown` 不判 `miss` |
| `jobs.grad_year` | `"26"` 或 `"不限"` | `NULL`（**不写空串**） | 是 |
| `parse_grad_years` 三态 | `["26"]` / `[]`（不限） | `None` | 是，三态已有，本方案不动它 |

四条必须守住的（从 007 §4 继承）：

1. **在读实习存 `"不限"`，不存 `None`。** 这两个是不同的状态。
2. **应届类存 `"26"`，跟站点入口走。** 不许写成 `str(date.today().year % 100)` 之类的自动推导。
3. **认不出的 projectId 落 `NULL`，不许兜底。** 站点对 pid=2 返回 `null` → 我们推断成 `"26"` 是例外，这一处要写明理由。
4. **分桶必须按 `projectId`，不按 `recruitLabelName`。** 本方案就是在改这一条。

### 5. 硬约束在哪一行

本方案没有不可撤销后果，**全是软约束**：改的是一个推导函数的判据键，写库走 `refresh_grad_year --apply` 或 `sync` 的常规 UPDATE 路径，跑错了改回来再跑一轮。不涉及代投、删除、外发。

### 6. 方案选型（如果有多个）

**方案 A（本方案）**：改 `_parse_grad_year` 签名为 `_parse_grad_year(project_id: int | None) -> str | None`，内部按 `projectId` 分桶，pid=2 作为硬编码例外返回 `CURRENT_CAMPUS_YEAR`。

**优点**：用站点自己的键，不会错分 pid=12。
**缺点**：pid=2 是硬编码例外，站点新加一个「应届实习 II」项目时认不出。

**方案 B（被否）**：改成 `_parse_grad_year(project_id, label)` 双键，先按 pid 查表，查不到时按 label 兜底。

**否掉理由**：双键兜底比单键例外更不可控 —— pid=999 进来时，兜底会猜「999」这三个字符含不含「实习」，而单键例外会返回 `None`（明确的「不知道」）。007 §4 第 3 条「认不出的落 `NULL`，不许兜底」就是在拦这个。

**方案 C（被否）**：保持现在的标签匹配，文档里记一笔「pid=12 会被错分到 pid=4，但两者届别语义一样所以无害」。

**否掉理由**：无害的前提是两个项目的届别语义永远一样。站点明确把它们分成两个 projectId，说明它们在站点那边是两个独立可变的实体。用站点自己的键更稳。

### 7. 影响面

| 影响项 | 预期值 | 核实命令 |
|---|---|---|
| 真库刷新行数 | 0（逻辑等价重构） | `refresh-grad-year --source tencent_join` 预演 |
| 测试通过 | 400 passed（改 fixture，不改断言） | `uv run pytest -q` |
| SPEC §9 三档条数 | 95 / 1952 / 2533 不变 | `uv run python -m jobagent.cli status` |
| pid=12 那 5 条的值 | `"不限"` → `"不限"`（不变） | 刷新前后查库 `SELECT id, grad_year FROM jobs WHERE external_id IN (SELECT external_id FROM (SELECT external_id, json_extract(raw_json, '$.projectId') AS pid FROM snapshots WHERE source_key='tencent_join' ORDER BY id DESC LIMIT 807) WHERE pid=12)` |

### 8. 这次明确不做什么

- **不改 pid=2 的处理**。它仍按「应届实习 → 跟应届生走」推断，是唯一一处本地例外。这一处要在代码注释和文档里写明理由：站点分派表对它返回 `null`，我们按标签推断它是 `"26"`。
- **不动 `CURRENT_CAMPUS_YEAR`**。换季逻辑和告警是 008 的另一半。
- **不改 `_recruit_type`**。它表达岗位性质（校招/实习），不表达届别，007 已经把它从届别判据里剥离。
- **不动飞书适配器**。`grad_year=None` 是「源站真的没有」，和腾讯「源站按 projectId 分派」是两件事。
- **不把 `grad_year` 加进 `_fp()`**。这是 008 §8 已经否掉的方案 A。

---

## 二、具体实现

### 1. 代码改动点

**`jobagent/adapters/tencent_join.py`**：

```python
# 届别推导：按站点自己的 projectId 分桶（已核实 2026-08-09）
#   应届桶 {1, 14}             → 26 届（站点当前入口年份）
#   实习桶 {4, 5, 12, 20}      → 不限（站点实习入口明说「不限毕业时间」）
#   projectId=2（应届实习）    → 26 届（本地例外：站点对它返回 null，我们按标签推断）
#   其余                       → None（站点未声明，我们不猜）
#
# 站点在 renderProjectMeta 里按 projectId 分派届别声明，这是站点自己的键。
# 007 用的是 recruitLabelName 字符串匹配，重建站点逻辑，但标签会错分项目：
# pid=12 的项目名「项目实习生」、标签「日常实习」，字符串匹配认不出它是另一个项目。
#
# 每个招聘季入口年份会变，换季时核对 Project_CampusSubtitle（见 plan 008）。
CURRENT_CAMPUS_YEAR = "26"

def _parse_grad_year(project_id: int | None) -> str | None:
    """按 projectId 推导届别。
    
    Args:
        project_id: 源站的 projectId 字段值
        
    Returns:
        "26" / "不限" / None（站点未声明）
    """
    if project_id in {1, 14}:           # 应届桶
        return CURRENT_CAMPUS_YEAR
    if project_id in {4, 5, 12, 20}:    # 实习桶
        return "不限"
    if project_id == 2:                 # 应届实习（本地例外）
        # 站点 renderProjectMeta 对 pid=2 返回 null，但标签原文是「应届实习」，
        # 我们推断它跟应届生走。如果它其实是「在读即可」，93 条会漏报（选漏报
        # 方向：宁可让它对当届可见，也不放宽成不限）。
        return CURRENT_CAMPUS_YEAR
    return None
```

`_to_raw_job` 改调用点：

```python
def _to_raw_job(item: dict) -> dict:
    # ... 既有字段 ...
    grad_year = _parse_grad_year(item.get("projectId"))
    # ... 返回 ...
```

`grad_year_from_raw` 改取键：

```python
@staticmethod
def grad_year_from_raw(raw: dict) -> str | None:
    """从源站原文重算届别，和 fetch 走的是同一条规则（`_parse_grad_year`）。
    ...
    """
    return _parse_grad_year(raw.get("projectId"))
```

**`tests/test_adapter_tencent.py`**：

改 fixture 的 `make_item`，把 `recruitLabelName` 改成 `projectId`。新增一条测试 `test_pid_2_is_local_exception_returns_campus_year`：

```python
def test_pid_2_is_local_exception_returns_campus_year():
    """应届实习（projectId=2）站点返回 null，我们按标签推断是应届。"""
    item = make_item(title="后端开发", projectId=2, recruitLabelName="应届实习")
    job = adapter._to_raw_job(item)
    assert job["grad_year"] == "26"
```

既有测试的改动：
- `test_known_labels_still_work` → 改成 `test_known_project_ids_still_work`，fixture 用 `projectId=20`（青云实习）
- `test_unknown_type_grad_year_none` → 改成 `test_unknown_project_id_returns_none`，fixture 用 `projectId=999`

### 2. 文档改动点

**`docs/SPEC.md` §3**：

```markdown
推导规则（已核实 2026-08-09）：
- 应届桶（`projectId in {1, 14}`）→ `"26"`（站点当前入口年份）
- 实习桶（`projectId in {4, 5, 12, 20}`）→ `"不限"`（站点实习入口明说「不限毕业时间」）
- `projectId == 2`（应届实习）→ `"26"`（本地例外：站点对它返回 null，我们按标签推断）

**用 `projectId` 而不是 `recruitLabelName`**：站点自己在 `renderProjectMeta` 里按 `projectId`
分派届别声明，这是站点的键。标签字符串匹配会错分项目：pid=12 的项目名「项目实习生」、
标签「日常实习」，字符串匹配认不出它是另一个项目。详见 `docs/plans/009-按projectId分桶.md`。
```

**`docs/plans/007-届别窗口区间.md` §8**：

在「观察项，不在本方案内」那段后面追加一句：

```markdown
**已被 009 替代**：007 按 `recruitLabelName` 字符串匹配分桶，009 改按站点自己的 `projectId`。
标签匹配已知会错分 pid=12（项目实习生，标签「日常实习」），详见 009。
```

---

## 三、怎么验

### 9. 验证命令

命令 A —— 改动前后 807 行的值分布（应该一样，逻辑等价重构）：

```bash
cd /Users/wujingyu/Desktop/AI/projects-jobs/job-agent
sqlite3 data/jobagent.db <<SQL
SELECT grad_year, COUNT(*) AS cnt
FROM jobs
WHERE source_key = 'tencent_join'
GROUP BY grad_year
ORDER BY grad_year;
SQL
```

期望输出：`26|459`、`不限|348`，和改动前一样。

命令 B —— 刷新预演（应该 `changed=0`）：

```bash
uv run python -m jobagent.cli refresh-grad-year --source tencent_join
```

期望输出：`届别已是最新，0 行需要更新`。

命令 C —— pid=12 那 5 条的值（改动前后都该是 `"不限"`）：

```bash
sqlite3 data/jobagent.db <<SQL
SELECT j.id, j.external_id, j.grad_year
FROM jobs j
JOIN (
  SELECT external_id
  FROM snapshots
  WHERE source_key = 'tencent_join'
    AND json_extract(raw_json, '$.projectId') = 12
  GROUP BY external_id
  HAVING id = MAX(id)
) s ON j.external_id = s.external_id
ORDER BY j.id;
SQL
```

期望输出：5 行，`grad_year` 列全是 `不限`。

命令 D —— 测试通过：

```bash
uv run pytest -q
```

期望输出：`400 passed`。

命令 E —— SPEC §9 三档条数（应该不变）：

```bash
uv run python -m jobagent.cli status
```

期望输出：`95 hit / 1857 hit (allow missing 届别) / 581 hit (allow missing 岗位族+届别) / 6866 miss`（SPEC §8 的口径）。

---

## 四、复盘

### 10. 测试钉的是哪几条

| 测试 | 钉的不变量 | 文件:行号 |
|---|---|---|
| `test_known_project_ids_still_work` | pid=20（青云实习）→ `"不限"` | `tests/test_adapter_tencent.py` |
| `test_unknown_project_id_returns_none` | 认不出的 pid → `None`，不许兜底 | 同上 |
| `test_pid_2_is_local_exception_returns_campus_year` | pid=2（应届实习）→ `"26"`，是本地例外 | 同上 |
| `TestGradYearRefresh` 全部 11 条 | 刷新的三条硬约束（008 §2） | `tests/test_ingest.py` |

### 11. 实现和方案的偏差（如果有）

无偏差。改动完全按方案执行：
- 改 `_parse_grad_year()` 签名从 `(label: str)` 到 `(project_id: int | None)`
- 改调用点：`_to_raw_job()` 传 `row.get("projectId")`，`grad_year_from_raw()` 传 `raw.get("projectId")`
- 改测试 fixture `_position()` 加 `projectId` 字段，默认值 1（应届桶）
- 改 CLI 测试的 `seed()` 函数，签名从 `(conn, ext_id, label, grad_year)` 到 `(conn, ext_id, project_id, grad_year)`
- 新增测试 `test_pid_2_is_local_exception_returns_campus_year`
- 真库刷新：逻辑等价重构，807 行全部 `unchanged`，0 行 `changed`

### 12. 踩的坑

**坑 1：改了适配器但忘了改测试 fixture，4 个 CLI 测试失败。**

错误信息：`届别已是最新 tencent_join · 查了 1 行，无需改动，1 行跳过：重算出来是空值，但库里有值`。

原因：`seed()` 函数在 `snapshots.raw_json` 里只写了 `{"recruitLabelName": label}`，但 `grad_year_from_raw()` 现在读 `projectId`，读不到返回 `None`，触发了「不拿空值覆盖好值」的守卫。

修复：改 `seed()` 签名，第二个参数从 `label: str` 改成 `project_id: int`，`raw_json` 写成 `{"projectId": project_id}`。所有调用点的标签字面量改成对应的 pid：`"日常实习"` → `4`、`"应届毕业生"` → `1`、`"应届实习"` → `2`、`"在校生项目"` → `999`。

教训：改了数据源的取键，测试 fixture 的 mock 数据也要同步改。快照重算的输入是 `raw_json`，它必须和真实 API 响应的形状一致，否则刷新测试全失效。

### 13. 下次改进

无。这次是纯重构，形状没变、行为没变，测试覆盖充分（401 passed，新增 1 条 pid=2 例外测试）。

---

**方案完毕**。等待评审或实施。
