# Muse：网页领取查询候选实验已结束

2026-09-30。Muse 此前在 `296e797` 上完成只读核对：与原尝试绑定的账号和目标匹配，
当时 Inventory 明确为 60/60、`isClaimed:false`。这提供了原领取尝试后的服务器状态，
可以确认那次核对时目标尚未领取；此前 `7614144` 的领取前库存不能替代这个证据。

## 0d30837 实测结果与当前交接

Muse 已执行此候选，报告路径为
`probe-reports/web-claim-0d30837-20260930-2235.json`。以下依据用户转述的脱敏结果，
本机没有独立读取 Muse 上的完整报告：

- 退出码 1、`claim_unconfirmed`，共 5 次请求、1.7 秒，`watch_sends:0`。
- 领取前目标为 60/60、`is_claimed:false`，真实领取实例存在，
  `preconditions_met:true`，本次服务端明确返回 `account_link_state:true`。
- 固定候选提交后记录 `response_challenge:{present:true,type:"integrity"}`。
  这是此次领取请求收到 integrity challenge 的直接证据；不能补作旧响应的类型证据。
- 遇 challenge 即停止，没有领取后的 Inventory 读数。领取前的 false 不能当作
  候选提交后的状态；本轮仍为未确认，原记录和候选记录均保留。

**候选实验已结束，下面的命令与边界仅作历史记录，不再执行。** 更换为这份源码派生
hash 没有获得领取确认，也没有验证服务器接受了该查询定义。当前不再试其他 hash、
切换 Client-Id、重新领取或为重复提交创建新候选／状态目录。

此前 Muse 浏览器曾两次取得 HTTP 200 的 integrity token，但携带 token 的 dashboard
请求仍被挑战。因此缺失的是**被 Twitch 接受、身份绑定一致且能够正常续期的完整性
会话**，不能简化为“从未拿到 token”或“补一个头即可解决”。本入口没有这套会话，
纯 Python 观看达标也不能证明领取和完整活动发现已可用。

用户已联系 Muse 平台，正在等待浏览器出口和持续运行条件的回复。当前保留 Cookie、
两份 journal、探针报告和本地改动；不启动领取／浏览器复跑或常驻领取任务。
收到可执行的出口方案后先评估是否改变已有阻塞，再决定有界的正常网页验证。
即使恢复联网，也仍须证明受保护请求接受会话、正常续期以及领取后库存确认，
不能直接宣称 7×24 小时方案完成。

## 这次具体改变什么

新增入口 `check_web_claim.py` 固定使用源码派生的 persisted hash：

```text
3b8a08f5a35dc95d7de229dea731a106a9aa9fa2e84c8f693fd159943f273e4f
```

仍加载现有 WEB CookieJar，使用同一验证流程、同一个
`DropsPage_ClaimDropRewards` operation、同格式的 `input.dropInstanceID`，实例 ID
必须重新从当前 Inventory 取得。实际发送的领取请求只改变 persisted hash；不更换
Client-Id、OAuth 签发类型、设备身份，不生成 integrity token，不调用浏览器或观看逻辑。
原 `constants.py` 的 ClaimDrop 常量保持不变，入口也不提供任意 hash 或任意 query 参数。

入口继续调用原 `BaseDrop._claim()`，传输适配器验证原 operation 与已授权实例 ID，
然后仅为这一个固定候选替换线上请求的 hash。它不是通用重试工具。

## 源码依据与限制

定位与官方 bundle 链接见 [领取 challenge 源码记录](claim-challenge-source.md)。
[离线复现脚本](derive-claim-hash.cjs) 使用已下载包自身的 Apollo cache 转换、
persisted-query link 和 GraphQL printer，重建 hash 输入；不访问 Twitch。
当前领取 AST 的输入为 346 字节，包含 `__typename` 和末尾换行，稳定得到上述 hash。

阳性控制仍未完成：本地下载集缺少 Dashboard 所依赖的 `rewardCampaign` 模块 `403690`，
不能重建并比对已从网页请求抓到的 Dashboard hash；“旧领取只选择 status”的假设也没有
匹配旧 hash。脚本因此报告 `offline_derived_control_unverified`、退出码 2。
这不否定已完成的源码复现，但不能把它写成服务器已接受候选，更不能归因自动化检测。

