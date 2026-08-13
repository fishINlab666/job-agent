# 把 job-agent 的只读层接到对话里

方案见 [014-MCP只读层.md](plans/014-MCP只读层.md)。这份只讲怎么配、怎么用、怎么确认它真的通了。

配完之后你在对话里问「蔚来还有几个开放岗位」，模型直接查本地库回答，
不用我跑命令再把输出贴进来。

**这一层查得到、动不了。** 没有投递工具 —— 代投全程留在命令行里，
因为提交不可逆、必须人工逐字段确认。想投递还是走 `jobagent prepare`。

---

## 一、配

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
      "command": "/Users/wujingyu/Desktop/AI/projects-jobs/job-agent/.venv/bin/python",
      "args": ["-m", "jobagent.mcp_server"],
      "cwd": "/Users/wujingyu/Desktop/AI/projects-jobs/job-agent"
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
cd "/Users/wujingyu/Desktop/AI/projects-jobs/job-agent" && .venv/bin/python -c "
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

**上面那节配的是 Claude Desktop，Claude Code 读不到它。** 两个客户端各有自己的配置：

```
Claude Desktop  ~/Library/Application Support/Claude/claude_desktop_config.json
Claude Code     ~/.claude.json 的 projects.<项目路径>.mcpServers（local scope）
                或项目根的 .mcp.json（project scope）
```

配 Claude Code：

```bash
cd "/Users/wujingyu/Desktop/AI/projects-jobs/job-agent" && claude mcp add job-agent -s local -- "$PWD/.venv/bin/python" -m jobagent.mcp_server
```

**`-s local` 不能省。** 省了虽然也默认 local，但写清楚是为了和下面那条对照 ——
`-s project` 会把配置写进项目根的 `.mcp.json`，而那条路有两个坑：

1. `command` 是**绝对路径**，钉着写它的人的家目录。这个仓库是公开的，
   别人 clone 下来那条路径不存在，server 直接起不来。
2. project scope 的 server **首次使用要人工批准**（`⏸ Pending approval`），
   防的是「clone 一个仓库就自动跑它配置里的进程」。批准动作只能人做。

个人项目用 local：不进仓库、不用批准。要团队共享再考虑 project scope，
那时候得先解决相对路径怎么写。

配完当场确认，**不要等到进对话才发现没连上**：

```bash
cd "/Users/wujingyu/Desktop/AI/projects-jobs/job-agent" && claude mcp list
```

要看到 `job-agent: ... - ✔ Connected`。其他两种状态的意思：

```
⏸ Pending approval   project scope 的 server 还没被批准，去跑 claude 批一下
✘ Failed to connect  路径不对 / 依赖缺了，去看 claude mcp get job-agent
```

**配完要重启 Claude Code 会话**，工具才会出现在当前对话里 ——
`claude mcp list` 显示 `Connected` 只说明进程能拉起来，
不代表**已经开着的**那个会话已经加载了它。

---

## 二、确认它真的通了

先确认 server 自己能起来（会挂住等 stdio 输入，`Ctrl-C` 退出 —— 挂住就是对的）：

```bash
cd "/Users/wujingyu/Desktop/AI/projects-jobs/job-agent" && .venv/bin/python -m jobagent.mcp_server
```

再确认注册表里是那 6 个工具。**这条比读代码可靠**，它问的是运行时：

```bash
cd "/Users/wujingyu/Desktop/AI/projects-jobs/job-agent" && .venv/bin/python -c "
from jobagent import mcp_server as m
import asyncio
for t in asyncio.run(m.mcp.list_tools()): print(t.name)
"
```

应该正好这六行：

```
list_jobs
explain_match
list_sources
list_sync_runs
job_changes
check_form_selectors
```

最后 —— **在对话里实调一次**。前面两条只证明进程能起、注册表对，
不证明客户端连上了。随便问一句「现在库里有多少开放岗位」，
看模型是不是真调了 `list_jobs`（界面上会显示工具调用）。
没看到工具调用就是没连上，去看客户端日志。

**这一步 2026-08-13 第一次真跑，当场发现前面全绿而它是坏的。**
经过值得写下来，因为失败形状不显眼：

问的是「蔚来还有几个开放岗位」（就是 §2 那句原话），模型**没有**调
`list_jobs`，而是直接开只读连接查了库 —— 答案是对的，611 行 / 336 个岗位型。
**答案对，恰恰是这个失效最难发现的地方**：没有报错、没有空结果，
只有「工具调用」那一行没出现。如果当时只看答案对不对，会以为它通了。

真因不是「忘了重启」，是**配错了客户端**：当时只跑过 Claude Desktop 那一节，
本节这条 `claude mcp add` 写在文档里但从没执行过，
`~/.claude.json` 里那一项是空的。所以：

> 写进文档的命令 ≠ 跑过的命令。这份文档能同时是「完整的」和「没用的」。

---

## 三、六个工具各干什么

| 工具 | 问什么 | 注意 |
|---|---|---|
| `list_jobs` | 当前开放岗位，可按族/城市/公司/届别筛，可只看命中我画像的 | `total` 是筛完的**全量**条数，不受 `limit` 影响 |
| `explain_match` | 某条岗位为什么命中／不命中 | `state` 是**三态**：`hit`/`miss`/`unknown`。`unknown` 是信息不全，不是不合格 |
| `list_sources` | 每个源的岗位数、最近采集、投递配额 | `last_run` 为 null = **一次都没跑过**，和「跑过但失败了」不是一回事 |
| `list_sync_runs` | 采集批次历史 | `finished_at` 为 null = 这轮没收尾（进程被杀或正在跑），不是数据缺失 |
| `job_changes` | 岗位变动：新开、关闭、改动、源首次接入 | **只有岗位侧事件。** 投递记录不在这一层 |
| `check_form_selectors` | 投递表单的判据还认不认对方页面 | ⚠️ **会启真浏览器，一次几十秒。** 需要 `user_data_dir` |

### 几个容易读错的地方

**判不出族的岗位按任何族筛都查不到，包括 `other`。** 那一列是空的，
不是「归到 other 里了」。想看这批得不带 `family` 参数。

**`check_form_selectors` 的 `all_valid=true` 不等于「站点没改过文案」。**
有些判据只在异常页面上才触发（岗位已关闭、提交成功、重复投递），
拿一个正常岗位页核不动它们 —— 那几条在 `unprovable` 里列着。
只说「全部有效」等于把「这次没验成」洗成了「验过是好的」。

**`check_form_selectors` 别连着反复调。** 它是这六个里唯一对外发请求的，
其余五个都只读本地库。改完选择器、或者隔一段时间没投过，才跑一次。

**没有登录态时它返回一条 blocker，不是一片假红。** `reached_form=false`
就是走到登录墙了，那时候的红不代表判据坏了。

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
