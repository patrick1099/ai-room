# ai-room · Claude Code 版

先读同目录的 `SKILL.md`（默认模式判定、三方铁律、`ask` 命令形、信箱协议全文）。这一份只讲 Claude Code 特有的部分。

**默认用 `ask`。** 需要另一个 AI 的意见或想派活时，直接跑一条 `ai-room ask`，**不要反问用户要不要开第二个窗口**。信箱那套（`join`/`wait`/`send`/`reply`）你也能用，但只在用户点名要对话模式、或两个窗口已经在跑时才走。

## 默认路径：直接派

```bash
# 只读咨询：要另一个模型的独立判断
ai-room ask --to codex --question "这个状态机在 wait 被中断后的恢复路径有没有空窗？" \
  --related-doc src/service.py --cwd /path/to/project

# 派真活：允许它改文件、跑测试
ai-room ask --to codex --question "给 parse_frame 补边界单测并跑通" \
  --permission workspace-write --cwd /path/to/project --timeout 600
```

返回的一行 JSON 里看 `ok`（厂商自己的成败判定）、`text`（回答）、`changed_files`（回执，不是沙箱）、`session_id` / `ledger`（续接用）。

**派给 claude 时有一项别人没有的待遇**：session id 是**派发前预分配**的（一个 uuid，用 `--session-id` 传下去）。哪怕子 agent 一个字没吐出来就被掐死，台账里那个 handle 照样能 `claude -r ID` 续上。codex 和 opencode 的 id 要等它开口才存在。

档位映射：

| `--permission` | 实际参数 |
|---|---|
| `read-only`（默认） | `--permission-mode plan` |
| `workspace-write` | `--permission-mode acceptEdits` + `--allowedTools Edit,Write` |
| `full-access` | `--dangerously-skip-permissions` |

`--permission-mode` 可以手动覆盖前两档。工作目录用 `--add-dir` 钉死。

## 你的 Bash 工具超时：默认 ~600s，上限 600000ms

- **`ai-room ask`** 同步阻塞。默认 `--timeout 300` 在你 600s 的窗口内是安全的；派真任务用更大的 `--timeout` 时，**记得同时把 Bash 调用的 timeout 抬上去**，两道闸取小者生效。
- **`ai-room wait`**（只在信箱模式下用）是故意阻塞且静默的。在 Bash 工具里跑就把 `timeout` 拉到上限，否则十分钟后工具报超时——那只是这次等待结束，**不会**离开房间、不会丢消息、不会确认掉任务。重跑 `wait` 即可，租约到期后同一条消息会重新投递。用户按 Esc 或 Ctrl+C 也是同样的效果。

想边等边干活？`ask` 不会 detach。要并行就自己起一个 subagent，让**它**去做那次阻塞调用。但代理不是免费的——一趟琐碎任务光开销就两三分钟，所以咨询同步做，只有真任务才值得代理。

## 身份从哪来

`SessionStart` hook 在会话启动时注入两个环境变量：

- `AI_ROOM_CLAUDE_SESSION_ID`
- `AI_ROOM_CLAUDE_TRANSCRIPT_PATH`

两个必须成对存在。只有一个 → `incomplete Claude session environment`。两个都没有 → `no current AI session detected`，去查 `~/.claude/settings.json` 里 `hooks.SessionStart` 有没有那条 `ai_room.hooks.claude_session_start`（matcher 应含 `startup|resume|clear|compact`）。

`ask` 不需要身份（探测不到就把 `sender` 记成 `null`），**信箱那套才需要**。上下文用量也是从这个 transcript 里读 usage 的：文件缺失、读不了或格式漂移时状态明确是 `unknown`，消息收发照常，但**跳过压缩时机判断**，不要用文件大小或累计 token 顶替。

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

按 `SKILL.md` 的信箱协议走。特别注意两条会咬人的：

- **`reply` 前必须先被 `wait` 投递过。** 从 `status` 抄 task ID 直接 reply 会被拒（`task_not_delivered`）——先跑一次 `wait` 收下它，那一刻才拍下工作树基线。
- **reply 回来 `state` 变 `blocked` 且 `guard_violations` 非空**：任务窗口内有 `writable_docs` 之外的文件变了。ai-room 只看得到"变了什么"，看不到"谁改的"——可能是你越界，也可能是主聊或用户在同一工作树里动了文件。自己判断后决定要不要重发，工具不会代为回滚。

## 压缩

阈值和流程在 `SKILL.md`。这里只强调一条：**你永远不能自己执行 `/compact`**。拿到 `COMPACT_READY` 之后，告诉用户是哪个窗口准备好了，请他手动跑。压缩完从记录的下一步入口继续，闲下来回到 `ai-room wait`。
