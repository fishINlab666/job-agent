# 方案：diff 为空时不发 `job_updated`，并把「指纹与列不同步」这件事记成可观测的

> 编号 `016` · 日期 `2026-08` · 状态：已实现（§11–§13 已回填）
> 涉及文件：`jobagent/ingest.py`、`jobagent/cli.py`、`tests/test_ingest.py`、`tests/test_cli.py`、`scripts/mutate_016.sh`

---

## 0. 当前进度（边做边回写，不是写完方案就不管了）

| 步骤 | 状态 | 核实命令 / 实际偏差 |
|---|---|---|
| 复现空 diff 事件 | 已核实 | `PYTHONPATH="$PWD" .venv/bin/python scripts/measure_family_rule.py sync-events` → 情形 1 打出 `[('job_updated', {})]` |
| 确认不是字段遗漏 | 已核实 | `_fp()` 六个字段（[ingest.py:52](../../jobagent/ingest.py#L52)）vs diff 推导式五个 + `cities` 单独补（[ingest.py:398-412](../../jobagent/ingest.py#L398)）= 六个都在 |
| 确认「diff 为空不许发事件」当前没有测试 | 已核实 | `grep -rn 'diff.*==.*{}\|空 diff\|diff 为空' tests/` → 只有注释，没有断言 |
| 基线测试数 | 已核实 | `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q -p no:cacheprovider` → 725 passed |
| 存量待重算指纹的行数 | 已核实 | §9 命令 B → **8594 行**（全库 9403 的 91.4%），飞书四源在架行的全部，腾讯 807 条一条不差 |
| 8594 是否全部来自 `/detail` 后缀 | 已核实 | §9 命令 B 第二段：剥掉后缀重算，8594/8594 全部对上，**0 条解释不了** |
| 代码实现 | 已核实 | `ingest.py` 加 `fp_desync` + `stats["fingerprint_desync"]`，发事件条件改 `not bootstrap and not fp_desync`；`cli.py` 加一行输出 |
| 测试 | 已核实 | `pytest -q` → **733 passed**（基线 725，新增 8：`test_ingest.py` 6 + `test_cli.py` 2） |
| 逐条改坏验证 | 已核实 | `bash scripts/mutate_016.sh` → 7 条改坏**全部单独变红**，还原后 733 passed，sha256 校验通过 |
| 存量 8594 行会不会真的静默 | 已核实 | §9 命令 E：字节 7368 行喂进去，`updated=7368` / `fingerprint_desync=7368` / **新增事件 0 条**；第二轮 `updated=0` |
| 改前对照（不是我说修好了就修好了） | 已核实 | 剥掉守卫重建「改前」模块跑同一批：`job_updated` 7368 条，**7368/7368 全是空 diff** |
| 偏差：改坏 3 第一版插错位置 | 已核实 | `continue` 插在 `stats["updated"] += 1` 后面，而 UPDATE 在它上面十几行 —— 那个 continue 只跳过发事件（正常行为），所以「没红」是改坏写错了。挪到 UPDATE 之前才红 |
| 偏差：改坏 3 的另一组预期列写错 | 已核实 | 跳过 UPDATE 同时跳过了 `stats["updated"] += 1`，所以另一组红 1 条是对的。改成声明真实分布，同方案 015 改坏 3 那次 |

---

## 一、产品设计

### 1. 为什么现在做

用户收到「岗位 XXX 有更新」，点开什么都没有。事件本身是噪声，但更糟的是它**消耗信任**：
这个项目给用户的价值就是「有变化我告诉你」，一旦通知里出现空的，用户下次就会开始
怀疑所有通知。

而且这是**同一个症状的第三次出现**：

| 次数 | 成因 | 修法 | 规模 |
|---|---|---|---|
| 1 | `cities` 在 `_fp()` 里但不在 diff 里 | 把字段补进 diff（方案 006 问题 1） | 真库 16 条 |
| 2 | 同上，`cities` 的 JSON 字符串 vs list 比较 | 两边都过 `_cities()` 归一 | 同上 |
| 3 | `repair_apply_url` 改列不碰指纹 | 本方案 | 存量 8594 行 |

前两次都是「把当次的成因修掉」，所以第三次照样发生。这次要守的是**形状**：
不管什么成因导致「指纹变了但字段全等」，都不许发一条空的 `job_updated`。

### 2. 用户会看到什么

`sync` 之后：

- **不再有空 diff 的 `job_updated`**。事件数从 8594 降到 0（存量修复那批）。
- `sync` 的输出多一行，只在非 0 时打：
  ```
  指纹与列不同步 8594 条（已重算指纹，未发事件）
  ```
  这行是给**我**看的，不是给用户看的日常信息 —— 它的意义是「有人绕过 sync 改了库」。
  正常情况下它恒为 0，非 0 就说明刚跑过一个修复命令，或者有 bug。

### 3. 哪些是已核实的，哪些是我猜的

**已核实：**

- 空 diff 事件能复现（`sync-events` 情形 1）。
- `_fp()` 六字段和 diff 六字段完全对齐，**不是字段遗漏**。
- `db.add_event` 在 [ingest.py:428](../../jobagent/ingest.py#L428) 无条件调用，没有空 diff 守卫。
- 「diff 为空不许发事件」这条断言当前不存在（grep 过 `tests/`）。
- `repair_apply_url` 的硬约束 1「只写 apply_url 一列，不碰 fingerprint」写在 docstring 里，
  `refresh_grad_year` 是同款约束，各有测试守着。

**我猜的（要在 §9 里验掉）：**

- **修完之后这批行会不会真的一条事件都不发。** 走的是「重算指纹 + 不发事件」，
  理论上下一轮就同步了，但没试算过（§9 命令 E）。

**实现前补量掉的（原本列在这里当「我猜的」）：**

- **存量 8594 行**已核实（§9 命令 B）。issue #11 沿用的是 `repair_apply_url` 当时改的行数，
  我原以为中间跑过 sync 会消掉一部分 —— 实际一条都没消，因为消它需要**该行别的字段
  也变**，而这批岗位的标题/城市/部门在这段时间里没动。
- 8594 恰好等于飞书四源在架行的全部，两个数相等本该先当算错看（记忆：两列数字恰好
  相同先当算错）。验法是**反向重建**：剥掉 `/detail` 后缀重算指纹，8594/8594 全部对上、
  0 条解释不了；同时腾讯 807 条在不剥的情况下全对得上 —— 后者反证我的 `_fp` 重建
  没有系统性错误，否则腾讯也会全不同步。

---

## 二、具体实现

### 4. 数据长什么样，空值怎么办

三种状态要分清，现在的代码把后两种混成一种：

| 状态 | `prev["fingerprint"] != fp` | diff | 该做什么 |
|---|---|---|---|
| 真的变了 | 真 | 非空 | UPDATE + 发 `job_updated` |
| **指纹与列不同步** | 真 | **空** | UPDATE（重算指纹）+ **不发事件** + 计数 |
| 没变 | 假 | — | 只更 `last_seen_at` |

`reopened` 是独立维度，**不受这条守卫影响**：一个岗位重新开放，diff 可能确实是空的
（下线又原样上线，六个字段都没变），但 `job_reopened` 是必须发的 —— 它描述的是
`closed_at: 有值 → NULL` 这个状态转移，而那一列压根不在 diff 的六个字段里。
把它一起判空会漏掉真实信号，方向和这次要修的正好相反。

### 5. 硬约束在哪一行

1. **`job_reopened` 不判空。** 守卫只作用于 `job_updated`。
   写成 `if reopened or diff:` 而不是 `if diff:`。
2. **UPDATE 照旧执行。** 不发事件 ≠ 不写库。指纹必须重算，否则每轮 sync 都会
   重新进这个分支，计数器永远非 0，「不同步」这个信号就永久失真了。
3. **计数器要出现在 `sync` 的输出里。** 只加 `stats` 不打出来等于没有 ——
   方向 2 和方向 3 的差别全在这一条上。
4. **不动 `repair_apply_url` / `refresh_grad_year` 的硬约束。** 它们不碰指纹是对的
   （见 §8）。

### 6. 判据的粒度

判据是 `diff == {}`，不是「`apply_url` 变了但值相同」之类的具体成因。

选这个粒度的理由：前两次都是按成因修的，第三次照样发生。`diff` 是**发事件那一刻
手上唯一的事实** —— 事件的全部内容就是它，空的就没有可通知的东西。这个判据和成因
无关，所以第四种成因出现时它照样拦得住，并且会通过计数器**报出来**而不是静默。

### 7. 数字的口径

| 数 | 值 | 口径 | 测量日期 |
|---|---|---|---|
| 基线测试 | 725 passed | `pytest -q` 全量 | 2026-08-13 |
| 历史空 diff（`cities` 那次） | 16 条 | 真库 events 表，方案 006 问题 1 | 2026-08-07 |
| 存量待重算指纹的行 | **8594**（全库 9403 的 91.4%） | `jobs` 表里 `fingerprint != _fp(该行)` 的行数，含已下线 | 2026-08-13 |
| ↳ 其中在架 | 8594（全部） | 同上 + `closed_at IS NULL` | 2026-08-13 |
| ↳ 能被 `/detail` 后缀解释 | 8594 / 8594，0 条解释不了 | 剥掉后缀重算指纹再比 | 2026-08-13 |
| 修复后新增的空 diff 事件 | 应为 0 | 临时库试算一轮 sync | 实现后量 |

第三行原本是 §3 点名的「我猜的」，已经量掉了：**恰好还是 8594**，一条都没被 sync 消掉。
消它需要该行别的字段也变，而这批岗位的标题/城市/部门这段时间没动。

按源：`feishu:bytedance:campus` 7368、`feishu:nio:campus` 634、`feishu:xiaopeng:campus` 431、
`feishu:sensetime:edu` 161，`tencent_join` **0**。腾讯是 0 因为 `repair_apply_url` 只扫
`source_key LIKE 'feishu%'`。

**不带测量日期的计数迟早变成假的「已核实」**（002 §11 记过同一条），所以上面每行都带日期。

### 8. 这次明确不做什么

- **不让修复命令重算指纹。** 那会破它们自己的硬约束，而且指纹重算后下一轮 sync
  完全静默 —— 8594 行 `apply_url` 变了却一条事件都没有，那是另一种说谎。
  现在的分工是对的：修复命令改列，sync 负责「这个变化要不要告诉用户」。
- **不做历史事件清理。** 库里已有的空 diff 事件是历史事实，删掉等于篡改。
  它们的存在正是这个 issue 的证据。
- **不改 `job_reopened` 的口径**（§4 说明了为什么）。
- **不动 #9 的东西。** 那个 issue 走的是相反路线（不写修复命令、交给 sync 自愈），
  产出的 diff 如实且一次性。

---

## 三、怎么验

### 9. 验证命令

| 编号 | 命令 | 验的是 | 结果 |
|---|---|---|---|
| A | `PYTHONPATH="$PWD" .venv/bin/python scripts/measure_family_rule.py sync-events` | 改前情形 1 打空 diff | 改前 `[('job_updated', {})]` |
| B | `PYTHONPATH="$PWD" .venv/bin/python scripts/measure_desync.py count` | §7 存量行数 | 8594，其中 8594/8594 能被 `/detail` 解释 |
| C | `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest tests/test_ingest.py -q -p no:cacheprovider -k EmptyDiff` | 本方案 ingest 侧的测试 | 6 passed |
| D | `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q -p no:cacheprovider` | 全量不回归 | 733 passed（基线 725） |
| E | `PYTHONPATH="$PWD" .venv/bin/python scripts/measure_desync.py dry-sync` | 存量修完真的静默 | 7368 行 → 事件 0 条；第二轮 `updated=0` |
| F | `bash scripts/mutate_016.sh` | 每条新判据都能单独变红 | 7/7 变红，还原后 733 passed |
| G | `PYTHONPATH="$PWD" .venv/bin/python scripts/measure_desync.py before-after` | 改前对照 | 改前 7368 条事件，7368/7368 空 diff |

B/E/G 三条落在 `scripts/measure_desync.py` 里，不写成 heredoc —— §0 每行「已核实」都得配一条能跑的命令，
埋在 markdown 里的长 heredoc 没人会真去跑（plan 001 §12 引用一个不存在的测试就是这么来的）。

### 10. 测试钉的是哪几条

落地 8 条（方案原稿写 6 条，偏差见 §11）。`TestEmptyDiffEmitsNothing` 在
`tests/test_ingest.py`，`TestSyncSurfacesDesyncCount` 在 `tests/test_cli.py`：

| 测试 | 钉住什么 | 改坏它的那条（`mutate_016.sh` 编号） |
|---|---|---|
| `test_empty_diff_emits_no_job_updated` | 指纹不同步 + 字段全等 → 0 条事件 | 1 守卫不加 |
| `test_reopened_with_empty_diff_still_emits` | `job_reopened` 不受守卫影响 | 2 守卫写成 `if diff:` |
| `test_empty_diff_still_updates_fingerprint` | 不发事件但指纹必须重算，且第二轮计数回 0 | 3 空 diff 时跳过 UPDATE |
| `test_empty_diff_is_counted_in_stats` | 计数器数得对（2 行 → 2） | 4 只判空不计数（退化成方向 2） |
| `test_clean_sync_reports_zero_desync` | 正常变更时计数必须是 0 | 5 计数器恒真 |
| `test_nonempty_diff_still_emits` | 真实变化照发，diff 内容对 | 5 计数器恒真 |
| `test_nonzero_desync_is_printed` | §5 约束 3：数字要能被看见 | 6 CLI 不打这个数 |
| `test_zero_desync_is_not_printed` | 0 不许打，否则每轮一行噪声 | 7 CLI 无条件打 |

第二条是这批里最容易写错的：`if diff:` 读起来完全合理，但会吃掉真实的重新开放信号。

倒数两条**必须走 CLI**，测 stats 不算：方案 §8 的方向 2 和方向 3 的差别全在「打不打出来」
这一条上，只加 stats 键不打印，等于选了方向 2 还以为选了方向 3。

---

## 四、复盘（实现完回填，不是写方案时填）

### 11. 方案和实现差在哪

两处，都不改结论：

1. **测试从 6 条变成 8 条。** 原稿那条叫「stats 计数器要出现在 CLI 输出里」的用例，
   落地时拆成 `test_nonzero_desync_is_printed` + `test_zero_desync_is_not_printed`：
   「打出来」和「别每轮都打」是两个判据，合成一条的话第二个改不坏。
   另外补了 `test_clean_sync_reports_zero_desync` 当反向对照 —— 没有它的话
   「计数器恒真」这个改坏红不了。§10 的表已按落地结果重写。
2. **多了 `scripts/measure_desync.py`。** §9 原本写「待写」，落地时发现 B/E/G 三条各二三十行，
   写成 markdown 里的 heredoc 没人会跑（plan 001 §12 那次就是这样），所以做成脚本。

存量规模这个数**没有偏差**：实现前量出来还是 8594，和 issue #11 沿用的数一样。我原以为
中间跑过 sync 会消掉一部分，实际一条没消 —— 消它需要该行**别的**字段也变。

### 12. 实现中踩到的坑

1. **改坏 3 第一版插错了位置。** `continue` 插在 `stats["updated"] += 1` 后面，
   而 UPDATE 在它上面十几行 —— 那个 `continue` 只跳过了发事件（正常行为），
   指纹照样重算。看起来像「这条判据没有测试守着」，实际是**改坏本身写错了**。
   教训：改坏脚本自己也要验，判断方式是打出改坏后的那几行源码确认落点。
2. **改坏 3 的另一组预期列又写错了。** 跳过 UPDATE 同时跳过了 `stats["updated"] += 1`，
   所以另一组红 1 条是对的。和方案 015 改坏 3 **一模一样的错**：测量对，标签错。
   `run()` 的第 4 个参数就是为这个存在的，这次记得用了但第一遍还是漏了。
3. **`asdict()` 只吃真 dataclass。** 试算脚本第一版用 `class R: pass` 拼对象，
   `sync` 里 `payload = asdict(j)` 直接抛。必须构造真 `RawJob`。
4. **`RawJob` 不在 `jobagent.models`。** 它在 `jobagent/adapters/base.py`，
   猜路径浪费了一轮。
5. **文本替换重建「改前」模块要用真 package 名。** `types.ModuleType('ingest_before')`
   + `exec` 会在 `ingest.py` 的相对 import 上抛 `attempted relative import with no
   known parent package`。得用 `importlib.util.spec_from_file_location("jobagent.ingest_before", …)`。
6. **8594 恰好等于飞书四源在架行的全部，先当算错看。** 按记忆里那条「两列数字恰好相同
   先当算错」，用反向重建验：剥掉 `/detail` 重算，8594/8594 全对上、0 条解释不了；
   同时腾讯 809/809 在不剥的情况下全对得上 —— 后者是关键，它反证 `_fp` 重建没有系统性
   错误。少了对照组，「飞书全不同步」也可能只是我重建错了。

### 13. 如果重来，方案里该提前写上哪句话

| 该提前写的 | 为什么 |
|---|---|
| 「改坏脚本的每条改坏，都要打出改坏后的源码确认落点」 | 坑 1，第二次踩（015 是 `.pyc`，这次是插错行）。改坏不生效和判据没守住，症状一模一样 |
| 「`run()` 的预期列必须声明真实分布，默认值不是全绿」 | 坑 2，和 015 改坏 3 同一个错。默认「全绿」会诱导我不去想连带影响 |
| 「试算 sync 必须构造真 `RawJob`（在 `adapters/base.py`）」 | 坑 3+4，两轮试错 |
| 「重建改前模块用 `spec_from_file_location` 配 `jobagent.` 前缀」 | 坑 5。这是第二次重建改前模块了（015 §12 也有一次），下次直接抄 |
| 「计数类的数字，先找一个**没被影响的对照组**再信它」 | 坑 6。腾讯 809/809 才是让 8594 可信的那一半，而方案原稿里没写这一步 |
