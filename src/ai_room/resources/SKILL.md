---
name: ai-room
description: 用本机 ai-room CLI 调动另一个 AI，默认走 `ask` 无头模式，两种用法——① **咨询决策者**：把自己的方案交给对面那个 peer 审（你是 claude 就问 codex，是 codex 就问 claude），让他说行不行、哪里要改；② **派 opencode 执行**：把拆好的任务交给它去做。角色固定不串岗：claude 和 codex 是咨询者兼决策者，opencode 只当执行者。**派活给 opencode 之前必须先让决策者过一遍方案。** 每次 ask 都要把场景上下文说全，子 agent 是零上下文的。当用户说"问问另一个 AI""让 codex 看看我的方案""派个子 agent 去做""opencode 跑一下"，或你要动手做一件有分量的事时使用。用户点名要"开两个窗口互当顾问 / 走信箱 / join"时才改用 join/wait/send/reply。
---

# ai-room

本机 CLI，命令名 `ai-room`（等价 `python -m ai_room`）。每条命令输出**一行 JSON**，成功是 `{"ok":true,...}`。退出码：`0` 成功、`2` 参数错、`3` 运行或业务失败、`130` 被中断。

## 三个角色，谁也不许串岗

| 角色 | 是谁 | 干什么 | 绝不 |
|---|---|---|---|
| **主聊** | 你——此刻正在服务用户的这一个 | 出方案、拆任务、收口，对用户负责 | — |
| **决策者**（咨询对象） | **对面那个 peer**：你是 claude 就是 codex，你是 codex 就是 claude | 审你的方案、指出哪里要改、拍技术取舍 | 不派它干活，不给它写权限 |
| **执行者** | **只有 opencode** | 接一件已经拆好、边界清楚的任务去做 | 不问它决策，不让它定方案 |

claude 和 codex 是**咨询者兼决策者**；opencode **只当执行者**。这不是偏好，是这三方在本工作流里的固定角色。

## `ask` 的两种用法

### 用法一 · 咨询决策者（只读）

把**你自己的方案**交给 peer 审，问他行不行、哪里要改。不是让他替你想，是让他挑毛病。

```bash
ai-room ask --to codex --question "<场景><我的方案><我不确定的点>" \
  --related-doc <精确路径> --cwd <项目根>
```

保持默认 `read-only`。决策者不动手，给它写权限就是角色串岗。

### 用法二 · 派 opencode 执行（可写）

方案定稿、任务拆好之后，交给 opencode 去做。

```bash
ai-room ask --to opencode --question "<场景><已定方案><这一件具体任务><完成标准>" \
  --permission workspace-write --related-doc <精确路径> --cwd <项目根>
```

## 铁律：先咨询，后执行

**任何要交给 opencode 的任务，派之前必须先把方案送决策者过一遍。** 完整顺序：

1. 你出方案；
2. `ask --to <peer>` 咨询：这方案行不行、哪里要改；
3. 按意见修正——不同意就说明理由，你是主聊，最后由你收口；
4. 把定稿方案拆成边界清楚、完成标准明确的任务；
5. `ask --to opencode --permission workspace-write` 逐件派出去；
6. 收回执，自己复核。

跳过第 2 步直接派 opencode，等于让一个没有决策权的执行者，去执行一个没人审过的方案。

## 每次 `ask` 都必须把场景说全

子 agent 是**无头、零上下文**的：它不知道你在做什么项目、前面聊过什么、用户到底要什么。一句"这个函数怎么改"到它那里就是一句没有主语的话。每条 `--question` 至少要有：

- **场景**——什么项目、在解决什么问题、现在卡在哪；
- **约束**——技术栈、不能动的东西、用户已经定过的偏好；
- **这一次要什么**——咨询就写清"我的方案是 X，我不确定的是 Y"；执行就写清"做这一件事，完成标准是 Z"；
- **精确路径**——用 `--related-doc` 给全，它读不到你的对话。

## 什么时候才不走 `ask`

只有三种情况走信箱对话模式（`join`/`wait`/`send`/`reply`）：用户明说要开两个窗口互当顾问；两个窗口已经在跑（`ai-room status` 显示对方 `waiting`）；或者主聊接近上下文上限、需要对方确认压缩安全节点——那件事本身就依赖两边在线。

其余一律 `ask`。**不要反问用户"要不要开一个 codex 窗口"**——那是把一条命令能办的事变成一次协商。

## 先认领身份，再读你自己那一份

| 你是 | 读同目录下的 | 你的角色 |
|---|---|---|
| Claude Code | `claude-code.md` | 咨询者/决策者。当主聊时：咨询 **codex**，派 **opencode** 执行 |
| Codex | `codex.md` | 咨询者/决策者。当主聊时：咨询 **claude**，派 **opencode** 执行 |
| opencode | `opencode.md` | **执行者**。只能用 `ask`，信箱模式加不进去 |

这三份文件就在本 skill 目录里，和 SKILL.md 同级。**只读你自己那一份。** 三方的可用命令、身份探测方式和 shell 默认超时都不一样，照抄别人的会直接报错。

分不清自己是谁时看环境变量：有 `CODEX_THREAD_ID` 就是 Codex；有 `AI_ROOM_CLAUDE_SESSION_ID` 就是 Claude Code；两个都没有，基本是 opencode。

## 三方共同的铁律

1. **角色不串岗。** 决策者（peer）只出判断，保持 `read-only`；执行者（opencode）只干活，不替你定方案。信箱对话模式里的顾问同理：只做技术决策和需求/设计/计划文档审查，只能改 `--writable-doc` 精确列出的文件，**绝不改源码**，**绝不替主聊跑测试、构建、部署或任何真实操作**。只有派给 opencode 执行的那一路才是有手的工人，上面这套约束在那一路上不成立。
2. **路径必须精确。** 所有 `--*-doc` 都要相对工作树根的**精确文件路径**。目录、glob、`..`、只给个扩展名，一律不会扩大权限。
3. **压缩永远手动。** 任何一方都不得自己执行 `/compact`，只能在拿到 `COMPACT_READY` 之后**提示用户**手动压缩。
4. **`ask` 的 `changed_files` 回执不是沙箱。** 它是 `git status --porcelain` 的前后差集：看不见 gitignore 里的东西，也救不了跑错项目——子 agent 整个跑在别的仓库时，回执是一片干净。边界只能靠工作目录参数**事先**钉死，事后检测兜不住。
5. **一次只交办一件事**：咨询就一个具体问题，执行就一件边界清楚的任务，外加它所需要的每一条精确路径。永远不要让对方"全面 review 一下"或"把这个模块做完"。

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

### 三个目标的差异

| 目标 | `read-only` 映射成 | 可写档映射成 | session id | 续接 |
|---|---|---|---|---|
| claude | `--permission-mode plan` | `acceptEdits` + `--allowedTools Edit,Write`；`full-access` 是 `--dangerously-skip-permissions` | **派发前就预分配**，被掐死也能续 | `claude -r ID` |
| codex | `-s read-only` + `approval_policy="never"` | `-s workspace-write` + 允许沙箱拒绝升级到沙箱外；`full-access` 是 `danger-full-access` | 它开口后才有 | `codex exec resume ID` |
| opencode | `--agent plan` | `--auto`；**没有第三档，`full-access` 与 `workspace-write` 完全一样** | 它开口后才有 | `opencode run --session ID` |

只有 claude 支持预分配 handle。派给 codex / opencode 的活被外层掐死时，session id 是从它已经吐出来的输出里边跑边抢救的，抢不到就没有。

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
