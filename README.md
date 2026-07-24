# ai-room 操作手册

`ai-room` 是一个 Windows 优先、本机运行的 CLI，让一个可见的 Codex 交互式会话和一个可见的
Claude Code 交互式会话通过 SQLite 房间互相咨询。当前实现已经通过自动测试，状态仍是
**awaiting dual-window acceptance**；真实双窗口验收尚未执行。

顾问只负责技术决策和需求、设计、计划文档的审查或修改。顾问可以按需只读源码，但不能修改
源码，也不能替主聊 AI 运行测试、构建、部署或其他真实操作。

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

- `%USERPROFILE%\.codex\skills\ai-room\SKILL.md`
- `%USERPROFILE%\.claude\skills\ai-room\SKILL.md`
- `%USERPROFILE%\.claude\settings.json`
- settings 需要改变时，同目录的 `settings.json.<UTC时间>.bak`

只有用户看过预演并明确批准真实安装后，才可单独执行：

```powershell
python -m pip install --user .
python -m ai_room.install --apply
```

不要把 `--apply` 混进日常自动测试或文档验收。安装器只合并 ai-room 的 Claude `SessionStart`
hook；遇到已有冲突文件、异常 JSON 结构或不安全目标会拒绝写入。

## 启动两个窗口

在同一个工作树根目录分别执行：

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

1. 确认 `%USERPROFILE%\.codex\skills\ai-room\SKILL.md` 和
   `%USERPROFILE%\.claude\skills\ai-room\SKILL.md` 仍是 ai-room 安装副本，再删除这两个
   `ai-room` skill 目录；用户改过的文件先备份，不要覆盖。
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
