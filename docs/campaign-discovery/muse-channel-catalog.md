# Muse：按频道发现活动候选

`check_channel_catalog.py` 使用现有 WEB Cookie 和 Python GQL 连接，读取指定
频道的 `AvailableDrops`。它不依赖浏览器或成功的 `ViewerDropsDashboard`。
用途是列出可继续核实的活动候选，不是直接启动矿机。

范围始终为 `coverage="channels"`、`all_campaigns_verified=false`。
即使本次检查通过，也不表示获得全站或该账号的完整活动列表，不表示已确认
参与资格、观看、领取或全天候运行。

## 调用与约束

在现有 Python 环境中运行；代理只传环境变量名，启动时读取当前值：

```bash
DISPLAY=:99 env/bin/python check_channel_catalog.py \
  --cookie-file cookies.jar --channel-login disguisedtoast --proxy-env HTTPS_PROXY
```

`--channel-login` 可换成其他当前正在直播、可能有活动的频道，不应固定使用 hJune。
目录中的 DropsEnabled 和直播标题仅用于选择候选，仍须以 AvailableDrops 实际响应验证。

正常序列最多四个请求：身份验证、GetStreamInfo、AvailableDrops、同账号
Inventory 对照。只查询一个显式频道，不自动扩展到游戏目录，不重试，不观看、
不领取、不启动浏览器。WEB Cookie 只加载到本次会话，原 Cookie 文件不保存或覆盖。
不创建或修改领取 journal，不建立定时任务。

TLS 在系统信任初始化后显式创建新的验证上下文，并用于每次请求。
证书和主机名验证保持开启。连接失败只记录异常类型及经过筛选的调用位置；
不记录异常消息、代理值、Cookie、OAuth、完整请求头或 claim instance ID。

## 结果解释

- `campaigns_state` 区分 `list`、`null`、`missing`、`error`。未知不是空列表，
  解析失败不能标为成功；字段不完整不能用计数 0 表示。
- 只保留接口实际提供的活动、游戏及掉宝字段。不从掉宝时间推造活动时间，
  不默认 `self.isAccountConnected=true`，不把观察到的单个频道变成完整 ACL。
- 候选与 Inventory 用户态分开记录，不直接构造 `DropsCampaign`。
  Inventory 的 `currentUser.id` 必须与已验证账号相同；缺失、空用户或异常
  条目都不能用于证明某活动不在库存中。
- 只有双方列表可比较时，才计算候选活动 ID 减去库存活动 ID。
  相同数量不等于相同集合；差集未知时数量也未知。
- 429、challenge、身份异常和预算耗尽会停止本次运行。退出码 0 仅表示
  本次有限检查满足其数据条件，不是完整活动发现或领取验收通过。

## 同一游戏的不同主播与掉宝

用户明确补充：Rust 等游戏会按主播提供不同物品。`game_id` 只标识游戏，
不能用作活动或掉宝的去重键，也不能因某个主播查询成功就把该游戏标为发现完成。
已有实测中，hJune 的 Rust Isles AR 与 disguisedtoast 的 Rust Isles SAR
同属 Rust，却有不同的 campaign ID 和 drop ID。

后续多频道聚合与观看调度须保留以下关系：

- 游戏用于分组；活动按 `campaign_id` 区分，活动内掉宝按 `drop_id` 区分。
  同游戏、同名活动或同名物品不能据此合并。
- 保存每条候选的来源频道与观察时间。同一活动从不同频道返回时，保留不同
  掉宝和各自来源，不用后一个频道的结果覆盖前一个；冲突数据保留待核实。
- 从一个频道观察到活动，不等于该频道能产出活动中的所有掉宝，也不等于已知
  完整可用主播名单。具体观看目标须结合服务端频道限制、资格和进度证据确认，
  不把发现来源伪造为完整 `allow.channels`，也不推定任意 Rust 主播都可替代。