## 历史执行方式（已结束，勿复跑）

`0d30837` 交接时使用的命令如下，仅供解释既有报告：

```bash
DISPLAY=:99 python check_web_claim.py --cookies cookies.jar --campaign-name "Rust Isles AR" --proxy-env HTTPS_PROXY
```

`HTTPS_PROXY` 是当前平台代理环境变量的名字；按实际名称替换，不传密码或完整代理 URL。
入口启动时读取当前代理，保留 TLS 验证，不将轮换凭据写死。不要先重复运行 finish 观看
实验，也不要在此命令上添加其他目标、扫描或定时任务。

如果原尝试使用过自定义 `--state-dir`，此处必须传入**同一个原状态目录**。
默认仍是 `~/.local/state/twitchdropsminer/finish-drop`。

## 前置条件与请求边界

- 总时长最多 240 秒，最多 8 次请求；不发送观看事件、不打开浏览器、不签发 token。
  只读取既有登录的有效性、Inventory，并允许下面这一次候选领取。
- 必须存在原 `journal.json`，账号、campaign ID、名称、drop ID 均与它绑定；首次候选要求
  原记录的 outcome 为 `attempted`。缺失、畸形、不匹配或原记录已确认都不会获得新尝试。
- 重新读取 Inventory，要求用户 ID 明确匹配、目标唯一、服务器分钟满足正的要求、
  `isClaimed:false`、真实非空 `self.dropInstanceID`，且在原领取窗口范围内。
  关联状态或 `hasPreconditionsMet` 明确为 false 会停止，未知状态不会伪造成 true。
- 临提交前再验证身份和读取 Inventory。实例 ID 缺失时不等待、不拼接、不用旧值代替；
  已经领取则直接记录 `already_claimed`，不发送 mutation。
- 一次候选提交之后，只以同一目标 Inventory 的 `self.isClaimed:true` 确认领取；
  最多三次只读复查，后续两次间隔 15 秒。HTTP 200、原方法返回 True 和目标消失均不足以
  认定已领取。已领取后的分钟重置不否定明确的 `isClaimed:true`。
- 任何 challenge、429、身份或网络错误立即停止，不自动重试。新结果会仅记录 challenge
  是否存在和受限的类型分类，不输出原 challenge 内容；旧报告未保存的类型不能补猜。

## 两份记录仍属于同一个目标

原 `journal.json` 在此次入口中保持不变。固定候选使用同一状态目录、同一进程锁下的
`web-query-claim-v1.json`，并在发送 mutation 前持久记录“已尝试”。

只要候选记录已经存在，无论 outcome 是什么，随后启动这个入口都只做身份与 Inventory
两次读取，核对原候选结果，不发送第二次领取。第一次候选在响应丢失或进程退出时也遵守
这一规则，不因结果未知重新获得提交机会。候选结果不会覆盖原尝试的历史证据。

禁止编辑、删除两份 journal 或锁文件，也不能换状态目录、复制／克隆旧状态来重新尝试。
日志包含用于账号绑定的私有 user ID，但不保存 OAuth、Cookie、代理凭据或真实领取实例 ID；
不要提交这些状态文件或贴进聊天。正常报告使用脱敏 JSON。

## 历史报告格式与状态含义

将本次完整脱敏标准输出保存在持久的 `docs/campaign-discovery/probe-reports/` 下一个新文件，
另记录实际提交、时间和退出码，不覆盖原报告，也不要为了保存输出再执行一遍。
重点回传 `state`、`phase`、`error`、`query`、`inventory_checks`、`claim` 和 `requests`。
`watch_sends` 应始终为 0；若出现 challenge，保留 `claim.response_challenge` 的实际摘要。

- `claim_confirmed`：同一目标 Inventory 明确已领取；看 `claim.previous_attempt` 区分
  新候选确认与仅核对既有候选。
- `already_claimed`：提交之前已经领取，本轮没有新领取。
- `claim_unconfirmed`：候选已记录一次尝试，但尚未获得明确库存确认；不能再次提交。
- `failed`：前置条件或读取失败，查看具体阶段，不解释为空库存或领取成功。

前两种状态退出码为 0，其余为 1。后续活动发现、持续运行和 VM 恢复仍未完成验收。
用户唯一可用的运行机器仍是 Muse 云主机，7×24 小时目标尚未完成。
