# Muse：匹配 SMARTBOX 身份的单次领取候选

2026-10-01。唯一候选已实测：原领取方法接受响应、无 challenge，进行中库存目标随后消失；
严格领取确认尚未获得。不要重新提交领取。下述领取命令仅保留为实验记录。

## a133686 的验证进展

- 本机在已提交 HEAD 的干净副本中只叠加本次五个文件，163 项相关测试通过；
  Muse 同步 `a1336861e4c6ac04a8b2864978ed6ee17b98d323` 后，40 项新增测试通过。
- Muse 只读预检报告为 `probe-reports/smartbox-claim-preflight-a133686-20261001-0156.json`。
  Codex 已直接读取 Muse 会话中的回报，尚未取得原始 JSON 的完整本地副本。
  退出码 0、`preflight_ready`：旧 SMARTBOX token 有效，同账号原目标仍为 60/60、
  `is_claimed:false`、真实实例存在，关联与前置资格均为 true。
- 预检仅发出 validation 与 Inventory 两次请求，均 HTTP 200，无 challenge/429，
  `watch_sends:0`、`claim.attempted:false`。Muse 报告两份 Cookie 及原、WEB journal
  的运行前后 hash 一致；没有创建候选领取记录。
- 唯一候选报告为 `probe-reports/smartbox-claim-a133686-20261001-015752.json`：
  退出码 1、`claim_unconfirmed`、`phase:claim_confirmation`，
  `error:claim_not_confirmed_by_inventory`。8 次请求均为 HTTP 200，无观看；
  mutation 的 `response_challenge.present:false`，原方法 `miner_response_accepted:true`。
  原 `_claim()` 在此次路径只会对 `ELIGIBLE_FOR_ALL` 或 `DROP_INSTANCE_ALREADY_CLAIMED`
  返回 true，原始状态字符串未保存，不能补猜是哪一个。
- 后续三次 Inventory 中原目标均 absent；这不等于 `isClaimed:false`，也不是新的
  integrity 拒绝。`smartbox-claim-v1.json` 已持久保存 attempted，不能重提；
  Muse 报告两份 Cookie、原和 WEB journal 的 hash 均保持不变。
- 当前确认器仅查看 `dropCampaignsInProgress`，没有核对另一个已获奖励集合
  `gameEventDrops`。原 miner 使用 benefit ID 与授予时间作辅助判断，官网也分别
  展示进行中和已获奖励。但不能由目标消失推导领取成功，也没有证明服务端必移除
  已领取活动。Muse 没有保存完整原响应或目标 benefit 映射，无法离线补回这些证据。

## 只读补充：已获奖励集合

固定公开快照的 [目标元数据](rust-isles-ar-benefit.json) 将原 campaign/drop 映射为
唯一 benefit `0c95681b-95ad-4be7-a2ba-07dcace74891_CUSTOM_ID_11589`。
该来源为第三方公共活动快照，不是账号资格或领取证据。
`check_smartbox_awards.py` 只使用已有 SMARTBOX 候选 journal，验证账号并读取一次原
Inventory，核对该 benefit 的实际授予时间，最多两次请求，不再 mutation。

```bash
DISPLAY=:99 python check_smartbox_awards.py --cookies cookies.jar.bak --proxy-env HTTPS_PROXY
```

若 `reward_grant_observed`，其含义仅为这份元数据对应的全部奖励在该账号的实际
已获记录中出现，授予时间不早于候选尝试且不晚于本次读取。它不冒充 `isClaimed:true`，
不改写旧 claim 结论或任何 journal；没有领取前 award baseline、映射来自公共快照，
仍不足以排除共享 benefit 或其他客户端领取的归因歧义。
未知、null、缺失、过旧或未来时间都不能认定本次已授予。

### e4b3cb3 实测结果：已有奖励，不能归因本次

本机干净副本中，38 项新核对测试和 40 项 SMARTBOX 领取测试共 78 项通过。
Muse 同步 `e4b3cb3ed1f00bfafc4564974a4b606518014aed` 后，38 项新增测试通过，
仅执行一次只读核对，报告为 `probe-reports/smartbox-awards-e4b3cb3-20261001-020800.json`。
以下为 Codex 直接读取 Muse 会话回报的结果，完整 JSON 仍保存在 Muse：

- 退出码 1、`reward_grant_unconfirmed`，`error:null`，同账号及 SMARTBOX token 验证通过。
- 原目标仍不在 `dropCampaignsInProgress` 中。所映射 benefit 确实存在于本账号的
  `Inventory.gameEventDrops`，`lastAwardedAt` 为 `2026-09-30T13:02:08Z`。
- 该时间早于 SMARTBOX 尝试的 `2026-09-30T17:57:56Z`；核对时间为同日
  `18:08:03Z`。因此 `within_attempt_window:false`，没有将已有奖励误认成这次新增。
- 仅 validation 与 Inventory 两次 HTTP 200。未领取、未观看、未写 journal，
  Muse 报告两份 Cookie 及原、WEB、SMARTBOX 三份 journal 的 hash 前后相同。

本次结论是：匹配 SMARTBOX 的原领取请求无 challenge，并得到原矿机认可的响应；
尚未证明一次新的奖励领取成功。具体 mutation 状态没有保存，不能补猜为
`DROP_INSTANCE_ALREADY_CLAIMED`，旧奖励授予也不能用于解释先前 WEB 响应的实际效果。
此固定目标不再重提领取。后续要验收新领取，需一个当前未授予的新目标，并在其
领取前保存官方 benefit 映射、已获奖励基线及脱敏 mutation 状态，再比较领取后结果；
不能降低标准，把原目标消失或旧奖励记录当作新领取成功。

