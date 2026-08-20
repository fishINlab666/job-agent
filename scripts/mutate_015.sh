#!/usr/bin/env zsh
# 逐条改坏方案 015 的新判据，确认每条都能单独变红。
# 用法: scripts/mutate_015.sh
# 不留副作用：每次改完立刻还原，最后校验 sha256 与开跑前一致。
# PYTHONDONTWRITEBYTECODE=1 是必须的 —— 这是重排式改动，.pyc 会让还原后的测试
# 假装还是红的（见 memory/reorder-mutations-leave-stale-pyc.md）。
set -euo pipefail
cd "${0:A:h}/.."
NZ=jobagent/normalize.py
BAK=$(mktemp); cp "$NZ" "$BAK"
SHA0=$(shasum -a 256 < "$NZ")
export PYTHONDONTWRITEBYTECODE=1

run() {  # run <标签> <该红的-k> <另一组-k> [说明] [另一组摘要]
  local label=$1 want_red=$2 other=$3 other_expect=${4:-全绿} other_summary=${5:-}
  local red_output green_output red green red_rc=0 green_rc=0
  red_output=$(.venv/bin/pytest tests/test_normalize.py -p no:cacheprovider -q -k "$want_red" 2>&1) || red_rc=$?
  green_output=$(.venv/bin/pytest tests/test_normalize.py -p no:cacheprovider -q -k "$other" 2>&1) || green_rc=$?
  red=${red_output##*$'\n'}
  green=${green_output##*$'\n'}
  print -r -- "  该红的一组 → $red"
  print -r -- "  另一组（预期$other_expect） → $green"
  [[ $red_rc -eq 1 && $red == *failed* && $red != *"no tests ran"* ]] || {
    print -r -- "  ✗ 目标组没有按预期失败"
    return 1
  }
  if [[ -n $other_summary ]]; then
    [[ $green_rc -eq 1 && $green == *$other_summary* ]] || {
      print -r -- "  ✗ 另一组的失败分布不符合预期"
      return 1
    }
  else
    [[ $green_rc -eq 0 ]] || {
      print -r -- "  ✗ 另一组没有保持全绿"
      return 1
    }
  fi
  print -r -- "  ✓ 变异结果符合预期"
}

mutate() { .venv/bin/python - "$1" "$2" <<'PY'
import pathlib, sys
p = pathlib.Path("jobagent/normalize.py"); s = p.read_text()
old, new = sys.argv[1], sys.argv[2]
assert s.count(old) == 1, f"要替换的串命中 {s.count(old)} 次，不是 1"
p.write_text(s.replace(old, new))
PY
}
restore() { cp "$BAK" "$NZ" }
cleanup() { restore; rm -f "$BAK" }
interrupted() {
  local code=$1
  trap - EXIT INT TERM
  cleanup
  exit "$code"
}
trap cleanup EXIT
trap 'interrupted 130' INT
trap 'interrupted 143' TERM

print "=== 1. 整层不加（回到 issue #9 的状态）==="
mutate 'if domain_at is not None and function_at is not None and domain_at < function_at:
        return "tech"' 'if False:
        return "tech"'
run 1 "does_not_steal" "test_tech_word_before_domain_word_stays_design"
restore

print "\n=== 2. 有序退化成纯 AND ==="
mutate 'domain_at is not None and function_at is not None and domain_at < function_at' \
       'domain_at is not None and function_at is not None'
run 2 "test_tech_word_before_domain_word_stays_design" "does_not_steal"
restore

print "\n=== 3. d==0 用真值判断 ==="
# 这条的第二列**期望 5 红 1 绿**，不是全绿：6 条目标里 5 条 d==0，只有
# 3D视觉研发（d==2）活下来。方案 §10 预测的就是这个分布 —— 只看「有测试红了」
# 会以为规则整体没生效，实际是漏了 d==0 那一类。分布本身才是判据。
mutate 'domain_at is not None and function_at is not None and domain_at < function_at' \
       'domain_at and function_at and domain_at < function_at'
run 3 "test_domain_word_at_index_zero_is_a_hit" "does_not_steal" \
      "5 红 1 绿" "5 failed, 1 passed"
restore

print "\n=== 4. 职能词表补回 数据/安全/硬件/模型 ==="
mutate '"后台", "后端", "前端", "客户端", "研发",' \
       '"数据", "安全", "硬件", "模型", "后台", "后端", "前端", "客户端", "研发",'
run 4 "operations_jobs_with_visual_domain or excludes_the_four_words" "does_not_steal"
restore

print "\n=== 5. 域词表补回 设计/美术 ==="
mutate '"视觉", "交互", "动效", "动画", "特效", "GUI", "多媒体",' \
       '"设计", "美术", "视觉", "交互", "动效", "动画", "特效", "GUI", "多媒体",'
run 5 "domain_list_excludes_function_words or tech_direction_stays_design" "does_not_steal"
restore

print "\n=== 6. 两表相交（把 研发 也塞进域词表）==="
mutate '"视觉", "交互", "动效", "动画", "特效", "GUI", "多媒体",' \
       '"视觉", "交互", "动效", "动画", "特效", "GUI", "多媒体", "研发",'
run 6 "test_the_two_lists_do_not_overlap" "does_not_steal"
restore

print "\n=== 7. 词表里写族名 ==="
mutate '"后台", "后端", "前端", "客户端", "研发",' \
       '"后台", "后端", "前端", "客户端", "研发", "tech",'
run 7 "test_word_lists_contain_no_family_names" "does_not_steal"
restore

print "\n=== 还原校验 ==="
SHA1=$(shasum -a 256 < "$NZ")
[[ "$SHA0" == "$SHA1" ]] || {
  print "  ✗ 文件没还原干净"
  exit 1
}
print "  ✓ sha256 与开跑前一致"
.venv/bin/pytest tests/test_normalize.py -p no:cacheprovider -q 2>&1 | tail -1
find . -name '*.pyc' -newer "$BAK" 2>/dev/null | head -3
