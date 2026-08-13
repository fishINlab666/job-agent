#!/usr/bin/env bash
# 方案 017 的判据逐条改坏，验每条都能单独变红。只验绿等于没验。
#
# 用法：bash scripts/mutate_017.sh
# 跑完自动还原并校验 sha256 —— 改坏脚本自己留下脏文件是最坏的结果。
#
# 比 mutate_016.sh 多两道闸，各对应一次真踩过的坑：
#   1. **-k 匹配 0 条算失败。** 测试改名之后 `-k` 会静默匹配不到任何测试，
#      pytest 报 "no tests ran"，而 case 里只认 *failed*，于是显示成「没红」——
#      和「判据没测试守着」长得一模一样，结论却相反。
#   2. **每条改坏先打出落点。** mutate_016 的第 3 条 perl 插错了行（插在 UPDATE
#      之后而不是之前），只跳过了发事件，所以「没红」是脚本 bug 不是测试缺失。
#      现在每条改坏后 diff 一下，看不到 diff 就直接算脚本错。
set -uo pipefail
cd "$(dirname "$0")/.."

NORM=jobagent/normalize.py
MEAS=scripts/measure_family_gaps.py
TMP=$(mktemp -d)
cp "$NORM" "$TMP/normalize.py.orig"
cp "$MEAS" "$TMP/measure.py.orig"
BEFORE_NORM=$(shasum -a 256 "$NORM" | cut -d' ' -f1)
BEFORE_MEAS=$(shasum -a 256 "$MEAS" | cut -d' ' -f1)

# 不写 .pyc：重排式改坏会留过期字节码，还原后假装还是红的。
export PYTHONDONTWRITEBYTECODE=1
PYTEST=(.venv/bin/pytest -q -p no:cacheprovider)
FAILED=0

restore() { cp "$TMP/normalize.py.orig" "$NORM"; cp "$TMP/measure.py.orig" "$MEAS"; }

# 改坏有没有真落到文件上。没落上就不必看颜色了 —— 那是脚本的错，不是判据的。
landed() {
  local f=$1 orig=$2
  if diff -q "$orig" "$f" >/dev/null; then
    echo "  ✗ **改坏没落上**（文件和原件相同）—— perl 表达式没匹配到，先修脚本"
    return 1
  fi
  echo "  落点：$(diff "$orig" "$f" | grep '^[<>]' | head -4 | sed 's/^/         /')"
  return 0
}

# run <标签> <该红的-k> <另一组-k> [另一组的预期，默认「全绿」] [改的文件，默认 normalize]
run() {
  local label=$1 red_k=$2 other_k=$3 other_expect=${4:-全绿} which=${5:-norm}
  echo "───────────────────────────────────────────────"
  echo "改坏：$label"
  local f=$NORM orig=$TMP/normalize.py.orig
  [ "$which" = meas ] && { f=$MEAS; orig=$TMP/measure.py.orig; }
  if ! landed "$f" "$orig"; then FAILED=1; restore; return; fi
  local out
  out=$("${PYTEST[@]}" -k "$red_k" 2>&1 | tail -1)
  echo "  该红的一组（-k '$red_k'）：$out"
  # 判据顺序有讲究：先问「跑了吗」，再问「红了吗」。
  # -k 一条都没匹配上时 pytest 的末行是 `768 deselected in 0.48s` ——
  # 既没有 "no tests ran" 也没有 "0 selected"，所以只能反过来判：
  # 末行里没有 passed/failed/error 任何一个，就说明一条都没跑。
  # 第一版按 "no tests ran" 认，结果这种情况掉进了下面的「没红」分支，
  # 报成「判据没有测试守着」—— 同一个 ✗ 指向两个相反的结论。
  case "$out" in
    *passed*|*failed*|*error*) : ;;
    *) echo "  ✗ **-k 一条测试都没匹配上** —— 测试大概改名了，这条等于没验"
       FAILED=1; restore; return ;;
  esac
  case "$out" in
    *failed*|*error*) echo "  ✓ 红了" ;;
    *)                echo "  ✗ **没红** —— 这条判据没有测试守着"; FAILED=1 ;;
  esac
  out=$("${PYTEST[@]}" -k "$other_k" 2>&1 | tail -1)
  echo "  另一组（-k '$other_k'，期望 $other_expect）：$out"
  restore
}

