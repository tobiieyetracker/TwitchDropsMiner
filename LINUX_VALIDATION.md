# Muse：Linux campaign 发现验证交接

用户已明确最终目标：在仅有 Xvfb 的 Linux 云电脑上，全天候发现活动、累计观看进度并
自动领取。本文的只读检查是前期验证，不是最终交付范围。后续任务和最新阻塞见
[CLOUD_OPERATION.md](CLOUD_OPERATION.md)，不要继续将“本次不观看、不领取”作为永久限制。

最新进展：用户已授权并向 Muse 提供 WEB token；Muse 报告验证有效、已写入 Cookie。
转发器路径能加载网页但活动查询仍被 integrity 拒绝，MITM/TLS 指纹原因尚未证实。
先按 CLOUD_OPERATION.md 的当前步骤推进，以下交互登录步骤仅适用于有可用桌面的环境。

请在用户实际运行矿机的 Linux 环境验证这份候选补丁，并根据真实失败修正。
先读本文、[CAMPAIGN_DISCOVERY.md](CAMPAIGN_DISCOVERY.md) 和
[客户端组合实测记录](docs/campaign-discovery/twitch-android-campaign-validation.md)，
然后检查 `web_session.py`、`gql_recovery.py`、`twitch.py`、`main.py` 和 `tests/`。
继续现有调查，不要从“换一个 hash”重新开始，也不要把离线测试当成实测通过。

## 代码与已知结果

- 仓库：<https://github.com/tobiieyetracker/TwitchDropsMiner>
- 分支：`codex/campaign-discovery`，不是 `master`。
- 上游起点：`22d0c6134f9291d1e904012c465504a22bd3f97c`。
- 候选实现提交：`42f6409a45bebca82bf03e7299a994535dbc4482`。
- 早期 40 项离线测试已在 Muse 通过；Windows 当前 63 项通过，新增测试需在 Muse 运行。
  Linux 完整发现、观看与领取尚未验证。
- Windows 上，真实 WEB 登录态配合仓库的 aiohttp、恢复和活动构造代码：
  Dashboard 160 个活动，Inventory 0 个进行中活动，124 个适用的新活动进入矿机对象；
  详情和关联状态匹配，真实 integrity challenge 后重试成功。
  GUI 当时替换为测试接收器，浏览器来自已登录的内置浏览器，未覆盖正常启动和 Tk 渲染。
- 独立 Edge 窗口的登录未完成，原因尚未确定。正常 Playwright 登录仍是待验证环节。
- SMARTBOX 签发 OAuth + ANDROID_APP，使用当前 hash 或补齐 integrity 仍返回 `null`。
  WEB 签发 OAuth + ANDROID_APP + 匹配的完整网页身份和 integrity 实测返回 160 个活动。
  这只是诊断对照；候选程序的 `--browser-auth` 使用完整 WEB 身份，保持此实现先验证。
- 160/124 是当时的活动数量，不是固定验收值。网页的游戏数量也不等于 campaign 数量。

## 先确认 Linux 环境

记录发行版、架构、Python 版本、浏览器版本和代码提交。
源码要求 Python 3.10 以上；`main.py` 在解析参数前就初始化 Tk，
`--check-campaigns` 也需要图形界面。浏览器通过 Playwright 以可见方式启动。

Muse 已确认仅有 Xvfb、没有用户可操作桌面，不要重复检查。用户已授权使用其提供的
WEB 凭据，但当前 `--browser-auth` 尚未实现导入或持久化；写入 `cookies.jar` 不会登录
该临时浏览器。后续应实现并验证专用会话导入，不读取默认浏览器资料。
若在另一个有可用桌面的环境测试当前实现，则由用户在官方页面自行登录。
仅使用不可见的 Xvfb 不等于完成了交互登录。

依赖包含 Tkinter、PyGObject/GTK 和浏览器所需的系统库。按实际发行版安装，
不要直接套用别的系统的软件包名。仓库 `.github/workflows/ci.yml` 的 Ubuntu 22.04
构建使用 `libgirepository1.0-dev`、`gir1.2-ayatanaappindicator3-0.1`、
`libayatana-appindicator3-1` 和 `python3-tk`；源码安装还可能需要 venv、
Python 开发头文件和编译工具。以实际安装结果检查兼容性。

## 安装与首次只读验证

