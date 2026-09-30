# Muse：修正浏览器会话导入与诊断转发器寿命

2026-09-30。用户要求继续修复领取，Codex 已通过 Muse 对话直接协作。
本轮没有新领取尝试，也不修改原 `journal.json` 和 `web-query-claim-v1.json`。

## 本轮新增的具体证据

Codex 本地核对确认，旧 `check_campaign_auth.py` 虽读取 CookieJar 验证 WEB token，
但在浏览器导航前只注入 `auth-token`，没有带入 `unique_id` 等原会话 Cookie。
这与 Python 观看实验使用已有 CookieJar 的方式不同。

Muse 只读核对实际使用的 `/tmp/fwd_final.py`，报告该转发器有 25 秒总寿命，
随后关闭全部连接；已有另一个 `proxy_forward.py` 并非那两轮探针实际用的转发器。
这限制了较长的浏览器验证，也无法用于观察自然续期。

**以上是适配器缺陷，不是已证明的 integrity 根因。** `a623b25` 的 SDK 域 429
约在 5.8 秒出现，早于 25 秒。不能把它归因于这条寿命限制，也不能推断设备 Cookie
一定导致先前拒绝。旧报告没有原始 token 配对资料，无法事后补齐。

## 代码修正

- 新的 `browser_cookie_import.py` 从指定的可信本地 CookieJar 只读转换适用的 Twitch
  Cookie。保留可恢复的范围、标志和绝对过期信息，不导入其他网站的 Cookie。
  对旧格式缺失的范围信息采取收窄，不把已过期或期限无法恢复的 Cookie 延寿。
- 导航前完成 Cookie 注入及浏览器 Cookie 存储回读，分别验证登录值和已有设备值。
  只在报告中输出计数、`browser_auth_matches`、`browser_device_matches` 等布尔值，
  不保存或打印 Cookie 值。
- 被动监听的签发请求和 dashboard 请求增加 `device_cookie_matches`。没有原设备值
  或无法读取请求头时保持未知；已知不匹配时不执行 Python 对照。
- 保留现有原生页面、浏览器启动配置、SDK 和证书校验。没有改变 hash、身份类型、
  网页功能开关或 429 停止规则。

Muse 另行准备转发器生命周期修正：保留旧文件，新的诊断父任务持有监听和连接，
只绑定本机临时端口，启动时读取当前上游代理配置，在任务结束时清理。
它仍只是诊断适配器，不能写成平台认可的长期出口。先用本地假上游验证双向转发、
超过 25 秒仍可工作，以及退出后的清理；通过之前不访问 Twitch。

## 验证顺序与成功边界

1. 完成本地 Cookie 转换、导航顺序、设备比对、脱敏和 429 回归测试。
2. Codex 核对 Muse 的转发器修正及本地测试后，再给出一次有界的只读实测指令。
   不复跑旧领取入口，不自动添加 cron，不让旧实验命令充当本轮操作指令。
3. 原成功标准保持不变：官网 dashboard 同用户、列表非 null、无错误或 challenge，
   再验证同身份的 Python 对照。导入成功或 HTTP 200 token 不算通过。
4. 任何相关 429 仍立即停止；同样失败不重复运行，不关闭校验或调整 SDK。
5. 即使这一步通过，也只证明会话／读取链路。本目标领取必须另行保留尝试记录，
   以同一目标提交之后的 Inventory `isClaimed:true` 验收；续期与全天候仍需实测。

本轮改动是恢复领取所需会话的候选修正，尚不能宣称 claim 已修复。

## Muse 离线接入结果

Muse 已安全快进至 `e3ac929`，并报告同组离线测试通过。对真实 CookieJar 的只读
导入暴露了加载器过度限制：普通 `server_session_id` 在 `.twitch.tv/` 与
`m.twitch.tv/` 下有不同值，原实现把合法的不同域 Cookie 拒绝为歧义。
修正为保留各自的原域／路径；完全相同范围的冲突仍拒绝，适用于官网页面的
`auth-token` 和 `unique_id` 仍必须唯一。不能通过修改源 Cookie 来迁就加载器。

Muse 另报告新的 `diag_forward.py` 完成 14 项本地假上游检查，父任务
`diag_probe_parent.py` 完成 9 项认证转发／超时清理检查。Codex 已读到实际转发代码，
并进一步核对父任务对浏览器子进程的清理和整体期限。这些检查均未发 Twitch 请求。
源文件、报告和两份领取日志保留；尚没有本轮服务器结果。