echo "=== 基线 ==="
"${PYTEST[@]}" 2>&1 | tail -1

# 1. `顾问` 不加 —— 回到 issue #8 的现状
perl -0pi -e 's/"解决方案", "顾问"/"解决方案"/' "$NORM"
run "顾问 不加进 sales 组" "TestAdvisorIsSales" "TestProcurementIsOther or TestServiceIsDeliberately"

# 2. `顾问` 写成 sales 之外的族 —— 判得出但判错
perl -0pi -e 's/"解决方案", "顾问"\), "sales"/"解决方案"), "sales"),\n    (("顾问",), "hr"/' "$NORM"
run "顾问 判成 hr" "TestAdvisorIsSales" "TestProcurementIsOther"

# 3. `采购` 挪进 COMPOUND_RULES —— 实测会改判 5 条
perl -0pi -e 's/\(\("物业", "办公规划", "行政"\), "other"\)/(("物业", "办公规划", "行政", "采购"), "other")/' "$NORM"
perl -0pi -e 's/\n    \(\("采购",\), "other"\),//' "$NORM"
run "采购 挪进 COMPOUND_RULES（第 2 层之前）" \
    "test_procurement_direction_does_not_steal or test_other_group_is_last" \
    "test_procurement_is_other" "全绿（采购本身还是判 other，只是抢了别人）"

# 4. `采购` 组挪到 tech 之前 —— 只有 `硬件采购实习生` 会翻
#    这条和 3 分开是因为它们红的原因不同：3 是整层抢跑，4 是同层内换位次。
perl -0pi -e 's/\n    \(\("采购",\), "other"\),//' "$NORM"
perl -0pi -e 's/(    \(\("数据", "安全", "硬件")/    (("采购",), "other"),\n$1/' "$NORM"
run "采购 组挪到 tech 组之前" \
    "test_procurement_direction_does_not_steal or test_other_group_is_last" \
    "TestAdvisorIsSales"

# 5. `服务` 加进 sales 组 —— 数据上看着划算的那个错
perl -0pi -e 's/"解决方案", "顾问"/"解决方案", "顾问", "服务"/' "$NORM"
run "服务 加进 sales 组" "TestServiceIsDeliberatelyNotSales" "TestAdvisorIsSales" \
    "全绿（顾问不受影响）"

# 6. 试算函数抄一份规则表 —— `candidates` 的数会静默按老表算
perl -0pi -e 's/    rules = list\(n\.TITLE_RULES\)/    rules = [(kw, f) for kw, f in n.TITLE_RULES if f != "sales"]/' "$MEAS"
run "试算函数抄一份规则表（漏掉 sales 组）" \
    "TestSimulatorMatchesProduction or TestInsertPositionMatters" \
    "TestDecorationListIsWordsNotFamilies" 全绿 meas

# 7. `at_end` 参数不接线 —— 两个插入位置变等价，看着验了两种其实只验了一种
perl -0pi -e 's/        if idx is not None and not at_end:/        if False:/' "$MEAS"
run "at_end 参数不接线（永远加在末尾）" \
    "TestInsertPositionMatters" "TestSimulatorMatchesProduction" 全绿 meas

echo "───────────────────────────────────────────────"
restore
AFTER_NORM=$(shasum -a 256 "$NORM" | cut -d' ' -f1)
AFTER_MEAS=$(shasum -a 256 "$MEAS" | cut -d' ' -f1)
[ "$BEFORE_NORM" = "$AFTER_NORM" ] && echo "normalize.py 已还原 ✓" \
  || { echo "normalize.py **没还原**"; exit 1; }
[ "$BEFORE_MEAS" = "$AFTER_MEAS" ] && echo "measure_family_gaps.py 已还原 ✓" \
  || { echo "measure_family_gaps.py **没还原**"; exit 1; }
# 还原后必须回到全绿，且不许有过期 .pyc 撑着
find . -name '*.pyc' -newer "$TMP/normalize.py.orig" -not -path './.venv/*' -delete 2>/dev/null
echo "=== 还原后 ==="
"${PYTEST[@]}" 2>&1 | tail -1
rm -rf "$TMP"
[ "$FAILED" = 0 ] && echo "=== 7 条改坏全部单独变红 ✓ ===" \
  || { echo "=== **有条目没红或没落上，见上面的 ✗** ==="; exit 1; }

