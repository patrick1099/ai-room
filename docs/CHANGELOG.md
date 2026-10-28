# ai-room CHANGELOG

> 只记用户感觉得到的变化，专讲为什么。新条目插顶部，旧条目不改写。
> 本文件 2026-09-24 才建立；更早的来龙去脉看 `git log` 和 `docs/` 下各文件。

## 2026-10-28 并行派发同时结束时，台账不再丢条目

**起因**：vibe-flow 新增的头脑风暴要同时派 4 个席位（3 codex + 1 claude）。codex 审方案时读源码指出：台账文件不存在时用 `w` 打开，几个派发同时收尾会互相截断。复现测试让 12 个进程同时首写同一本台账，修之前 5 次全挂，每次丢 1–7 条。
**改成**：写台账前先拿 `.ai-room/ledger.lock` 上的跨进程排他锁（Windows 用 `msvcrt`，其他系统 `fcntl`），一律追加打开，文件为空才写表头，一条记录一次写完。持锁进程被杀时锁由系统释放，不会把台账锁死。
**没选的路**：
- 四个席位改成串行派发：一场头脑风暴从十几分钟拖到半小时以上。
- 直接用 `msvcrt.LK_LOCK`：它只每秒重试一次、十次就放弃，十来个派发同时结束就会有人报 `Resource deadlock avoided`，派发明明成功了却在写台账时崩掉。改成自己每 20 毫秒试一次，最多等 60 秒。

**影响**：不用做任何事。`.ai-room/` 里多一个空的 `ledger.lock`，已被同目录的 `.gitignore` 忽略。

## 2026-09-30 决策者默认最强模型 + 思考开满，resume 沿用上一轮档位

**起因**：用户："ai-room需要升级吧，能选模型。决策的话。至少选claude opus或者gpt astra这一档。并且思考开到max。" 以前不给 `--model` 就吃厂商 CLI 自己的默认（本机 codex 是 gpt-6-sol），思考档位完全没法指定，咨询时实际跑的是什么谁也不知道。
**改成**：
- 新增 `--effort`（low…max，codex 另有 ultra；opencode 翻成 `--variant`）。厂商不认的值在派发前就报 `E_VALIDATION`，不花钱。
- 不给 `--model` / `--effort` 时读包内 `resources/models.toml`：claude 是 `opus` + `max`，codex 是 `gpt-6-astra` + `max`，opencode 不设。toml 读不了或写坏了就拒绝派发，不悄悄退回厂商默认。
- `resume` 自动沿用该会话上一轮的模型和档位（按 session id 记在 `.ai-room/sessions/`，会话一开口就落盘，被外层掐死的也能续上同一档）；显式传参才覆盖。
- claude 加了 `--include-partial-messages`：max 档一想就是几分钟，整条消息写完前 claude 一个字都不吐，沉默预算会把正在思考的它当成卡死掐掉。
- 回执多了 `model` / `effort` 两个字段，台账多一行「思考档位」。
**没选的路**：
- 模型名写在各 driver 里：用户当场指出这是硬编码——模型名是会过期的数据（astra 没有"最新旗舰"别名），换代要改代码和测试。
- 做成硬下限，拒绝比 opus/astra 弱的模型：得维护一张"哪些型号够格"的表，每出新模型都要改；平时调用 ai-room 的是 AI，默认值本身就起到下限的作用。
- toml 出错就退回厂商默认：等于悄悄降档，正是这次要防的事。

**影响**：咨询变慢、变贵，这是要的效果。codex 在 max 档下长时间思考时会不会超过 300 秒不吐字，还没碰到过；碰到了就按 `idle` 超时的回执 resume，或者调大 `AI_ROOM_TIMEOUT`。

## 2026-09-24 派 codex 时自动用桌面版自带的新版 codex

**起因**：`ai-room ask --to codex` 连续两天派不出去。先是 "gpt-6-astra requires a newer version"；Codex 桌面版今天早上自动更新以后，又变成 "failed to load models cache: missing field `base_instructions`"。用户以为更新完就好了："现在我ai-room应该好了，你试试"。
**根因**：ai-room 按 PATH 找 `codex`。PATH 第一个是 7 月手装的 0.146.0（`%LOCALAPPDATA%\Programs\OpenAI\Codex\bin`）。桌面版把自带的新版（0.155）装在 `%LOCALAPPDATA%\OpenAI\Codex\bin\<随机名>\codex.exe`，这个目录名每次更新都会变，所以没法写进 PATH。新版一运行就把共用的模型缓存改成了新格式，旧版既调不动新模型，也读不懂这份缓存。
**改成**：派 codex 前，把 PATH 上的那份和桌面版自带的几份放在一起比版本号，用最新的那份；版本一样时用 PATH 上的。桌面版以后再更新，ai-room 自动跟上，不用改 PATH。没装桌面版时，行为和以前一样。
**没选的路**：
- 把新目录写进 PATH：桌面版一更新目录名就变，下次又坏。
- 升级 npm 全局的 codex，再调 PATH 顺序：要人记得常去手动升级，而且 PATH 里那份 7 月的旧版还会挡在前面。
- 只看文件修改时间、不比版本号：手装一个更新的 codex 时，会被桌面版的旧副本盖掉。

**影响**：不用做任何事。终端里直接敲 `codex`，拿到的仍然是 PATH 上那份旧版；那个要不要处理，另说。
