# M6 投递层手工测试指南

由于投递操作涉及真实网站交互（登录、表单提交），自动化测试难以覆盖全流程。
本文档提供手工测试步骤，验证 TencentJoinSubmitter 在真实环境下的表现。

## 前置准备

1. 安装 Playwright 浏览器：
   ```bash
   uv run playwright install chromium
   ```

2. 创建用户画像文件：
   ```bash
   cp profile.yaml.example profile.yaml
   # 编辑 profile.yaml，填入真实信息
   ```

3. 确保数据库中有岗位数据：
   ```bash
   uv run python -m jobagent.cli sync
   uv run python -m jobagent.cli jobs --family operations
   ```

## 测试场景

### 场景 1：首次投递（无登录态）

**目标**：验证登录检测逻辑。

```bash
# 使用无头模式
uv run python -m jobagent.cli apply <job_id> --headless

# 预期结果：
# ✗ 投递失败
#   原因: 需要登录（请手动登录后重试，或使用 user_data_dir 持久化登录态）
#   截图: screenshots/submit_<job_id>_<timestamp>.png
```

### 场景 2：手动登录后投递（持久化登录态）

**目标**：验证登录态持久化与表单填充。

```bash
# 第一步：有头模式，手动登录
uv run python -m jobagent.cli apply <job_id> \
  --no-headless \
  --user-data-dir ~/.cache/playwright-tencent

# 操作：
# 1. 浏览器打开后，点击「立即申请」
# 2. 扫码 / 手机号登录
# 3. 观察表单是否自动填充
# 4. 如果成功填充，手动点击「提交申请」验证流程
# 5. 关闭浏览器

# 第二步：无头模式，复用登录态
uv run python -m jobagent.cli apply <job_id> \
  --headless \
  --user-data-dir ~/.cache/playwright-tencent

# 预期结果：
# ✓ 投递成功
#   时间: 2026-08-04T16:30:00
#   截图: screenshots/submit_<job_id>_<timestamp>.png
```

### 场景 3：重复投递

**目标**：验证重复检测逻辑。

```bash
# 投递同一个岗位两次
uv run python -m jobagent.cli apply <job_id> \
  --user-data-dir ~/.cache/playwright-tencent

# 第二次预期结果：
# ✗ 投递失败
#   原因: 重复投递
```

### 场景 4：岗位已关闭

**目标**：验证岗位状态检测。

```bash
# 找一个已关闭的岗位（从旧数据或手动关闭）
uv run python -m jobagent.cli apply <closed_job_id> \
  --user-data-dir ~/.cache/playwright-tencent

# 预期结果：
# ✗ 投递失败
#   原因: 岗位已关闭
```

### 场景 5：上传简历

**目标**：验证文件上传。

```bash
# 在 profile.yaml 中添加：
# resume_path: /path/to/resume.pdf

uv run python -m jobagent.cli apply <job_id> \
  --no-headless \
  --user-data-dir ~/.cache/playwright-tencent

# 观察：
# 1. 简历文件是否自动上传
# 2. 上传后是否显示文件名
# 3. 提交是否成功
```

## 调试技巧

### 1. 截图诊断

所有投递操作都会在 `screenshots/` 目录保存截图，失败时可查看最终状态：

```bash
ls -lt screenshots/ | head -5
open screenshots/submit_<job_id>_<timestamp>.png
```

### 2. 有头模式观察

去掉 `--headless` 可以实时观察浏览器操作：

```bash
uv run python -m jobagent.cli apply <job_id> --no-headless
```

### 3. 选择器调试

如果表单填充失败，可能是选择器不匹配。打开 Playwright Inspector：

```python
# 在 tencent_join.py 的 submit 方法中添加：
page.pause()  # 暂停，打开 Playwright Inspector
```

### 4. 网络日志

查看 Playwright 的网络请求：

```python
# 在 TencentJoinSubmitter.__init__ 中添加：
context.on("request", lambda req: print(f"→ {req.method} {req.url}"))
context.on("response", lambda res: print(f"← {res.status} {res.url}"))
```

## 已知限制

1. **验证码**：如果腾讯启用验证码（滑块 / 拼图），当前实现无法自动处理。
   - 缓解方案：使用 user_data_dir 持久化登录态，降低验证码触发概率。

2. **反爬风控**：高频投递可能触发风控，导致账号被限制。
   - 建议：每次投递后间隔 30-60 秒。

3. **页面结构变化**：腾讯如果改版，选择器可能失效。
   - 维护：定期检查选择器是否匹配最新页面。

4. **多步骤流程**：某些岗位可能有额外问卷 / 测评，当前未处理。
   - 扩展点：在 _fill_form 后添加自定义逻辑。

## 成功标准

- ✅ 能检测登录态
- ✅ 能自动填充基本信息
- ✅ 能上传简历文件
- ✅ 能检测岗位关闭 / 重复投递
- ✅ 失败时有清晰的错误消息和截图
- ✅ 登录态可持久化，避免重复登录

## 下一步

1. **扩展 schema**：新增 `submissions` 表记录投递历史。
2. **批量投递**：`apply-batch` 命令，读取 digest 输出批量投递。
3. **重试机制**：网络抖动时自动重试。
4. **验证码处理**：集成打码平台 / 人工介入。
5. **多公司支持**：为字节、阿里等公司实现 Submitter。
