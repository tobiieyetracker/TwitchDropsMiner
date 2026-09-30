# SMARTBOX campaign discovery

`ViewerDropsDashboard` and `DropCampaignDetails` do not provide the account-wide
campaign list for the SMARTBOX login used by this miner. A bounded fallback uses
the channel-first approach reported by GrubDrops: query `DirectoryPage_Game`
with `DROPS_ENABLED` for configured games, then query
`DropsHighlightService_AvailableDrops` for up to ten channels per game. An exact
channel login can be added even when it is outside those directory results.

Live evidence found an important client difference. For `rainbow6`, one
SMARTBOX-authenticated run scanned 64 channels but found no new campaigns and
reported 14 availability results as unknown. Its GUI then waited for a manual
close and Muse killed the process, so the report's numeric exit code is null;
the campaign check itself printed its normal completion summary. A same-account
WEB-cookie control then queried that exact channel and returned the `DRON-E Chat Badge` campaign
(`3376e74e-…`) and one zero-minute drop (`7de165a8-…`), absent from the same
account's three in-progress Inventory campaigns. All four control requests were
HTTP 200; the service did not return an account-link boolean. This shows that a
channel can have a candidate which the SMARTBOX `AvailableDrops` identity did
not reveal.

For this case, `--campaign-web-cookie-file` provides a separate, read-only WEB
identity for `AvailableDrops`. Before using it, the miner validates the WEB
token and confirms its user ID matches the primary SMARTBOX identity. The WEB
cookie is never saved or replaced. Inventory, watch events and claims continue
to use SMARTBOX. If the WEB token is absent, invalid, or belongs to another
account, the candidate source is skipped; a challenge, rate limit, or failed
campaign request stops the scan rather than silently retrying under another
identity.

```bash
DISPLAY=:99 python main.py --smartbox-auth --check-campaigns --cookie-file cookies.jar.bak --campaign-web-cookie-file cookies.jar --campaign-channel rainbow6
```

`--cookie-file` is accepted only with `--check-campaigns`. That mode reads the
existing cookie jar without saving to it and exits instead of starting a login
flow when the token is absent, invalid, or belongs to a different client. The
separate WEB cookie option is read-only and same-account checked. This command
checks Inventory and `rainbow6`; configured Priority games may also be scanned.
To broaden the scan, add one or more `--campaign-game "Game Name"` arguments;
each adds at most ten high-ranked channels. Results retain sources at campaign
and drop level, merge by campaign ID and drop ID, and use Inventory as the source
of truth for progress and account-link state. A channel's null `AvailableDrops`
value remains unknown; other channel results are retained. Challenges and
rate limits stop the scan.

For a normal SMARTBOX run, pass the WEB jar explicitly when it is available:

```bash
python main.py --smartbox-auth --campaign-web-cookie-file cookies.jar
```

Campaign source channels are added to the normal miner channel candidates, so a
broadcaster-specific drop found by the scan is not lost when its streamer falls
outside the ordinary game directory results. Inventory ACL data remains the
authoritative channel restriction when present.

This remains partial discovery. It cannot prove that every active Twitch
campaign has been found: it sees selected games and the channels queried for
those games, plus explicitly named channels. The upstream report in issue #1165
also says `AvailableDrops` can expose only one campaign in some situations;
that limitation was not independently confirmed by these two rainbow6 runs.
The WEB-cookie source fixes the observed SMARTBOX visibility gap for a
channel-scoped candidate; it does not replace the WEB dashboard or guarantee a
complete account-wide campaign list. `--check-campaigns` prints the IDs, drop
requirements, link state and source channels for newly discovered campaigns.

## Research sources

- [Twitch Drops campaign report, issue #1165](https://github.com/DevilXD/TwitchDropsMiner/issues/1165)
- [Closed, unmerged SMARTBOX fallback PR #1174](https://github.com/DevilXD/TwitchDropsMiner/pull/1174)
- [GrubDrops channel-first implementation](https://github.com/aalejandrofer/GrubDrops/blob/master/internal/platform/twitch/chandisc.go)
- [KickDropsMiner API client](https://github.com/HyperBeats/KickDropsMiner/blob/main/core/api.py) uses Kick-specific campaign endpoints and browser handling; it does not expose a Twitch query that can be reused here.
