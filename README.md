# ai-room

让正在干活的那个 AI 自己把活派出去，或者把方案交给另一个 AI 审一遍，不用你开第二个窗口、
也不用你在两边复制粘贴上下文。

Claude Code / Codex / opencode 三方互通，本机运行，Windows 优先，Python 3.11+。

## 先看它干什么

主聊 AI 判断需要外援时，直接派一个无头子 agent，不需要窗口、不进信箱、跑完即返回：

```powershell
# ① 咨询决策者：交上去的是自己的方案,只读
ai-room ask --to codex --related-doc src/protocol/parser.c `
  --question "项目是<X>,要解决<Y>。我的方案是<方案>,不确定的是<疑点>。请判断是否成立、哪里必须改。"

# ② 派 opencode 执行：交上去的是一件拆好的任务,可写
ai-room ask --to opencode --permission workspace-write --related-doc src/protocol/parser.c `
  --question "方案已定为<定稿>(已过评审)。这一件任务:给 parse_frame 补边界单测并跑通。完成标准:<可验证标准>。"
```

跑完在项目根目录 `.ai-room/ledger.md` 追加一条台账：

```markdown
### 2026-08-12T08:59:40+08:00 - claude [`ok`]
- 状态: ok (exit 0)
- 问题: Continue the task from this conversation where you stopped...
- 子 agent session id: `2fb3d273-ea15-4884-b23c-1f46aaf675c0`
- 续接: `claude -r 2fb3d273-ea15-4884-b23c-1f46aaf675c0`
- 费用: $0.577442 USD, 4 turn(s)
```

（真实台账，截去了 usage 明细。）最后那两行是这个工具存在的理由，见下面「超时不重复付钱」。

## 安装

```powershell
python -m pip install --user .
python -m ai_room.install --check     # 先只读预演,看它要动哪些文件
python -m ai_room.install --apply     # 看过预演再执行
```

`--check` 和 `--apply` 走同一条写路径，区别只在前者用不落盘的 writer。装的是三方各自的
skill 目录和一个 Claude `SessionStart` hook，细节与回滚见
[安装与卸载](docs/install-and-rollback.md)。

## 三个角色，不许串岗

| 角色 | 是谁 | 干什么 |
|---|---|---|
| 主聊 | 此刻正在服务用户的那一个 | 出方案、拆任务、收口 |
| 决策者 | 对面那个 peer：claude ↔ codex | 审方案、指出哪里要改、拍技术取舍。保持 `read-only`，不动手 |
| 执行者 | 只有 opencode | 接一件拆好的任务去做。不问它决策，不让它定方案 |

给决策者写权限就是角色串岗，所以 `--permission` 默认 `read-only`，只有派 opencode 干活才
用 `workspace-write`。派给 opencode 的是有手的工人，顾问那套「只改指定文档、不跑测试构建」
的约束在这条路上不成立。

**opencode 是廉价劳力，机械活随便派**：批量改名、跑格式化、加日志、补样板代码、跑测试并摘出
失败、改错别字。这类活没有方案可审，直接派。

有技术取舍时才走「先咨询，后执行」，而且门槛是方案级的一次，不是每件任务都重审。判定只有
一句：这件事有没有技术取舍？没有就直接派。

两种用法都必须把场景上下文说全，子 agent 是零上下文的，它不知道你在做什么项目。

## 超时不重复付钱

厂商按整轮计费，所以超时那一轮的钱已经花掉了，重发同一条 `ask` 等于同一份钱付两次。因此
超时不是报错，而是一个带把手的结果：`ok:false` + `status:"timeout"`，并带上 `session_id`、
已产出的 `text` 和可直接跑的 `resume_command`。

```powershell
ai-room resume --to opencode --session ses_xxx --permission workspace-write --cwd .
```

不给 `--session` 就续接本房间最近一个被外层掐死的派发。子 agent 一开口，session id 就落盘到
`.ai-room/inflight/`，所以哪怕整个 ai-room 进程被上层 shell 杀掉（那种情况连台账都来不及写），
把手也还在。正常报告结束的派发会清掉这条记录，留下的就只有真正的孤儿。

配套的两道闸也是为这件事设的：`--timeout` 是「允许沉默多久」，子 agent 每吐一行就把死线往后
推，还在输出的不会被掐；`--max-runtime` 才是墙钟硬顶。分开是因为「卡住了」和「停不下来」是
两种故障，用一个墙钟同时管，会把正在收敛的那一轮误杀。出厂默认 300s / 3600s，可由
`AI_ROOM_TIMEOUT` / `AI_ROOM_MAX_RUNTIME` 覆盖。

> 硬顶必须小于外层 shell 闸，否则先响的是外层，那种死法拿不到任何回执，只能盲跑 `resume`。
> 用配套脚本 `ai-timeouts set <分钟>` 一次改到位，别手动只动其中一个。

## 命令

| 命令 | 干什么 |
|---|---|
| `ask` | 无头派发一个子 agent（默认路径） |
| `resume` | 续接被掐断的那一轮 |
| `status` | 看成员和当前任务 |
| `join` / `wait` / `send` / `reply` / `leave` | 双窗口信箱模式，见下 |

`--cwd` 决定针对哪个项目派发，并用各厂商的原生参数钉死（claude `--add-dir`、codex `-C`、
opencode `--dir`），不靠进程 cwd。opencode 尤其必须给：它完全无视进程 cwd，不给就会跑去改
它自己记着的上一个项目，且照样 exit 0。

退出码 0 表示厂商自己说这一轮成功，失败和超时退 3。子 agent 改了哪些文件作为回执返回，只供
复核，不影响退出码。回执有两条硬限制，别把它当沙箱：**看不到 gitignore 里的东西**（回执是
`git status --porcelain` 的差集），**救不了跑错目录**（子 agent 整个跑在别的项目里时，这里
一片干净）。边界只能靠工作目录参数钉死。

## 双窗口信箱模式

一个可见的 Codex 会话和一个可见的 Claude Code 会话通过 SQLite 房间互当顾问，opencode 参加
不了这条。只想问一句话或派件活不需要这套，直接用 `ask`。

```powershell
# 分别在两个窗口,同一个工作树根目录
ai-room join codex   ;  ai-room wait
ai-room join claude  ;  ai-room wait
```

`wait` 正常时保持静默并阻塞，需要交互时按 `Esc`（客户端没传 Esc 就按 `Ctrl+C`）。中断只结束
本次等待，不会离开房间或删除任务。谁当前服务用户谁就是主聊，另一边是本次顾问，下一项任务可
反向发送。每个会话必须用自己的身份加入。

顾问只负责技术决策和需求、设计、计划文档的审查或修改，可以按需只读源码，但不能修改源码，
也不能替主聊 AI 运行测试、构建、部署。`--writable-doc` 是顾问唯一可修改的精确文件，且只有
`requirements-review` / `design-review` / `plan-review` 三类允许携带它。

这条路当前状态是 **awaiting dual-window acceptance**，真实双窗口验收尚未执行。

## 其余文档

- [排错、并发与上下文](docs/troubleshooting.md)
- [安装集成、备份与卸载](docs/install-and-rollback.md)
- [需求清单](docs/NEEDS.md) · [设计文档](docs/superpowers/specs/2026-07-23-ai-room-design.md)
- [真实双窗口验收清单](docs/acceptance/dual-window.md)

自动验证通过不等于真实可用。只有用户在一个真实 Codex 窗口和一个真实 Claude Code 窗口完成
全部未勾选清单后，设计状态才可以改为 `accepted`。
