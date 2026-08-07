---
name: ai-room
description: 用本机 ai-room CLI 把活派给另一个 AI（Claude Code / Codex / opencode）。**默认走 `ask` 无头模式**：一条命令派一个一次性子 agent 去咨询或干活，不需要用户开第二个窗口，问完即返回。当用户说"问问另一个 AI""让 codex/claude 看一下""派个子 agent 去做""opencode 跑一下"，或你需要另一个模型的独立判断时用它。只有用户明确要求"开两个窗口互当顾问 / 走信箱 / join 房间"时，才改用 join/wait/send/reply 的对话模式（且 opencode 不支持那个模式）。
---

# ai-room

本机 CLI，命令名 `ai-room`（等价 `python -m ai_room`）。每条命令输出**一行 JSON**，成功是 `{"ok":true,...}`。退出码：`0` 成功、`2` 参数错、`3` 运行或业务失败、`130` 被中断。

## 默认走 `ask`，别问用户要不要开窗口

它提供两件互不相干的能力，**默认永远是 B**：

| | **能力 B：`ask` 无头派发（默认）** | 能力 A：信箱顾问（要用户明确要求） |
|---|---|---|
| 是什么 | 一条命令派一个**一次性子 agent** 去咨询或干活，不需要窗口、不进信箱，跑完即返回 | 两个**用户亲手打开的可见窗口**互相咨询，谁在服务用户谁是主聊，另一边是本次顾问 |
| 命令 | `ask` | `join` `wait` `send` `reply` `status` `leave` |
| 谁能用 | 三方都能**调用**；claude / codex / opencode 都能当**目标** | **只有 codex 和 claude** |
| 对方是 | 工人，`workspace-write` 档位下会改文件、跑测试构建 | 顾问，只出意见和改指定文档 |
| 前置条件 | **无。** 直接跑就行 | 用户已经亲手开好两个窗口并各自 `join` 过 |

**判定规则，只有一条**：用户没有明确说要"开两个窗口 / 走信箱 / 让另一个 AI 加入房间 / join"，就用 `ask`。需要另一个模型的意见时直接派发，**不要反问用户"要不要开一个 codex 窗口"**——那是在把一件一条命令能办的事变成一次协商。

只在这些情况下才走能力 A：用户明说要对话模式；或者两个窗口已经在跑（`ai-room status` 显示对方 `waiting`）；或者主聊接近上下文上限、需要对方确认压缩安全节点（那件事本身依赖两边都在线）。

## 先认领身份，再读你自己那一份

| 你是 | 读同目录下的 | 你能用 |
|---|---|---|
| Claude Code | `claude-code.md` | 默认 `ask`；用户点名要对话模式时才走信箱 |
| Codex | `codex.md` | 默认 `ask`；用户点名要对话模式时才走信箱 |
| opencode | `opencode.md` | **只有 `ask`**，信箱模式加不进去 |

这三份文件就在本 skill 目录里，和 SKILL.md 同级。**只读你自己那一份。** 三方的可用命令、身份探测方式和 shell 默认超时都不一样，照抄别人的会直接报错。

分不清自己是谁时看环境变量：有 `CODEX_THREAD_ID` 就是 Codex；有 `AI_ROOM_CLAUDE_SESSION_ID` 就是 Claude Code；两个都没有，基本是 opencode。

## 三方共同的铁律

1. **顾问 ≠ 工人。** 能力 A 的顾问只做技术决策和需求/设计/计划文档审查，只能改 `--writable-doc` 精确列出的文件，**绝不改源码**，**绝不替主聊跑测试、构建、部署或任何真实操作**。能力 B 用 `ask` 派出去的是有手的工人，顾问这套约束在 B 上不成立。
2. **路径必须精确。** 所有 `--*-doc` 都要相对工作树根的**精确文件路径**。目录、glob、`..`、只给个扩展名，一律不会扩大权限。
3. **压缩永远手动。** 任何一方都不得自己执行 `/compact`，只能在拿到 `COMPACT_READY` 之后**提示用户**手动压缩。
4. **`ask` 的 `changed_files` 回执不是沙箱。** 它是 `git status --porcelain` 的前后差集：看不见 gitignore 里的东西，也救不了跑错项目——子 agent 整个跑在别的仓库时，回执是一片干净。边界只能靠工作目录参数**事先**钉死，事后检测兜不住。
5. **一次只问一个具体问题**，外加回答它所需要的每一条精确路径。永远不要让对方"全面 review 一下"。

## `ask` 命令形（默认模式，三方通用）

```text
ai-room ask --to claude|codex|opencode --question TEXT
            [--related-doc EXACT_PATH]...
            [--permission read-only|workspace-write|full-access]
            [--model MODEL] [--cwd DIR] [--timeout SECONDS]
            [--permission-mode MODE] [--sandbox MODE] [--no-ledger]
```

