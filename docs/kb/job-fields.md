---
来源: jobagent/schema.sql、jobagent/normalize.py、jobagent/match.py；填充率见 scripts/run_five.py 输出
版本: v2
生效时间: 2026-08-06
权限范围: 公开
更新负责人: wujingyu
审核状态: 已审核
---

# 岗位字段口径与空值语义

**Agent 读这份文件是为了不误判空值。** 本项目最贵的一类 bug 全部长一个样：
「没有值」被当成「不合格」，岗位被静默丢掉，用户永远不知道它存在过。

## 三态，不是两态

| 状态 | 含义 | 该怎么办 |
|---|---|---|
| 命中 | 字段有值且匹配 | 推 |
| **信息不全** | **字段没有值** | **不许判否**，进 `--loose` 列表让人看 |
| 不命中 | 字段有值但不匹配 | 不推 |

`match.Verdict` 的 `state` 就是这三档（`hit` / `unknown` / `miss`）。
凡是新增判定逻辑，先回答「这个字段取不到时走哪一档」。

## 字段表

| 字段 | 有值时 | 取不到时 | 注意 |
|---|---|---|---|
| `external_id` | 源站 id | **不许 fallback 到 title** | title 会重复，撞 `UNIQUE(source_key, external_id)` |
| `title` | 原文标题 | —— | **届别常写在这里**（`27届校招-xxx`），结构化字段里反而没有 |
| `job_family` | `family_from_title()` 的结果 | **`None`，不许兜底 `"other"`** | 判不出率 **6%~44%**（四个飞书校招门户，2026-08-06）。**这是公司属性不是系统属性**，别当常量。混进 `other` 会让这批岗位从按族筛里消失 |
| `recruit_type` | `campus` / `intern` / `social` | `None` | 只有这三个值。**判定先看 `recruit_type.parent`（社招/校招）再看叶子** —— 只看叶子会把校招池的 `正式` 判成 `social` |
| `grad_year` | 两位数字符串（`26`/`27`）| `None` = 信息不全 | 腾讯 100% 有值；**飞书结构化字段里没有**，一小部分在标题里 |
| `cities` | 归一后的城市列表 | `[]` | **必须过 `normalize_city()`**：`中国香港`→`香港`，不归一会分成两个城市 |
| `raw_location` | 原文位置串 | `None` | 保留原文用于核对 |
| `raw_category` | 源站分类原文 | `None` | **只落原文，不参与族判定** —— 取值是门户自定义的，映射不过来 |
| `department` | —— | **飞书恒 `None`** | 接口只给 `department_id` 没给名字，**不编** |
| `description` | 列表接口的 `description` + `requirement` | `None` | 腾讯为空（详情要另打接口）|
| `apply_url` | 岗位详情页。飞书是 **`https://<host>/<portal>/position/<id>`** | —— | **点开就是官网那一页**，核对的锚点。**门户段必须有**：裸 `/position/<id>` 是 404（body 9 字节，2026-08-06 四个租户一致），不带门户的老源退到 `index` |
| `closed_at` | 关闭时间 | `NULL` = 仍开放 | 查开放岗位一律 `closed_at IS NULL` |

## 已核实「这个源就是没有」的空字段

**这些 0% 是对的，不是 bug。** 别去官网白找：

| 源 | 字段 | 原因 |
|---|---|---|
| feishu | `grad_year` | 结构化字段里没有届别（4 个租户 × 12 个门户全量核实，含校招门户）|
| feishu | `department` | 接口只给 id 不给名字 |
| tencent_join | `description` | 详情要单独打 `jobDetails`，当前未拉 |

**反过来也成立：不在这张表里的 0% 就是该查的。**
`scripts/run_five.py` 的字段报告会把这类标红。

## 计数口径

- 任何计数**带测量日期**。岗位池是活的：同一天内 nio 从 2265 变到 2266。
- 任何区间**带样本来源**（哪几个租户/门户）。两个样本量出来的区间不是接口的性质。
- **报数前算交集。** 两个门户键不等于两批岗位。

## 变更记录

| 版本 | 日期 | 变化 |
|---|---|---|
| v1 | 2026-08-05 | 首版 |
| v2 | 2026-08-06 | `apply_url` 形状改正（飞书必须带门户段，裸 `/position/<id>` 是 404）；`recruit_type` 补「先看 parent」；判不出族区间按校招门户重测为 6%~44% 并注明是公司属性；`grad_year` 核实范围扩到 12 个门户。来源：`docs/plans/003-校招门户采集.md` §0 偏差 4 / §11 |
