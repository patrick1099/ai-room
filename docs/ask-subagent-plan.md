# ai-room `ask` 无头派发 + 台账 plan

> 状态：**已实现**（2026-08-04）。`ask` 命令 + `drivers/` 驱动层 + `.ai-room/ledger.md` 台账已落地，244 个测试通过。

## 目标

新增 `ai-room ask` 命令，把任务无头派发给其它厂商 CLI 子 agent（claude / codex / opencode），
拿到子 agent 的 **session id**，并自动追加到项目根目录 `.ai-room/ledger.md` 台账，
供 `-r/--resume` 续接该次会话。

背景：现有六大命令（join / wait / send / reply / status / leave）都依赖**可见交互会话**。
`ask` 把"同伴"从可见会话解耦成无头子 agent，天然支持多厂商，且能记录可续接的会话 id。

## 改动范围（仓库 `<local-repo-path>`，local 账号已确认 `patrick1099`）

### 新增文件
| 文件 | 内容 |
|---|---|
| `src/ai_room/drivers/__init__.py` | 导出协议 / 结果 / 注册表 |
| `src/ai_room/drivers/protocol.py` | `Driver` ABC + `DriverResult` + `DriverError` |
| `src/ai_room/drivers/claude.py` | `claude -p --output-format json-1 --permission-mode plan ...`，解析 `session_id` |
| `src/ai_room/drivers/codex.py` | `codex exec --json -s read-only ...`，解析 JSONL 的 `session_id` |
| `src/ai_room/drivers/opencode.py` | `opencode run ...`（v1 纯文本，session_id=None） |
| `src/ai_room/drivers/registry.py` | `driver_for(name)` 按名选驱动 |
| `src/ai_room/ledger.py` | 台账追加：`append_ledger(root, entry)` 写 `.ai-room/ledger.md` |

### 改动文件
| 文件 | 改动 |
|---|---|
| `src/ai_room/cli.py` | ① 新增 `ask` 子命令（`--to/--question/--related-doc/--writable-doc/--model/--cwd/--timeout/--permission-mode/--sandbox/--no-ledger`）② main 里加独立 `ask` 分支（不依赖 join/binding，registry/store 保持 None）③ 加 `DriverError` 的 except 分支 |

### 说明
- **不改** `AgentName` 枚举（避免破坏可见会话逻辑）；`ask --to` 用独立 choices `(claude, codex, opencode)`。
- `--cwd` 指定子 agent 工作目录，台账随之写到该目录 `.ai-room/ledger.md`（解决"打开工作区却聊另一个项目"）。
- 台账格式：每条一个区块（时间 / 状态 / 模型 / 问题 / 相关文档 / 子 agent session id / 续接命令）。

## 台账样例 `.ai-room/ledger.md`

```markdown
# ai-room 派发台账
> 自动生成，勿手改。每次 `ai-room ask` 无头派发后追加一条。

### 2026-08-04T14:00:00+08:00 — claude [`ok`]
- 状态: ok (exit 0)
- 模型: (default)
- 问题: 审查 OTA 方案在 X 上的风险
- 相关文档: Code/App/Code/app/Protocol/protocol_IoT.c
- 子 agent session id: `abc123`
- 续接: `claude -r abc123`
```

## 测试
- `tests/test_ledger.py`：首次创建 / 追加 / 续接提示
- `tests/test_drivers.py`：用 mock 的 `subprocess.run` 验证 claude / codex 的 session_id 解析（不真调 CLI）

## 提交
- 全部改动在 `<local-repo-path>`，`git add -A` + commit。
- 账号已确认 `patrick1099`（`245735497+patrick1099@users.noreply.github.com`），当前 `main` 分支。