# ai-room `ask` 无头派发 + 台账 plan

> 状态：**已实现**（2026-08-04）。`ask`命令 + `drivers/`驱动层 + `.ai-room/ledger.md`台账已落地，278 个测试通过（另 3 个 `contract`真机冒烟默认跳过，用 `pytest -m contract`单独跑）。

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
| `src/ai_room/drivers/claude.py` | `claude -p --output-format json --permission-mode plan` ...（只读默认），解析 `session_id` |
| `src/ai_room/drivers/codex.py` | `codex exec --json -s read-only` ...（只读默认），解析 JSONL 的 `session_id` |
| `src/ai_room/drivers/opencode.py` | `opencode run --format json --agent plan ...` |
| `src/ai_room/drivers/registry.py` | `driver_for(name)` 按名选驱动 |
| `src/ai_room/ledger.py` | 台账追加：`append_ledger(root, entry)` 写 `.ai-room/ledger.md` |

### 改动文件
| 文件 | 改动 |
|---|---|
| `src/ai_room/cli.py` | ① 新增 `ask` 子命令（`--to/--question/--related-doc/--writable-doc/--model/--cwd/--timeout/--permission-mode/--sandbox/--no-ledger`）② main 里加独立 `ask` 分支（不依赖 join/binding，registry/store 保持 None）③ 加 `DriverError` 的 except 分支 |

### 说明
- **不改** `AgentName` 枚举（避免破坏可见会话逻辑）；`ask --to` 用独立 choices `(claude, codex, opencode)`。
- `--cwd`决定针对哪个项目派发；子 agent 实际跑在从该路径解析出的房间根目录，台账写到该根目录 `.ai-room/ledger.md`。
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
- `tests/test_drivers.py`：用真实 CLI 捕获的 fixture 验证 claude / codex / opencode 的 session_id 解析，另用 `pytest -m contract` 真机冒烟。

## 提交
- 全部改动在 `<local-repo-path>`，`git add -A` + commit。
- 账号已确认 `patrick1099`（`245735497+patrick1099@users.noreply.github.com`），当前 `main` 分支。
## 2026-08-04 review 修复（对真实 CLI 验证后）

原版三个 driver 从未对真实 CLI 跑过，测试全是自编 payload 喂自己的 parser，导致三套猜错的 schema 一路绿灯合进 main。本次修复：

1. `claude`：`--output-format json-1` 非法（实测直接 exit 1），改为 `json`；该格式返回单个对象（非数组），parser 同时接受 dict / list。
2. `codex`：真实事件是 `thread.started.thread_id` + `item.completed.agent_message`，而不是顶层 `session_id` / `result.payload.status`；保留旧分支兼容。
3. `opencode`：裸调是启 TUI，非法项目路径；补 `run --format json --agent plan`，解析 `sessionID` + `part.text`。
4. 编码：三个 driver 的 `text=True` 在 cp936 当地化下一说中文就崩；统一改用 `encoding="utf-8", errors="replace"` + `stdin=DEVNULL`（新 `drivers/process.py`）。
5. 边界：`--related-doc`/`--writable-doc` 从之前只进台账，变为通过 `compose_prompt` 真正发给子 agent；ask 复用 `capture_workspace`/`compare_workspace` 做边界护卫。
6. 退出码：失败 / 超时 / 越界均返回 3，超时也会写台账（`timeout` 状态）。
7. 测试：新增 `tests/fixtures/driver_{claude,codex,opencode}.*` 真实捕获样本，parser 测试只吃这些样本；`tests/test_process.py` 回归编码问题；`tests/test_cli_ask.py` 验证退出码 / 超时台账 / 越界；`tests/test_contract.py` 真机冒烟（默认跳过）。
8. 台账：`codex` 续接改为子命令 `codex exec resume ID`（`--resume` 是错的）；status 扩展为 ok/error/timeout/guard-blocked；问题单行化截断；首创时写 `.ai-room/.gitignore`（`*`）避免误提交。
9. 安装：`pip install -e .` 重装，使安装版也暴露 `ask`（之前 site-packages 是旧版，报 `invalid choice: ask`）。
