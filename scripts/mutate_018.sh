#!/usr/bin/env bash
# 方案 018 的判据逐条改坏，验每条都能单独变红。只验绿等于没验。
#
# 用法：bash scripts/mutate_018.sh
#
# 沿用 mutate_017.sh 的三道闸（-k 匹配 0 条算失败、改坏先看落点、还原校验 sha256），
# 018 自己加了第四道：
#   4. **30 个词逐个删，每个都要让「它自己那条用例」变 None。**
#      光跑 pytest 不够 —— 用例可能因为标题里还有别的职能词而照样绿。
#      `智驾系统安全培训生` 有 `安全` 兜着、`嵌入式软件培训生` 有 `软件` 兜着，
#      删掉 `系统`/`嵌入式` 它们照样判 tech。这两条是写测试时真挑错的用例，
#      被这道闸逮出来才换成 `BMS系统培训生`/`嵌入式EMC培训生`。
set -uo pipefail
cd "$(dirname "$0")/.."

NORM=jobagent/normalize.py
MEAS=scripts/measure_family_gaps.py
TMP=$(mktemp -d)
cp "$NORM" "$TMP/normalize.py.orig"
cp "$MEAS" "$TMP/measure.py.orig"
BEFORE_NORM=$(shasum -a 256 "$NORM" | cut -d' ' -f1)
BEFORE_MEAS=$(shasum -a 256 "$MEAS" | cut -d' ' -f1)

export PYTHONDONTWRITEBYTECODE=1
PYTEST=(.venv/bin/pytest -q -p no:cacheprovider)
FAILED=0

restore() { cp "$TMP/normalize.py.orig" "$NORM"; cp "$TMP/measure.py.orig" "$MEAS"; }

# 中途死掉也必须还原。真被咬过：`bash scripts/mutate_018.sh | head -45` —— head
# 读够 45 行就关管道，SIGPIPE 打死脚本，第 7 条改坏（`_split_last` 切第一个分隔符）
# 就留在了工作树里。下一次跑的时候「基线」本身是坏的，于是
#   第 6 条的对照组 1 failed  → 看着像「改坏影响面溢出」
#   第 7 条改坏没落上         → 看着像「perl 表达式写错」
#   还原后 1 failed           → 唯一指对方向的那行
# 三个症状里两个把人往错的地方带。改坏脚本自己留下的脏状态，症状长得和判据失守
# 一模一样 —— 所以还原不能只挂在正常路径上。
# 必须**只跑一次**。第一版是 `trap 'restore; rm -rf "$TMP"' EXIT HUP INT TERM PIPE`，
# 管道被 `| head -20` 关掉之后脚本每写一行都吃一次 SIGPIPE，trap 跟着反复触发 ——
# 实测约 50 次。第一次就把 `$TMP` 删了，后面所有还原都无源可复制，于是**装了 trap
# 比不装更坏**：不装只留下最后一条改坏，装了留下一堆。修「中途死掉不还原」的时候
# 自己造出一个更大的同类缺陷。
#
# 中间还写过一版用 `$BASHPID != $MAIN_PID` 挡子 shell —— 两个问题：macOS 自带的
# 是 bash 3.2，没有 `BASHPID`，`set -u` 直接报 unbound variable；而且这个判断根本
# 不需要，实测 bash 在子 shell 里会重置 EXIT trap（命令替换 + 显式 `( )` 各一次，
# EXIT 只触发 1 次）。多余的防护自己成了故障点。
CLEANED=0
cleanup() {
  [ "$CLEANED" = 1 ] && return 0
  CLEANED=1
  [ -f "$TMP/normalize.py.orig" ] && restore
  rm -rf "$TMP"
}
# 信号路径要还原完就退出，别让脚本带着改坏继续往下跑。
on_signal() { cleanup; trap - EXIT; exit 130; }
trap cleanup EXIT
trap on_signal HUP INT TERM PIPE