新的依据是 [GrubDrops 作者的第一手报告](https://github.com/DevilXD/TwitchDropsMiner/issues/1165#issuecomment-5873543952)：其 SMARTBOX/TV 登录配合相同客户端身份，通过频道发现、Inventory 和直接 Go HTTP 路径观看并领取。其 [profile](https://github.com/aalejandrofer/GrubDrops/blob/855fae41983d30cfa03e6979fe1510358df79224/internal/platform/twitch/profile.go)、[transport](https://github.com/aalejandrofer/GrubDrops/blob/855fae41983d30cfa03e6979fe1510358df79224/internal/platform/twitch/client.go) 与 [claim](https://github.com/aalejandrofer/GrubDrops/blob/855fae41983d30cfa03e6979fe1510358df79224/internal/platform/twitch/claim.go) 支持这一具体实现方向，但作者未附逐请求及领取后库存证据，不能替代 Muse 实测。

此前 Muse 的领取失败使用 WEB 身份；SMARTBOX 的既有对照是搭配 ANDROID_APP 查询 dashboard。它们没有验证同签发方 SMARTBOX 领取。旧 `muse-web-claim-candidate.md` 中停止换客户端和新候选的要求属于那次 WEB hash 实验的历史边界；本次是源码证据支持的新身份对照，保留所有旧记录，不重新执行 WEB 失败尝试，不需要用户再次传话确认。

## 保留内容与固定边界

- 显式只读 Muse 已有旧 TV 备份 `cookies.jar.bak`；不覆盖当前 WEB `cookies.jar`，不把 Cookie 复制到别的域，不把 token 贴到聊天或命令行。
- 必须找到原 `~/.local/state/twitchdropsminer/finish-drop/journal.json`，验证相同账号、campaign ID 和 drop ID。目标来自原 journal，不重新扫描或改换主播。
- OAuth 验证必须确认 SMARTBOX 签发方；GQL 使用原矿机 `ClientType.SMARTBOX` 的 Client-Id／User-Agent。GrubDrops 的 TV User-Agent 与原矿机不同，本候选不是其请求的逐字重放。
- 复用原矿机共享 aiohttp session、`Twitch._gql_request_once()` 和 `BaseDrop._claim()`。只有 validate、精确 Inventory 和原 `DropsPage_ClaimDropRewards` 查询可发出；原 hash `a455deea…` 不变，不生成 integrity，不枚举 hash/client。
- TV host-only Cookie 未发给 GQL 是正常域隔离，OAuth 仍携带该 TV token。实际发送的 Cookie 若与 OAuth 冲突则停止。
- 代理从当前环境动态读取，使用新的验证型 SSLContext，不禁用证书检查、不跟随重定向。遇 429、challenge、身份错误或网络异常停止，不自动复跑。

## 第一步：默认只读预检

在 Muse 已有仓库与 Python 环境中执行一次：

```bash
DISPLAY=:99 python check_smartbox_claim.py --cookies cookies.jar.bak --campaign-name "Rust Isles AR" --proxy-env HTTPS_PROXY
```

没有 `--claim` 时最多两次请求：验证 token、读取 Inventory。不会创建候选领取尝试。备份缺失、过期、签发方不符或账号不匹配时如实失败；不要用当前 WEB Cookie 替换，不要将其解释为 SMARTBOX 领取失败。

`preflight_ready` 表示同账号原目标已具备服务端分钟、真实实例 ID 等前置条件，并不代表领取成功。资格字段未知时保持未知，明确 false 则停止，不为了预检填造 true。`already_claimed` 表示读取时已经领取，本轮无需 mutation。

## 第二步：一次候选领取

预检通过后，本轮已授权的同一候选可执行一次：

```bash
DISPLAY=:99 python check_smartbox_claim.py --cookies cookies.jar.bak --campaign-name "Rust Isles AR" --proxy-env HTTPS_PROXY --claim
```

本命令自行重新做预检，在提交前再次验证身份并读取 Inventory。目标必须仍与原 journal 一致、服务器进度达标、有真实非空 `self.dropInstanceID`，且资格未明确否定；不从旧报告取实例，不拼接实例 ID。若两次读取之间实例 ID、账号或目标身份改变，沿用原状态校验停止，不提交预检时的旧实例。

总预算最多 240 秒、8 次请求，包括最多一次原 claim mutation、最多三次领取后 Inventory 确认，后续确认间隔沿用 15 秒。只有同账号同目标明确 `self.isClaimed:true` 才报告 `claim_confirmed`。HTTP 200、mutation 状态或目标从进行中列表消失都不足以确认。

## 固定记录与中断

新记录固定为原状态目录内 `smartbox-claim-v1.json`，与原尝试共用 `finish-drop.lock`。提交 mutation 前持久写入账号／目标绑定和尝试状态，不保存 OAuth、Cookie、proxy 密码或真实 claim 实例。

原 `journal.json`、`web-query-claim-v1.json` 和 native 候选记录不改动。若 SMARTBOX 候选记录已存在，无论是否传 `--claim`，都只能做两次只读核对，不能再次提交。响应丢失、VM 中断或结果未知不会重新获得机会；不删除日志、换目录或克隆状态来重试。

## 回报与限制

将本次完整脱敏 stdout 保存到持久的 `docs/campaign-discovery/probe-reports/` 下独立文件，记录实际提交、时间和退出码；不要为了保存输出重跑。重点回报 `state`、`mode`、`phase`、`error`、`smartbox_token_valid`、`target`、`requests`、`inventory_checks`、`claim`。`watch_sends` 必须为零。Cookie 及其他 journal 字节应保持不变。

一次成功只验证这个账号和目标在此时的 SMARTBOX 领取；全量 campaign 发现、后续 token 续期、后台恢复及 7×24 小时运行仍需分别验证。
