# 纯 Python 鉴权验证：2026-09-30

用户希望 Muse 在没有可用浏览器网络的 Linux 环境中全天候运行，接下来优先验证
Python 直接访问 Twitch 的方案。浏览器常驻是当前候选实现的依赖，尚未证明是所有
可能方案的必要条件；同样，不能把提供网页 Cookie 当作纯 Python 长期运行已经可用。

## 本次新增实测

测试运行在 Windows，使用当前已获授权的官方 Twitch WEB 登录态。
仅通过受支持的浏览器网络观察取得该网页的身份信息；OAuth 的 WEB 签发方已校验。
Python 使用 aiohttp 自行请求服务器，没有导入网站的完整性脚本。

输入身份字段为 Authorization、Client-Id、User-Agent、X-Device-Id、Client-Session-Id、
Client-Version 和 Referer。纯 HTTP 组未提供任何浏览器取得的 Client-Integrity。

1. Python 直接 POST `https://gql.twitch.tv/integrity`，携带上述身份和新的
   Client-Request-Id。WEB 与 ANDROID_APP 两组都得到 HTTP 200、非空 token，
   以及约 3611 秒后的 expiration。
2. Python 用各组刚取得的 token 查询 ViewerDropsDashboard，均得到 HTTP 200，
   但活动字段为 null，存在 GraphQL errors 和 `challenge.type == "integrity"`。
   两组均在首次拒绝后停止，没有声称完成自然刷新测试。
3. 正常网页自身的活动查询返回 160 个活动且没有错误。
4. 对照组保持相同 WEB 身份与 Python aiohttp 请求，只改为正常网页实际使用的
   Client-Integrity，返回 160 个活动且没有错误。

因此，本次验证中 Python 请求活动本身可以工作，但 Python 裸调 integrity 接口
取得的 token 没有被活动查询接受。`/integrity` 返回 200 和 token 不是验收标准，
必须继续验证受保护的实际查询。

这尚未证明所有无浏览器方案都不可能，也未定位 token 的具体服务端判定规则。
不要猜测或宣称某个 scope、请求头或本地算法已解决它。

## 对 Muse 后续工作的影响

- 保留 Python 作为运行主体，不再把修通 Chrome 当作当前唯一主线。
- 用户已表示愿意提供自己账号的网页 Cookie，可在用户授权的专用存储中进行导入和
  恢复验证。不要索取聊天中的明文凭据，不要提交 Cookie、OAuth 或完整性 token 到 Git。
- 用户提供的是另一个 Linux 会话时，先验证实际签发方和响应；本机 WEB 对照结果
  不等于 Muse 的账号、出口和 Cookie 已经验证通过。
- 仅转用 WEB OAuth，或复制完整的身份请求头，再裸调 integrity，在本次测试中仍不足。
  正常网页取得的 token 有效期有限，复制一次不能解决全天候刷新。
- 如继续验证 Muse 已有的 `manual_watch.py`，使用服务器返回的真实进度和领取后的
  状态作为证据。主程序本地推算的分钟数不能证明 Twitch 已计入进度。
- 频道级 AvailableDrops 只能作为受限候选来源。上游已说明它会漏掉离线频道、
  优先列表之外的游戏，并可能被全频道徽章活动遮住其他活动。
  不能用它承诺全量新活动发现，也不能默认推断账号已关联。
- 当前没有已经实测可交付的“仅给 Cookie、完全无浏览器、全量发现并长期刷新”方案。
  下一阶段需要证明有效完整性凭据的正常获取和持续刷新，或验证足以覆盖用户目标的
  其他活动发现来源，然后再完成真实进度、领取、重启恢复和全天候验收。

上游来源：[PR #1174 的关闭说明及维护者评论](https://github.com/DevilXD/TwitchDropsMiner/pull/1174)。
该 PR 未合并，其频道发现实现不能直接当作完整修复。

## 脱敏结果

- [Python 自行申请 token 的结果](docs/campaign-discovery/twitch-http-integrity-check-result.json)
- [Python 使用正常网页 token 的对照结果](docs/campaign-discovery/twitch-http-integrity-control-result.json)

临时本地测试服务已退出，浏览器网络记录已停止，凭据仅在内存使用后清理。
本次没有观看或领取，也未修改生产鉴权代码。此前 56 项离线测试的结果不变；
这些新证据不能将候选状态提升为 Linux 或全天候运行通过。
