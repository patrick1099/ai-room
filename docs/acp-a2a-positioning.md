# ai-room 与 ACP / A2A 的关系

> 日期：2026-08-27 · 状态：**结论已定，未开工**
> 结论：不迁移架构。ACP 将来只作为 `drivers/` 下的第四种 driver 进来，A2A 不进。

## 为什么写这份

「ai-room 是不是落后于 ACP / A2A」这个问题会反复冒出来，因为这两个协议看上去和
ai-room 在做同一件事：让 AI 之间说上话。它们不在同一根轴上，但每次都要重新推一遍很贵，
所以把推理落在这里。

先拆缩写。**ACP 有两个**：

| 缩写 | 全名 | 现状 |
|---|---|---|
| ACP | Zed 的 Agent **Client** Protocol | JSON-RPC over stdio，编辑器 ↔ agent。**这个跟我们有关** |
| ACP | IBM/BeeAI 的 Agent **Communication** Protocol | REST，2025 年已并入 A2A，不必再管 |
| A2A | Google → Linux Foundation 的 Agent2Agent | HTTP/gRPC，远程 agent 互操作 |

下面说「ACP」一律指第一个。

## A2A：不相关，谈不上落后

A2A 解决的是**跨组织、跨信任边界的远程 agent 互操作**：Agent Card 发现、HTTP/gRPC 传输、
task 生命周期状态机、push notification、企业身份。一周年 150+ 组织，三大云都接了，是真标准。

它对 ai-room 零价值，因为它要解决的问题这里一个都不存在：

- **没有发现问题** —— 成员写死在 `AgentName` 枚举里，就四个。
- **没有信任边界** —— 全在本机、同一个用户、同一个工作树。
- **没有远程** —— 传输是 `subprocess`，一行启动，没有比它更便宜的了。

给这种拓扑套 A2A，买到的是 HTTP 栈 + Agent Card + 状态机，卖掉的是那一行 `subprocess`。
社区自己的建议也是「先 MCP，等边界真的成了部署约束再上 A2A」——ai-room 的边界永远不会
变成部署约束，因为它根本不部署。

**判定：不做，也不需要复审。** 除非哪天 ai-room 要跨机器派发，那时再回来看这一节。

## ACP：正对位，而且我们已经手搓了它的一半

把 ACP 的方法表和 `src/ai_room/drivers/protocol.py` 并排看：

| ACP | ai-room 现状 |
|---|---|
| `session/new` + `session/prompt` | `DriverRequest` + 每家一套 argv 拼装 |
| `session/update` 流式通知 | `session_id_in_line()` 盲扫三个 key，各家一个 `_parse_*` |
| `session/request_permission` | `PERMISSION_TIERS` 预先映射成各家 flag |
| `session/load` | `resume_session` → `-r` / `--session` |
| `fs/read_text_file`、`fs/write_text_file` | `workspace_guard` 事后 diff 兜底 |
| `session/cancel` | 外层 kill + `inflight/` 捞尸 |

厂商侧现在基本都在 ACP 这边了：Gemini CLI、Copilot CLI、Goose、Cline、OpenHands 原生支持；
Claude Code 和 Codex CLI 走官方 adapter；opencode 也在 Zed 的 External Agents 列表里。

**真实的债只有一处，而且代码自己招认了。** `drivers/opencode.py` 的 `_parse_jsonl` 文档串：

> Fragments are joined with `\n`; this is a guess from a single-part capture, so
> multi-fragment replies may carry extra newlines until the real stream shape is captured.

以及 `drivers/protocol.py` 的 `_SESSION_KEYS`——三个厂商的 key 全扫一遍，因为流可能断在半行。
这两处不是设计缺陷，是没有协议时的唯一活法。但它**按厂商数量线性增长，按厂商版本无声漂移**：
某家改了 JSONL 形状，这里不会报错，只会开始返回半截答案并且 exit 0。这是 ACP 唯一真能消掉的东西。

## ACP 给不了 ai-room 存在的理由

核过规范，两条硬缺口：

**① 协议完全不管 token 用量和费用。** 而 README 里写死的那句「最后那两行是这个工具存在的
理由」，指的正是 `total_cost_usd` / `num_turns` / `resume_command`。「超时不重复付钱」整套——
silence budget 与 wall clock 分成两道闸、session id 在子 agent 一开口就落 `inflight/`、超时
返回 `ok:false` 而不是报错——ACP 一个字都没有。因为编辑器场景里用户看得见界面，不需要给一个
无人值守的派发留后事。

**② 没有定义 headless client。** 规范原话是 client "typically code editors"。要用它，
ai-room 得自己当一个没有 UI 的 client：`session/request_permission` 要自动应答（这倒是正好
让 `PERMISSION_TIERS` 有了正经落点）、`fs/*` 和 `terminal/*` 要自己实现服务端。这几百行不比
现在三个 driver 便宜多少。

至于台账、角色纪律（顾问只读 / 工人可写，见 `compose_prompt`）、双窗口 SQLite 信箱、
context checkpoint——都是产品层策略，任何协议都不会替我们想。

**所以「落后」不成立：ai-room 不在协议赛道上，它是骑在协议上面的那一层。**

## 定下来的做法

加**第四种 driver**，和 claude / codex / opencode / dsh 平级：

- 落在 `src/ai_room/drivers/acp.py`，实现同一个 `Driver` 抽象基类。
- `DriverResult` 契约一行不改。ACP 拿不到的 cost 字段留 `None`——现有类型已经是
  `float | None`，天然容得下。
- `service.py` / `storage.py` / `ledger.py` / 信箱模式**完全不碰**。

**开工门槛：哪家的原生 headless 最难用，才让哪家走 ACP。**

按此前 dsh 桥接调研的结论，dsh 官方 headless 有三处硬缺口、要自己写插件。
**如果 dsh 已经支持 ACP，那个自定义插件的活可以直接作废**——这是目前唯一一处 ACP 能立刻省掉
真金白银的地方，动手前先去核这一条。

claude / codex / opencode 三家现有 driver 已经跑通且能拿到 cost，走 ACP 反而丢字段。**不换。**

## 参考

- [Agent Client Protocol — Protocol Overview](https://agentclientprotocol.com/protocol/overview)
- [Agent Client Protocol — Agents](https://agentclientprotocol.com/get-started/agents)
- [Zed — External Agents](https://zed.dev/docs/ai/external-agents)
- [Zed Blog — The ACP Registry is Live](https://zed.dev/blog/acp-registry)
- [Linux Foundation — A2A 一周年，150+ 组织](https://www.linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year)
- [Google A2A Protocol in 2026: Adoption, Hype, and Reality](https://www.glukhov.org/ai-systems/comparisons/a2a-protocol-2026-adoption/)
