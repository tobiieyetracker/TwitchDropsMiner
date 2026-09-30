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

## 下一步的被动观测

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
