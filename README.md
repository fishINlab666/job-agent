# 校招 Agent

自动化的校招岗位监控与推荐系统，专为 26/27 届产品/运营岗求职者设计。

## 核心功能

**M1-M7 已完成**：
- **M1 数据采集**：适配器模式对接各公司招聘 API
- **M2 变更检测**：基于指纹的增量 diff，产出类型化事件流
- **M3 用户画像**：YAML 配置文件定义筛选条件
- **M4 智能匹配**：规则引擎过滤 + 关键词打分
- **M5 CLI 界面**：Rich 格式化表格，支持增量推送

- **M6 代投（两阶段）**：Playwright 填表 + 简历上传，**填完停下等你确认才提交**
- **M7 投递记录**：只读的投递漏斗与拦截原因

**待实现**：
- 邮件解析跟踪投递状态
- 更多公司适配器（阿里、华为、美团等）
- 折叠段字段（工作经历 / 项目经历 / 证书）—— 机制已通，等画像层支持多条目

**M6 的实测边界**（说清楚比说漂亮重要）：填表、上传、下拉选择、隐私政策勾选、
判据体检都在真页面上验过；**`execute()` 最后那一下「提交」从未真跑过**，
所以 `_is_success` / `_is_duplicate` 的成功文案仍是推测的 —— 只验过「未提交时不误报」。

## 快速开始

### 安装

```bash
# 克隆项目
cd job-agent

# 安装依赖（使用 uv 管理环境）
uv sync

# 初始化数据库
uv run python -m jobagent.cli init
```

### 配置画像

编辑 `profile.yaml` 设置你的筛选条件：

```yaml
intent:
  grad_years: ["26", "27"]              # 毕业届别
  families: ["operations", "product"]   # 岗位族：运营/产品
  cities: ["北京", "上海", "杭州", "广州", "深圳"]
  recruit_types: ["campus", "intern"]   # 校招/实习
  boost_keywords: ["内容运营", "产品运营", "用户运营", "策略"]
  exclude_keywords: ["外包", "派遣", "客服专员"]

identity:  # M6 自动填表用
  name: "张三"
  phone: "13800138000"
  email: "zhangsan@example.com"
  school: "清华大学"
  major: "计算机科学与技术"
```

### 基本使用

```bash
# 同步最新岗位
uv run python -m jobagent.cli sync

# 查看状态
uv run python -m jobagent.cli status

# 查看匹配岗位（按画像筛选）
uv run python -m jobagent.cli jobs

# 查看增量推送（新增/变更）
uv run python -m jobagent.cli digest

# 标记已读
uv run python -m jobagent.cli digest --mark

# 立即跑一轮五家公司观察（腾讯、蔚来、小鹏、字节、商汤）
uv run python -m jobagent.cli observe

# 安装每天 09:30 / 14:30 / 20:30 的 macOS 自动观察
uv run python -m jobagent.cli schedule-install

# 查看三工作日观察进度；周末照常运行，但不占验收天数
uv run python -m jobagent.cli observation-status

# 停止自动观察（历史记录和岗位数据库保留）
uv run python -m jobagent.cli schedule-uninstall

# M6: 自动投递岗位（第一个参数是源站的 external_id，不是 jobs.id）
uv run python -m jobagent.cli apply <external_id> \
  --profile-path profile.yaml \
  --user-data-dir ~/.cache/playwright-tencent

# M7: 看投递记录（只读）—— 投了什么、卡在哪、截图在哪
uv run python -m jobagent.cli applications
uv run python -m jobagent.cli applications --funnel          # 分档汇总 + 拦截原因
uv run python -m jobagent.cli applications --status blocked  # 只看被拦的
uv run python -m jobagent.cli applications --company 蔚来     # 一家公司的全部源
```

自动观察只访问公开招聘接口，不打开登录浏览器、不读取 `identity`、不执行投递。
单家公司失败时其余公司继续跑，但整轮返回失败状态并指出具体公司；未完成同期官网核对的
轮次不会被计入连续三工作日验收。电脑休眠后若延迟超过 60 分钟才补跑，记录仍保留，
但不能冒充原计划时段。官网核对通过 JSON 证据文件录入，文件至少包含固定
`source_key`、`official_url`、采集时间、核对者、官网完整岗位编号清单、已逐项核对的
变化事件编号和说明；系统自行计算漏报、误报，不能手填两个零代签。录入命令为
`observe-review <轮次> <source_key> --evidence <证据.json>`，JSON 字段名依次是
`source_key`、`official_url`、`captured_at`、`reviewer`、`external_ids`、
`verified_event_ids`、`note`。