保留已有工作目录和用户设置；在不存在的新目录克隆，或先检查已有克隆的改动。
不要用 `reset --hard`、`clean` 或删除目录来更新。

```bash
git clone --branch codex/campaign-discovery --single-branch https://github.com/tobiieyetracker/TwitchDropsMiner.git TwitchDropsMiner-campaign-check
cd TwitchDropsMiner-campaign-check
git rev-parse HEAD
python3 -m venv env
source env/bin/activate
python -m pip install -r requirements-browser.txt
python -m pip install pytest
python -m playwright install chromium
python -m pytest -q tests
python main.py --browser-auth --browser-channel chromium --check-campaigns
```

若 Playwright 报缺少系统库，再按当前平台补齐其浏览器依赖。
若已安装 Google Chrome，可改用 `--browser-channel chrome`；Linux 默认值也是 Chrome。
Chromium 和 Chrome 的正常登录效果尚未在 Linux 验证，不保证替换渠道就能解决登录问题。

请用户在矿机新开的官方 Twitch 页面自行登录，并留在 `/drops/campaigns`。
准备好后再启动程序：当前实现最多等待登录约 5 分钟。
会话是临时的，每次重启都需重新登录；它不读取或覆盖原来的 `cookies.jar`。
不要让用户在聊天里发送密码、验证码、OAuth 或 Cookie，不要上传浏览器请求头或存储。

保持 `--check-campaigns`，不启动观看或领取。正常情况下 stdout 和 GUI 应出现：

```text
Campaign sources: ... in progress, ... on dashboard, ... newly discovered. Account links: ... connected, ... not connected.
Campaign discovery check completed: ... campaigns.
```

首次检查只运行一轮，随后会关闭后台会话并保留矿机窗口供查看。
记录结果、查看库存页后再关闭窗口；进程等待用户关窗不是检查卡死。
不要为记录日志开启 `--debug-gql`、`--dump` 或完整网络转储。

## 验收与后续修正

1. 确认实际 Linux 启动、官方网页登录和 Tk 库存页渲染成功。
2. 对比同一账户网页和矿机的活动，至少找到一个原始 Inventory 不含的新 campaign
   进入矿机，核对活动 ID/名称、掉落要求和账号关联状态。
   若账户确实没有这种活动，记录验证不足，不用模拟数据补一个“通过”。
3. 区分 `null`、缺少字段、未登录、完整性拒绝和真正的 `[]`；HTTP 200 本身不代表成功。
4. 首次只读验证通过后，再验证持续刷新。现有 `--check-campaigns` 会结束会话，
   不能通过反复启动它证明同一会话的自然过期刷新。
   如需补充测试入口，让同一个 `TwitchWebSession` 保持打开，只进行低频读取，
   跨过返回的刷新期限后确认刷新及后续列表/详情读取成功。
   可仅记录过期时间、刷新次数和响应计数，不记录 token。
   Windows 已验证过真实 challenge 恢复和强制本地刷新期限；自然过期尚未验证。
5. 如果发现错误，根据实际失败修复，并运行相关测试与 `git diff --check`。
   不重放成功的领取操作，不把静默 `null` 转为 `[]`。
   Twitch 若拒绝正常浏览器登录或权限访问，保留失败证据，不绕过页面挑战或权限检查。

请报告：环境和提交、执行命令、离线测试结果、登录是否成功、网页与矿机活动数量、
新活动样例、关联状态、刷新验证方式与结果、错误摘要及代码修改。
明确区分“只读发现通过”“自然刷新通过”和“尚未验证”；本节检查不测试实际观看或领取。
用户已授权后续全天候自动观看和领取。前置验证通过后按
[全天候运行交接](CLOUD_OPERATION.md) 继续真实进度、领取和恢复验证。
不要因为首次列表成功就宣称所有 Linux 功能和长期运行均已修复。

## 已保存的证据

- [原矿机传输与恢复实测结果](docs/campaign-discovery/twitch-live-check-result.json)
- [SMARTBOX / WEB 对照结果](docs/campaign-discovery/twitch-android-http-check-result.json)
- [当前网页请求体](docs/campaign-discovery/twitch-android-campaigns-query.json)

这些文件不含凭据。当前请求体只说明 operation/hash/variables；它不能让 SMARTBOX
token 自动获得活动列表。历史结果中的 `passed` 仅指其注明的测试范围。
