# 安装集成、备份与卸载

## 开发安装

- Windows，Python 3.11 或更高版本
- Git（用 `git rev-parse --show-toplevel` 确定房间根目录）

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\ai-room.exe --help
```

开发安装只影响该虚拟环境。

## 装集成：先预演，后明确批准

先用开发环境对目标操作做只读预演：

```powershell
.\.venv\Scripts\python.exe -m ai_room.install --check
```

`--check` 和 `--apply` 共用同一条安装操作路径，区别只在 `--check` 用的是只记录、不落盘的
writer。检查输出中的目标应只包括：

- `%USERPROFILE%\.codex\skills\ai-room\` 下的整个 skill 目录（`SKILL.md` + 三份 vendor 手册）
- `%USERPROFILE%\.claude\skills\ai-room\` 下的同一批文件
- `%USERPROFILE%\.claude\settings.json`
- settings 需要改变时，同目录的 `settings.json.<UTC时间>.bak`

安装源是 `integrations/ai-room/`（打包进 wheel 的副本在 `src/ai_room/resources/`，两者必须
字节一致）。目录里的每个 `.md` 都会被装走，加一份新手册不需要改安装器代码。

目标已存在且内容不同时安装器拒绝写入。本机若用 symlink 把这两个位置指到别处（例如 hub 金库），
`--check` 会明确报 `installation ancestor is not a directory`。这是有意为之：不穿过 symlink
覆盖别人管理的目录。

只有用户看过预演并明确批准后，才可单独执行真实安装：

```powershell
python -m pip install --user .
python -m ai_room.install --apply
```

不要把 `--apply` 混进日常自动测试或文档验收。安装器只合并 ai-room 的 Claude `SessionStart`
hook；遇到已有冲突文件、异常 JSON 结构或不安全目标会拒绝写入。

## skill 的四个文件

`SKILL.md` 是路由而不是全文：它讲三方共有的部分（三个角色、`ask` 两种用法、先咨询后执行、
命令形、信箱协议），然后按身份把读者分流到三份 vendor 手册。

| 读者 | 手册 | 角色与能力 |
|---|---|---|
| Claude Code | `claude-code.md` | 咨询者/决策者。主聊时咨询 codex、派 opencode 执行；也能走信箱 |
| Codex | `codex.md` | 咨询者/决策者。主聊时咨询 claude、派 opencode 执行；也能走信箱 |
| opencode | `opencode.md` | 执行者，只有 `ask`。`join`/`wait`/`send`/`reply`/`status` 一律失败 |

每份手册只写该 vendor 特有的机制：身份从哪个环境变量来、它自己的 shell 默认超时
（claude ~600s、opencode 120s、codex 10s）怎么和阻塞的 `ask`/`wait` 配合、以及它作为 `ask`
目标时的档位映射。共有的合同只在 `SKILL.md` 写一遍，手册不复述，避免四份文档互相漂移。

## 备份与回滚

升级、回滚或人工排障前，可以先复制 `%LOCALAPPDATA%/ai-room` 保存消息历史。不要在两个会话
仍运行时复制一个正在写入的数据库。安装器修改现有 Claude settings 前会在同目录创建
`settings.json.<UTC时间>.bak`。

回滚用户集成时只处理 ai-room 自己的内容：

1. 确认 `%USERPROFILE%\.codex\skills\ai-room\` 和 `%USERPROFILE%\.claude\skills\ai-room\`
   里的每个 `.md` 都还是 ai-room 安装副本（和 `integrations/ai-room/` 逐字节比对），再删除
   这两个 `ai-room` skill 目录。用户改过的文件先备份，不要覆盖。安装器只写不删，所以上一版
   留下的旧手册若已改名，会残留在目录里，回滚时一并清掉。
2. 若安装后 Claude settings 没有其他变化，可选择正确的 `settings.json.<UTC时间>.bak` 恢复为
   `settings.json`。若之后已有用户修改，则只从 `hooks.SessionStart` 删除 command 含
   `ai_room.hooks.claude_session_start` 的 ai-room group，保留所有其他设置和 hook。
3. 执行 `python -m pip uninstall ai-room` 只卸载对应 Python 包；虚拟环境开发安装则可直接
   移除该专用 `.venv`。

普通回滚或卸载不会删除 `%LOCALAPPDATA%/ai-room`，从而保留队列和历史。只有用户明确要求擦除
历史并确认已备份后，才另行删除该目录。
