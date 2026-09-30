# Muse：完成一个已发现的掉宝并核验领取

2026-09-30。目标仍是在用户唯一可用的 Muse 云主机上实现全天候发现、观看、领取和恢复。
本入口只完成下一项有界验证：延续已在 Inventory 中出现的 Rust Isles AR，尝试领取一次，
再读取同账号服务器库存确认。它不是常驻服务，也不代表全部 campaign 发现已修复。

## 已获得的证据

Muse 报告在 `547f783` 上原样执行十分钟测试，退出码 0、`progress_observed`：
CurrentDrop 基线是空 dropID，目标分钟为未知；约五分钟和十分钟分别返回同一目标的
4/60、8/60，Inventory 也出现该目标，三个完整检查点的用户 ID 均匹配。
十次原 `Channel.send_watch()` 返回 204，实际请求的 Cookie／OAuth／WEB 身份匹配。

正确表述是“未知基线后出现正进度，并观察到同一目标 4→8”，不能改写成已知的 0→8。
此前手写适配器同时存在 Cookie、游戏字段来源和用户状态解析等差异，因此本轮成功
不能单独证明“缺 Cookie 就是此前失败的唯一原因”。它证明这条真实会话、原矿机方法的
纯 Python 路径已能与服务器正进度同时成立，值得继续验证领取。

## 7614144 实测：观看达标，领取结果未知

Muse 报告已保存 `probe-reports/finish-drop-7614144-20260930-2110.json`。
用户转述的只读复核确认：起始 Inventory 为 18/60，46 次观看事件后目标达到 60/60。
领取前再次验证身份并读取 `claim_inventory`，其 `is_claimed:false`；随后唯一一次
ClaimDrop 请求 HTTP 200，程序以 `error:gql_challenge`、`phase:claim` 退出。

**本轮没有领取后的 Inventory 读取。** 最后的 `claim_inventory` 是提交前快照，
确认循环未执行。`claim_unconfirmed` 表示结果未知，不能改写为“领取确定失败”。
旧版对任何非 null 的 `extensions.challenge` 都给出同一个错误码，没有保存 type；
所以“此次是 integrity challenge”尚无记录支持，更不能推断领取的风控严格程度。

新代码将 GQL 响应的 challenge 摘要保存在对应请求的 `response_challenge`，领取响应
另放在 `claim.response_challenge`。只保留 presence 和固定类型枚举；未知字符串、原始
challenge、token 和响应体均不输出。实际请求记录只增加 `integrity_header_present`
布尔值。它们服务于以后的观测，无法补回旧响应，也不是重新提交领取的理由。

### 当前下一步：只读核对一次

保留原状态目录，更新到本次交接指定提交后执行一次：

```bash
DISPLAY=:99 python finish_channel_drop.py --cookies cookies.jar --channel hjune --campaign-name "Rust Isles AR" --proxy-env HTTPS_PROXY --reconcile-only
```

若此前显式用了不同的 `--state-dir`，必须使用同一个目录。此模式必须找到原领取尝试
日志；缺失或损坏就在联网前失败，绝不会改走观看或领取。它最多运行 120 秒、发送两次
请求：一次验证 WEB 身份，一次读取 Inventory，并检查原账号与 campaign/drop 绑定。
请求层和 GQL 层都拒绝其他操作，不启动浏览器，不提交 mutation，不自动重试。

回报脱敏 JSON、退出码与新提交，保存为独立的新报告，不覆盖原报告：

- `claim_confirmed` 且 `claim.previous_attempt:true`：现在同一目标的 `isClaimed:true`，
  原尝试记录可标为已确认。它不证明一定是哪一个客户端使状态发生了变化。
- `claim_unconfirmed`：当前仍是 false、目标消失或读取失败；保留日志，不能自动再次领取。
- `reconcile_journal_missing`、账号／目标不匹配或日志损坏：保持原文件并回报，不创建
  替代日志、不换目录，不去掉 `--reconcile-only`。

这次核对只回答“现在是否已经领取”，不重新获取旧 challenge 类型，也不验证新的
完整性方案。后续正常网页领取链的源码比对见[领取 challenge 源码记录](claim-challenge-source.md)。

## 保留文件并更新

在现有仓库和 Python 环境中工作，保留 Cookie、`manual_watch.py`、本地报告及所有改动。
先查看 `git status --short` 和当前提交，再 fetch `origin codex/campaign-discovery`，
只做可以保留现有改动的快进更新，并用 `git rev-parse HEAD` 核对本次交接指定的提交。
若存在分叉、冲突或文件覆盖风险，先回报，不使用 reset、clean、强制 checkout 或自动清理。
不要推断本地分支名与远端相同，也不要为了更新切换或覆盖现有工作。

本次新增入口是 `finish_channel_drop.py`；之前的 `check_channel_watch.py` 仍保留十分钟语义。
不要修改原有脚本以延长它，也不要把新入口加入 cron。

## 历史：7614144 的观看及领取命令（当前不再执行）

下面记录的是已结束实验的命令；当前使用上面的 `--reconcile-only` 命令：

```bash
DISPLAY=:99 python finish_channel_drop.py --cookies cookies.jar --channel hjune --campaign-name "Rust Isles AR" --proxy-env HTTPS_PROXY --linked-confirmed
```

