# ai-room · Codex 版

先读同目录的 `SKILL.md`（两种能力、三方铁律、`ask` 命令形、信箱协议全文）。这一份只讲 Codex 特有的部分。

**你两件都能做**：既能作为可见窗口加入信箱当主聊或顾问，也能用 `ask` 派无头子 agent。

## 身份从哪来

环境变量 `CODEX_THREAD_ID`，交互式会话自带，不需要装任何 hook。读不到就是 `no current AI session detected`——多半是你在一个不属于当前会话的终端里跑命令。

上下文用量从这个 thread id 对应的 token 记录读。记录缺失或格式漂移时状态是 `unknown`：消息收发照常，但**跳过压缩时机判断**，不要用文件大小或累计 token 顶替。

## 头号坑：你的 shell 默认超时是 10 秒

三方里你最短，短一个数量级。

```
你的 shell 超时（默认 10s）  ←  连一个普通提问都撑不到
ai-room --timeout（默认 300s）
```

**每一次调用 `ai-room ask` 或 `ai-room wait` 都必须显式给 `timeout_ms`**，否则命令必然被掐死。这不是"长任务才需要注意"——`ask` 一趟咨询就要几十秒，10 秒连子 agent 启动都不够。

`ai-room wait` 是故意阻塞且静默的，被掐只是这次等待结束，**不会**离开房间、不会丢消息、不会确认掉任务；租约到期后同一条消息会重新投递，重跑 `wait` 即可。用户按 Esc 或 Ctrl+C 也是同样效果。

想边等边干活？`ask` 不会 detach。要并行就自己起一个子 agent 去做那次阻塞调用；但代理开销起步就是两三分钟，咨询同步做，只有真任务才值得代理。

## 当主聊 / 当顾问

按 `SKILL.md` 的信箱协议走，用 `ai-room join codex` 加入，**不要冒充 claude**。

```bash
ai-room send --to claude --type decision \
  --question "帧解析走状态机还是一次性切片？" \
  --related-doc src/parser.c --checkpoint-doc docs/工作节点.md
```

两条会咬人的：

- **`reply` 前必须先被 `wait` 投递过。** 从 `status` 抄 task ID 直接 reply 会被拒（`task_not_delivered`）——先跑一次 `wait` 收下它，那一刻才拍下工作树基线。
- **reply 回来 `state` 变 `blocked` 且 `guard_violations` 非空**：任务窗口内有 `writable_docs` 之外的文件变了。ai-room 只看得到"变了什么"，看不到"谁改的"——可能是你越界，也可能是主聊或用户在同一工作树里动了文件。自己判断后决定要不要重发，工具不会代为回滚。

## 用 `ask` 派活时，codex 作为目标的特殊之处

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

## 压缩

阈值和流程在 `SKILL.md`。这里只强调一条：**你永远不能自己执行 `/compact`**。拿到 `COMPACT_READY` 之后，告诉用户是哪个窗口准备好了，请他手动跑。压缩完从记录的下一步入口继续，闲下来回到 `ai-room wait`。
