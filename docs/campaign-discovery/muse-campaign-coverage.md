# Muse 全量 campaign 验证：2026-10-01

用户明确要求先验证 Muse 是否能获取完整 campaign 列表。本轮只读，未观看或领取。

## 范围与执行

- 基于提交 `c68b800`，Muse 新建本地薄包装 `check_campaign_coverage.py`，未改旧探针。
- 复用 `check_campaign_auth.py` 的官网会话与单次 Python 对照，补充 campaign ID
  集合、双向差集、游戏/状态计数及 Inventory 差集观测；失败时不继续补请求。
- Muse 报告离线 `--self-check` 通过：list/null/missing/空列表、ID 去重、双向差集、
  分页键检查和 Inventory 解析。它不是实际服务器验证。
- 已存网页源码中的 `ViewerDropsDashboard.currentUser.dropCampaigns` 是无参数列表，
  没有 first/after/pageInfo。`fetchRewardCampaigns` 控制另一个字段，不是该列表的分页。
  验收范围是该账号的完整 dashboard 时间掉宝列表，不是全球所有活动。

## 实测结果

原报告保留在 Muse：
`docs/campaign-discovery/probe-reports/campaign-coverage-c68b800-20261001-0032.json`。
本记录来自 Codex 直接读取的 Muse 回报，不是原始 JSON 的替代副本。

- 子进程退出码 **1**；父包装器的退出码 0 不代表探针成功。
- WEB token 验证 HTTP 200，21/21 个 Cookie 导入一致。
- 捕获到一条官网 `ViewerDropsDashboard` 响应：HTTP 200、用户存在且匹配，
  但 `dropCampaigns=null`，有错误及明确 `integrity` challenge。Muse 随后从原报告
  程序化提取确认 OAuth/WEB Client 匹配，该请求没有 `Client-Integrity` 头。
- 其后 `k.twitchcdn.net` 的 SDK document 返回 HTTP 429，无可解析 Retry-After，
  探针停止。没有取得可接受的网站 campaign 列表。
- Python dashboard 对照及 Inventory 差集阶段均未执行。全量活动数、游戏数、
  集合一致性与 Inventory 之外的活动仍未验证，不填成零。
- 浏览器关闭、relay/端口清理完成；Muse 报告无残留进程。Cookie、旧 journal、
  新包装脚本与原报告均保留；未创建常驻任务、未重复实验。

**结论：本次未能获取完整 campaign 列表，不能宣称 Muse 已具备全量发现能力。**
`null` 不表示活动真的为空。之前 Inventory 查询成功也不等于 dashboard 成功；
两次属于不同 operation，不能把它们比较成同一接口的回归。
本轮记录了 dashboard challenge 与其后的 SDK 429，没有确定二者的根因或因果关系。

观测限制：新薄包装只在 dashboard 被接受后保存 query 身份。因此本轮原报告没有
实际 operationName/hash/variables 字段；不能用源码里的值代填为实际请求参数。
响应被原探针识别为 dashboard 的事实与这个元数据缺口分开记录。未为补齐字段重发请求。
