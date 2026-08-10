---
来源: 用户实地核对入口（2026-08-05）+ 全量翻页实测（2026-08-06 16:50 核对库 / 17:07 主库）+ 投递页实地探测（2026-08-10 四租户）；每家的归属都用页面标题核实过
版本: v5
生效时间: 2026-08-10
权限范围: 公开
更新负责人: wujingyu
审核状态: 已审核
---

# 已核实的公司 → 门户对应关系

**收录标准：页面标题自称的公司名和我们认领的公司对得上。**
只靠域名 slug 猜归属栽过一次（见文末）。

## 已接的五家

| 公司 | 系统 | 社招入口 | 校招入口 | `website-path` | 门户下开放合计 | 其中 campus / intern |
|---|---|---|---|---|---|---|
| 腾讯 | 自建 | `join.qq.com/post.html` | 同一个站 | —— | **805** | 366 / 439 |
| 蔚来 | feishu `nio` | `nio.jobs.feishu.cn/` | **`campus.nio.com/#/`** | `campus` | **634** | 218 / 416 |
| 小鹏汽车 | feishu `xiaopeng` | `xiaopeng.jobs.feishu.cn/` | **`xiaopeng.jobs.feishu.cn/398875`** | `campus` | **431** | 312 / 119 |
| 字节跳动 | feishu `bytedance` | `bytedance.jobs.feishu.cn/` | **`jobs.bytedance.com/campus`** | `campus` | **7368** | 2073 / 5295 |
| 商汤科技 | feishu `sensetime` | `sensetime.jobs.feishu.cn/` | **`hr-jobs.sensetime.com/edu/`** | `edu` | **161** | 92 / 69 |

飞书四家合计 **8594**（634+431+7368+161），五家合计 **9399**。
测量时间 2026-08-06 16:50（核对库）/ 17:07（主库），两库逐源相等。

**2026-08-10 复量：腾讯 805 → 807，五家合计 9399 → 9401**，飞书四家逐源未变。
**是重测漂移不是改错** —— 上面那张表在它自己的测量时间上是对的，别去「修」它。
腾讯这条的历史轨迹：08-04 795 → 08-06 805 → 08-10 807。

**这一列的名字改过，因为老名字是个口径坑。** 它原来叫「校招条数」，值是
`627+436+7395+160 = 8618` —— 但那个数是**校招门户下的全部开放岗位，含实习**，
不是 `recruit_type=campus`。两个口径差得很远：飞书四家门户下 8594 条里，
`campus` 只有 2695 条，`intern` 有 5899 条。说「校招 8594 条」会让人以为
有 8594 个应届岗位。**报这个数必须说清是哪一个。**

`8618 → 8594` 不是改错，是**重测漂移**：v2 的数来自 00:43 那一轮，
16:50 重新同步后各家都动了（蔚来 +7、商汤 +1、小鹏 −5、字节 −27），
岗位池本来就是活的。同理腾讯实习 435 → 439。核对办法见文末命令。

**不含** 小鹏的 `398875`（335 条）：它是 `campus` 的子门户，
`campus ∩ 398875 = 335` 完全包含。**采了 `campus` 就别再采它。**

**海底捞被换掉了**：`campus`/`edu` 都回 `code=-9000003`，没有校招门户，
用户核对时也指出官网找不到校招入口。替补是字节跳动 —— 归属用页面标题核实过
（`bytedance.jobs.feishu.cn/campus/` 与 `jobs.bytedance.com/campus`
同为「字节跳动校园招聘官网」）。

**投递链接的形状**：`https://<host>/<portal>/position/<id>/detail`。
裸 `/position/<id>` 是 404（body 9 字节，四个租户一致）；
少 `/detail` 的 HTTP 是 200 但渲染「页面不存在」（2026-08-10 实测）。
见 `job-fields.md` v4。

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

## 复核命令

上面每一个条数都从这条命令出，**报数前重跑一遍**（岗位池是活的，数会漂）：

```bash
uv run python -c "
import collections
from pathlib import Path
from jobagent import db
rows=[dict(r) for r in db.connect(Path('data/jobagent.db')).execute('''
  SELECT source_key, recruit_type, COUNT(*) n FROM jobs
  WHERE closed_at IS NULL GROUP BY 1,2''')]
per=collections.defaultdict(dict); tot=collections.Counter()
for r in rows:
    per[r['source_key']][r['recruit_type'] or 'NULL']=r['n']; tot[r['source_key']]+=r['n']
for k in sorted(per): print(f'{k:32s} 合计 {tot[k]:5d}  {dict(per[k])}')
print('开放合计', sum(tot.values()))
print('飞书合计', sum(v for k,v in tot.items() if k.startswith('feishu:')))
"
```

配套看一眼是哪一轮同步出的数（漂移都能在这儿对上）：

```bash
uv run python -c "
from pathlib import Path
from jobagent import db
for r in db.connect(Path('data/jobagent.db')).execute(
    'SELECT started_at, source_key, status, fetched FROM runs ORDER BY started_at'):
    print(dict(r))
"
```

## 变更记录

| 版本 | 日期 | 变化 |
|---|---|---|
| v1 | 2026-08-05 | 首版。五家已接，校招入口全部改为用户核对过的地址 |
| v2 | 2026-08-06 | 海底捞（无校招门户）换成字节跳动 7395 条；校招合计 1222 → **8618**；商汤 159 → 160；补投递链接形状（必须带门户段）；已排除租户表扩到 15 个。来源：`docs/plans/003-校招门户采集.md` §0 偏差 4/6、§11 |
| v3 | 2026-08-06 | 「校招条数」列改名成「门户下开放合计」并拆出 campus / intern —— 老列名是口径坑：`8618` 是门户下**含实习**的全部开放岗位，不是应届岗位数（飞书 8594 条里 campus 只有 2695）。条数按 16:50/17:07 轮重测：蔚来 627→**634**、小鹏 436→**431**、字节 7395→**7368**、商汤 160→**161**、腾讯实习 435→**439**，飞书合计 8618→**8594**，五家合计 **9399**；**这是重测漂移不是改错**，v2 的数在 00:43 那一轮是对的。新增「复核命令」节（两条命令都实跑过，`runs` 的列名是 `fetched` 不是 `n_fetched`）。来源：`docs/plans/004-届别补全.md` §9 |
| v4 | 2026-08-10 | 投递链接形状加 `/detail`：`/<portal>/position/<id>` 的 HTTP 是 200 但**渲染「页面不存在」**，库里 8594 条飞书链接因此全是死的（四租户实地探测）。**判死活的判据改成渲染后的正文，不是状态码** —— SPA 的 404 在渲染层。来源：`docs/plans/010-飞书代投.md` §3 / §12 |
| v5 | 2026-08-10 | 补 2026-08-10 复量的漂移注：腾讯 **805 → 807**、五家合计 **9399 → 9401**，飞书四家逐源未变；写明「表在它自己的测量时间上是对的，别去修它」，并记下腾讯 795→805→807 的轨迹。表体和测量时间行保持原样 —— **带日期的观测不该被后来的观测覆盖写**。来源：`docs/plans/011-飞书届别第三通道.md` 收敛阶段的全文数字复核 |
