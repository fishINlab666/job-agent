# 把 job-agent 的只读层接到对话里

方案见 [014-MCP只读层.md](plans/014-MCP只读层.md)。这份只讲怎么配、怎么用、怎么确认它真的通了。

配完之后你在对话里问「蔚来还有几个开放岗位」，模型直接查本地库回答，
不用我跑命令再把输出贴进来。

**这一层查得到、动不了。** 没有投递工具 —— 代投全程留在命令行里，
因为提交不可逆、必须人工逐字段确认。想投递还是走 `jobagent prepare`。

---

## 一、配

> 本节只说明未来如何接入，不授权现在修改任何客户端配置。只有代码进入 `main`、
> 干净安装通过，并在只读 MCP 检查点获得明确批准后，才恢复一个客户端。

### Claude Desktop

配置文件在：

```
~/Library/Application Support/Claude/claude_desktop_config.json
```

把 `job-agent` 这一段加进 `mcpServers`（文件不存在就整份写进去）：

```json
{
  "mcpServers": {
    "job-agent": {
      "command": "/absolute/path/to/job-agent/.venv/bin/python",
      "args": ["-m", "jobagent.mcp_server"],
      "cwd": "/absolute/path/to/job-agent"
    }
  }
}
```

三个字段都不能省，各自有原因：

- **`command` 用 venv 里的绝对路径。** 不写 `python` ——
  客户端不走你的 shell，`PATH` 里那个 python 大概没装 `mcp` 和 `httpx`。
- **`args` 用 `-m`。** 模块方式启动，相对 import 才成立。
- **`cwd` 填上。** 库路径是 `db.ROOT / "data" / "jobagent.db"`（`ROOT` 从
  `__file__` 解析，所以和 `cwd` 无关）。但 MCP 规范里 `cwd` 是建议提供的，
  且某些将来的命令可能需要它解析相对路径。

一条命令写进去（**会覆盖 `mcpServers` 里的同名项，其余保留**）：

```bash
cd "$(git rev-parse --show-toplevel)" && .venv/bin/python -c "
import json, pathlib
p = pathlib.Path.home()/'Library/Application Support/Claude/claude_desktop_config.json'
d = json.loads(p.read_text()) if p.exists() else {}
root = str(pathlib.Path.cwd())
d.setdefault('mcpServers', {})['job-agent'] = {
    'command': root + '/.venv/bin/python',
    'args': ['-m', 'jobagent.mcp_server'],
    'cwd': root,
}
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(d, indent=2, ensure_ascii=False))
print('已写入:', p)
print('现有 server:', list(d['mcpServers'].keys()))
"
```

**改完必须重启 Claude Desktop。** 配置只在启动时读一次。

### Claude Code（CLI）

```bash
cd "$(git rev-parse --show-toplevel)" && claude mcp add job-agent -- "$PWD/.venv/bin/python" -m jobagent.mcp_server
```

---

## 二、确认它真的通了

先确认 server 自己能起来（会挂住等 stdio 输入，`Ctrl-C` 退出 —— 挂住就是对的）：

```bash
cd "$(git rev-parse --show-toplevel)" && .venv/bin/python -m jobagent.mcp_server
```

再确认注册表里是那 5 个工具。**这条比读代码可靠**，它问的是运行时：

```bash
cd "$(git rev-parse --show-toplevel)" && .venv/bin/python -c "
from jobagent import mcp_server as m
import asyncio
for t in asyncio.run(m.mcp.list_tools()): print(t.name)
"
```

应该正好这五行：

```
list_jobs
explain_match
list_sources
list_sync_runs
job_changes
```

最后 —— **在对话里实调一次**。前面两条只证明进程能起、注册表对，
不证明客户端连上了。随便问一句「现在库里有多少开放岗位」，
看模型是不是真调了 `list_jobs`（界面上会显示工具调用）。
没看到工具调用就是没连上，去看客户端日志。

---

## 三、五个工具各干什么

| 工具 | 问什么 | 注意 |
|---|---|---|
| `list_jobs` | 当前开放岗位，可按族/城市/公司/届别筛，可只看命中我画像的 | `total` 是筛完的**全量**条数，不受 `limit` 影响 |
| `explain_match` | 某条岗位为什么命中／不命中 | `state` 是**三态**：`hit`/`miss`/`unknown`。`unknown` 是信息不全，不是不合格 |
| `list_sources` | 每个源的岗位数、最近采集、投递配额 | `last_run` 为 null = **一次都没跑过**，和「跑过但失败了」不是一回事 |
| `list_sync_runs` | 采集批次历史 | `finished_at` 为 null = 这轮没收尾（进程被杀或正在跑），不是数据缺失 |
| `job_changes` | 岗位变动：新开、关闭、改动、源首次接入 | **只有岗位侧事件。** 投递记录不在这一层 |

### 几个容易读错的地方

**判不出族的岗位按任何族筛都查不到，包括 `other`。** 那一列是空的，
不是「归到 other 里了」。想看这批得不带 `family` 参数。

表单判据检查不属于 MCP。它会启动浏览器并接触登录态，只能在明确授权的
本地人工流程中运行，不能从对话工具注册表恢复。

---

## 四、这一层为什么动不了库

三条硬约束，都在形状上，不是提示词里的请求：

1. **注册表里没有写动词。** `prepare`/`execute`/`submit`/`apply`/`sync`
   一个都不注册，模型调不到不存在的工具。守它的是
   `tests/test_mcp_server.py::test_no_write_verb_is_registered` ——
   遍历**真实注册表**比对黑名单，谁手滑加一个写工具那条就红。
   （已验过它真的会红：注册一个 `execute_apply` 进去，三条守卫同时失败。）
2. **连接是 `mode=ro`。** SQLite 自己拒绝写。管的是「我在工具体里写错一句 SQL」。
3. **只有 `intent` 过边界。** `profile.yaml` 里有姓名/手机/身份证，
   `_intent()` 是这一层唯一读那个文件的地方，按白名单挑键往下传。
   哨兵测试往 profile 里塞可识别的假身份值，调**每一个**工具，
   断言哨兵串不出现在任何输出里。

代投为什么不在这儿：`execute()` 提交之后对方系统里那条记录撤不回来，
闸门的价值全在「人看过字段清单再点头」。做成工具就是把闸门交给一个
会自己决定要不要调工具的东西。
