# ai-room · opencode 版

先读同目录的 `SKILL.md`（默认模式判定、三方铁律、`ask` 命令形）。这一份只讲 opencode 特有的部分。

**你只有 `ask` 一条路，它也正好是默认路径。** 需要另一个 AI 的意见或想派活时直接跑，**不要反问用户要不要开第二个窗口**——那个模式你根本参加不了。

## 你只能用 `ask`，不能加入信箱

这不是权限问题，是 ai-room 里根本没有 opencode 这个成员身份：`AgentName` 枚举只有 `codex` 和 `claude`，也没有探测 opencode 会话的适配器。所以：

| 你跑 | 会得到 |
|---|---|
| `ai-room join opencode` | `argument_error: argument agent: invalid choice: 'opencode' (choose from 'codex', 'claude')`，退出码 **2** |
| `ai-room join claude` / `join codex` | 同样失败——它先探测当前会话，探不到就报 `session_detection_failed`。**不要冒充别人的身份** |
| `ai-room wait` / `send` / `reply` / `status` / `leave` | `session_detection_failed: no current AI session detected; expected CODEX_THREAD_ID or the Claude SessionStart environment values`，退出码 **3** |
| `ai-room ask ...` | **能跑。** ask 是唯一对"无身份调用者"做了兜底的命令（台账里 `sender` 记成 `null`） |

所以你在这个 skill 里的角色只有两个：**派活的人**，和**别人 `ask --to opencode` 时被派的那个工人**。

## 头号坑：你的 shell 超时比 `ask` 的默认值还短

`ask` 是同步阻塞的，而且两道闸串在一起：

```
你的 bash 工具超时（默认 120s）   ←  先掐死的是这一道
ai-room --timeout（默认 300s）
```

**默认配置下，任何超过两分钟的派发都会先被你自己的 shell 掐掉**，而且此时 ai-room 进程被杀，连台账都可能来不及写。两条出路，任选：

1. 抬高全局默认：环境变量 `OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS`。
2. 把 `--timeout` 压进你 shell 的上限之内，例如 `--timeout 100`，并接受"复杂任务会超时"。

问一个具体问题一般几十秒，够用；派一个真任务通常几分钟，必须先抬闸。

## 派发时 `--cwd` 要自己给

`ai-room` 用 `--cwd`（不给就用进程 cwd）去找 git 工作树根，那个根才是子 agent 的工作目录。你的 bash 工具的 cwd 未必是你以为的那个项目，所以**明确给 `--cwd`**，别赌。

（另一件相关但不用你操心的事：当**别人**派活给 opencode 时，`opencode run` 会完全无视进程 cwd、跑去它记着的上一个项目，所以驱动里已经强制加了 `--dir`。这条已经修好了。）

## 你能派给谁、会发生什么

```bash
# 只读咨询：问 claude 一个具体问题
ai-room ask --to claude --question "这个函数的错误处理漏了哪条路径？" \
  --related-doc src/foo/bar.py --cwd /path/to/project

# 派真活：让 codex 补单测，允许它写文件跑测试
ai-room ask --to codex --question "给 parse_frame 补边界单测并跑通" \
  --permission workspace-write --cwd /path/to/project --timeout 600
```

三个目标的差异，挑目标时用得上：

| 目标 | read-only 映射成 | 可写档映射成 | session id | 续接 |
|---|---|---|---|---|
| claude | `--permission-mode plan` | `acceptEdits` + `--allowedTools Edit,Write`；full-access 是 `--dangerously-skip-permissions` | **派发前就预分配**，被掐死也能续 | `claude -r ID` |
| codex | `-s read-only` + `approval_policy=never` | `-s workspace-write` + 允许升级；full-access 是 `danger-full-access` | 它开口后才有 | `codex exec resume ID` |
| opencode | `--agent plan` | `--auto`；**没有第三档，full-access 和 workspace-write 完全一样** | 它开口后才有 | `opencode run --session ID` |

只有 claude 支持预分配 handle。派给 codex / opencode 的活如果被外层掐死，session id 是从它已经吐出来的输出里边跑边解析抢救的，抢不到就没有。

## 读结果

返回的一行 JSON 里，你要看的是：

- `ok` —— 厂商自己判定这一轮成不成（`exit_code == 0` 且有正文；claude 还会额外看它自己的 `is_error`）。
- `text` —— 子 agent 的回答。
- `changed_files` —— 回执，**不是沙箱**（见 SKILL.md 铁律 4）。
- `session_id` / `ledger` —— 续接用。

## 什么时候值得用

- 用户明确要"问问 claude / 让 codex 看看"。
- 你需要另一个模型的**独立判断**，而不是自己再想一遍。
- 有几件互不依赖的活可以并行派出去。

不值得的：琐碎问题、你自己两秒能答的、或者只是想"多一个意见"而没有具体问题——派发本身有固定开销，一次往返起步就是分钟级。
