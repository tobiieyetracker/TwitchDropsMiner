# Muse：纠正单频道实验的会话与进度观测

2026-09-30。当前仍是候选方案，没有通过 Muse 的观看、领取或全天候运行验收。

## 上一轮实验不能证明什么

Muse 已确认，`073e2ba` 上的十分钟实验使用了重写的 HTTP 流程，没有调用原
`Channel.send_watch()`；新建 Session 没有加载 `cookies.jar`，游戏字段使用硬编码，
进度读取又用 `currentUser or {}` 混淆了未登录与真正无会话。

因此保留的结论只有：那个适配器收到了十次 HTTP 204，没有观测到明确的服务器进度。
不能据此写成“原矿机的纯 Python 观看无效”，也不能写成已观察到真实的 `0/60`。
缺 Cookie 是实现差异，不是已证实的根因；即使按域匹配后没有 Cookie，事件还包含
user_id，现有证据不足以宣称 Twitch 一定把它当成匿名事件。

另一个路径纠正：原 `Stream.from_get_stream()` 读取 `broadcastSettings.game`，
不是 `stream.game`。不能因为后者为空，就声称原矿机会发送空游戏信息。

## 新入口的范围

新增 `check_channel_watch.py`：

- 实际调用原 `Stream.from_get_stream()`、`Channel.get_spade_url()`、`Channel.send_watch()`，
  不复制观看 payload。用户、频道、直播和游戏 ID 均取自本次服务器响应。
- 在创建共享 aiohttp Session **之前**设为 WEB client；从指定文件加载完整 CookieJar，
  按正常域规则发送。GQL、频道 HTML/settings 和 Spade 共用这个 Session。
- GQL 使用原 `Twitch._gql_request_once()` 和 `_AuthState.headers()`；入口自行验证现有
  WEB token，不调用会打开浏览器或保存 Cookie 的正常登录／退出流程。
- 传输层是有界适配器：每请求最多 20 秒，不跟随重定向、不自动重试；遇 429、challenge、
  HTTP 错误或身份异常立即结束。保持 TLS 验证，不向 Spade 人为补 GQL 的鉴权头。
- trace 只输出实际发送的 Cookie 数量、auth-token Cookie 是否存在／等值、OAuth 是否
  匹配、WEB client／UA 是否匹配、HTTP 状态等。没有值、token 散列或完整 URL。
  目标域存在不同 auth-token 时终止；这说明 token 不同，不擅自推断是另一个账户，
  也不清空、覆盖或跨域复制 Cookie。
- CurrentDrop／Inventory 的 `currentUser` 必须是非空对象；若返回用户 ID 则必须匹配。
  查询变体不返回 ID 时，`user_matches:null` 如实保留，同一 OAuth 的身份来自 validate。
  字段缺失、null、错误类型不会再变成空列表或零分钟。
- 不构造 `DropsCampaign`，不依赖 dashboard，也不运行本地补分钟、WebSocket 或领取。
  这是隔离观看链的诊断入口，尚未接入完整矿机。

## Muse 执行一次

保留现有文件并更新分支后，在仓库目录、现有 Python 环境中运行下面的命令。
它默认先验证身份、直播、活动及基线；任何前置失败都不会发出观看事件。

```bash
python check_channel_watch.py --cookies cookies.jar --channel hjune --campaign-name "Rust Isles AR" --proxy-env HTTPS_PROXY --watch --linked-confirmed
```

`HTTPS_PROXY` 是环境变量名，按实际平台变量替换，不把代理密码放进命令行。
`--linked-confirmed` 对应 Muse 已报告的 Facepunch connections 页面关联证据；若此证据
不再成立则不能使用这个参数。它不把未知的服务端 campaign 关联字段伪造成 true；
输出会明确标为 `operator_confirmed_connections`，服务端是否认可仍是实测问题。

观看窗口默认最多 600 秒，最多 10 次原矿机事件、间隔至少原有的 59 秒；没有补发突发。
在基线、中途、结束时读取服务器进度。中途复核直播 ID／游戏，发送前检查窗口及
活动/drop 有效期；慢请求越过截止后不会继续发送观看事件。窗口外仅可做最终只读检查。
整轮另有硬超时；超时、网络错误或字段未知会失败，不解释为零进度。

去掉 `--watch` 仅做前置只读检查；不要先后无必要地跑两遍。不要改回旧手写脚本，
也不要恢复浏览器 integrity 探针或扩大为频道扫描。hJune 已离线／活动失效时本轮结束，
回传明确状态后再决定目标。

## 读结果

- `preflight_passed`：只读检查通过，没有观看。
- `progress_observed`：同一目标 drop 出现可见服务器正进度；`increase` 表示与已知分钟数
  比较有增加，`new_positive_progress` 保留 `before_minutes:null`，不伪造零基线。
  这仍不单独证明增量一定由本脚本引起（比如同账户另有播放），也不代表领取和全天候通过。
- `no_progress_observed`：有效身份／有效响应下未观察到目标增量。保留原快照，不能扩大成
  所有 Python 方案不可能；没有新差异时不重复相同实验。
- `failed`：看 `error`／`phase`；未知或失败不得当成正常空库存。

`watch_sends` 只统计已返回 204 的发送；在途失败可能仍有一条 `requests` 记录。
退出码 0 只对应前置检查通过或观察到进度，退出码 1 对应失败／没有增量。
输出为脱敏 JSON，请保存到 Muse 的持久报告目录并回传；Cookie 文件始终不保存或覆盖。

本地新增的离线测试只验证代码边界，不代表已在 Muse 或 Twitch 服务器通过。

## 常驻恢复可以独立验证

Muse 已报告 systemd user 无 bus、VM 替换会终止裸进程，而 runtime 会恢复 cron 配置。
这使 cron supervisor 成为当前候选部署方式，但“配置能恢复”不等于任务结束后一定执行，
也不等于 VM 能自动被唤醒。可用不访问 Twitch 的空载心跳任务核实：
任务结束后记录是否继续、子进程退出后是否恢复、已发生／平台支持的 VM 替换后是否重启。
使用单实例锁和退避，状态留在 `~`；每次启动读取当前代理配置。尚无证据的恢复项仍记未知。
不要让 cron 重复运行这个观看诊断，更不要让它循环重试完整性或 429。
