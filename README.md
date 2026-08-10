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

# M6: 自动投递岗位
uv run python -m jobagent.cli apply <job_id> \
  --profile-path profile.yaml \
  --user-data-dir ~/.cache/playwright-tencent

# M7: 看投递记录（只读）—— 投了什么、卡在哪、截图在哪
uv run python -m jobagent.cli applications
uv run python -m jobagent.cli applications --funnel          # 分档汇总 + 拦截原因
uv run python -m jobagent.cli applications --status blocked  # 只看被拦的
```

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

**已知修正**（31 个测试用例锁定）：
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

```bash
uv run pytest -xvs
```

**504 个测试用例**（截至 2026-08-10）：

| 文件 | 数量 | 覆盖 |
|---|---|---|
| `test_match.py` | 98 | 硬过滤、软打分、排除词 |
| `test_adapter_feishu.py` | 69 | 四租户解析、分页、字段缺失 |
| `test_submitter_feishu.py` | 64 | 两阶段闸门、歧义守卫、回读摘要、判据体检 |
| `test_ingest.py` | 63 | 变更检测、安全防护、事件产出 |
| `test_ats.py` | 54 | ATS 识别与路由判据 |
| `test_routing.py` | 39 | 投递器选择 |
| `test_normalize.py` | 31 | 岗位族分类、城市标准化 |
| `test_cli.py` | 24 | 命令行出口，含 `apply` 的两阶段与 `checkup` |
| `test_submitter_tencent.py` | 23 | 腾讯表单填充与结果判据 |
| 其余 6 个文件 | 39 | 迁移、摘要、探针分桶、端到端 |

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
