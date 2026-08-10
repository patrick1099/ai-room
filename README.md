# ai-room 操作手册

`ai-room` 是一个 Windows 优先、本机运行的 CLI，让 Claude Code / Codex / opencode 互相咨询和派活。
它有两条路，**默认是第一条**：

1. **`ask` 无头派发（默认）**——一条命令派一个一次性子 agent，不需要窗口、不进信箱、跑完即
   返回。AI 决定要外援时应该直接派，而不是反问用户"要不要开一个 codex 窗口"。见
   [`ask`（无头子 agent 派发）](#ask无头子-agent-派发)。
2. **双窗口信箱顾问（用户点名才走）**——一个可见的 Codex 会话和一个可见的 Claude Code 会话
   通过 SQLite 房间互当顾问。opencode 参加不了这条。这一路当前状态仍是
   **awaiting dual-window acceptance**；真实双窗口验收尚未执行。

### 三个角色

`ask` 这条路上角色是**固定的，不许串岗**：

| 角色 | 是谁 | 干什么 |
|---|---|---|
| 主聊 | 此刻正在服务用户的那一个 | 出方案、拆任务、收口 |
| **决策者**（咨询对象） | **对面那个 peer**：claude ↔ codex | 审方案、指出哪里要改、拍技术取舍。保持 `read-only`，不动手 |
| **执行者** | **只有 opencode** | 接一件拆好的任务去做。不问它决策，不让它定方案 |

于是 `ask` 有两种用法：**① 咨询决策者**（`--to <peer>`，只读，交上去的是你自己的方案，让他挑
毛病）；**② 派 opencode 执行**（`--to opencode --permission workspace-write`，交上去的是一件
边界清楚、完成标准明确的任务）。

**opencode 是廉价劳力，机械活随便派、鼓励派**：批量改名、跑格式化、加日志、补样板代码、跑测试
并摘出失败、改错别字。这类活没有方案可审，直接派。

**有技术取舍时才走「先咨询，后执行」**，而且门槛是**方案级**的一次，不是每件任务都重审：
opencode 不许执行没人审过的方案，但方案定稿后拆出来的多件任务直接派。判定只有一句——
**这件事有没有技术取舍？** 没有就直接派。

两种用法都必须把**场景上下文**说全——子 agent 是零上下文的，它不知道你在做什么项目。

信箱那条路的角色另算：其中的**顾问**只负责技术决策和需求、设计、计划文档的审查或修改，可以
按需只读源码，但不能修改源码，也不能替主聊 AI 运行测试、构建、部署或其他真实操作。

## 前置条件与开发安装

- Windows，Python 3.11 或更高版本；
- Git（Git 工作树会使用 `git rev-parse --show-toplevel` 确定房间根目录）；
- 两个由用户亲自打开的可见交互式窗口：Codex 和 Claude Code。

在仓库根目录建立开发环境并以 editable 方式安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\ai-room.exe --help
```

开发安装只影响该虚拟环境。面向当前 Windows 用户的真实安装
`python -m pip install --user .` 属于单独的验收步骤，必须另行获得用户明确批准后才能执行。

## 安装集成：先预演，后明确批准

先使用开发环境对目标操作做只读预演：

```powershell
.\.venv\Scripts\python.exe -m ai_room.install --check
# 若已激活这个专用虚拟环境，等价命令：
python -m ai_room.install --check
```

`--check` 和 `--apply` 共用同一条安装操作路径，但 `--check` 使用只记录、不落盘的 writer。
检查输出中的目标应只包括：

- `%USERPROFILE%\.codex\skills\ai-room\` 下的**整个 skill 目录**（`SKILL.md` + 三份 vendor 手册，见下节）
- `%USERPROFILE%\.claude\skills\ai-room\` 下的同一批文件
- `%USERPROFILE%\.claude\settings.json`
- settings 需要改变时，同目录的 `settings.json.<UTC时间>.bak`

安装源是 `integrations/ai-room/`（打包进 wheel 的副本在 `src/ai_room/resources/`，两者必须字节一致）。
目录里的每个 `.md` 都会被装走，加一份新手册不需要改安装器代码。目标已存在且内容不同时安装器
拒绝写入——本机若用 symlink 把这两个位置指到别处（例如 hub 金库），`--check` 会明确报
`installation ancestor is not a directory`，这是有意为之：不穿过 symlink 覆盖别人管理的目录。

只有用户看过预演并明确批准真实安装后，才可单独执行：

```powershell
python -m pip install --user .
python -m ai_room.install --apply
```

不要把 `--apply` 混进日常自动测试或文档验收。安装器只合并 ai-room 的 Claude `SessionStart`
hook；遇到已有冲突文件、异常 JSON 结构或不安全目标会拒绝写入。

## skill 的四个文件

`SKILL.md` 是**路由**，不是全文：它讲三方共有的部分（三个角色、`ask` 两种用法、先咨询后执行、
五条铁律、命令形、信箱协议），然后按身份把读者分流到三份 vendor 手册。三方角色并不相同：

| 读者 | 手册 | 角色与能力 |
|---|---|---|
| Claude Code | `claude-code.md` | 咨询者/决策者。主聊时咨询 codex、派 opencode 执行；也能走信箱 |
| Codex | `codex.md` | 咨询者/决策者。主聊时咨询 claude、派 opencode 执行；也能走信箱 |
| opencode | `opencode.md` | **执行者，只有 `ask`** —— `AgentName` 里没有 opencode，也没有它的会话探测器，`join`/`wait`/`send`/`reply`/`status` 一律失败 |

每份手册只写该 vendor 特有的机制：身份从哪个环境变量来、它自己的 shell 默认超时（claude ~600s、
opencode 120s、codex **10s**）怎么和阻塞的 `ask`/`wait` 配合、以及它作为 `ask` 目标时的档位映射。
共有的合同只在 `SKILL.md` 写一遍，手册不复述，避免四份文档互相漂移。

## 启动两个窗口（非默认，用户点名要对话模式时才做）

只想让 AI 问另一个 AI 一句话或派件活，**不需要这一节**——直接看 [`ask`](#ask无头子-agent-派发)。
下面这套是双窗口信箱模式，在同一个工作树根目录分别执行：

```powershell
# Codex 窗口
ai-room join codex
ai-room wait
```

```powershell
# Claude Code 窗口
ai-room join claude
ai-room wait
```

`wait` 正常时保持静默并阻塞。需要与某一边继续交互时，按 `Esc`；若客户端没有把 Esc 传给
Shell，则按 `Ctrl+C`。中断只结束本次等待，不会离开房间、确认消息或删除任务。交流或正式回复
结束后重新执行 `ai-room wait`。

谁当前服务用户，谁就是主聊 AI；另一边是本次顾问。下一项任务可以反向发送，角色无需重新配置。
每个会话必须用自己的身份加入，不能在 Codex 窗口执行 `join claude`，反之亦然。

## 六个命令

### `join`

登记当前会话。可选的命名房间仍绑定第一次登记的精确工作树根目录：

```powershell
ai-room join codex
ai-room join claude --room 文档评审
```

### `wait`

等待一条任务或回复；补齐压缩检查记录时可提供精确 checkpoint 和恢复入口：

```powershell
ai-room wait
ai-room wait --checkpoint docs/工作节点.md --next-entry "从 Task 4 的验证继续"
```

### `send`

发送一次具体咨询。技术决策和上下文检查是只读任务：

```powershell
ai-room send --to claude --type decision --question "方案 A 还是 B？"
ai-room send --to codex --type context-check --question "现在是否适合压缩？" `
  --related-doc docs/工作节点.md --checkpoint-doc docs/工作节点.md `
  --next-entry "继续实现 CLI"
```

文档审查可使用 `requirements-review`、`design-review` 或 `plan-review`。`--related-doc` 表示可读
文档，`--writable-doc` 是顾问唯一可修改的精确文件：

```powershell
ai-room send --to claude --type design-review --question "确认状态机边界" `
  --related-doc docs/design.md --writable-doc docs/design.md `
  --checkpoint-doc docs/工作节点.md --idempotency-key review-design-1
```

文档路径必须是相对于当前工作树根目录的精确文件路径。目录、glob、`..`、只给扩展名或模糊名称
都不会扩大权限。只有三类文档审查允许携带 `--writable-doc`；`decision` 和 `context-check`
的可写清单始终为空。回复前若发现清单外源码或文档发生新变化，工具会阻止回复并列出路径，
不会自动回滚或覆盖用户文件。

### `reply`

顾问对**已经通过 `wait` 收到**的任务回复一次。任务在 `send` 之后立即变成 `working`，但那只表示
请求消息已经存在、可以被领取；只有 `wait` 真正投递时才会拍下工作树基线。因此从 `status` 抄一个
task ID 直接 `reply` 会被拒绝（`task_not_delivered`），先跑一次 `wait` 即可：

```powershell
ai-room reply TASK_ID --outcome done --message "采用方案 A"
ai-room reply TASK_ID --outcome blocked --message "需要用户选择目标平台"
ai-room reply TASK_ID --outcome checkpoint-needed --message "请补充验证结果和下一步入口"
ai-room reply TASK_ID --outcome compact-ready --message "记录完整，可以提示手动压缩"
```

### `status`

查看成员和当前任务：

```powershell
ai-room status
```

成员状态为 `never_joined`、`joined_not_waiting` 或 `waiting`。任务状态为 `queued`、`working`、
`waiting_checkpoint`、`done`、`blocked` 或 `compact_ready`。对外回复结果为 `DONE`、
`BLOCKED`、`CHECKPOINT_NEEDED` 或 `COMPACT_READY`。

### `leave`

清除本会话的在线/等待状态，但保留消息和数据库历史：

```powershell
ai-room leave
```

### `ask`（无头子 agent 派发）

`ask` 把任务无头派发给某厂商 CLI 子 agent（claude / codex / opencode），不依赖可见会话或信箱，
并把派发记录和子 agent 的 **session id** 追加到项目根目录 `.ai-room/ledger.md` 台账，供续接：

两种用法（角色见上面的[三个角色](#三个角色)）：

```powershell
# ① 咨询决策者：交上去的是自己的方案，只读
ai-room ask --to claude --related-doc Code/App/Code/app/Protocol/protocol_IoT.c `
  --question "项目是<X>，要解决<Y>。我的方案是<方案>，不确定的是<疑点>。请判断是否成立、哪里必须改。"

# ② 派 opencode 执行：交上去的是一件拆好的任务，可写
ai-room ask --to opencode --permission workspace-write --related-doc Code/App/Code/app/Protocol/protocol_IoT.c `
  --question "方案已定为<定稿>（已过评审）。这一件任务：给 parse_frame 补边界单测并跑通。完成标准：<可验证标准>。"
```

- 权限走档位 `--permission`，默认 `read-only`。**咨询决策者时保持默认**——决策者不动手，给它
  写权限就是角色串岗。只有派 opencode 执行时才用 `workspace-write`（改文件、跑测试和构建），
  `full-access` 解除沙箱。**派给 opencode 的是有手的工人，不是顾问**——顾问那套「只改指定
  文档、不跑测试构建」的约束在这条路上不成立。
- **有技术取舍**的活，派 opencode 之前必须先让决策者过一遍方案；机械活直接派，不用问任何人。
  这条顺序 CLI 不强制，靠 skill 里的约定。审几次由上层工作流定——vibe-flow 按档位分（省档
  不咨询、好档审方案、不可逆/给别人的好档逐步回审）。
- `--cwd` 决定针对哪个项目派发，并用各厂商的原生参数钉死（claude `--add-dir`、codex `-C`、
  opencode `--dir`），不靠进程 cwd。opencode 尤其必须给：它**完全无视进程 cwd**，不给就会跑去
  改它自己记着的上一个项目，且照样 exit 0。
- 退出码 0 = 厂商自己说这一轮成功；失败和超时退 3。子 agent 改了哪些文件作为**回执**返回
  （`changed_files` 和台账里的「改动回执」），只供复核，**不影响退出码**——派活时事先并不知道
  它该动哪些文件，没有可越的界。
- 台账写 `.ai-room/`，并自动生成 `.ai-room/.gitignore`（`*`）避免被提交进仓库。
- **`--timeout` 是"允许沉默多久"，不是"允许跑多久"**：子 agent 每吐一行（stdout 或 stderr）就把
  死线往后推，所以还在输出的子 agent 不会被掐。封顶的是 `--max-runtime`，到点无条件结束。这两
  道闸分开，是因为"卡住了"和"停不下来"是两种故障，用一个墙钟同时管会把正在收敛的那一轮误杀。
- 两者的出厂默认是 **300s / 3600s**，可由环境变量 `AI_ROOM_TIMEOUT` / `AI_ROOM_MAX_RUNTIME`
  覆盖（显式 flag 优先；变量写坏会回落默认值而不是让派发失败）。之所以做成环境变量：合适的值
  取决于**调用方自己的 shell 超时**，那是"谁在驱动 ai-room"的属性，不是 ai-room 的属性。
- 调整用配套脚本 **`ai-timeouts`**（`ai-timeouts show` / `ai-timeouts set <分钟>`），它把三个平台的
  shell 闸和这两个预算一起改到位。**关键约束：硬顶必须小于外层 shell 闸**，否则先响的是外层，
  那种死法拿不到任何回执，只能盲跑 `ai-room resume`。别手动只动其中一个。

### `resume`（续接被掐断的那一轮）

超时那一轮**厂商已经按整轮计费了**，重发同一条 `ask` 等于同一份钱付两次。所以超时不是报错，
而是一个带把手的结果：`ok:false` + `status:"timeout"`，并带上 `timeout_reason`、`session_id`、
已产出的 `text`、以及可直接跑的 `resume_command`。

```powershell
ai-room resume --to opencode --session ses_xxx --permission workspace-write --cwd .
```

- 不给 `--session` 就续接本房间**最近一个被外层掐死**的派发。子 agent 一开口，session id 就落盘
  到 `.ai-room/inflight/`，所以哪怕整个 ai-room 进程被上层 shell 杀掉（那种情况连台账都来不及
  写），把手也还在；正常报告结束的派发会把这条记录清掉，留下的就只有真正的孤儿。
- 给 `--session` 时必须同时给 `--to`：一个 id 不会自报是哪家发的。
- 其余选项与 `ask` 相同，且**不会自动继承**上一轮的档位，要照原样再给一次。
- 兜底的厂商原生续接：`claude -r SESSION_ID`、`codex exec resume SESSION_ID`、
  `opencode run --session SESSION_ID`。注意 `codex exec resume` 不吃 `-s` 也不吃 `-C`，档位改走
  `-c sandbox_mode=`，工作目录沿用原会话记录的那个。

回执有两条硬限制，别把它当沙箱：

- **看不到 gitignore 里的东西**：回执是 `git status --porcelain` 的差集，被忽略的路径不出现。
- **救不了跑错目录**：子 agent 要是整个跑在别的项目里，这里的回执是一片干净。边界只能靠工作
  目录参数钉死，事后检测兜不住。

## 运行数据、恢复与并发

运行数据位于 `%LOCALAPPDATA%/ai-room`，不会写进项目仓库。每个规范化工作树根目录对应独立
房间，因此同一仓库的两个 worktree 默认也互相隔离。SQLite 使用 FIFO 队列；同一时刻只有一个
顾问任务处于 `working`，其余任务保持 `queued`。

任务只有在正式 `reply` 或后续确认后才推进。`wait` 被 Esc/Ctrl+C 中断、进程结束或机器重启时，
未确认消息会在租约到期后重新投递。`leave` 和卸载都不删除历史。

## 上下文与手动压缩

Codex 从当前 `CODEX_THREAD_ID` 对应的 token 记录读取输入 token；Claude Code 从
`SessionStart` 登记的当前 transcript 读取 usage。若记录缺失、不可读或格式漂移，状态明确为
`unknown`：消息收发继续工作，但跳过自动压缩时机判断，不能用文件大小或累计 token 代替。

- 低于 150k：继续正常工作；
- 150k–200k：请顾问检查安全节点；
- 高于 200k：先完成或明确暂停最小工作单元，再优先检查。

安全 checkpoint 必须写明重要决定、实际改动、验证结果、未解决问题和下一步恢复入口，并且当前
没有进行中的写入、测试、构建或诊断。信息不足时回复 `CHECKPOINT_NEEDED`；补齐精确文档后用
`ai-room wait --checkpoint ... --next-entry ...` 继续同一个检查。

只有收到 `COMPACT_READY` 后，主聊 AI 才提示用户 **manual** 执行 `/compact`。ai-room、Codex
和 Claude 顾问都不得自动执行 `/compact`。

## 故障排查

- `no current AI session detected`：确认命令确实在对应交互式会话的终端中运行；Codex 需要
  `CODEX_THREAD_ID`，Claude 需要 SessionStart hook 提供的两个 `AI_ROOM_CLAUDE_*` 值。
- **Windows：SessionStart hook 报 `Unexpected token '-m'` 或 `requires bash but Git Bash was not
  found`。** 装的 hook 命令按 Windows 规则加引号（`Program Files` 下的 Python 必须如此），而
  PowerShell 会把"以引号路径开头的一行"当成字符串表达式，还没启动 python 就先解析失败。因此
  hook 里钉了 `"shell": "bash"`。要让 Claude Code 在任何环境下都找得到 Git Bash，在
  `~/.claude/settings.json` 的 `env` 里给出绝对路径：

  ```json
  "env": { "CLAUDE_CODE_GIT_BASH_PATH": "D:\\Software\\Git\\bin\\bash.exe" }
  ```

  尤其是 claude 被当作 ai-room 子 agent 跑起来时——那个子进程的搜索路径和你终端里的不一样，
  找不到 Git Bash，hook 就整个失效（子 agent 拿不到自己的会话身份）。
- `peer_not_joined`：先让对方窗口在同一工作树执行 `join`。
- `room_binding_missing`：当前会话尚未加入；执行 `join`。若要换命名房间，先 `leave`。
- `room_database_missing` 或 schema 错误：保留现场，不要自行删除数据库；先备份
  `%LOCALAPPDATA%/ai-room` 再诊断。
- `task_not_delivered`：这一轮还没有投递到当前会话，因此没有工作树基线可比。执行一次
  `ai-room wait` 收下该任务（租约到期后会重新投递同一 message ID），再 `reply`。任务不会因此
  丢失或卡死。
- reply 结果里 `state` 变成 `blocked` 且 `guard_violations` 非空：任务窗口内有 `writable_docs`
  之外的文件发生变化，这一轮被判为未完成。ai-room 只能看到「变了什么」，看不到「谁改的」——
  可能是顾问越界，也可能是主聊或你自己在同一工作树里动了文件；请自行判断后决定是否重发任务。
  工具不会代为回滚。
- `database_busy`：确认没有异常长事务，稍后重试；不要删除 WAL 或数据库文件。
- token 为 `unknown`：检查当前会话 ID、transcript 路径和 JSONL 格式；这不影响普通消息。
- `wait` 看似无输出：这是正常阻塞状态；用另一窗口 `status` 确认 `waiting`。

## 备份、回滚与卸载

升级、回滚或人工排障前，可以先复制 `%LOCALAPPDATA%/ai-room` 保存消息历史；不要在两个会话
仍运行时复制一个正在写入的数据库。安装器修改现有 Claude settings 前会在同目录创建
`settings.json.<UTC时间>.bak`。

回滚用户集成时只处理 ai-room 自己的内容：

1. 确认 `%USERPROFILE%\.codex\skills\ai-room\` 和 `%USERPROFILE%\.claude\skills\ai-room\`
   里的每个 `.md` 都还是 ai-room 安装副本（和 `integrations/ai-room/` 逐字节比对），再删除
   这两个 `ai-room` skill 目录；用户改过的文件先备份，不要覆盖。安装器只写不删，所以上一版
   留下的旧手册若已改名，会残留在目录里，回滚时一并清掉。
2. 若安装后 Claude settings 没有其他变化，可选择正确的
   `settings.json.<UTC时间>.bak` 恢复为 `settings.json`。若之后已有用户修改，则只从
   `hooks.SessionStart` 删除 command 含 `ai_room.hooks.claude_session_start` 的 ai-room
   group，保留所有其他设置和 hook；不要用旧备份覆盖新修改。
3. 执行 `python -m pip uninstall ai-room` 只卸载对应 Python 包；虚拟环境开发安装则可直接
   移除该专用 `.venv`。

普通回滚或卸载**不会删除** `%LOCALAPPDATA%/ai-room`，从而保留队列和历史。只有用户明确要求
擦除历史并确认已备份后，才另行删除该目录。

## 验收状态

- [需求清单](docs/NEEDS.md)
- [设计文档](docs/superpowers/specs/2026-07-23-ai-room-design.md)
- [真实双窗口验收清单](docs/acceptance/dual-window.md)

自动验证通过不等于真实可用。只有用户在一个真实 Codex 窗口和一个真实 Claude Code 窗口完成
全部未勾选清单后，设计状态才可以改为 `accepted`。
