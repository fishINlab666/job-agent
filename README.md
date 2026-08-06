# 校招 Agent

自动化的校招岗位监控与推荐系统，专为 26/27 届产品/运营岗求职者设计。

## 核心功能

**M1-M5 已完成（发现侧 MVP）**：
- **M1 数据采集**：适配器模式对接各公司招聘 API
- **M2 变更检测**：基于指纹的增量 diff，产出类型化事件流
- **M3 用户画像**：YAML 配置文件定义筛选条件
- **M4 智能匹配**：规则引擎过滤 + 关键词打分
- **M5 CLI 界面**：Rich 格式化表格，支持增量推送

- **M6 自动投递**：Playwright 浏览器自动化，表单填充 + 简历上传

**待实现**：
- M7：邮件解析跟踪投递状态
- 更多公司适配器（字节、阿里、华为等 19 家）

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
```

**M6 投递流程**：
1. 首次运行需手动登录（浏览器自动打开）
2. 登录态持久化到 `--user-data-dir`（后续无需重复登录）
3. 自动填充表单（姓名、手机、邮箱、学校、专业、学历、毕业时间）
4. 自动上传简历（如果 `profile.yaml` 中配置了 `resume_path`）
5. 提交并返回结果（成功/失败/重复投递/岗位已关闭）
6. 截图保存在 `screenshots/` 目录

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

**41 个测试用例**：
- `test_ingest.py`：变更检测、安全防护、事件产出
- `test_normalize.py`：岗位族分类、城市标准化

## 生产数据验证

**Tencent join.qq.com**（2026-08-04）：
- 总计：795 个开放岗位
- 命中画像：41 个（产品/运营，一线城市，26/27 届）
- 典型岗位：
  - 产品策划（深圳）
  - 内容运营（北京/上海）
  - 游戏发行培训生（深圳）

## 下一步

**选项 A：横向扩展（发现侧）**  
添加 19 家公司适配器：字节、阿里、华为、美团...

**选项 B：纵向深入（投递侧）**  
先实现 M6（浏览器自动化），验证代投可行性后再扩展公司池

**建议**：先做 B。M6 是最大未知风险（反爬、验证码、多步骤流程），在一家公司上验证可行性，比盲目采集 20 家数据更稳妥。

## 技术栈

- **语言**：Python 3.13
- **数据库**：SQLite（WAL 模式）
- **CLI**：Typer + Rich
- **HTTP**：httpx
- **测试**：pytest
- **包管理**：uv

## 许可证

MIT