# landed <文件> <原件> [改坏后必须出现的字符串]
#
# 「文件变了」**不等于**「改坏落上了」。第三条参数是这次运行加的，起因是真被
# 咬过：第 3 条改坏本意是把 `软件` 从第 2 层挪进第 0 层，两条 perl 里
#   删除  s/\n    \(\("软件",\), "tech"\),//                 → 匹配上了
#   插入  s/^TECH_MARKERS = \(/.../m                         → 没匹配（真代码是
#                                                              `TECH_MARKERS: tuple[str, ...] = (`）
# 于是「挪」退化成「删」，和第 1 条改坏完全同义，而 diff 非空所以这一层放过了。
# 多组合式的改坏（删+插、挪位置）必须把插入侧的落点也断言一次。
landed() {
  local f=$1 orig=$2 must=${3:-}
  if diff -q "$orig" "$f" >/dev/null; then
    echo "  ✗ **改坏没落上**（文件和原件相同）—— perl 表达式没匹配到，先修脚本"
    return 1
  fi
  if [ -n "$must" ] && ! command grep -qF "$must" "$f"; then
    echo "  ✗ **改坏只落了一半** —— 文件变了，但 '$must' 没出现。"
    echo "     多半是删除侧匹配上、插入侧没匹配，改坏退化成了另一条。"
    return 1
  fi
  echo "  落点：$(diff "$orig" "$f" | command grep '^[<>]' | head -4 | sed 's/^/         /')"
  return 0
}

# run <标签> <该红的-k> <另一组-k> [另一组的预期] [改的文件] [改坏后必须出现的串]
#
# 另一组的结果原来只 echo 不判。那就是「只验绿等于没验」翻了一层：这一层验的是
# 「改坏的影响面没有溢出」，printf 出来没人比对等于没验。第 3 条改坏的另一组当时
# 是 `2 failed, 20 passed`，一路绿着过去了。现在预期是「全绿」就必须真全绿。
run() {
  local label=$1 red_k=$2 other_k=$3 other_expect=${4:-全绿} which=${5:-norm} must=${6:-}
  echo "───────────────────────────────────────────────"
  echo "改坏：$label"
  local f=$NORM orig=$TMP/normalize.py.orig
  [ "$which" = meas ] && { f=$MEAS; orig=$TMP/measure.py.orig; }
  if ! landed "$f" "$orig" "$must"; then FAILED=1; restore; return; fi
  local out
  out=$("${PYTEST[@]}" -k "$red_k" 2>&1 | tail -1)
  echo "  该红的一组（-k '$red_k'）：$out"
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
  echo "  另一组（-k '$other_k'，期望 ${other_expect}）：$out"
  case "$out" in
    *passed*|*failed*|*error*) : ;;
    *) echo "  ✗ **另一组的 -k 一条都没匹配上** —— 对照组是空的，等于没有对照组"
       FAILED=1; restore; return ;;
  esac
  # 预期全绿就必须全绿。预期本身写的是「非全绿」时（目前没有），这里跳过。
  case "$other_expect" in
    全绿*) case "$out" in
             *failed*|*error*)
               echo "  ✗ **对照组也红了** —— 改坏的影响面比预期大，或者预期写错了。"
               echo "     两种都得查：判据可能耦合，也可能这条改坏其实是另一条的同义词。"
               FAILED=1 ;;
           esac ;;
  esac
  restore
}

echo "=== 基线 ==="
"${PYTEST[@]}" 2>&1 | tail -1

