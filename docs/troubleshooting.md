# 排错、并发与上下文

## 排错

- `no current AI session detected`：确认命令确实在对应交互式会话的终端里运行。Codex 需要
  `CODEX_THREAD_ID`，Claude 需要 SessionStart hook 提供的两个 `AI_ROOM_CLAUDE_*` 值。

- **Windows：SessionStart hook 报 `Unexpected token '-m'` 或 `requires bash but Git Bash was
  not found`。** 装的 hook 命令按 Windows 规则加引号（`Program Files` 下的 Python 必须如此），
  而 PowerShell 会把「以引号路径开头的一行」当成字符串表达式，还没启动 python 就先解析失败。
  因此 hook 里钉了 `"shell": "bash"`。要让 Claude Code 在任何环境下都找得到 Git Bash，在
  `~/.claude/settings.json` 的 `env` 里给绝对路径：

  ```json
  "env": { "CLAUDE_CODE_GIT_BASH_PATH": "D:\\Software\\Git\\bin\\bash.exe" }
  ```

  claude 被当作 ai-room 子 agent 跑起来时尤其要注意：那个子进程的搜索路径和你终端里的不一样，
  找不到 Git Bash，hook 就整个失效，子 agent 拿不到自己的会话身份。

- `peer_not_joined`：先让对方窗口在同一工作树执行 `join`。

- `room_binding_missing`：当前会话尚未加入，执行 `join`。要换命名房间先 `leave`。

- `room_database_missing` 或 schema 错误：保留现场，不要自行删除数据库；先备份
  `%LOCALAPPDATA%/ai-room` 再诊断。

- `task_not_delivered`：这一轮还没投递到当前会话，因此没有工作树基线可比。执行一次
  `ai-room wait` 收下该任务（租约到期后会重新投递同一 message ID），再 `reply`。任务不会
  因此丢失或卡死。

- reply 结果里 `state` 变成 `blocked` 且 `guard_violations` 非空：任务窗口内有 `writable_docs`
  之外的文件发生变化，这一轮判为未完成。ai-room 只能看到「变了什么」，看不到「谁改的」，
  可能是顾问越界，也可能是主聊或你自己在同一工作树里动了文件。自行判断后决定是否重发。
  工具不会代为回滚。

- `database_busy`：确认没有异常长事务，稍后重试；不要删除 WAL 或数据库文件。

- token 为 `unknown`：检查当前会话 ID、transcript 路径和 JSONL 格式。这不影响普通消息。

- `wait` 看似无输出：这是正常阻塞状态，用另一窗口 `status` 确认 `waiting`。

## 运行数据、恢复与并发

运行数据位于 `%LOCALAPPDATA%/ai-room`，不会写进项目仓库。每个规范化工作树根目录对应独立
房间，因此同一仓库的两个 worktree 默认也互相隔离。

SQLite 使用 FIFO 队列，同一时刻只有一个顾问任务处于 `working`，其余保持 `queued`。任务只有
在正式 `reply` 或后续确认后才推进。`wait` 被 Esc/Ctrl+C 中断、进程结束或机器重启时，未确认
消息会在租约到期后重新投递。`leave` 和卸载都不删除历史。

## 上下文与手动压缩

Codex 从当前 `CODEX_THREAD_ID` 对应的 token 记录读取输入 token；Claude Code 从 `SessionStart`
登记的当前 transcript 读取 usage。若记录缺失、不可读或格式漂移，状态明确为 `unknown`：消息
收发继续工作，但跳过自动压缩时机判断。不能用文件大小或累计 token 代替。

| 输入 token | 动作 |
|---|---|
| 低于 150k | 继续正常工作 |
| 150k–200k | 请顾问检查安全节点 |
| 高于 200k | 先完成或明确暂停最小工作单元，再优先检查 |

安全 checkpoint 必须写明重要决定、实际改动、验证结果、未解决问题和下一步恢复入口，并且当前
没有进行中的写入、测试、构建或诊断。信息不足时回复 `CHECKPOINT_NEEDED`；补齐精确文档后用
`ai-room wait --checkpoint ... --next-entry ...` 继续同一个检查。

只有收到 `COMPACT_READY` 后，主聊 AI 才提示用户手动执行 `/compact`。ai-room、Codex 和 Claude
顾问都不得自动执行 `/compact`。
