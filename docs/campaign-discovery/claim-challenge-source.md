# 领取 challenge：离线网页源码核对

2026-09-30。源码分析仅使用此前下载的 Twitch 官方 JS，本地未发 Twitch 请求。
`constants.py` 的领取 hash 未改；原领取 journal 不删除、不重置。

## Muse 原报告的证据边界

`7614144` 实验已达到同一目标 60/60，并记录一次领取尝试。已核对的 JSON 为
`phase:"claim"`、`error:"gql_challenge"`。最后的 `claim_inventory` 是提交之前的库存；
程序在 challenge 处停止，没有提交之后的 Inventory 复查。因此该条 `is_claimed:false`
不能说明领取之后仍未领取。旧报告没有记录 `challenge.type`，目前也不能将它精确写成
“integrity challenge”。HTTP 200 不改变上述边界。

随后 Muse 已完成 `296e797 --reconcile-only`：两次请求核对原账号和目标，Inventory
仍返回 60/60、`isClaimed:false`。这提供了领取尝试之后的当前状态，尚未确认领取成功；
它没有补回旧 challenge 类型。用户转述的报告为
`probe-reports/reconcile-296e797-20260930-2230.json`，本机没有独立读取 Muse 上的文件。

新的单次候选入口只对齐下载网页源码的领取 hash，具体见
[Muse 网页查询候选](muse-web-claim-candidate.md)。它使用原账号的全新库存资格检查，
在同目录、同进程锁下单独记录候选尝试，原 journal 保留。这不是通用重试开关；
候选记录一旦存在，再启动也只能核对，不能再次提交。

## 官方源码和定位方式

本地副本都位于仓库父目录 `.investigations`，未纳入本次代码提交：

- [Drops 页面源码](https://assets.twitch.tv/assets/pages.drops.components.drops-root-4318b7132d246a556b3c.js)：
  本地 `twitch-drops-root.js`。
- [网页传输与 integrity manager](https://assets.twitch.tv/assets/21956-b5a5c32dd4e02f095dd2.js)：
  本地 `twitch-assets/21956-b5a5c32dd4e02f095dd2.js`，下称 `21956`。
- [Apollo 与 GraphQL 库代码](https://assets.twitch.tv/assets/49198-87edf11f696f172878f7.js)：
  本地 `twitch-assets/49198-87edf11f696f172878f7.js`，下称 `49198`。

文件为压缩单行 JS。以下偏移是 Python 以 UTF-8 解码后字符串中的零基字符索引，
不是字节位置；结合 webpack 模块编号或搜索串定位，避免依赖无意义的“第 1 行”。
这些定位对应已保存的副本，不表示重新确认过线上最新构建。

## 领取 operation 与 hash

`twitch-drops-root.js` 模块 `820599`、偏移 `81227` 定义的 mutation 仍为
`DropsPage_ClaimDropRewards($input: ClaimDropRewardsInput!)`。页面 hook 约偏移 `49900`
传入 `dropInstanceID`；`21956` 模块 `664865`、偏移 `11936` 的 `AR` 将其包装为
`{variables:{input:e}}`。所以实际变量结构仍是：

```json
{"input":{"dropInstanceID":"<Inventory 返回的真实实例 ID>"}}
```

该 mutation 选择 `status`、`isUserAccountConnected`、`dropType` 下的活动信息、
`error.code` 和 `error.message`。这份下载源码没有硬编码领取 hash，而是在请求链动态计算。
本地矿机 `constants.py` 的 hash 是
`a455deea71bdc9015b78eb49f4acfbce8baa7ccbedd28e549bb025bd0f751930`。

离线复现使用下载包自身的 `InMemoryCache.transformDocument` 和 GraphQL printer，
在不提供网络或应用启动能力的 Node VM 中取得 transformed AST 的打印文本，再以
Node 标准 SHA-256 计算，结果是：

```text
3b8a08f5a35dc95d7de229dea731a106a9aa9fa2e84c8f693fd159943f273e4f
```

复现链：`21956` 偏移 `166465` 的 `graphqlCacheConfig` 未关闭 `addTypename`，
偏移 `414441` 创建该 cache；`49198` 的默认配置在偏移 `688333` 为 `addTypename:true`，
模块 `225558` 执行转换；模块 `80928`、偏移 `810649` 的 Document printer 包含末尾换行；
模块 `25931`、偏移 `793311` 的 persisted-query link 计算 `sha256(print(query))`。
必须保留自动加入的 `__typename` 和最后的换行，否则会得出其他 hash。

复核脚本 [derive-claim-hash.cjs](derive-claim-hash.cjs) 可对既存副本离线复现：

```bash
node docs/campaign-discovery/derive-claim-hash.cjs ..
```

脚本运行下载包中的 persisted-query link，其传输端替换为内存 observer，实际参与 hash
的打印文本为 346 字节。两次实现路径得到同一值。已观察过的 Dashboard hash 阳性控制
仍未完成：本地缺少它依赖的 rewardCampaign 模块 `403690`，没有伪造空 fragment。
“旧领取查询仅选择 status”的假设也未匹配旧 hash，不能据此声称恢复了历史 AST。
脚本因此报告 `offline_derived_control_unverified`、退出码 2，而非线上验收通过。

**这是可复现的源码派生值，尚未线上验证。** 它与旧常量不同不能证明此前 challenge
的根因是旧 hash，也不能保证被服务器接受。固定候选入口只验证这一处差异，主矿机
及旧诊断仍使用原常量；不会因此自动重放、刷新 token 或切换身份。

## 网页的正常 challenge 处理

`21956` 偏移 `408561` 检查 `extensions.challenge.type`；类型为 `integrity` 时，
偏移 `409078` 调用 `fetchNewToken("gql-challenge")`，随后带 `Client-Integrity` 重放。
偏移 `407352` 的 `replayOperation` 保留原 operation 的 extensions、name 和 variables，
通过网页自己的 GQL fetch 参数 POST。这个通用分支没有排除 mutation。

网页 GQL 参数构造在偏移 `411821`：Client-Id、X-Device-Id、Client-Version、
Client-Session-Id 与 OAuth 使用当前会话；按开关加入已保存的 Client-Integrity，
另有可选 Trusted-Twitch-Session。偏移 `369198` 的 integrity 签发同样使用该会话的
client/device/session/version/OAuth；此前源码已确认其 SDK 加载和约 90% 生命周期刷新。
这说明官方网页具备领取 challenge 的恢复流程，不证明 Muse 当前环境能够完成它。

`twitch-drops-root.js` 偏移 `51254` 附近的领取按钮判断进度、已领取、时间及前置条件；
偏移 `50420` 附近要求领取结果存在、无 `error` 且状态被接受，然后刷新数据。
在本地已下载源码可见范围中，未发现该类普通 timed drop 的另一条可直接替代领取路径。

已有尝试的服务器状态核对已经完成；现在以独立、限次的候选验证请求定义差异。
通用 challenge 仍不能自动解释为 integrity；候选再次被挑战后立即结束，不展开 hash
枚举、Client-Id 切换、浏览器探针或循环提交。
