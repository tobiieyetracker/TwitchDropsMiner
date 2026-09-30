# Integrity 启动依赖与下一轮观测

本记录复用 2026-09-30 已下载的 Twitch 前端源码，没有重新调查 query hash，
也没有据此宣称 Muse 的实际失败原因已经定位。

## 源码证据

- [已分析的网页传输 bundle](https://assets.twitch.tv/assets/21956-b5a5c32dd4e02f095dd2.js)
  的 SHA256 为 `06605B5A126FD12DD1AF1E1318284CAEEA356D6DE37756D949220133755262FE`。
- `loadKPSDK()` 创建的脚本来自 `k.twitchcdn.net`，路径以 `/p.js` 结束。
  它等待 SDK 的 load/ready 事件，源码中默认超时为 15 秒。
- `fetchIntegrityResponseWithRetries()` 在 `shouldUseKasadaSDK` 为真时，先等待
  `loadKPSDK()`，再调用 `rawFetchIntegrityResponse()` 发出 POST。
  因此 SDK 未就绪可能阻止 integrity HTTP 请求；这只是依赖关系，不是 Muse 的实测根因。
- 管理器是否启用、是否使用 SDK 和是否开机获取 token 都有网页自身的配置条件。
  没观察到 POST 不能单独证明是哪一个条件或依赖出了问题；不要强改网页开关。
- 同一 bundle 中 `static-cdn.jtvnw.net` 的用途包括默认头像、直播预览和封面图片。
  不能把这个域名上发生的所有失败都称作 JS 失败。
- 已保存的活动页 HTML 中，66 个带 src 的 script 元素均指向 `assets.twitch.tv`。
  HTML 的 SHA256 为 `18A556E17C831951D8D98C10864F6AFC67A38AE7385600F1D879A76DE0EAACCE`。
  这个统计不包括页面运行后动态插入的 SDK 或其他脚本。

## Muse 已验证的边界

用户转述的 `656add8` 探针结果：WEB token 验证通过，网页请求的 OAuth、WEB client-id
及 user_id 均匹配，说明这次 Cookie 导入成功。活动字段为 null，响应带明确的
integrity challenge，请求没有 Client-Integrity；Python 对照按设计未执行。

该版本的 `integrity_responses` 是响应体观测，不是请求计数。空数组不能证明
“60 秒内请求从未发出”。旧版还只保留前 20 条资源失败，且把 CDN 主机记为 other，
无法从该输出分辨失败的图片和 SDK 脚本。这两项观测缺口已修正。

浏览器报告的 ERR_CERT_AUTHORITY_INVALID 与另一个 TLS 探针的握手超时是两项观测。
在确认客户端、目标、代理路径和测试时间一致前，不把它们改写成同一个错误或因果结论。

## 下一轮看什么

按 CLOUD_OPERATION.md 的同一条命令运行更新后的探针一次，保留正常 TLS 校验。

1. `network_summary` 查看 `k.twitchcdn.net` 的 script 及后续资源，以及
   `assets.twitch.tv` 的 script。区分 HTTP 错误、网络失败、完成下载和尚未结束。
   `static-cdn.jtvnw.net` 若只出现 image 失败，不能以此认定完整性脚本没有加载。
2. `page_status` 是观察窗口结束后的只读快照：SDK 脚本元素是否存在、全局对象是否存在、
   SDK 自己报告是否 ready。脚本元素存在或 HTTP 200 均不等于脚本已成功运行。
3. `integrity_network` / `integrity_requests` 分别记录本浏览器上下文观察到的请求、
   响应头、下载完成、失败和仍等待的状态。没有 HTTP 响应与没有发出请求可以区分。
4. 若确认 SDK 的正常请求因证书或连通性失败，先修复相应域名的正常网络/信任链，
   使用平台提供的受支持配置；不要关闭证书验证、替换 SDK 或伪造 ready 状态。
5. 若依赖已下载且 SDK 就绪，仍没有 integrity 请求，再调查当前网页的初始化和
   challenge 处理是否执行。不要预先把它归因于“低信任 token”或自动化检测。

这轮只定位已有失败；官网活动查询成功之前不执行 Python 对照，仍不启动矿机。
它不验证自然刷新、进度、领取或全天候恢复。