`HTTPS_PROXY` 是当前代理环境变量的名称，按平台实际名称替换，不能填代理密码或完整
代理地址。入口每次启动读取当前环境，不把轮换凭据写死。`--linked-confirmed` 只记录
操作员已确认关联，不把服务端未知的关联字段改成 true；若原有证据已不成立，停止并回报。

将这一次运行的完整脱敏标准输出保存到 Muse 持久目录
`docs/campaign-discovery/probe-reports/` 下独立的新 JSON 文件，另记录提交、开始／结束时间
和退出码。可以由现有任务工具捕获输出，不要为了保存报告再执行第二遍，不覆盖既有报告。

入口先读取现有 WEB CookieJar 并验证 token，再以 Inventory 中的活动名称与真实 ID
确定唯一目标。Inventory 的 `currentUser.id` 必须明确匹配验证过的用户；活动或掉宝缺失，
以及字段缺失、null、畸形或重复目标都不会变成零进度。起始 Inventory 没有该目标时停止，
不会重新扫描、凭名字拼出活动、改换频道或回到浏览器探针。

如果起始 Inventory 已明确满足领取条件，入口可以在频道离线时直接进行领取验证；
只有尚需继续积累分钟时，才要求 hJune 仍在直播、游戏和活动匹配。直播或游戏变化会停止。

## 限制和成功判据

- 整次运行最多 90 分钟，包括前置检查、观看和领取核验；网络请求总数最多 180。
  单请求最多 20 秒，并受剩余总时长约束。
- 观看使用已验证的共享 WEB 会话、正常 Cookie 域匹配和原 `Channel.send_watch()`；
  事件之间至少间隔原有的 59 秒，没有追赶式补发，不使用本地“补分钟”。
- 约每 300 秒重新校验身份、直播／游戏和服务器进度；900 秒没有观察到服务器进度增加
  就停止。最后预留 120 秒用于只读状态和领取核验，不能拿来无限延长观看。
- 只有新鲜、身份匹配的 Inventory 中，目标分钟达到正的 `requiredMinutesWatched`、
  `isClaimed:false`、实际返回非空 `self.dropInstanceID`，且未出现明确的前置条件失败时，
  才允许提交。`hasPreconditionsMet:false` 会停止；未知不会被伪造成 true。
- 使用原 `BaseDrop._claim()`，只允许这一目标的一次 mutation。不会调用 `generate_claim()`，
  不拼实例 ID。满分钟但实例 ID 尚未返回时，只允许两次间隔 15 秒的只读等待，之后停止。
- 原方法返回 True／HTTP 成功仍不足以确认领取。最多三次 Inventory 复查，后续两次间隔
  15 秒，只有同一目标的 `self.isClaimed:true` 才记录确认。领取后服务器分钟重置不否定
  这个布尔确认；目标从“进行中”列表消失则记为未确认，绝不当作已领取。

有效期检查沿用原矿机的领取窗口上限，依据实际活动结束时间判断；这不是“结束后必定还能
领取 24 小时”的服务端保证。窗口内仍需真实实例 ID 和领取后库存确认，过期不会继续观看。

任何 429、challenge、身份异常、重定向、网络错误或未知字段都会停止。禁止自动复跑、
重新提交领取、换目标、恢复浏览器 integrity 探针或扩大扫描范围。

## 领取记录和中断恢复

默认状态目录是 `~/.local/state/twitchdropsminer/finish-drop`，位于 Muse 已知持久的 `~` 下。
其中锁防止两个实例同时执行；日志记录账户绑定、活动／掉宝 ID、尝试时间和确认结果，
在 Linux 上使用仅本用户可访问的目录和文件权限。

日志包含用于绑定账号的私有 user ID，但不包含 OAuth token、Cookie、代理凭据或真实
`dropInstanceID`；不要把该日志提交 GitHub 或贴进聊天。回报用脱敏 JSON，不用原始状态文件。

入口在提交领取之前持久记录“已尝试”。即使当时尚未发出网络请求就被中断，后续启动也
保守地只读核对同账号、同目标 Inventory，不再发观看或领取事件。若领取回应丢失、VM
被替换或程序退出，不能通过删除日志、锁文件，或改 `--state-dir` 来重新获得提交机会。
本次要求只运行一次；发生中断先保存结果，后续恢复核验另行说明，不自动安排循环。

## 回报结果

- `claim_confirmed`：本次或先前记录的尝试后，同一目标 Inventory 明确返回 `isClaimed:true`。
  查看 `claim.previous_attempt` 区分是否是在核对先前尝试。
- `already_claimed`：在提交之前就发现目标已领取，本轮没有新领取。
- `incomplete`：本轮时间上限内仍未满足领取条件，不代表服务器否认所有观看事件。
- `claim_unconfirmed`：已经记录一次尝试，但未取得同一目标 `isClaimed:true` 的最终证据；
  包括目标消失或核验失败。不得因此重新提交 mutation。
- `failed`：观看或领取之前的某个条件失败，查看 `phase`／`error`；失败不能读成空库存。

退出码 0 仅对应 `claim_confirmed` 或 `already_claimed`，其余为 1。请回传完整脱敏 JSON、
退出码和提交，重点保留 `inventory_checks`、`current_checks`、`watch_sends`、`claim`、
`phase`、`error`、`elapsed_seconds` 及请求的实际身份匹配结果。204 次数不是领取凭证。

这次完成后，自动发现其他活动、持续运行、cron supervisor 和 VM 替换后的恢复仍需单独
验收。用户只有 Muse 主机这一约束不变；一次成功领取也不能写成已经实现 7×24 小时。
