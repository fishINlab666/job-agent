# 把 job-agent 的只读层接到对话里

最初设计见 [014-MCP只读层.md](plans/014-MCP只读层.md)，固定封装见
[027-MCP固定只读封装.md](plans/027-MCP固定只读封装.md)。这份只讲怎么配、怎么用、
怎么确认它真的通了。

配完之后你在对话里问「蔚来还有几个开放岗位」，模型直接查本地库回答，
不用我跑命令再把输出贴进来。

**这一层查得到、动不了。** 没有投递工具 —— 代投全程留在命令行里，
因为提交不可逆、必须人工逐字段确认。想投递还是走 `jobagent apply <job_id>`：
它填好表就停下，你逐字段看过再点头才提交，没有 `--yes` 那种开关。

---

## 一、先准备固定运行包

> 本节只说明未来如何接入，不授权现在修改任何客户端配置。只有候选进入 `main`、
> 固定目录的干净安装通过，并在只读 MCP 检查点获得明确批准后，才恢复一个客户端。

**不要让客户端启动日常开发目录。** 开发分支、未提交修改和虚拟环境都会继续变化；
同一份客户端配置可能在没有提醒的情况下换成另一版代码。固定运行包必须来自一个已审核
提交，并直接在最终目录创建虚拟环境，不能先建好 `.venv` 再移动目录。

准备时需要固定四个输入：

- 已审核的 Git commit；
- 不再移动的运行目录；
- 生产数据库的绝对路径；
- `profile.yaml` 的绝对路径，且文件不得给 group/world 任何权限。

安装命令由发布流程按上述四项生成并单独复核。安装完成后，真正的启动形状必须是：

```bash
/absolute/fixed/job-agent/.venv/bin/python -m jobagent.mcp_server \
  --db /absolute/path/to/jobagent.db \
  --profile /absolute/path/to/profile.yaml
```

`--db` 和 `--profile` 都是必填项。路径缺失、是相对路径、经过符号链接、数据库 schema
不完整或画像权限过宽时，server 会在开放 stdio 前失败；不会创建空库，也不会退回开发
目录里的默认文件。

## 二、接入客户端（候选模板）

### Claude Desktop

常见配置位置是：

```
~/Library/Application Support/Claude/claude_desktop_config.json
```

**不要只凭“文件里已经写入”就判定配置生效。** 不同发行版可能读取不同的
Application Support 子目录；2026-08-13 的历史排查中，配置写进了 `Claude/`，
实际运行的客户端却只读取 `Claude-3p/`，所以那次写入从未生效。真正恢复客户端前，
必须用该客户端自己的运行时注册表或日志确认它读取的配置，并在重启后看到下面五个
工具；当前文档不授权修改任何客户端配置。

获批激活时，把 `job-agent` 这一段作为**候选**加入 `mcpServers`：

```json
{
  "mcpServers": {
    "job-agent": {
      "command": "/absolute/fixed/job-agent/.venv/bin/python",
      "args": [
        "-m",
        "jobagent.mcp_server",
        "--db",
        "/absolute/path/to/jobagent.db",
        "--profile",
        "/absolute/path/to/profile.yaml"
      ]
    }
  }
}
```

关键点：

- `command` 是固定目录虚拟环境的绝对路径，不写裸 `python`。
- `args` 同时固定模块、数据库和画像；不能省略两个路径参数。
- 不依赖 `cwd`。客户端从任意目录启动，仍只使用上面明确绑定的两个文件。
- 不提供自动改配置脚本。激活必须先退出客户端、备份并精确检查同名项，避免恢复旧实例。

**改完必须重启 Claude Desktop。** 配置只在启动时读一次。

### Claude Code（CLI）

```bash
claude mcp add job-agent -- /absolute/fixed/job-agent/.venv/bin/python \
  -m jobagent.mcp_server \
  --db /absolute/path/to/jobagent.db \
  --profile /absolute/path/to/profile.yaml
```

## 三、确认它真的通了

先确认 server 自己能起来（会挂住等 stdio 输入，`Ctrl-C` 退出 —— 挂住就是对的）：

```bash
/absolute/fixed/job-agent/.venv/bin/python -m jobagent.mcp_server \
  --db /absolute/path/to/jobagent.db \
  --profile /absolute/path/to/profile.yaml
```

重启客户端后，从客户端自己的**运行时注册表**确认应该正好是五个工具：

```
list_jobs
explain_match
list_sources
list_sync_runs
job_changes
```

最后在对话里实调一次 `list_sources` 和 `list_jobs`。前面的启动只证明进程能起，
不证明客户端连上了。随便问一句「现在库里有多少开放岗位」，
看模型是不是真调了 `list_jobs`（界面上会显示工具调用）。
没看到工具调用就是没连上，不能把“配置文件里已经写了”当成成功。

---

## 四、五个工具各干什么

| 工具 | 问什么 | 注意 |
|---|---|---|
| `list_jobs` | 当前开放岗位，可按族/城市/公司筛，可只看命中我画像的 | 届别来自画像匹配，不是独立工具参数；`total` 不受 `limit` 影响 |
| `explain_match` | 某条岗位为什么命中／不命中 | `state` 是**三态**；命中时 `matched_on` 列硬条件，`score_breakdown` 列排序加分。分数不是录用概率 |
| `list_sources` | 每个源的岗位数、最近采集、投递配额 | `last_run` 为 null = **一次都没跑过**，和「跑过但失败了」不是一回事 |
| `list_sync_runs` | 采集批次历史 | `finished_at` 为 null = 这轮没收尾（进程被杀或正在跑），不是数据缺失 |
| `job_changes` | 岗位变动：新开、关闭、改动、源首次接入 | **只有岗位侧事件。** `since` 按带时区的真实时刻筛选，不按时间字符串外观比较 |

### 几个容易读错的地方

**判不出族的岗位按任何族筛都查不到，包括 `other`。** 那一列是空的，
不是「归到 other 里了」。想看这批得不带 `family` 参数。

`explain_match` 的 `score` 只是同一画像下的轻量展示顺序。看它时要同时看
`score_breakdown`，不要把高分理解成“更容易录用”。

表单判据检查不属于 MCP。它会启动浏览器并接触登录态，只能在明确授权的
本地人工流程中运行，不能从对话工具注册表恢复。

---

## 五、这一层为什么动不了库

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