**M7 是只读的**，没有任何改状态的开关：状态变更必须走 `apply` 的
prepare/execute 两阶段闸门，从查看命令里改终态等于给那条闸门开后门。

**M6 投递流程（两阶段闸门）**：
1. 首次运行需手动登录（浏览器自动打开，手机号 + 验证码只能你自己做）
2. 登录态持久化到 `--user-data-dir`（后续无需重复登录）
3. **prepare**：填表 → 截图 → 渲染逐字段清单（值来自画像哪一行、哪些没填上、
   页面判哪个值不合法）→ 发一个一次性 `confirm_token`。**停在提交按钮前。**
4. 你看完清单点头
5. **execute**：校验 token → **回读页面上现在的值重算摘要**，和你确认过的对不上
   就拒绝提交 → 勾隐私政策 → 点「提交简历」
6. 截图保存在 `screenshots/` 目录

没有 `--yes` 这种开关，这是故意的：提交不可逆，对方系统里多一条记录撤不回来。

**限投额度**：很多公司对校招投递有次数上限（腾讯这类通常 1~3 个岗位）。两阶段闸门
保的是「这一次提交是你确认过的」，它保不了「这是这家公司的第几次」—— 挨个确认 5 个
岗位，每一步看起来都正常。所以 `apply` 在开浏览器之前先查一次额度：

```bash
# 登记时给上限。拿不到真实上限就别填，留空 = 不限（不猜一个数）
uv run python -m jobagent.cli source-add feishu:nio:campus \
  --company 蔚来 --entry-url https://nio.jobs.feishu.cn/campus --apply-limit 2
```

用量按**公司**算，不按源算：一家公司在库里可以有多行（蔚来就有 `feishu:nio` 和
`feishu:nio:campus` 两行），按源数会把用量拆成两份、每份都不到上限，于是投穿。
算占用的是 `submitted` / `duplicate` / `failed` —— `failed` 也算，因为它全都写在
点击提交之后，点击超时不代表没点上。

```bash
# 只填表看清单，不提交（安全，可反复跑）
uv run python -m jobagent.cli apply <external_id> --dry-run --user-data-dir .browser

# 判据体检：核一遍选择器还认不认页面。只读，不填不投
uv run python -m jobagent.cli checkup <external_id> --user-data-dir .browser
```

`checkup` 存在的理由：投递器里靠字符串认页面的常量有一打（中文字段名、
CSS-modules 类名前缀、勾选框旁的文案、下拉选项全称）。它们坏掉的方式全是
**静默**的 —— 命中 0 个，然后代投交出一张几乎空的表单加一句「填了 2 个字段」。
判据写对了不算完，得有一条命令能回答「怎么知道它失效了」。

详见 [M6 手工测试指南](docs/M6_MANUAL_TEST.md)。

```

## 架构设计

### 数据流

```
招聘 API → Adapter → 原始 JSON
                        ↓
                   normalize.py（标准化）
                        ↓
                   ingest.py（diff 引擎）
                        ↓
                  events 表（类型化事件）
                        ↓
                   match.py（画像匹配）
                        ↓
                   CLI digest（增量推送）
