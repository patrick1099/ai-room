# ai-room · Claude Code 版

先读同目录的 `SKILL.md`（三个角色、`ask` 两种用法、先咨询后执行、三方铁律、三目标差异表、信箱协议全文）。这一份只讲 Claude Code 特有的部分。

## 你的角色

**你是咨询者兼决策者。** 你有两个身份，看你此刻在哪一头：

- **当主聊时**（用户在跟你说话）：你出方案、拆任务、收口。你的决策者是 **codex**，你的执行者是 **opencode**。
- **当被 `ask` 出来时**（别人把你当决策者咨询）：只出判断——说这方案行不行、哪里要改、代价是什么。**不动手改代码**，哪怕你觉得改起来很快。这一路默认就是 `read-only`，写操作会被挡（`--permission-mode plan`）。

不要反问用户"要不要开一个 codex 窗口"——`ask` 是无头的，直接派。

## 主聊的完整流程

```bash
# ① 咨询决策者：把你的方案交给 codex 审
ai-room ask --to codex --cwd /path/to/project \
  --related-doc src/service.py --related-doc docs/NEEDS.md \
  --question "项目是 <X>，要解决 <Y>，现在卡在 <Z>。约束：<技术栈/不能动的东西/用户定过的偏好>。
我的方案是：<方案>。我不确定的是 <具体疑点>。请判断方案是否成立、哪里必须改，给出理由。"

# ② 采纳或说明理由后拆任务，逐件派 opencode 执行
#    超时不用给 flag（默认值已由 ai-timeouts 调好）；要给的是**你这次 Bash 调用**的 timeout
ai-room ask --to opencode --permission workspace-write --cwd /path/to/project \
  --related-doc src/service.py \
  --question "项目是 <X>，方案已定为 <定稿方案>（已经过 codex 评审）。
这一件任务：<一件边界清楚的事>。完成标准：<可验证的标准>。不要动 <边界外的东西>。"
```

**有技术取舍时顺序不可跳**：派 opencode 之前必须先让 codex 过一遍方案，否则就是让一个没有决策权的执行者，去替你做技术决定。但这是**方案级**的一次，不是每件任务都重审——方案定稿后拆出来的多件任务直接派。

**机械活根本不用走这一步。opencode 是廉价劳力，随便用。** 批量改名/替换、跑格式化、加日志、照现成模式补样板代码、跑测试构建并摘出失败、写死板的单测骨架、大范围 grep 汇总、改错别字补注释补类型标注——这类活没有方案可审，直接派，别自己埋头做。判定只有一句：**这件事有没有技术取舍？** 没有就直接派。

拿回结果看 `ok`（厂商自己的成败判定）、`status`（`ok` / `error` / `timeout`）、`text`（回答，**超时时也有**，是它已经说出来的那部分）、`changed_files`（回执，不是沙箱）、`session_id` / `resume_command` / `ledger`（续接用）。**opencode 的产出你必须自己复核**，它是执行者不是负责人。

各目标的档位映射见 `SKILL.md` 的「三个目标的差异」。这里只补一条你独有的：**派给 claude 时 session id 是派发前预分配的**（uuid 经 `--session-id` 传下去），哪怕子 agent 一个字没吐出来就被掐死，那个 handle 照样能续上（走 `ai-room resume --to claude --session ID`）。

## 三道闸，只有一道要你在调用时操心

| 闸 | 是什么 | 谁在管 |
|---|---|---|
| **你 Bash 工具的 timeout** | **硬墙**——到点连 ai-room 进程一起杀，回执、台账全没有。**默认只有 120000ms** | 每次调用自己传，上限是 `BASH_MAX_TIMEOUT_MS` |
| ai-room `--timeout` | **沉默预算**——只要子 agent 还在输出就一直不掐 | `AI_ROOM_TIMEOUT`，默认值已调好 |
| ai-room `--max-runtime` | 硬顶——不管多话到点结束，**被有意设得比外层闸小**，好让 ai-room 先响、给出带 resume 的回执 | `AI_ROOM_MAX_RUNTIME`，默认值已调好 |

