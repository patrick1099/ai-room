# ai-room · Codex 版

先读同目录的 `SKILL.md`（默认模式判定、三方铁律、`ask` 命令形、信箱协议全文）。这一份只讲 Codex 特有的部分。

**默认用 `ask`。** 需要另一个 AI 的意见或想派活时，直接跑一条 `ai-room ask`，**不要反问用户要不要开第二个窗口**。信箱那套（`join`/`wait`/`send`/`reply`）你也能用，但只在用户点名要对话模式、或两个窗口已经在跑时才走。

## 头号坑：你的 shell 默认超时是 10 秒

三方里你最短，短一个数量级。

```
你的 shell 超时（默认 10s）  ←  连一个普通提问都撑不到
ai-room --timeout（默认 300s）
```

**每一次调用 `ai-room ask` 都必须显式给 `timeout_ms`**，否则命令必然被掐死。这不是"长任务才需要注意"——`ask` 一趟咨询就要几十秒，10 秒连子 agent 启动都不够。信箱模式下的 `ai-room wait` 同理。

## 默认路径：直接派

```bash
# 只读咨询：要另一个模型的独立判断（记得给 timeout_ms）
ai-room ask --to claude --question "帧解析走状态机还是一次性切片？各自的代价是什么？" \
  --related-doc src/parser.c --cwd /path/to/project

# 派真活：允许它改文件、跑测试
ai-room ask --to claude --question "给 parse_frame 补边界单测并跑通" \
  --permission workspace-write --cwd /path/to/project --timeout 600
```

返回的一行 JSON 里看 `ok`（厂商自己的成败判定）、`text`（回答）、`changed_files`（回执，不是沙箱）、`session_id` / `ledger`（续接用）。

想边等边干活？`ask` 不会 detach。要并行就自己起一个子 agent 去做那次阻塞调用；但代理开销起步就是两三分钟，咨询同步做，只有真任务才值得代理。

## codex 作为 `ask` 目标时的特殊之处

档位不是只由 `-s` 决定的，两个轴必须一起设，而且**一律不继承本机 `config.toml`**：

| `--permission` | 实际参数 |
|---|---|
| `read-only`（默认） | `-s read-only` + `-c approval_policy="never"` |
| `workspace-write` | `-s workspace-write` + `-c approval_policy="on-failure"` + `-c approvals_reviewer=auto_review` |
| `full-access` | `-s danger-full-access` + `-c approval_policy="never"` |

**注意 `workspace-write` 在 codex 上比字面更宽**：它允许沙箱的一次拒绝被升级成沙箱外执行。这不是疏忽，是两边都实测过的取舍——

- `-s read-only` 配上开着 approvals 的机器，照样把两个"不该写"的文件写了。**approvals 开着时 read-only 不构成边界。**
- `-s workspace-write` 配 `approval_policy=never`，产出的文件调用者根本读不了：沙箱写入失败后回退，文件归了沙箱主体，exit 0、回执列出它、谁也打不开。允许升级才能让写入落成正常文件。

用 `on-failure` 而不是 `on-request`：只在沙箱**真的拒绝**之后升级，不是模型一开口就升级。

其他：工作目录用 `-C` 钉死；目标目录不是 git 仓库时自动加 `--skip-git-repo-check`；session id 要等它开口才存在（被掐死时从已吐出的输出里抢救，抢不到就没有），续接用 `codex exec resume ID`。

## 身份从哪来

环境变量 `CODEX_THREAD_ID`，交互式会话自带，不需要装任何 hook。读不到就是 `no current AI session detected`——多半是你在一个不属于当前会话的终端里跑命令。

`ask` 不需要身份（探测不到就把 `sender` 记成 `null`），**信箱那套才需要**。上下文用量从这个 thread id 对应的 token 记录读：记录缺失或格式漂移时状态是 `unknown`，消息收发照常，但**跳过压缩时机判断**，不要用文件大小或累计 token 顶替。

---

以下是**非默认模式**，只在用户点名要两个窗口对话时才用。

## 当主聊 / 当顾问

按 `SKILL.md` 的信箱协议走，用 `ai-room join codex` 加入，**不要冒充 claude**。

```bash
ai-room send --to claude --type decision \
  --question "帧解析走状态机还是一次性切片？" \
  --related-doc src/parser.c --checkpoint-doc docs/工作节点.md
```

`ai-room wait` 是故意阻塞且静默的，被掐只是这次等待结束，**不会**离开房间、不会丢消息、不会确认掉任务；租约到期后同一条消息会重新投递，重跑 `wait` 即可。用户按 Esc 或 Ctrl+C 也是同样效果。

两条会咬人的：

- **`reply` 前必须先被 `wait` 投递过。** 从 `status` 抄 task ID 直接 reply 会被拒（`task_not_delivered`）——先跑一次 `wait` 收下它，那一刻才拍下工作树基线。
- **reply 回来 `state` 变 `blocked` 且 `guard_violations` 非空**：任务窗口内有 `writable_docs` 之外的文件变了。ai-room 只看得到"变了什么"，看不到"谁改的"——可能是你越界，也可能是主聊或用户在同一工作树里动了文件。自己判断后决定要不要重发，工具不会代为回滚。

## 压缩

阈值和流程在 `SKILL.md`。这里只强调一条：**你永远不能自己执行 `/compact`**。拿到 `COMPACT_READY` 之后，告诉用户是哪个窗口准备好了，请他手动跑。压缩完从记录的下一步入口继续，闲下来回到 `ai-room wait`。
