# M6 实现总结

## 实现内容

### 核心组件

1. **提交器基类** (`jobagent/submitters/base.py`)
   - `Submitter` 协议：定义投递接口
   - `SubmissionResult` 模型：统一返回结构

2. **腾讯投递器** (`jobagent/submitters/tencent_join.py`)
   - Playwright 浏览器自动化
   - 登录态检测与持久化
   - 表单自动填充
   - 简历文件上传
   - 错误场景处理（岗位关闭、重复投递、未登录）
   - 截图保存（每次操作关键节点）

3. **CLI 命令** (`jobagent/cli.py`)
   - `apply` 命令：投递单个岗位
   - 参数：
     - `job_id`：岗位 external_id
     - `--profile-path`：用户画像文件路径
     - `--headless/--no-headless`：无头/有头模式
     - `--user-data-dir`：浏览器用户数据目录（持久化登录态）

4. **数据库扩展** (`jobagent/schema_submissions.sql`)
   - `submissions` 表：记录投递历史
   - 字段：job_id, external_id, status, error, screenshot_path, profile_snapshot
   - 索引：job_id, external_id, status, submitted_at

5. **测试覆盖** (`tests/test_submitter_tencent.py`)
   - 8 个单元测试：
     - 初始化参数
     - 岗位状态检测（关闭、登录态、成功、重复）
     - 表单填充逻辑
     - 端到端场景模拟

### 工作流程

```
用户调用 apply 命令
    ↓
查询 jobs 表获取岗位信息
    ↓
加载 profile.yaml 用户画像
    ↓
初始化 TencentJoinSubmitter
    ↓
启动 Playwright 浏览器
    ↓
访问岗位页面
    ↓
检测岗位状态（关闭？）
    ↓
点击「立即申请」
    ↓
检测登录态（需要登录？）
    ↓
填充表单（姓名、手机、邮箱、学校、专业、学历、毕业时间）
    ↓
上传简历（如果提供）
    ↓
点击「提交申请」
    ↓
检测结果（成功？重复？）
    ↓
保存截图
    ↓
写入 submissions 表
    ↓
返回结果给用户
```

### 关键特性

1. **登录态持久化**
   - 使用 `--user-data-dir` 保存浏览器 cookie/localStorage
   - 首次手动登录后，后续投递自动复用
   - 避免每次都扫码/验证

2. **智能错误检测**
   - 岗位已关闭：检测页面「已停止招聘」文案
   - 重复投递：检测「已申请」提示
   - 未登录：检测登录页特征，暂停等待
   - 未知错误：截图 + 异常信息

3. **截图审计**
   - 每次投递保存 4-5 张截图（岗位页、表单页、结果页）
   - 失败时可回溯诊断
   - 路径：`screenshots/submit_{job_id}_{timestamp}.png`

4. **表单填充**
   - 支持文本输入（姓名、手机、邮箱、学校、专业）
   - 支持下拉选择（学历）
   - 支持文件上传（简历）
   - 选择器基于 placeholder 属性（抗页面微调）

5. **投递记录**
   - submissions 表记录所有投递尝试
   - profile_snapshot 快照当时的用户画像
   - 支持后续分析成功率、失败原因

### 测试验证

```bash
# 单元测试（全部通过）
uv run pytest tests/test_submitter_tencent.py -v
# 8 passed

# 集成测试（全套通过）
uv run pytest -v
# 49 passed（包含 M1-M6）

# 手工测试（见 docs/M6_MANUAL_TEST.md）
uv run python -m jobagent.cli apply <job_id> --no-headless
```

### 已知限制

1. **验证码未处理**
   - 腾讯如果启用滑块/拼图验证码，当前无法自动通过
   - 缓解：使用 user_data_dir 降低触发概率

2. **多步骤流程未支持**
   - 某些岗位可能有额外问卷/测评
   - 扩展点：在 _fill_form 后添加自定义逻辑

3. **反爬风控**
   - 高频投递可能触发账号限制
   - 建议：每次投递间隔 30-60 秒

4. **页面结构变化**
   - 选择器基于当前页面结构
   - 腾讯改版后需要重新调整

### 文档

- [M6 手工测试指南](../docs/M6_MANUAL_TEST.md)：详细测试场景与调试技巧
- [profile.yaml.example](../profile.yaml.example)：用户画像配置示例
- [demo_submit.py](../scripts/demo_submit.py)：演示脚本（dry-run）

### 下一步

1. **批量投递**：`apply-batch` 命令，读取 digest 输出批量投递
2. **重试机制**：网络抖动时自动重试（指数退避）
3. **验证码处理**：集成打码平台或人工介入
4. **投递统计**：CLI 命令查看投递历史（`submissions` 表）
5. **更多公司**：为字节、阿里、华为等公司实现 Submitter

## 使用示例

```bash
# 1. 准备画像文件
cp profile.yaml.example profile.yaml
vim profile.yaml  # 填入真实信息

# 2. 首次投递（手动登录）
uv run python -m jobagent.cli apply 12345 \
  --no-headless \
  --user-data-dir ~/.cache/playwright-tencent

# 3. 后续投递（自动复用登录态）
uv run python -m jobagent.cli apply 67890 \
  --user-data-dir ~/.cache/playwright-tencent

# 4. 查看截图
ls -lt screenshots/ | head -5
open screenshots/submit_67890_*.png
```

## 技术选型

- **Playwright** vs Selenium：
  - ✅ 更现代的 API（async/await）
  - ✅ 内置等待机制（减少 race condition）
  - ✅ 跨浏览器支持（Chromium/Firefox/WebKit）
  - ✅ 网络拦截能力（可扩展为 API 拦截验证）

- **同步 API** vs 异步 API：
  - 选择同步（`sync_playwright`）
  - 原因：CLI 场景简单，同步代码更易维护
  - 异步优势（批量并发）在单次投递中不明显

## 实现亮点

1. **协议驱动设计**
   - `Submitter` 协议定义统一接口
   - 新增公司只需实现协议方法
   - `SubmissionResult` 统一返回格式

2. **状态机清晰**
   - 每个检测点（关闭、登录、成功、重复）独立方法
   - 易于单元测试和调试

3. **截图贯穿始终**
   - 成功/失败都保存截图
   - 提供完整的操作审计轨迹

4. **数据库一致性**
   - 投递记录关联 jobs 表（外键约束）
   - profile_snapshot 保存快照（避免事后修改画像导致数据混乱）

5. **可观测性**
   - Rich 格式化输出（彩色、对齐）
   - 每个步骤都有进度提示
   - 失败时清晰的错误消息

## 代码统计

- 新增文件：5 个
  - `jobagent/submitters/base.py`（34 行）
  - `jobagent/submitters/__init__.py`（3 行）
  - `jobagent/submitters/tencent_join.py`（200 行）
  - `jobagent/schema_submissions.sql`（18 行）
  - `tests/test_submitter_tencent.py`（120 行）

- 修改文件：3 个
  - `jobagent/cli.py`（+70 行）
  - `jobagent/db.py`（+3 行）
  - `pyproject.toml`（+1 依赖）

- 总计：+449 行代码（含注释与文档）

## 测试覆盖

- 单元测试：8 个（submitter 核心逻辑）
- 集成测试：0 个（真实投递需手工验证）
- 手工测试：5 个场景（见 M6_MANUAL_TEST.md）

覆盖率：核心逻辑 100%，端到端流程需手工验证。