- **要调的只有第一道。** 派真任务时把 Bash 调用的 `timeout` 传到上限；ai-room 那两个不用给 flag，默认值已经由 `ai-timeouts` 统一设好。`ai-timeouts show` 看当前值，`ai-timeouts set <分钟>` 一次性调全部——**别照抄文档里的数字，也别手动只动其中一个**。
- **绝不要为了"躲开 Bash 超时"去压小 `--timeout`。** 它是"允许沉默多久"不是"允许跑多久"，压小只会误杀正在思考的子 agent，那一轮照样计费。
- **超时不要重发。** 回执带 `session_id`、已产出的 `text` 和 `resume_command`——先看 `text`（常常已经够用），不够再跑 `resume_command`。重发同一条 ask 等于把计过费的一轮重新买一遍。连回执都没拿到（Bash 先把 ai-room 掐了）时直接 `ai-room resume --cwd <项目根>`，它会认领最近一个被掐死的派发。完整规则见 `SKILL.md` 的「超时了怎么办」。
- **`ai-room wait`**（只在信箱模式下用）是故意阻塞且静默的，在 Bash 工具里跑就把 `timeout` 拉到上限。超时只是这次等待结束，**不会**离开房间、不会丢消息、不会确认掉任务；重跑即可，租约到期后同一条消息会重新投递。用户按 Esc 或 Ctrl+C 同理。

`ask` 不会 detach。要边等边干活就自己起一个 subagent 去做那次阻塞调用——但代理开销起步两三分钟，咨询同步做，只有真任务才值得代理。

## 身份从哪来

`SessionStart` hook 注入两个环境变量，必须成对存在：

- `AI_ROOM_CLAUDE_SESSION_ID`
- `AI_ROOM_CLAUDE_TRANSCRIPT_PATH`

只有一个 → `incomplete Claude session environment`。两个都没有 → `no current AI session detected`，去查 `~/.claude/settings.json` 里 `hooks.SessionStart` 有没有 `ai_room.hooks.claude_session_start`（matcher 应含 `startup|resume|clear|compact`）。

**`ask` 不需要身份**（探测不到就把 `sender` 记成 `null`），信箱那套才需要。上下文用量也从这个 transcript 读 usage：缺失或格式漂移时状态明确是 `unknown`，消息收发照常但**跳过压缩时机判断**，不要拿文件大小或累计 token 顶替。

---

以下是**非默认模式**，只在用户点名要两个窗口对话时才用。

## 当主聊：什么时候该问顾问

材料级的技术决策、需求/设计/计划文档审查、以及被要求的上下文检查。一次一个具体问题，把回答它需要的每条精确路径都给全。

```bash
ai-room send --to codex --type design-review \
  --question "状态机在 wait 被中断后的恢复路径是否有空窗？" \
  --related-doc docs/design.md --writable-doc docs/design.md \
  --checkpoint-doc docs/工作节点.md --idempotency-key review-design-1
```

发完回到 `ai-room wait` 收回复。

## 当顾问：一次一个 outcome

按 `SKILL.md` 的信箱协议走。两条会咬人的：

- **`reply` 前必须先被 `wait` 投递过。** 从 `status` 抄 task ID 直接 reply 会被拒（`task_not_delivered`）——先跑一次 `wait` 收下它，那一刻才拍下工作树基线。
- **reply 回来 `state` 变 `blocked` 且 `guard_violations` 非空**：任务窗口内有 `writable_docs` 之外的文件变了。ai-room 只看得到"变了什么"，看不到"谁改的"——可能是你越界，也可能是主聊或用户在同一工作树里动了文件。自己判断后决定要不要重发，工具不会代为回滚。

## 压缩

阈值和流程在 `SKILL.md`。这里只强调一条：**你永远不能自己执行 `/compact`**。拿到 `COMPACT_READY` 之后，告诉用户是哪个窗口准备好了，请他手动跑。压缩完从记录的下一步入口继续，闲下来回到 `ai-room wait`。