```

### 核心表

- **snapshots**：每次抓取的原始 JSON，保留审计轨迹
- **jobs**：标准化后的岗位表，含 status (open/closed)
- **events**：变更事件流，digest 的唯一输入
  - `job_opened` / `job_reopened` / `job_closed`
  - `job_updated`：payload.diff 记录字段级变更
  - `family_first_seen`：某公司某岗位族从 0→非 0
  - `batch_started`：单日新增突增（疑似批次发布）

### 变更检测逻辑

**指纹计算**（排除 description 避免噪声）：
```python
fingerprint = hashlib.sha256(
    json.dumps({
        "title": job["title"],
        "cities": job["cities"],
        "job_family": job["job_family"],
        "recruit_type": job["recruit_type"],
        "apply_url": job.get("apply_url"),
    }, sort_keys=True).encode()
).hexdigest()[:16]
```

**安全防护**：
- **空响应防护**：0 条结果判定为上游异常，拒绝同步
- **批量关闭防护**：单次消失 ≥40% 且 ≥5 条时阻止关闭，记录警告

**首次启动特殊处理**：
- Bootstrap 模式：只记录 `source_bootstrapped` 事件，避免事件洪水
- 第二次开始才产出 `job_opened` 等增量事件

## 已知问题与边界

### 腾讯 join.qq.com 适配器

✅ **已验证**：795 个岗位成功入库  
⚠️ **已知限制**：
- `projectId` 参数无效（后端忽略），实际过滤靠 `positionFamily`
- 届别推导基于 `recruitLabelName`：应届→26 届，实习→27 届
- 需要 `Referer: https://join.qq.com/` 否则返回空

### 岗位族分类（normalize.py）

**三层规则**避免误判：
1. **技术强信号优先**：大模型、算法、Agent、开发 → `tech`
2. **复合职能特例**：员工福利+薪酬 → `hr`，投资+风险 → `finance`
3. **常规职能词**：运营 → `operations`，产品 → `product`

**已知修正**（由 `tests/test_normalize.py` 锁定，条数见下面「测试覆盖」那节的命令）：
- ✅ "混元基座模型-视觉理解大模型研究" 曾误判为 `design`，现在正确识别为 `tech`
- ✅ "腾讯营销—广告推荐基础大模型" 曾误判为 `marketing`，已修正
- ✅ "运营开发" 曾误判为 `operations`，现在识别为 `tech`

### 匹配逻辑（match.py）

**硬过滤**（任一不符合直接淘汰）：
- 届别、岗位族、城市、招聘类型

**软打分**（boost_keywords 加分，用于排序）：
- 每个命中的 boost 关键词 +10 分

**当前限制**：
- ❌ 不支持复杂布尔表达式（如 "北京 OR 上海 AND 非客服"）
- ❌ 不支持地点的层级匹配（如 "海淀区" 无法命中 "北京"）
- ✅ exclude_keywords 在标题中出现则直接排除

## 测试覆盖

当前基线不在文档里写死，直接跑：

```bash
uv run pytest -q
```

要看各测试文件的分布，再跑：

```bash
uv run pytest -q --collect-only | grep '::' | sed 's/::.*//' | sort | uniq -c | sort -rn
```

**测试验不到的东西**（写在这里免得数字给人虚假安全感）：所有投递器测试都跑在
假页面上，真页面的判据靠 `checkup` 命令在线核；`execute()` 的提交点击**没有任何
真实执行记录**，测试只能证明「token 不对/摘要漂移时它拒绝提交」。

## 生产数据验证

**Tencent join.qq.com**（2026-08-04）：
- 总计：795 个开放岗位
- 命中画像：41 个（产品/运营，一线城市，26/27 届）
- 典型岗位：
  - 产品策划（深圳）
  - 内容运营（北京/上海）
  - 游戏发行培训生（深圳）

## 下一步

当初的判断是「先做投递侧（M6），因为它是最大未知风险」。现在这个风险大部分已经落地
（表单机制、登录门归因、判据体检都通了），剩下的按不确定性排：

1. **真投一次**，核实 `_is_success` / `_is_duplicate` / `_is_job_closed` 的文案。
   这是唯一还在猜的地方，也是唯一需要你亲自点头的一步。
2. **画像层支持多条目**（`internships` / `projects`），折叠段的填写机制已经通了，
   缺的是数据 —— 现在 `profile.yaml` 里那两个还是空列表。
3. **横向扩展**：飞书系已覆盖 4 家（蔚来 / 小鹏 / 字节 / 商汤），同一套前端再加
   租户成本很低；Moka / 北森系需要另做，它们的厂商 API 对外是关的，只能走公开前端。

## 技术栈

- **语言**：Python 3.13
- **数据库**：SQLite（WAL 模式）
- **CLI**：Typer + Rich
- **HTTP**：httpx
- **测试**：pytest
- **包管理**：uv

## 许可证

MIT