- 找到首个有效频道即停止，是此次有限验证的规则，不是最终发现功能的完成条件。
  后续应保留未检查的频道与覆盖范围，不能按游戏只选一位主播后宣称奖励齐全。

当前脚本按 campaign ID 去重，并在活动内检查 drop ID；没有按 game ID 合并。
它一次只处理一个频道，尚未实现跨频道聚合或自动选择各掉宝对应的观看频道。

## 上游报告：多查主播仍不能保证完整性

2026-10-01 重新读取 issue #1165 的全部 34 条评论及 PR #1174 相关讨论。
维护者报告：`AvailableDrops` 虽返回数组，实际可能只给出一个活动；当全站
徽章或表情活动占据返回结果时，其他活动会在各频道的查询中不可见，调用者
不能指定返回哪个活动。维护者见过持续至该全站活动结束、最长约 2–4 周的情形。
这是上游对服务端行为的报告，尚未在 Muse 本轮复现，不应写成固定服务端保证。

来源：[PR #1174 原评论](https://github.com/DevilXD/TwitchDropsMiner/pull/1174#issuecomment-5870396683)、
[issue #1165 持续时间说明](https://github.com/DevilXD/TwitchDropsMiner/issues/1165#issuecomment-5888021804)。

因此，多频道汇总可以扩大观测范围，但不能单独保证全量发现或长期不漏奖励。
此前 SAR 新活动发现成功仍成立；应保留其作为有限候选来源，不能仅靠扩大扫描
把它升级为 Dashboard 的完整替代。此限制独立于游戏 ID 去重和主播限定问题。

## issue 中提到的公共目录候选

2026-10-01 只读核对 [rangermix PR #115](https://github.com/rangermix/TwitchDropsMiner/pull/115)
及 [SunkwiBOT/twitch-drops-api](https://github.com/SunkwiBOT/twitch-drops-api)。
Sunkwi 仓库 `811e19a` 的公共 `drops.json` 快照含 89 个游戏、129 个活动条目，
其中 Rust 有 17 个；本项目已验证的 AR、SAR 活动 ID 和各自频道限制均存在。
获取公共元数据不需要发送 Muse 的 OAuth 或 Cookie。

它值得作为另一种候选发现来源评估，能够提供本轮频道查询没有涵盖的活动条目，
但数量不是全量证明，时效和采集完整性没有保证。仓库公开的是从 API 拉取并镜像
数据的代码，不是后端如何获取活动的完整实现。公共数据中的任何 `self` 字段都
不能作为 Muse 账号的资格、关联、进度或领取状态；这些必须来自同账号的 Twitch
响应。PR #115 已关闭未合并，讨论记录了假关联及合并结构问题，不能直接照搬。
本轮只查阅公开源码与数据，未把该目录接入矿机。

## 已有证据与候选状态

此前 Muse 使用原矿机会话从 hJune 的 AvailableDrops 发现了 Rust Isles AR，
并已实际累计到服务器 60/60。这证明单频道的 Python 路径有实测依据；它不能
替代这个新入口的验证，也不能证明其他频道或活动都可发现。

Muse 最初实现为本地提交 `e97c0e7`，未推送。唯一一次新入口运行在身份 GET
阶段出现 `ClientConnectionError`，只有一条请求记录且没有 HTTP 状态，未进入
频道、活动或库存查询。报告保留在 Muse 的
`probe-reports/channel-catalog-af44d64-20261001-0048.json`。此结果不能归因为
Twitch integrity，也不能称本次活动列表为空。

取回该候选后，离线审查发现它遗漏显式 TLS 上下文，且会把某些未知数据标成
`passed`、把未知掉宝列表当空、忽略坏库存条目后继续计算差集。取回后的版本已
修正这些缺陷；旧报告不修改。39 项离线测试通过，包括实际 HTTP 调用边界的
TLS 上下文、请求顺序/预算、异常停止、身份匹配、未知值、敏感信息过滤与清理。
这些测试使用模拟服务器；相关频道/观看测试共 94 项通过。

Muse 随后在保留原分支的 `codex/catalog-reviewed-2958218` 上执行了一次修正版本
`2958218`。报告为 `probe-reports/channel-catalog-2958218-20261001-0107.json`：

- 退出码 1，`state=failed`、`phase=available_drops`、`error=catalog_null`。
- 三次请求均 HTTP 200：身份 GET、GetStreamInfo、AvailableDrops。未记录 challenge
  或 429；本轮没有 `request_failure`。
- 活动解析状态为 `null`，数量及候选保持未知。解析器将 data/channel/活动字段的
  null 映射到此状态，单凭此字段不能补写原响应具体哪一层为 null。
- hJune 的当前游戏为 `I'm Only Sleeping`，不是此前的 Rust。这不能单独解释
  null 的原因，也不能证明 Rust Isles AR 已不存在。
- Inventory 按停止条件未调用；账号库存对照与新增活动差集未知。
- Cookie、旧 journal、原报告和 Muse 的原 `e97c0e7` 分支保留；无残留进程，
  未运行浏览器、观看、领取或定时任务。

以上现场结果来自直接读取 Muse 对话回执；完整 JSON 保存在 Muse，尚未在本机
独立读取。本轮证明修正后的 Python 连接可以完成三次请求，不能仅由前后结果
断言上一轮连接错误的唯一根因。它也没有验证成功的频道候选列表；当轮未自动复跑。

## 更换主播后的成功验证

用户明确允许改用其他主播后，先通过 Twitch 官方 Rust 目录选出当前候选，
再让 Muse 复用同一入口，在 `19234d7`（实现为 `2958218`）上查询
`disguisedtoast`。未改代码、未重复 hJune，首个候选成功后即停止；备选
zchum、monstera 均未查询。

Muse 报告为
`probe-reports/channel-catalog-19234d7-20261001-0114-disguisedtoast.json`：

- 退出码 0，`state=passed`。身份验证、GetStreamInfo、AvailableDrops、
  Inventory 共四次请求均 HTTP 200，无 challenge、无 429。
- 频道 `disguisedtoast`（ID `87204022`）正在直播 Rust（游戏 ID `263490`）。
- AvailableDrops 返回一个真实活动 `Rust Isles SAR`，活动 ID
  `b7c42d19-0a30-4786-b5d7-8a3f3508541a`；结束时间为
  `2026-10-04T23:58:59.999Z`，检查时仍在有效期。
- 该活动返回一个掉宝，ID `3ee31a53-b767-11f1-b841-0a58a9feac02`，
  要求观看 60 分钟；窗口为 `2026-09-24T18:44:00Z` 至
  `2026-10-04T23:58:59.999Z`。
- Inventory 的 `same_account=true`，列出四个进行中活动；ID 差集
  `new_count=1`，本次 SAR 活动不在该账号原库存中。
- Cookie、旧领取 journal 和 hJune 报告保留，报告称相关文件 hash 未变；
  无残留进程、无 cron。此次未观看、未领取、未启动浏览器。

这次现场结果证明 Muse 的纯 Python 频道查询可以发现原 Inventory 未包含的
新活动，不局限于 hJune。SAR 的活动和掉宝 ID 与此前 Rust Isles AR 不同，
不能将两次结果当作同一目标的前后状态。它仍是频道范围的发现验证，
`all_campaigns_verified=false`；未验证该 SAR 活动的观看、领取或参与资格。

上述成功结果经本机直接读取 Muse 对话回执核对；完整 JSON 保存在 Muse，
尚未在本机独立读取。旧失败报告保留，不用新成功反推旧失败的唯一原因。

完整活动发现仍以 [dashboard 验证记录](muse-campaign-coverage.md) 为准；
领取仍以 [原生领取验证记录](muse-native-inventory-claim.md) 为准。当前没有
证据证明这两个阻塞已解决。
