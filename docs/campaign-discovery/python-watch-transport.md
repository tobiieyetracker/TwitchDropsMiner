# 单频道纯 Python 验证：实现事实与下一次对照

记录日期：2026-09-30。本轮补充源码和官网观察证据，并修正 AvailableDrops 的 null／缺失结果处理。
未修改浏览器探针或 Muse 的 `manual_watch.py`，未在 Muse 上执行观看实验。
Linux 上的活动发现、真实进度、领取和全天候恢复仍未通过验证。

## 实际观看实现

- `channel.py` 的 `Channel.send_watch()` 使用 Python HTTP：从频道 HTML／settings JS 提取 `spade_url`，POST `Stream.spade_payload` 中的 `minute-watched` 事件。
- `twitch.py` 的 `_watch_loop()` 按 `WATCH_INTERVAL`（当前 59 秒）调用它。这个发送方法没有调用浏览器或 WebSocket，也不主动取得 integrity token。
- WebSocket 用于接收 `drop-progress`、`drop-claim` 和频道状态消息；领取由 `inventory.py` 的 GQL `ClaimDrop` 实现发出。
- `_send_watch_playlist()` 和 `_send_watch_gql()` 标为 unused，不是当前实际观看路径。
- `send_watch()` 返回 True 只说明 HTTP 204，不能证明 Twitch 认可事件或增加掉宝进度。当前 Muse 环境尚未实际验证这一点。
- 完整 `_watch_loop()` 在进度缺报时可能调用 `bump_minutes()` 估算本地分钟数；验证必须读服务器 `CurrentDrop`／Inventory，不能使用 UI 倒计时或估算值判定成功。

因此，“观看必须浏览器或 miner WebSocket”不符合当前代码。正确状态是：存在纯 Python 发送实现，其当前服务端效果未知。

## 两个空结果能说明什么

Muse 报告 `oilrats247`、`streamerhouse` 在直播，AvailableDrops 均为 0，CurrentDrop 无会话，Inventory 无进行中活动。
这说明本轮没有获得可验证目标，不能据此证明其他频道无活动或纯 Python 观看不可行。

复核时必须记录原始语义：

- `data.channel.viewerDropCampaigns` 是真正的 `[]`、`null`、缺字段，还是带 GQL errors；HTTP 200 本身不足以区分。
- 旧 `Channel.get_stream()` 和批量频道检查中的 `viewerDropCampaigns or []` 会把 null 转空，不能用转换后的计数替代原始结果。
- `_check_drops_enabled()` 还要求活动已存在于 `_twitch._campaigns` 且可获取进度；全局缓存为空时，False 不能证明频道没有活动。
- 关闭 `available_drops_check` 会改变默认 `drops_enabled`，不能据此确认资格。

这些是源码中的判断风险，尚无证据表明 Muse 的两次原始响应具体经过了哪种转换。

本提交使 AvailableDrops 的字段 null、channel null 和结构变化分别报错，真正的 `[]` 仍有效；
单频道调用不再吞掉这些异常或明确的 integrity challenge，批量检查不再将缺失频道结果默认成空列表。
静默 null 不会触发 integrity 刷新，也不用于推断错误原因。GetStreamInfo 的离线／无用户早退保持不变。
这些改动避免错误的“无活动”结论，本身不提供新的活动发现来源。
新增 27 项离线回归测试通过，全套 155 项通过；覆盖真实 `Twitch.gql_request()` 的模拟传输，
以及单频道和批量调用、null／空数组区别、异常传播与不误触发刷新。另有 2 项登录回退测试，
确认 Twitch 不签发 device code 时，点击 Login 会切到 Chrome WEB 身份并保留 `cookies.jar`。
这些都不是 Twitch 实服验证。

## 官网观察到的候选对照

2026-09-30 约 18:38（UTC+08:00），当前正常授权的内置浏览器在 Twitch 官方活动页及直播目录观察到：

- Rust 的 `Isles AR` 条目限定 `hJune`／`Hutnik`；`SAR` 条目限定 `DisguisedToast`，各要求观看 1 小时。
- 页面显示结束时间为 2026-10-05 07:58（UTC+08:00）。
- 当时 `hJune` 与 `DisguisedToast` 均正在直播 Rust，标题也提到掉宝。

来源是 [Twitch 官方活动页](https://www.twitch.tv/drops/campaigns) 和当时的官方直播目录。它们提供了参与频道和直播状态的候选正对照；状态会变化，必须在 Muse 运行时重新确认。
此浏览器并非已确认与 Muse 相同的登录会话，不能移植其账号关联、资格或进度结论。

## 下一次有限验证

1. 保留 Cookie、报告和原脚本。主候选使用 `hJune`；仅在其离线或取得真正空列表且没有错误时，检查一次备用 `DisguisedToast`。不扩大为频道扫描。
2. 使用 Muse 现有匹配的 WEB 身份，复核直播游戏、AvailableDrops 原始字段、实际活动 ID、有效时间、频道限制及账号关联资格。必要时根据返回的真实活动 ID 查询现有 CampaignDetails；不得合成关联状态、活动时间或频道 ACL。遇 integrity challenge、429 或其他无法确认身份／资格的错误就停止，不切换身份或继续备用查询。
3. 只有资格明确后才进行一次总时长不超过 10 分钟的观看实验，单独建立受限适配脚本，复用 miner 现有 `Channel.send_watch()`；维持原 59 秒间隔、最多 10 次发送。不启动依赖全量 dashboard 的 `fetch_inventory()`，也不启动会本地补分钟的完整 `_watch_loop()`。原请求层有自动重试，适配器必须限制请求超时，并在 429／challenge 时立即结束，不能进入原重试循环。
4. 开始前、约 5 分钟及结束时读取服务器 CurrentDrop／Inventory。若尚无条目，保留“无条目”状态，不能伪造为已存在且 0 分钟。比较同一用户、活动和 drop 的服务器前后进度。报告请求次数、HTTP 状态和真实分钟值；HTTP 204、本地计时和活动标题均不能代替进度证据。没有增量就结束本轮并记录现象，不自动延长实验。
5. 达到服务器确认的领取条件后，才使用现有 ClaimDrop 实现并复查 Inventory。没达到条件就如实记录“未执行领取”，不要由本地估算或拼接 claim ID 冒充服务器确认。

本轮未新增或运行该实验程序。若主备都无法取得可验证目标，记录原始状态后结束本轮，不无变化地重复查询。
只有单频道的服务端进度通过后，才设计受限频道发现接入；它也不能证明已经恢复全量新活动发现。

## 持续运行仍需独立核验

一次约 2 小时的 uptime 不能证明 VM 永远可用，也不能证明长期运行或恢复一定不可能。
依据 Muse 已有的平台文档、cron／hook 恢复说明和运行记录，分别核验文件持久性、任务结束后的进程存活、VM 替换后的重启机制及代理凭据动态读取。
未证实的项目保留为未知；单次观看成功也不等于已经具备 24 小时自动运行能力。
