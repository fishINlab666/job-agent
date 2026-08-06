---
来源: 用户实地核对入口（2026-08-05）+ 全量翻页实测（2026-08-06）；每家的归属都用页面标题核实过
版本: v2
生效时间: 2026-08-06
权限范围: 公开
更新负责人: wujingyu
审核状态: 已审核
---

# 已核实的公司 → 门户对应关系

**收录标准：页面标题自称的公司名和我们认领的公司对得上。**
只靠域名 slug 猜归属栽过一次（见文末）。

## 已接的五家

| 公司 | 系统 | 社招入口 | 校招入口 | `website-path` | 校招条数 |
|---|---|---|---|---|---|
| 腾讯 | 自建 | `join.qq.com/post.html` | 同一个站 | —— | 366（26 届存量）+ 435 实习 |
| 蔚来 | feishu `nio` | `nio.jobs.feishu.cn/` | **`campus.nio.com/#/`** | `campus` | **627** |
| 小鹏汽车 | feishu `xiaopeng` | `xiaopeng.jobs.feishu.cn/` | **`xiaopeng.jobs.feishu.cn/398875`** | `campus` | **436** |
| 字节跳动 | feishu `bytedance` | `bytedance.jobs.feishu.cn/` | **`jobs.bytedance.com/campus`** | `campus` | **7395** |
| 商汤科技 | feishu `sensetime` | `sensetime.jobs.feishu.cn/` | **`hr-jobs.sensetime.com/edu/`** | `edu` | **160** |

校招合计 **8618**（627+436+7395+160，2026-08-06 全量翻页，每个门户
`rows == count` 且 id 全 unique）。

**不含** 小鹏的 `398875`（335 条）：它是 `campus` 的子门户，
`campus ∩ 398875 = 335` 完全包含。**采了 `campus` 就别再采它。**

**海底捞被换掉了**：`campus`/`edu` 都回 `code=-9000003`，没有校招门户，
用户核对时也指出官网找不到校招入口。替补是字节跳动 —— 归属用页面标题核实过
（`bytedance.jobs.feishu.cn/campus/` 与 `jobs.bytedance.com/campus`
同为「字节跳动校园招聘官网」）。

**投递链接的形状**：`https://<host>/<portal>/position/<id>`。
裸 `/position/<id>` 是 404（body 9 字节，四个租户一致）。见 `job-fields.md` v2。

## 三个容易踩的形态

1. **自建站在飞书前面。** `campus.nio.com` 是蔚来自己的 Vue 应用
   （`nio-school-front`），但「立即投递」跳 `nio.jobs.feishu.cn/campus/` ——
   **数据仍在飞书**，用 `FeishuAdapter` + `website-path: campus` 就能采。
   看到自建校招站，先查它的投递按钮跳哪儿。

2. **自定义域名是同一个租户。** `hr-jobs.sensetime.com` 与
   `sensetime.jobs.feishu.cn` 打同一个 `website-path` 返回同样条数、
   id 全交。**是同一个租户的两个入口，不是两家。**

3. **校招门户可以不存在。** 海底捞 `campus`/`edu` 都返回 `code=-9000003`。
   这是「这家没有校招门户」的事实，不是我们打错了。

## 已排除的租户（别再试）

| slug | 结论 | 怎么发现的 |
|---|---|---|
| `luckin` | **不是瑞幸**，页面自称「加入狂浪俱乐部」，`count=0` | 读页面标题 |
| `horizon` | **不是地平线**，页面自称「加入汉森」，`count=0` | 同上 |
| `haidilao` | 是海底捞，但**没有校招门户** | `campus`/`edu` → `code=-9000003` |
| `chagee` | 是霸王茶姬，校招只有 12 条（社招 85），太薄 | 全量翻页 |
| `yonghui` / `poizon` / `mogu` / `soulapp` / `wumart` | 归属对，活租户，**没有校招门户** | 同上 |
| `moonshot` / `horizonrobotics` / `keep` / `beingmate` / `missfresh` / `baic` / `hisense` | 活租户但 `count=0` | 同上 |

**域名 slug 不是归属判据** —— 必须读页面标题确认自称的公司名。
反面教训：找海底捞替补时先猜了 18 个 slug，16 个直接 `JSONDecodeError`。
**猜命名空间**（租户名、门户路径、body 参数）是这个项目复发三次的错法。

## 变更记录

| 版本 | 日期 | 变化 |
|---|---|---|
| v1 | 2026-08-05 | 首版。五家已接，校招入口全部改为用户核对过的地址 |
| v2 | 2026-08-06 | 海底捞（无校招门户）换成字节跳动 7395 条；校招合计 1222 → **8618**；商汤 159 → 160；补投递链接形状（必须带门户段）；已排除租户表扩到 15 个。来源：`docs/plans/003-校招门户采集.md` §0 偏差 4/6、§11 |
