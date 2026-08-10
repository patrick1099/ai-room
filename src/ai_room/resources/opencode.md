# ai-room · opencode 版

先读同目录的 `SKILL.md`（三个角色、`ask` 两种用法、先咨询后执行、三方铁律、三目标差异表）。这一份只讲 opencode 特有的部分。

## 你的角色：执行者

**在这套工作流里，你是干活的那一个。** claude 和 codex 是咨询者兼决策者，你不是——不要指望别人来问你"这个方案行不行"，也不要在别人交给你一件任务时把方案重新设计一遍。

你会经常接到很琐碎的活：批量改名、跑格式化、加日志、补样板代码、跑测试并摘出失败、改错别字。**这是设计如此，不是把你当摆设。** 这类机械活本来就该由你干掉，派你的人省下的时间用在判断上。接到就干，干干净净地干完，报告改了什么。

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

## 头号坑：三道闸，性质完全不同，别搞混

`ask` 是同步阻塞的，路上串着三道闸：

| 闸 | 是什么 | 谁在管 |
|---|---|---|
| **你的 bash 工具超时** | **硬墙**——到点连 ai-room 进程一起杀，回执、台账全没有 | `OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS` |
| ai-room `--timeout` | **沉默预算**——只要子 agent 还在输出就一直不掐 | `AI_ROOM_TIMEOUT` |
| ai-room `--max-runtime` | 硬顶——不管多话，到点结束（**被有意设得比外层闸小**，好让 ai-room 先响、给出带 resume 的回执）| `AI_ROOM_MAX_RUNTIME` |

**当前各是多少跑 `ai-timeouts show`，别照抄任何文档里的数字。** 要调就 `ai-timeouts set <分钟>`，一条命令把三道闸一起改到位，别手动去动其中一个——闸之间的大小关系是有讲究的，动歪了就拿不到回执。

**绝对不要为了"躲开 shell 超时"去压小 `--timeout`。** 它是"允许沉默多久"，不是"允许跑多久"；压小只会把**正在思考的**子 agent 误杀，而那一轮照样按整轮计费。要让长任务跑得完，该调大的是**你 bash 调用自己的 timeout**。

**opencode 独有的坑**：你的工具描述里硬编码写着"2 分钟"，不管 operator 把默认值设成多少都是那句话。别被它带着走——派长任务时**自己显式把 bash 的 timeout 传够**，不要只指望默认值，也不要随手传个 120000。

咨询一个具体问题一般几十秒，够用；派真任务才需要把 timeout 拉满。

## 被掐死了：续接，别重发

超时那一轮**已经计过费了**，重发同一条 `ask` 是同一份钱付两次，而且第二遍照样会超时。

- **ai-room 判的超时**：回执带 `status:"timeout"`、`timeout_reason`、`session_id`、已产出的 `text`、`resume_command`。**先看 `text`**——它经常已经把话说完了，那就直接用，连 resume 都不用跑；不够再跑 `resume_command`。
- **被你自己的 shell 掐掉**：什么回执都没有。直接跑 `ai-room resume --cwd <项目根>`——session id 在子 agent 一开口时就落盘到 `.ai-room/inflight/`，ai-room 进程被杀也还在。
- 只有一种情况该重发：回执里连 `session_id` 都没有（`resume_command` 是 `null`），说明它还没开口就被打死，没有东西可续。
- 完整规则见 `SKILL.md` 的「超时了怎么办」。

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

返回的一行 JSON 里看：`ok`（厂商自己的成败判定）、`status`（`ok` / `error` / `timeout`）、`text`（回答，**超时时也有**，是它已经说出来的那部分）、`changed_files`（回执，**不是沙箱**，见 `SKILL.md` 铁律 4）、`session_id` / `resume_command` / `ledger`（续接用）。

各目标的档位映射见 `SKILL.md` 的「三个目标的差异」。你自己这一格要记住一条：opencode **没有第三档**，`full-access` 和 `workspace-write` 完全一样（都是 `--auto`），`read-only` 是 `--agent plan`。

## 什么时候不值得叫外援

琐碎问题、你自己两秒能答的、或者只是想"多一个意见"而没有具体疑点——派发本身有固定开销，一次往返起步就是分钟级。
