# Integrity 签发身份与使用关系

本记录复用 2026-09-30 已下载的 Twitch 网页源码，并纳入用户转述的 Muse
`b7ea9ce` 探针结果。本机未读取 Muse 的 `/tmp/probe_b7ea9ce.json`，以下实测
数字来自转述，不是本机独立复现。本轮未通过，仍不能宣称 Linux campaign 发现已修好。

## Muse 本轮报告

- `k.twitchcdn.net` 的 SDK 主脚本 1/1 完成，0 失败；
  `assets.twitch.tv` 的 125 个脚本全部完成，0 失败。
- `sdk_script_present`、`sdk_global_present`、`sdk_ready` 均为 true。
- 2 次 `/integrity` POST 均完成，HTTP 200，`token_returned: true`。
- 第二次 dashboard 请求已携带 `Client-Integrity`，仍收到 integrity challenge，
  `campaigns_state` 仍为 null。
- SDK 域上另有一次 document HTTP 429 和一次 fetch `ERR_ABORTED`；现有报告
  没有建立它们与最终 campaign 拒绝之间的因果关系。
- 第三方域的两次证书错误未阻止已观察到的 Twitch 主脚本完成；这不能证明它们与
  SDK 的所有内部链路无关。
- 退出码 1；官网 dashboard 未获接受，因此未执行 Python 对照、未接入矿机。

这组结果区分了三个不同阶段：**SDK 就绪、接口返回 token、活动接口接受 token**。
前两个阶段通过，不意味着第三个阶段通过。旧版仅响应体数组为空，不能据此断言
integrity 请求从未发出；新版已经纠正了这一观测缺口。

## 已下载源码说明了什么

