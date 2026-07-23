# ai-room 真实双窗口验收清单

状态：真实双窗口验收尚未执行。本文所有项目必须由人在一个可见 Codex 交互式窗口和一个可见
Claude Code 交互式窗口中完成；自动测试不得代勾，证据不得包含秘密、完整 transcript、用户主目录
绝对路径或私有项目内容。

## 验收记录

- 日期：
- Codex 版本：
- Claude Code 版本：
- ai-room 版本/commit：
- 工作树根目录（可脱敏）：
- 测试人：
- 证据存放位置（可脱敏）：

开始前确认已获得用户对真实用户级安装和 `--apply` 的明确批准，并已完成 README 的 `--check`
预演。若没有批准，停止在这里，保持全部复选框未勾选。

## 双向角色与基本往返

- [ ] Codex 主聊、Claude 顾问：Codex 发送 decision，Claude 的阻塞 wait 被唤醒并返回同一
  task ID 和中文问题；Claude 回复后 Codex 收到同一 task ID。
  - 结果：[ ] Pass [ ] Fail
  - 证据/备注：
- [ ] Claude 主聊、Codex 顾问：反向重复一次，确认不需重配固定主从角色。
  - 结果：[ ] Pass [ ] Fail
  - 证据/备注：
- [ ] 顾问对一次 decision 给出且只给出一个 `DONE` 或 `BLOCKED` 终态，主聊收到决定回复，
  双方随后重新进入 `ai-room wait`。
  - 结果：[ ] Pass [ ] Fail
  - 证据/备注：

## 等待、中断与恢复

- [ ] 在阻塞的 `ai-room wait` 中按 Esc；若客户端不传递 Esc，则按 Ctrl+C。确认只结束当前
  Shell 调用，房间成员和未确认消息仍保留。
  - 使用按键：
  - 结果：[ ] Pass [ ] Fail
  - 证据/备注：
- [ ] 中断后重新执行 `ai-room wait`，确认状态重新变为 waiting，并能收到后续任务。
  - 结果：[ ] Pass [ ] Fail
  - 证据/备注：
- [ ] 让 wait 取得一条消息但不 reply，结束该进程/窗口并重启同一会话；租约到期后确认同一
  message ID/task ID 再次投递，随后完成回复。
  - 结果：[ ] Pass [ ] Fail
  - 证据/备注：

## 顾问文件与执行边界

- [ ] 主聊发起文档审查，只把一个精确文档加入 `--writable-doc`；顾问仅修改该文件且 reply
  成功。记录精确相对路径。
  - writable doc：
  - 结果：[ ] Pass [ ] Fail
  - 证据/备注：
- [ ] 新发起一项任务，在顾问取得任务后尝试改变一个源码文件；确认 reply 被
  `workspace_guard_violation` 阻止、列出源码路径，且工具没有自动恢复或删除该文件。
  - 测试后由主聊/用户恢复的源码路径：
  - 结果：[ ] Pass [ ] Fail
  - 证据/备注：
- [ ] 确认顾问没有运行测试、构建、部署或其他真实操作，也没有修改未精确列入
  `writable_docs` 的文件。
  - 结果：[ ] Pass [ ] Fail
  - 证据/备注：

## 队列与工作树隔离

- [ ] 同时发送两个任务，确认先提交的任务为 working、另一个为 queued；第一项终态 reply
  后第二项才按 FIFO 投递，无死锁、丢失或交叉回复。
  - 第一/第二 task ID（可缩写）：
  - 结果：[ ] Pass [ ] Fail
  - 证据/备注：
- [ ] 在两个不同工作树根目录分别 join；只向其中一个发送任务，确认另一个房间的 status 和
  wait 不出现该任务。
  - 结果：[ ] Pass [ ] Fail
  - 证据/备注：

## 上下文 checkpoint 与手动压缩

- [ ] 使用脱敏的 156k 当前会话记录触发 context check。checkpoint 尚缺决定、改动、验证结果、
  未解决问题或下一步入口时，顾问回复 `CHECKPOINT_NEEDED`，不得回复 `COMPACT_READY`。
  - 缺少的记录：
  - 结果：[ ] Pass [ ] Fail
  - 证据/备注：
- [ ] 补齐精确 checkpoint 文档，写明安全暂停点和下一步恢复入口，再执行
  `ai-room wait --checkpoint EXACT_PATH --next-entry TEXT`；顾问复查后回复
  `COMPACT_READY`，主聊收到包含工具、token、记录文档和恢复入口的提示。
  - checkpoint：
  - next entry：
  - 结果：[ ] Pass [ ] Fail
  - 证据/备注：
- [ ] 确认 ai-room 和两个 AI 都只建议用户 **手动** 执行 `/compact`，没有自动执行该命令；
  本项验收也不要求实际压缩。
  - 结果：[ ] Pass [ ] Fail
  - 证据/备注：

## 最终结论

- [ ] 上述每一项都有 Pass/Fail 和证据；不存在空白结果。
- [ ] 所有 Fail 已转换为聚焦的自动回归测试或明确阻断项，并重新验证受影响案例。
- [ ] 自动测试和真实双窗口验收都通过，才允许把设计状态从
  `implemented, awaiting dual-window acceptance` 改为 `accepted`。

最终结果：[ ] Pass [ ] Fail

测试人签字/日期：

备注：
