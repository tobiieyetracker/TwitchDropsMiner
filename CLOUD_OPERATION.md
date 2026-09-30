# Muse：全天候运行目标与当前下一步

最新状态：Muse 已收到并验证用户提供的 WEB token，经本地转发器也能加载 Twitch，
但报告网页自身的活动查询仍被 integrity 拒绝。Muse 随后纠正证书判断：example.com
由 Hatch 签发，www.twitch.tv 和 gql.twitch.tv 则报告 GlobalSign 证书，撤回 Twitch
遭 MITM 的解释。“检测到自动化浏览器”也尚未证实。下一步运行下述独立诊断入口。

用户希望以 Python 为运行主体。请同时阅读
[纯 Python 鉴权实测](PYTHON_AUTH_VALIDATION.md)：Python 裸调 integrity 得到了 token，
但活动查询仍拒绝；同一 Python 请求使用正常网页的 token 返回 160 个活动。
尚无已验证的纯 Python 完整性凭据获取和长期刷新方案。

最终目标是在用户现有、仅有 Xvfb 的 Linux 云电脑上持续发现适用活动、累计观看进度、
自动领取、切换活动，并在断网或进程重启后恢复。用户已经明确授权这个运行目标。
`--check-campaigns` 仅用于前期只读检查，不能作为完整交付。

当前仍是候选实现。代理修正、离线测试或网页可达都不能证明全天候运行已完成。

## Muse 已报告的事实

以下来自用户转述的 Muse 实测，未由 Windows 端重新执行：

- Ubuntu 24.04.5 LTS / x86_64 / Python 3.12.3；早期验证提交为 `d0c2a9e`，
  后续已确认在 `aa6b215` 上测试代理修正。
- `DISPLAY=:99` 下原 40 项离线测试通过；Windows 当前 78 项通过，
  不能视作 Muse 已运行新增测试。
- Chrome for Testing 154.0.8037.92 安装于 `/opt/chrome-linux64`，
  `/opt/google/chrome/chrome` 是对应软链接；`channel="chrome"` 可以找到并启动浏览器。
- 用户没有可交互的桌面、VNC/noVNC、浏览器接管或平台端口预览/转发功能。
  不要再重复让用户确认这些入口。Xvfb 能跑浏览器不等于已完成首次用户登录。
- 早期保存的 token 是 SMARTBOX 签发；最新报告中用户已提供有效的 WEB token，
  Muse 已验证签发客户端为 WEB，并写入 `cookies.jar`。不要继续把它当成 SMARTBOX。
- 代理认证已用独立的 username/password 字段测试，`proxy_auth: true`；
  Chrome 直接经该代理访问普通网站和 Twitch 仍报 `ERR_EMPTY_RESPONSE`。
- Python 本地转发器路径现在可以加载 Twitch，页面自身发起了 20 个 GQL 请求；
  Muse 报告其中的活动查询被 integrity 拒绝。后续证书检查纠正了此前 Twitch 证书
  被替换的判断：example.com 为 Hatch，两个 Twitch 域名为 GlobalSign。
- “代理根据 Chrome 进程拒绝 TCP”仍是解释假设；可观察事实是 Chrome 直接连接失败、
  Python/转发路径可用。“浏览器自动化特征使 token 信任度不足”也尚无隔离变量的对照。
- Muse 报告 `~` 之外的数据会在 VM 重启后清空，且代理密码会轮换。
  程序、会话存储和恢复所需文件应放在持久的用户目录；启动时读取当前代理配置。
- Linux 的 WEB 活动发现、真实进度、领取、自然刷新、重启恢复均未验证。

## 当前应做的验证

现有矿机已经由 Python 请求 GQL、由网页取得完整性凭据，不需要重写这个结构。
新脚本 `check_campaign_auth.py` 单独验证这条链路，不导入 Tk、不启动矿机或观看/领取。
它只读用户明确指定的本地 `cookies.jar`，验证 WEB 签发方，再将其中 auth-token
导入新的临时浏览器上下文。它不导入默认浏览器资料，不修改原 Cookie 文件。

在现有 Python 环境和仓库目录运行；`BROWSER_PROXY` 代表现有环境变量的名称，
其值应指向已经验证可供浏览器使用的转发器地址，按实际名称替换。
Python 校验和浏览器使用同一个代理设置；上游密码轮换仍由实际转发器按平台配置处理。

