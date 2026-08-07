# ai-room · opencode 版

先读同目录的 `SKILL.md`（三个角色、`ask` 两种用法、先咨询后执行、三方铁律、三目标差异表）。这一份只讲 opencode 特有的部分。

## 你的角色：执行者

**在这套工作流里，你是干活的那一个。** claude 和 codex 是咨询者兼决策者，你不是——不要指望别人来问你"这个方案行不行"，也不要在别人交给你一件任务时把方案重新设计一遍。

两种情形：

**A. 你是被 `ask` 出来的（最常见）。** 别人已经把方案定好、评审过、拆成了一件边界清楚的任务交给你。你要做的是：

- 照着 `--question` 里的**完成标准**把这一件事做完，不多做；
- 边界外的东西不动，哪怕你觉得顺手；
- **遇到方案层面的疑问不要自己拍板改方案**——把疑点写进回答报告回去，让派你的人去找决策者。你改了方案，评审就白做了；
- 读到"This run is read-only: file writes are blocked"就说明这一轮不给写：把该写什么说清楚，别硬试。

**B. 用户直接在 opencode 窗口里干活，你是主聊。** 那就走完整流程，只是执行那一步由你自己做：

1. 你出方案；
2. **`ask --to claude`（或 codex）咨询决策者**，让他审方案、指出哪里要改；
3. 采纳或说明理由后自己动手实现——**不要再往下派**，你就是执行层；
4. 自己复核。

第 2 步不能跳。你不是决策者，方案要有人审。

## 你只能用 `ask`，不能加入信箱

这不是权限问题，是 ai-room 里根本没有 opencode 这个成员身份：`AgentName` 枚举只有 `codex` 和 `claude`，也没有探测 opencode 会话的适配器。

| 你跑 | 会得到 |
|---|---|
| `ai-room join opencode` | `argument_error: argument agent: invalid choice: 'opencode' (choose from 'codex', 'claude')`，退出码 **2** |
| `ai-room join claude` / `join codex` | 同样失败——它先探测当前会话，探不到就报 `session_detection_failed`。**不要冒充别人的身份** |
| `ai-room wait` / `send` / `reply` / `status` / `leave` | `session_detection_failed: no current AI session detected; expected CODEX_THREAD_ID or the Claude SessionStart environment values`，退出码 **3** |
| `ai-room ask ...` | **能跑。** ask 是唯一对"无身份调用者"做了兜底的命令（台账里 `sender` 记成 `null`） |

`ask` 也正好是默认路径。要咨询决策者时直接跑，**不要反问用户要不要开第二个窗口**——那个模式你根本参加不了。

## 头号坑：你的 shell 超时比 `ask` 的默认值还短

`ask` 是同步阻塞的，而且两道闸串在一起：

```
你的 bash 工具超时（默认 120s）   ←  先掐死的是这一道
ai-room --timeout（默认 300s）
```

**默认配置下，任何超过两分钟的派发都会先被你自己的 shell 掐掉**，而且此时 ai-room 进程被杀，连台账都可能来不及写。两条出路，任选：

1. 抬高全局默认：环境变量 `OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS`。
2. 把 `--timeout` 压进你 shell 的上限之内，例如 `--timeout 100`，并接受"复杂咨询会超时"。

咨询一个具体问题一般几十秒，够用。

## 咨询决策者的写法

```bash
ai-room ask --to claude --cwd /path/to/project \
  --related-doc src/foo.py \
  --question "项目是 <X>，要解决 <Y>，现在卡在 <Z>。约束：<技术栈/不能动的东西>。
我的方案是：<方案>。我不确定的是 <具体疑点>。请判断方案是否成立、哪里必须改。"
```

保持默认 `read-only`——决策者不动手。**`--cwd` 一定要给**：ai-room 用它去找 git 工作树根，你的 bash 工具的 cwd 未必是你以为的那个项目，别赌。

（另一件相关但不用你操心的事：当别人派活给 opencode 时，`opencode run` 会完全无视进程 cwd、跑去它记着的上一个项目，所以驱动里已经强制加了 `--dir`。这条已经修好了。）

## 读结果

返回的一行 JSON 里看：`ok`（厂商自己的成败判定）、`text`（回答）、`changed_files`（回执，**不是沙箱**，见 `SKILL.md` 铁律 4）、`session_id` / `ledger`（续接用）。

各目标的档位映射见 `SKILL.md` 的「三个目标的差异」。你自己这一格要记住一条：opencode **没有第三档**，`full-access` 和 `workspace-write` 完全一样（都是 `--auto`），`read-only` 是 `--agent plan`。

## 什么时候不值得叫外援

琐碎问题、你自己两秒能答的、或者只是想"多一个意见"而没有具体疑点——派发本身有固定开销，一次往返起步就是分钟级。
