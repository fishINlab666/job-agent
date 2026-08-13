# 校招 Agent - 快速参考

> **历史文档（写于 M1–M6 阶段）。命令清单以 `docs/SPEC.md` §2 为准**，那份有守卫测试
> （`tests/test_docs_match_code.py`）盯着它和 `cli --help` 对齐；这份没有，M7/M8 和
> MCP 只读层的命令它不知道。

## 完整工作流（M1-M6）

### 1. 初始化
```bash
uv sync
uv run playwright install chromium
uv run python -m jobagent.cli init
```

### 2. 配置画像
```bash
cp profile.yaml.example profile.yaml
# 编辑 profile.yaml，填入：
# - 个人信息（姓名、手机、邮箱、学校、专业、学历）
# - 筛选条件（届别、职能族、城市）
# - 简历路径（可选）
```

### 3. 发现岗位
```bash
# 同步最新数据
uv run python -m jobagent.cli sync

# 查看状态
uv run python -m jobagent.cli status

# 浏览匹配岗位
uv run python -m jobagent.cli jobs --family operations
uv run python -m jobagent.cli jobs --family product

# 查看新增/变更
uv run python -m jobagent.cli digest

# 标记已读
uv run python -m jobagent.cli digest --mark
```

### 4. 投递岗位
```bash
# 首次投递（手动登录）
uv run python -m jobagent.cli apply <job_id> \
  --no-headless \
  --user-data-dir ~/.cache/playwright-tencent

# 后续投递（自动复用登录态）
uv run python -m jobagent.cli apply <job_id> \
  --user-data-dir ~/.cache/playwright-tencent

# 查看截图
open screenshots/submit_<job_id>_*.png
```

## 典型场景

### 每日监控
```bash
# 设置 cron 每天 9:00 同步
0 9 * * * cd /path/to/job-agent && uv run python -m jobagent.cli sync

# 手动查看新增
uv run python -m jobagent.cli digest
```

### 批量投递（手动）
```bash
# 1. 找到感兴趣的岗位
uv run python -m jobagent.cli jobs --family operations > jobs.txt

# 2. 从 jobs.txt 提取 job_id，逐个投递
uv run python -m jobagent.cli apply 12345 --user-data-dir ~/.cache/playwright-tencent
uv run python -m jobagent.cli apply 67890 --user-data-dir ~/.cache/playwright-tencent
```

## 数据模型

### 核心表
- `sources`：数据源配置
- `jobs`：标准化岗位表
- `events`：变更事件流（digest 的输入）
- `submissions`：投递记录（M6）

### 事件类型
- `job_opened`：岗位首次出现
- `job_reopened`：岗位重新开放
- `job_closed`：岗位关闭
- `job_updated`：岗位变更（标题/城市/族）
- `family_first_seen`：某公司某族首次出现
- `batch_started`：单日新增突增

## 故障排查

### 同步失败
```bash
# 查看最近一次运行状态
uv run python -m jobagent.cli status

# 检查错误日志
sqlite3 data/jobagent.db "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
```

### 投递失败
```bash
# 查看截图
ls -lt screenshots/ | head -5
open screenshots/submit_<job_id>_*.png

# 查看投递记录
sqlite3 data/jobagent.db "SELECT * FROM submissions ORDER BY id DESC LIMIT 10"
```

### 登录态失效
```bash
# 重新登录（有头模式）
uv run python -m jobagent.cli apply <job_id> \
  --no-headless \
  --user-data-dir ~/.cache/playwright-tencent
```

## 常见问题

**Q: 为什么 digest 没有新增？**
- A: 可能是首次同步（bootstrap 模式），再运行一次 sync 即可产出增量事件。

**Q: 投递提示"需要登录"？**
- A: 使用 `--no-headless --user-data-dir` 手动登录一次，后续会自动复用。

**Q: 如何查看某个岗位的详细信息？**
```bash
sqlite3 data/jobagent.db "SELECT * FROM jobs WHERE external_id='12345'"
```

**Q: 如何查看投递历史？**
```bash
sqlite3 data/jobagent.db "SELECT * FROM submissions WHERE status='success'"
```

## 文件结构

```
job-agent/
├── jobagent/
│   ├── adapters/          # M1: 数据采集适配器
│   │   └── tencent_join.py
│   ├── submitters/        # M6: 投递适配器
│   │   ├── base.py
│   │   └── tencent_join.py
│   ├── cli.py             # M5: CLI 入口
│   ├── ingest.py          # M2: 增量检测引擎
│   ├── match.py           # M4: 画像匹配
│   ├── normalize.py       # M2: 岗位分类
│   ├── db.py              # 数据库工具
│   ├── schema.sql         # 主表结构
│   └── schema_submissions.sql  # M6 扩展表
├── tests/
│   ├── test_ingest.py
│   ├── test_normalize.py
│   └── test_submitter_tencent.py
├── data/
│   └── jobagent.db        # SQLite 数据库
├── screenshots/           # 投递截图
├── profile.yaml           # 用户画像
└── README.md
```

## 下一步

1. **M7 邮件跟踪**：解析确认邮件更新投递状态
2. **批量投递**：`apply-batch` 命令自动投递 digest 输出
3. **更多公司**：字节、阿里、华为等 19 家适配器
4. **MCP Server**：封装为 Claude Desktop 工具
5. **定时任务**：内置调度器替代 cron