```bash
DISPLAY=:99 python check_campaign_auth.py --cookie-file ./cookies.jar --channel chrome --proxy-env BROWSER_PROXY --seconds 60
```

`--cookie-file` 只接受本程序 aiohttp 格式、可信的本地 Cookie 文件；不是浏览器插件
导出的 JSON。省略 `--proxy-env` 表示直连，不会自动使用代理环境变量。
脚本使用正常 Playwright 配置，不添加 stealth 或禁用证书校验的参数。
移除实验中额外的证书忽略参数属于恢复正常校验，不能据此宣称已找到反自动化规则。

输出为脱敏 JSON，退出码 0 仅表示本次官网活动读取及 Python 对照都成功：

- `web_token_valid`：token 有效且签发方为 WEB；不等于浏览器已经使用它。
- `dashboard_responses`：官网响应的 OAuth/客户端/账号匹配结果、活动字段类型和数量、
  是否携带完整性头、是否收到明确的完整性拒绝。静默 `null` 不被标成已证明的 integrity。
- `integrity_responses`：网页自身完整性请求的 HTTP 状态和是否返回 token；最多记录 20 条。
- `resource_failures` / `page_errors`：前 20 条资源网络/HTTP 失败及脚本异常计数。
  Twitch 主站和 GQL 证书正常不等于所有页面资源都加载成功，应检查 assets.twitch.tv 等。
  不输出资源 URL、脚本错误正文或请求头。
- `python_dashboard`：仅当官网查询成功、身份匹配且 Cookie 未变时，Python 才复制该
  请求的身份和完整性头，单独发送一次 ViewerDropsDashboard。不会重发原批次的其他操作。
  若官网始终失败，此项不会执行；`no_accepted_website_dashboard` 连同前述状态用于定位。

请回传 JSON 和退出码，不提供凭据。重点先区分登录身份不匹配、资源加载失败、
网页已取得完整性 token 但仍被拒绝，以及网页通过而 Python 失败。
没有受控对照时，不给 token 贴“低信任”标签，也不从单个错误推断自动化检测。

当前只有诊断脚本实现了一次性导入；矿机的 `--browser-auth` 仍使用空白临时上下文。
诊断成功后再把已验证的导入方式接入矿机，并实现持久化、自然刷新、真实进度和领取。
这次探针不会验证自然过期或无人值守恢复。

公开 HTML 目前也不是已验证的替代来源。已保存的官方活动页 HTML 中没有
ViewerDropsDashboard 或 dropCampaigns 字段；已分析的前端从 GQL 加载活动。
若发现其他公开页，需提供其实际包含的 campaign ID、有效期和覆盖范围，并继续核实
用户关联/进度/领取所需接口。不能把活动宣传信息当作完整的账户活动数据。

若 Muse 平台没有可用的受支持出口，则现有环境还不能承担已验证的全天候矿机。
可选择在网络正常的 Linux 主机常驻矿机，再由 Muse 管理；这也是待部署和验收的方案，
不是已经验证成功的迁移。不要继续要求用户提供更多 Cookie 来替代完整性问题的验证。

## Cookie 保留修正

此前普通启动默认 ANDROID_APP。若读到有效 WEB token，签发方不匹配分支会清空
Cookie 并删除原文件，再启动设备登录。现在对此明确报错并保留原文件，不自动更换登录。
失败后的 shutdown 也不覆盖未通过验证的保存会话；真正过期的 token 仍可正常重登，
用户主动退出登录仍会删除 Cookie。

这项保护本身不增加 WEB Cookie 导入、浏览器会话持久化或完整性恢复能力。
Cookie 保护提交的 7 项回归测试使用临时文件和模拟验证响应；当时 63 项离线测试通过。
新增独立探针后为 78 项通过，仍需 Muse 真实执行。

## 已完成的代理认证检查（历史步骤）

旧代码将整个代理 URL 放入 Playwright 的 `server` 字段。
检查已安装 Playwright 的 `ProxySettings` 和 `normalizeProxySettings` 实现可知：
`server` 被规范化为协议、主机和端口，URL 内的用户名、密码不会变成认证字段。
例如 `http://user:password@proxy:3128` 的认证信息会丢失。

新代码 `browser_proxy_settings()` 将认证拆到独立的 `username`/`password`，
解码 URL 编码的认证信息，保留代理协议和 IPv6 地址，并对不支持的带认证 SOCKS
配置给出明确错误。56 项离线测试通过，包含代理传参和错误输出检查。
Muse 已首次用独立认证字段实测，修正生效但 `ERR_EMPTY_RESPONSE` 仍存在。
以下探针步骤保留供配置变化后复验；不要在没有新变化时反复运行。

