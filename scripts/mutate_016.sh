#!/usr/bin/env bash
# 方案 016 的判据逐条改坏，验每条都能单独变红。只验绿等于没验。
#
# 用法：bash scripts/mutate_016.sh
# 跑完自动还原并校验 sha256 —— 改坏脚本自己留下脏文件是最坏的结果。
set -uo pipefail
cd "$(dirname "$0")/.."

ING=jobagent/ingest.py
CLI=jobagent/cli.py
TMP=$(mktemp -d)
cp "$ING" "$TMP/ingest.py.orig"
cp "$CLI" "$TMP/cli.py.orig"
BEFORE_ING=$(shasum -a 256 "$ING" | cut -d' ' -f1)
BEFORE_CLI=$(shasum -a 256 "$CLI" | cut -d' ' -f1)

# 不写 .pyc：重排式改坏会留过期字节码，还原后假装还是红的。
export PYTHONDONTWRITEBYTECODE=1
PYTEST=(.venv/bin/pytest -q -p no:cacheprovider)

restore() { cp "$TMP/ingest.py.orig" "$ING"; cp "$TMP/cli.py.orig" "$CLI"; }

# run <标签> <该红的-k> <另一组-k> [另一组的预期，默认「全绿」]
run() {
  local label=$1 red_k=$2 other_k=$3 other_expect=${4:-全绿}
  echo "───────────────────────────────────────────────"
  echo "改坏：$label"
  local out
  out=$("${PYTEST[@]}" -k "$red_k" 2>&1 | tail -1)
  echo "  该红的一组（-k '$red_k'）：$out"
  case "$out" in
    *failed*) echo "  ✓ 红了" ;;
    *)        echo "  ✗ **没红** —— 这条判据没有测试守着" ;;
  esac
  out=$("${PYTEST[@]}" -k "$other_k" 2>&1 | tail -1)
  echo "  另一组（-k '$other_k'，期望 $other_expect）：$out"
  restore
}

echo "=== 基线 ==="
"${PYTEST[@]}" 2>&1 | tail -1

# 1. 整条守卫不加 —— 回到 bug 本身
perl -0pi -e 's/if not bootstrap and not fp_desync:/if not bootstrap:/' "$ING"
run "守卫不加（空 diff 照发事件）" "test_empty_diff_emits_no_job_updated" "TestReopen or TestCitiesDiff"

# 2. 守卫写成 `if diff:` —— 吃掉重新开放信号
perl -0pi -e 's/fp_desync = not diff and not reopened/fp_desync = not diff/' "$ING"
run "守卫误判 reopened（写成 if diff:）" "test_reopened_with_empty_diff_still_emits" "test_empty_diff_emits_no_job_updated"

# 3. 判空但不重算指纹（跳过 UPDATE）
#    continue 必须插在 UPDATE **之前**。第一版插在 `stats["updated"] += 1` 后面，
#    而 UPDATE 在它上面十几行 —— 那个 continue 只跳过了发事件（正常行为），
#    指纹照样重算，所以「没红」是改坏写错了，不是测试没守住。
perl -0pi -e 's/(\s+)if fp_desync:\n(\s+)stats\["fingerprint_desync"\] \+= 1/$1if fp_desync:\n$2stats["fingerprint_desync"] += 1\n$2continue/' "$ING"
#    另一组也红 1 条是**对的**，不是漏网：continue 跳过 UPDATE 的同时跳过了
#    `stats["updated"] += 1`（它在 UPDATE 下面），而那条用例断言了 updated == 1。
#    这里声明真实分布而不是写「全绿」—— 方案 015 改坏 3 踩过同一个坑：
#    测量是对的，预期列写错了，看起来像判据失效。
run "空 diff 时跳过 UPDATE（指纹不重算）" "test_empty_diff_still_updates_fingerprint" "test_empty_diff_emits_no_job_updated" "1 红（updated 计数也被跳过）"

# 4. 只判空、不计数 —— 退化成方案 016 的方向 2
perl -0pi -e 's/(\s+)if fp_desync:\n\s+stats\["fingerprint_desync"\] \+= 1/$1if False:\n$1    stats["fingerprint_desync"] += 1/' "$ING"
run "不计数（退化成方向 2）" "test_empty_diff_is_counted_in_stats" "test_empty_diff_emits_no_job_updated"

# 5. 计数器恒真 —— 正常同步也报不同步，报警失去意义
perl -0pi -e 's/fp_desync = not diff and not reopened/fp_desync = True/' "$ING"
run "计数器恒真" "test_clean_sync_reports_zero_desync or test_nonempty_diff_still_emits" "test_empty_diff_emits_no_job_updated"

# 6. CLI 不打这个数 —— 方案 016 §5 约束 3
perl -0pi -e 's/f"  \[dim\]指纹与列不同步 \{desync\} 条（已重算指纹，未发事件）\[\/dim\]"/f"  [dim](skip)[\/dim]"/' "$CLI"
run "CLI 不打这个数" "test_nonzero_desync_is_printed" "TestEmptyDiffEmitsNothing"

# 7. CLI 无条件打 —— 正常每轮一行噪声
perl -0pi -e 's/(\s+)if desync:/$1if True:/' "$CLI"
run "CLI 无条件打（0 也打）" "test_zero_desync_is_not_printed" "test_nonzero_desync_is_printed"

echo "───────────────────────────────────────────────"
restore
AFTER_ING=$(shasum -a 256 "$ING" | cut -d' ' -f1)
AFTER_CLI=$(shasum -a 256 "$CLI" | cut -d' ' -f1)
[ "$BEFORE_ING" = "$AFTER_ING" ] && echo "ingest.py 已还原 ✓" || { echo "ingest.py **没还原**"; exit 1; }
[ "$BEFORE_CLI" = "$AFTER_CLI" ] && echo "cli.py 已还原 ✓" || { echo "cli.py **没还原**"; exit 1; }
# 还原后必须回到全绿，且不许有过期 .pyc 撑着
find . -name '*.pyc' -newer "$TMP/ingest.py.orig" -not -path './.venv/*' -delete 2>/dev/null
echo "=== 还原后 ==="
"${PYTEST[@]}" 2>&1 | tail -1
rm -rf "$TMP"