# ── 第一部分：30 个词逐个删 ────────────────────────────────────────────
# 每个词删掉之后要同时满足两件事：
#   ① pytest 里 TestVocabGapWords018 变红
#   ② 该词「自己那条用例」的判定变成 None（证明测试是靠这个词绿的）
# ② 单独用 python 验，因为 pytest 的 -k 匹配不到中文参数化 id。
echo "───────────────────────────────────────────────"
echo "第一部分：30 个词逐个删（②的明细逐词打，①合起来验一次）"
WORDS_BAD=0
while IFS='|' read -r word title; do
  [ -z "$word" ] && continue
  perl -0pi -e "s/\\n    \\(\\(\"$word\",\\), \"\\w+\"\\),//" "$NORM"
  if diff -q "$TMP/normalize.py.orig" "$NORM" >/dev/null; then
    echo "  ✗ 「$word」改坏没落上 —— 词表里的写法和 perl 表达式不一致"
    WORDS_BAD=1; restore; continue
  fi
  got=$(PYTHONPATH="$PWD" .venv/bin/python -c "
import jobagent.normalize as n
print(n.family_from_title('$title'))
" 2>&1 | tail -1)
  if [ "$got" != "None" ]; then
    echo "  ✗ 「$word」删掉后 '$title' 仍判 $got —— 这条用例测的不是这个词"
    WORDS_BAD=1
  fi
  restore
# 这 30 条标题不是手写的，是量出来的：对每个词，从真库里找「删掉这个词判定就变
# None」的最短标题。手写会挑到 `商家运营策略实习生` 这种 —— `运营` 先命中，删
# `商家` 判定不变，这条闸会报 ✗ 而问题在用例不在词表。
done <<'WORDS'
软件|软件资产管理 - IT
系统|机械系统实习生
SRE|SRE实习生-SRE
嵌入式|【27届暑期】嵌入式EMC实习生
结构|【27届校招】电驱总成结构培训生
材料|【27届校招】材料技术培训生
热管理|【27届校招】热管理性能分析培训生
品质|高品质影视级TTS
电控|【27届校招】电子电控培训生
仿真|Metalens仿真实习生-PICO
传感|机器人触觉传感器实习生-Seed
交付|交付实习生（肇庆）
履约|履约治理策略实习生-TikTok Shop
产销|海外产销实习生
咨询|管理咨询实习生-飞书
售后|售后实习生（肇庆）
零售|新零售实习生（肇庆）
商家|商家分析实习生 - 抖音电商
渠道|【27届校招】渠道拓展培训生
达人|增长达人实习生-AI创新业务
结算|IDC商务结算 - AI算力基础设施
税务|税务实习生
资金|资金账户管理实习生-国际支付
内控|内控实习生-内控
成本|【27届校招】成本分析专业培训生
定价|UK/EU定价和补贴策略实习生-TikTok Shop
传播|企业文化传播实习生-企业文化
法规|【27届校招】标准法规培训生
物流|供应链与物流实习生-TikTok Shop
备件|【27届校招】备件包装培训生
WORDS
[ "$WORDS_BAD" = 0 ] && echo "  ✓ 30 个词各自都是「自己那条用例」的唯一决定者" \
  || { echo "  ✗ **有词的用例测的不是它自己**"; FAILED=1; }

# ①：随便删一个词，整个类必须红
perl -0pi -e 's/\n    \(\("税务",\), "finance"\),//' "$NORM"
run "删掉 税务（代表 30 词任意一个）" \
    "TestVocabGapWords018" "TestProcurementIsOther or TestAdvisorIsSales"

# ── 第二部分：位置和层级 ──────────────────────────────────────────────

# 2. 把 `结构` 挪到 `采购` 之前 —— IoT采购履行经理 会从 other 翻成 tech
perl -0pi -e 's/\n    \(\("结构",\), "tech"\),//' "$NORM"
perl -0pi -e 's/(    \(\("采购",\), "other"\),)/    (("结构",), "tech"),\n$1/' "$NORM"
run "结构 挪到 采购 之前" \
    "test_procurement_precedes_the_018_words or test_procurement_beats_domain" \
    "test_procurement_is_other" "全绿（采购本身还判 other）"

# 3. 把 `软件` 挪进第 0 层 TECH_MARKERS —— 越层，会盖住所有其他规则
#
# 插入侧的正则原来写的是 `^TECH_MARKERS = \(`，匹配不上 —— 真代码带类型标注
# （`TECH_MARKERS: tuple[str, ...] = (`）。结果这条只删不插，退化成第 1 条的同义词，
# 而两道闸都放过了：diff 非空，另一组的 2 failed 没人比对。第 6 个参数 `"软件",`
# 就是为这个加的。
perl -0pi -e 's/\n    \(\("软件",\), "tech"\),//' "$NORM"
perl -0pi -e 's/^(TECH_MARKERS[^=]*= \()/$1\n    "软件",/m' "$NORM"
run "软件 挪进第 0 层" "test_all_words_are_in_layer_two" \
    "test_word_rescues_its_target" "全绿（软件那两条照样判 tech，只是越层了）" \
    norm '    "软件",'

# ── 第三部分：否决词被加回来 ──────────────────────────────────────────

# 4. `培训` 加回 —— 「救回条数最多」的那个错，会吃掉小鹏全部工程培训生
perl -0pi -e 's/(    \(\("备件",\), "other"\),)/$1\n    (("培训",), "hr"),/' "$NORM"
run "培训 → hr 加回词表" \
    "test_rejected_words_stay_out or test_training_shaped_titles_still_undecidable" \
    "test_word_rescues_its_target" "全绿（30 个词自己的用例不受影响）"

# 5. `内容` 加回 —— 库级改族从 0 变 5。这条是**新判据能红的证明**。
perl -0pi -e 's/(    \(\("备件",\), "other"\),)/$1\n    (("内容",), "operations"),/' "$NORM"
echo "───────────────────────────────────────────────"
echo "改坏：内容 → operations 加回词表（验库级判据能红）"
if landed "$NORM" "$TMP/normalize.py.orig"; then
  n=$(PYTHONPATH="$PWD" .venv/bin/python scripts/measure_family_gaps.py db-effect 2>&1 \
      | command grep '库级改族' | command grep -o '[0-9]\+' | head -1)
  echo "  db-effect 的「库级改族」行：$n（期望非 0）"
  [ "${n:-0}" -gt 0 ] && echo "  ✓ 库级判据红了 —— 它不是恒为 0 的假判据" \
    || { echo "  ✗ **库级判据没红** —— 那它和 017 那个「改判 0」一样没用"; FAILED=1; }
  out=$("${PYTEST[@]}" -k "test_rejected_words_stay_out" 2>&1 | tail -1)
  echo "  pytest 那一层（-k test_rejected_words_stay_out）：$out"
else FAILED=1; fi
restore

# ── 第四部分：测量脚本自己的判据 ──────────────────────────────────────

# 6. `REGION_TAIL` 改成也吃词干里的 `大区` —— 基数会从 399 掉下去
perl -0pi -e 's/^REGION_TAIL = re\.compile\(.*$/REGION_TAIL = re.compile(r"[^()（）]*大区[^()（）]*[)）]?\\s*\$")/m' "$MEAS"
run "REGION_TAIL 吃掉词干里的大区" "TestRegionTailStripped" \
    "TestSplitLastUsesLastSeparator" 全绿 meas

# 7. `_split_last` 切第一个分隔符 —— 就是探针第一版那个真实缺陷
perl -0pi -e 's/    idx = max\(\(title\.rfind\(s\) for s in SEPS\), default=-1\)/    idx = min([i for i in (title.find(s) for s in SEPS) if i >= 0], default=-1)/' "$MEAS"
run "_split_last 切第一个分隔符（探针第一版的真实缺陷）" \
    "TestSplitLastUsesLastSeparator" "TestRegionTailStripped" 全绿 meas

# 8. `_judge_without` 不摘词 —— 所有库级数会变成「什么都没救回」
perl -0pi -e 's/        kept = tuple\(k for k in kws if k not in drop\)/        kept = kws/' "$MEAS"
run "_judge_without 不摘词（库级数全部失真）" \
    "TestJudgeWithoutIsWiredUp" "TestEndInsertIsBlind" 全绿 meas

# 9. 脚本里那份 30 词副本漂移 —— `rescued` 会少算一个词
# 正则原来带 4 个前导空格，可是 `("税务", "finance")` 在源文件里是行中间的第二项
# （`("结算", "finance"), ("税务", "finance"), ("资金", "finance"),`），匹配不上。
# 这条是 landed() 第一道闸自己逮出来的 —— 它本来就该逮这个。
perl -0pi -e 's/\("税务", "finance"\), //' "$MEAS"
run "脚本副本漂移（少一个 税务）" "test_keep_table_matches_normalize or TestKeepAndRejectAreDisjoint" \
    "TestSplitLastUsesLastSeparator" 全绿 meas

# 9b. `city_collapse()` 临时换掉 `REGION_TAIL` 之后不还原 —— 同一进程里后面每次
# `_stem_map()` 都退回 428 口径，分母静默变大，所有百分比一起偏小而输出照样正常。
perl -0pi -e 's/    finally:\n        globals\(\)\["REGION_TAIL"\] = saved/    finally:\n        pass/' "$MEAS"
run "city_collapse 不还原 REGION_TAIL" "test_city_collapse_restores_the_region_regex" \
    "test_region_regex_matches_tail" 全绿 meas

# 10. 否决表里写一个已采纳的词 —— 两张表必须互斥
perl -0pi -e 's/    \("培训", "hr", /    ("售后", "hr", /' "$MEAS"
run "否决表里写 售后（已在采纳表里）" "TestKeepAndRejectAreDisjoint" \
    "TestEndInsertIsBlind" 全绿 meas

echo "───────────────────────────────────────────────"
restore
AFTER_NORM=$(shasum -a 256 "$NORM" | cut -d' ' -f1)
AFTER_MEAS=$(shasum -a 256 "$MEAS" | cut -d' ' -f1)
[ "$BEFORE_NORM" = "$AFTER_NORM" ] && echo "normalize.py 已还原 ✓" \
  || { echo "normalize.py **没还原**"; exit 1; }
[ "$BEFORE_MEAS" = "$AFTER_MEAS" ] && echo "measure_family_gaps.py 已还原 ✓" \
  || { echo "measure_family_gaps.py **没还原**"; exit 1; }
find . -name '*.pyc' -newer "$TMP/normalize.py.orig" -not -path './.venv/*' -delete 2>/dev/null
echo "=== 还原后 ==="
"${PYTEST[@]}" 2>&1 | tail -1
rm -rf "$TMP"
[ "$FAILED" = 0 ] && echo "=== 全部改坏都单独变红 ✓ ===" \
  || { echo "=== **有条目没红或没落上，见上面的 ✗** ==="; exit 1; }