保留 Muse 本地改动，更新 `codex/campaign-discovery` 分支。工作树干净时可执行
`git pull --ff-only origin codex/campaign-discovery`，有改动时先检查并保留它们。
激活现有 Python 环境，使用当前可供 curl/Python 正常联网的代理环境变量：

```bash
DISPLAY=:99 python check_browser_proxy.py --channel chrome --proxy-env HTTPS_PROXY
```

`HTTPS_PROXY` 是变量名示例；若实际代理存于 `https_proxy` 或其他变量，替换名字即可。
不要把带密码的代理 URL 写在命令行、聊天或报告里。变量中的 URL 协议描述代理自身，
不能因为访问的是 HTTPS 网站就把 HTTP 代理的 `http://` 改成 `https://`。

脚本使用正常可见浏览器和新的匿名上下文，不需要用户登录，不读取 `cookies.jar`，
也不启动矿机。它分别访问 example.com 和 Twitch 活动页，输出浏览器版本、
是否传递代理认证、HTTP 状态或经过筛选的网络错误码，然后退出。
HTTP 成功只证明页面可达，不证明已通过 Twitch 登录或 integrity。
退出码 0 表示两个页面导航均获得 2xx/3xx；退出码 1 表示仍有失败。

这个探针与矿机共用代理转换函数，但矿机本身读取 `settings.proxy`，
不会自动从探针选定的环境变量填入设置。探针成功后要确认矿机设置的是同一代理。

不要输出 Proxy-Authorization、完整请求头、带凭据 URL 或浏览器完整网络转储。
最新状态已进入网页活动查询被拒绝，后续按“当前应做的验证”推进。

## 代理恢复后仍需完成的工作

这些是后续实现和验收要求，不是当前已有功能：

1. **WEB 会话导入与恢复。** 用户已提供有效 WEB token，独立探针已有临时导入功能，
   当前矿机尚未接入该功能，也没有专用会话持久化。需要完成导入和恢复验证，
   不再重复要求不可用的云端交互桌面。不要读取默认浏览器个人资料。
   凭据不得出现在聊天、日志或 Git。已有 WEB token、只改 Client-Id 或裸调 integrity
   均不能作为已解决完整性鉴权的依据。
2. **自动刷新与会话变化。** 跨过自然刷新期限后仍能读活动和详情；验证 OAuth 变化时
   的受控恢复，防止静默切换账户。无法恢复的登录挑战需要明确状态，不能无限失败重试。
3. **真实进度和领取。** 新活动进入矿机后，使用已关联账号，按用户的游戏选择验证进度
   实际增加、领取成功及后续 Inventory 状态，并验证频道失效/活动结束时能够切换。
   到这一阶段移除 `--check-campaigns`；用户已经授权自动观看与领取。
4. **运行设置。** 当前默认 `PriorityMode.PRIORITY_ONLY`。需要结合用户想参与的游戏
   设置优先列表或合适的优先模式，否则程序可以正常发现活动却不开始获取进度。
5. **进程恢复。** 当前 `main.py` 在错误结束后保留 GUI，等待用户关闭；监督器可能看见
   进程仍在而不重启。先实现无人值守情况下可观察的失败状态、退出和恢复，
   再配置有退避的进程监督，避免仅添加重启服务就宣称可全天候运行。
   Muse 还需把 Chrome 等重启会丢失的依赖放入持久目录或提供可靠的重建步骤；
   检验 VM 本身是否能持续运行及被平台停止后的重新启动方式。进程重启不等于 VM 恢复。
   每次启动重新读取平台提供的代理配置；不要把会轮换的密码写死在脚本或服务定义里。
6. **持续验收。** 先记录一次完整的发现、进度和领取，再运行至少 24 小时，覆盖自然
   刷新、网络恢复和一次受控进程重启。记录脱敏日志和实际覆盖项。
   24 小时样本也不等于保证未来零中断；未覆盖的新活动出现、领取或重启不能填“通过”。

报告应把各阶段的“通过、失败、未验证”分别列清。遇到确实缺少可行首次授权路径时，
明确说明这个剩余条件；不得把只读检查完成或进程存活当作已完成用户的全天候任务。
