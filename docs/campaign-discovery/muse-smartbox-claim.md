# Muse：匹配 SMARTBOX 身份的单次领取候选

2026-10-01。当前是待实测候选，不是领取修复结论。默认只读，不启动观看、浏览器或常驻任务。

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