- `--permission` 默认 `read-only`。`workspace-write` 允许子 agent 在工作目录里改文件、跑命令；`full-access` 解除沙箱。档位由 ask 自己钉死，**不继承本机的厂商配置**——同一个 flag 在不同 config 下含义不同。
- `--cwd` 选择针对哪个项目派发。真正传给子 agent 的工作目录是**这个路径所在的 git 工作树根**，不是你给的子目录。不给就用当前进程 cwd。
- `--timeout` 默认 **300 秒**，这是 ai-room 掐子进程的时间。**你自己 shell 工具的超时是另一道闸，通常更短**——各家默认值差异很大，见你那一份手册。
- **`ask` 是同步阻塞的**，返回时子 agent 已经跑完。一个咨询几十秒，一个真任务几分钟。它**不会** detach。
- 退出码 0 表示厂商自己判定这一轮成功；失败和超时退 3。`changed_files` 是**回执**，只供复核，不影响退出码——派活时本来就不知道它该动哪些文件，没有可越的界。
- 台账写在 `<工作树根>/.ai-room/ledger.md`，含子 agent 的 **session id** 供续接，并自动生成 `.ai-room/.gitignore`（内容 `*`）避免进仓库。`--no-ledger` 关闭。

## 能力 A 的信箱协议（**非默认**，用户点名才走；codex 和 claude 适用，**opencode 跳过本节**）

```text
ai-room join codex [--room NAME]
ai-room join claude [--room NAME]
ai-room wait [--checkpoint EXACT_PATH]... [--next-entry TEXT]
ai-room send --to codex|claude --type decision|context-check --question TEXT [--related-doc EXACT_PATH]... [--checkpoint-doc EXACT_PATH]... [--next-entry TEXT] [--idempotency-key KEY]
ai-room send --to codex|claude --type requirements-review|design-review|plan-review --question TEXT --related-doc EXACT_PATH [--writable-doc EXACT_PATH]... [--checkpoint-doc EXACT_PATH]... [--next-entry TEXT] [--idempotency-key KEY]
ai-room reply TASK_ID --outcome done|blocked|compact-ready|checkpoint-needed --message TEXT
ai-room status
ai-room leave
```

**用自己的身份 join，永远不要冒充对方。** 角色不需要配置：谁在服务用户谁就是主聊，下一轮反向发送即可。

`ai-room wait` 是**故意阻塞且静默**的。用户按 `Esc` 中断（客户端不传 Esc 给 shell 时按 `Ctrl+C`）；中断只结束这次等待，**不会**离开房间、不会确认消息、不会丢任务——租约到期后同一条消息会重新投递。正式回复完毕后重新 `ai-room wait`。

只有三类文档审查可以带 `--writable-doc`；`decision` 和 `context-check` 的可写清单恒为空，是纯只读任务。

**当顾问时**：需要就只读源码，但只能写 `writable_docs` 里的那几个文件。把被问的那一个问题回答一次，给出**恰好一个** outcome：

- `DONE` —— 决策做完了，或允许的文档改完了。
- `BLOCKED` —— 需要用户拍板、需求没定，或事实拿不到。
- `CHECKPOINT_NEEDED` —— 压缩检查点记得不全。
- `COMPACT_READY` —— 检查点完整，可以提示用户手动压缩。

回复完回到 `ai-room wait`。给了 `DONE` 之后不要再要求一次全面复审，除非出现了新的具体问题。

**`reply` 前必须先被 `wait` 投递过。** `send` 之后任务立刻变 `working`，但那只表示消息存在、可被领取；只有 `wait` 真正投递时才拍下工作树基线。从 `status` 抄一个 task ID 直接 `reply` 会被拒（`task_not_delivered`）——先跑一次 `wait` 收下它。

**压缩节奏**（输入 token 由各自的 transcript / token 记录读出，读不到就是 `unknown`，此时跳过自动判断，不能拿文件大小或累计 token 代替）：

- < 150k：正常干活。
- 150k–200k：请顾问判断下一个安全节点，带上精确的 checkpoint 文档和下一步恢复入口。
- \> 200k：先完成或明确暂停**最小**工作单元，把重要决定、实际改动、验证结果、未解决问题、下一步入口写进 checkpoint 文档，再优先做这次检查。
- 绝不为了压缩打断正在进行的写入、构建、测试或诊断。

顾问返回 `CHECKPOINT_NEEDED` 时，只更新它指名的那几个文档，然后用
`ai-room wait --checkpoint EXACT_PATH --next-entry TEXT` 继续同一次检查。

## 运行数据

房间库在 `%LOCALAPPDATA%/ai-room`，不进项目仓库，按规范化的 git 工作树根分房间——**同一仓库的两个 worktree 默认互相隔离**。SQLite FIFO 队列，同一时刻只有一个顾问任务处于 `working`。`wait` 被中断、进程结束或重启时，未确认消息会在租约到期后重新投递；`leave` 和卸载都不删历史。