来源：[Twitch 网页传输 bundle](https://assets.twitch.tv/assets/21956-b5a5c32dd4e02f095dd2.js)，
SHA256 `06605B5A126FD12DD1AF1E1318284CAEEA356D6DE37756D949220133755262FE`。
可在该文件搜索下述函数名定位；没有重新调查或替换 persisted query hash。

### 签发请求与 GQL 请求携带的身份

`rawFetchIntegrityResponse()` 使用网页配置和会话对象构造：

- `Client-Id`
- `X-Device-Id`
- `Client-Session-Id`
- `Client-Version`
- 每次签发流程生成的 `Client-Request-Id`
- 若 `integrity.authToken` 已设置，则附带 OAuth Authorization

普通 GQL 的 `_getGqlFetchParams()` 也从网页配置和会话对象读取前四项，并读取
`apollo.authToken`。网页登录初始化代码会同时设置 Apollo 和 integrity 管理器
的 authToken。此处是客户端如何构造身份的源码事实，不能据此推断服务端具体校验了
哪些字段，或给未公开的服务端机制命名。

目前 dashboard 的 OAuth、WEB client-id 和 user_id 匹配，只证明该 dashboard
使用了预期登录态；不能代替对签发请求身份的检查。仅有完整性头存在的布尔值，
也不能说明其值对应两次签发中的哪一个响应。

### 初始化顺序与在途请求合并

integrity 管理器构造时 `authToken=null`。根据网站自身开关，它可能在构造阶段
调用 `fetchNewToken("app_boot")`；登录初始化稍后将 OAuth 写入两个管理器。
签发前又可能等待 SDK 就绪，所以源码并不能证明某次实际请求是匿名签发。
需要观察请求真正发出时的身份，而不是从调用顺序猜测。

`fetchAndStoreIntegrityToken()` 在已有签发 promise 时复用它。challenge 路径
调用的 `fetchNewToken("gql-challenge")` 也使用这套机制。因此启动取 token 与
挑战取 token 可能合并，需用实际请求和响应时间线确定。本轮“两次 POST 都成功”
还不足以确定各个 token 与被拒绝 dashboard 的对应关系。

### 挑战重放与 token 存储

challenge link 检查 `extensions.challenge.type == "integrity"`，等待签发结果，
重新读取当前 GQL 请求头，并把返回 token 放进 `Client-Integrity`。它用原 operation
的 `extensions`、`operationName` 和 `variables` 重放该 operation；没有依靠更换
hash 恢复。重放响应直接返回，不会再次经过同一 link 形成无限 challenge 循环。

`rawFetchIntegrityResponse()` 解析 JSON 后，只要响应中没有 `error` 字段就返回。
管理器随后保存 token，并根据 expiration 在剩余有效期约 90% 时安排刷新。
这个“签发成功”阶段没有验证 dashboard 是否接受该 token。

SDK 的 load/ready 事件说明初始化完成。当前已下载的网页封装代码没有解释 SDK
内部 document/fetch 的全部作用，因此无法证明本轮 429 或中止请求的后果。

## 配对探针的后续实测与当前边界

用户转述：按 `a623b25` 交接执行的最新探针约 7 秒即停止，退出码 1。
SDK fetch 请求 #72 在 4208 ms 发出、5548 ms 报 ERR_ABORTED；document 请求 #87
在 5361 ms 发出、5844 ms 返回 HTTP 429。停止前 SDK 主脚本 1/1、assets 脚本
73/73 完成，未进行页面状态快照。

该轮 `dashboard_responses` 和 `integrity_responses` 均为空，无法做配对或身份比较。
不能仅凭响应数组为空推断查询从未发出；完整性请求有独立生命周期计数，dashboard
没有 operation 级请求计数。此轮完整 JSON 尚未在 Windows 端读取。

这次早停由探针规则触发，不能证明网页遇到 429 后必然停止，也不能证明 SDK 429
就是上一轮活动拒绝的原因。与上一轮约相隔 15 分钟，两轮同类 429 均无可解析的
Retry-After；恢复时间和服务端原因未知，不据此指定固定时长后再次尝试。

旧 `b7ea9ce` 探针没有记录 token 等值关系、签发身份或请求相对时间。已有脱敏 JSON
只能保留当时的计数和状态，不能事后补回本轮希望比较的信息。

当前停止同环境重复探针，保存已有报告并确认平台支持的访问/常驻运行条件。
具体交接见 [CLOUD_OPERATION.md](../../CLOUD_OPERATION.md)。下述观测方案已经实现，
仅在有明确新条件、决定复验后使用，不是当前再次运行的要求。

## 已实现的被动观测

只观察网页正常发出的请求，不修改 SDK、网页开关、请求内容或浏览器身份。输出中
只保留序号、相对时间、状态和匹配结果，不能包含 OAuth、Cookie、integrity token
或可复用的身份信息。

1. 为每次 integrity 请求分配本轮序号，记录发出、响应、完成或失败的时间。
   在内存中把 dashboard 的 `Client-Integrity` 与已观察到的返回 token 做等值比较，
   输出匹配的签发序号；不能只比较“头是否存在”。
2. 比较签发请求与使用该 token 的 dashboard 的 OAuth、Client-Id、device、session、
   version，仅输出一致、不一致或无法判断。缺少请求头不能冒充一致。
3. 对照签发请求、签发响应与 dashboard 的相对时间，确认被使用 token 的签发顺序。
   本轮不估算 token 过期时间，也不解析或输出 token 的内部内容；有效期仍是未核验项。
4. 对 SDK 域异常记录相对时间、资源类型、状态、失败原因和必要的路径类别，区分
   主脚本与后续 document/fetch。记录 429 的 `Retry-After`，遵守其等待要求；不要
   连续重跑探针、并发发起诊断或通过更换身份来躲避限流。若未提供等待时长，先停止
   本轮，结合已有输出决定是否需要一次有明确目的的复验。

若确定被拒绝 dashboard 使用的正是已观察到的签发响应返回、且签发与使用身份一致
的 token，就能排除这组观测中的 token 配对和身份不一致问题；本轮不能据此判断
token 有效期。该结果仍不能证明是自动化检测、TLS 指纹、某个 Cookie 或 SDK 域
429 导致，也不支持继续做 stealth、指纹伪装或关闭证书验证。

官网活动查询未成功前，Python 对照仍不执行。即使后续单次查询成功，仍需另外验证
矿机会话持久化、自然刷新、真实进度、领取以及重启恢复，才能评估全天候运行。

## 2026-10-01 核对上游 issue #1165

已读取该 issue 当前全部 34 条评论，最新维护者更新为 2026-09-30。
维护者称基于 zendriver 的浏览器登录已工作；基于社区 get_integrity 示例的
原型在家用机器工作、办公机器不能获得有效 token，Linux/macOS 尚未确认。
这说明上游也仍在验证环境可靠性，不能把换驱动当作 Muse 已验证的修复。
来源：[维护者最新进展](https://github.com/DevilXD/TwitchDropsMiner/issues/1165#issuecomment-5908413313)。

社区曾报告在其改造的 `gql_request()` 中让 ClaimDrop 使用 `integrity=True`
后恢复领取；该参数依赖其附带的 token 获取与请求实现，不能只复制布尔参数
到原函数。它是需要取得服务端接受的 token 的候选实现，不是避开完整性检查。
Muse 已有“签发返回 token、GQL 仍拒绝”的证据，尚未满足这一前提。
来源：[领取改动评论](https://github.com/DevilXD/TwitchDropsMiner/issues/1165#issuecomment-5833126276)。

PR #1177 作者后来称，首次点击书签后保持原 Twitch 页面打开可自动续期，
不能把作者的方案概括成“每次刷新都必须手点”。但当前草稿 `e3afabf` 的
书签只执行一次签发与交付，未包含持续交付循环；localhost 的定时器是状态
轮询。该报告尚不足以证明矿机会持续收到新 token，仍需实现核对与自然过期实测。
PR 尚未合并；Muse 的正常浏览器网络、登录态和 token 接受问题也仍需满足。
来源：[作者补充](https://github.com/DevilXD/TwitchDropsMiner/issues/1165#issuecomment-5882655349)、
[PR #1177](https://github.com/DevilXD/TwitchDropsMiner/pull/1177)、
[当时的桥接代码](https://github.com/DevilXD/TwitchDropsMiner/blob/e3afabf50045f27bdcc335b78a71d18a4412fc37/bridge.py)。

此次只读取 GitHub 资料并核对代码，没有执行社区附件、切换登录身份或新增 Twitch 请求。
