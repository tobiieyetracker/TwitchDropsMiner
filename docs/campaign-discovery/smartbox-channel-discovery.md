# SMARTBOX campaign discovery fallback

`ViewerDropsDashboard` and `DropCampaignDetails` do not provide the account-wide
campaign list for the SMARTBOX login used by this miner. The fallback uses the
same channel-first approach reported by GrubDrops: query `DirectoryPage_Game`
with `DROPS_ENABLED` for configured games, then query
`DropsHighlightService_AvailableDrops` for up to ten live channels per game.
An explicit channel login can be added even if it is outside those directory
results.

```bash
DISPLAY=:99 python main.py --smartbox-auth --check-campaigns --cookie-file cookies.jar.bak --campaign-channel rainbow6
```

`--cookie-file` is accepted only with `--check-campaigns`. That mode reads the
existing cookie jar without saving to it and exits instead of starting a login
flow when the token is absent, invalid, or belongs to a different client.
This checks Inventory and the `rainbow6` channel only. To broaden the scan, add
one or more `--campaign-game "Game Name"` arguments; each adds at most ten
high-ranked channels. The fallback keeps activity sources at both campaign and
drop level, merges by campaign ID and drop ID, and uses Inventory as the source
of truth for progress and account link state. If a channel's AvailableDrops
value is null, that channel's availability remains unknown and the other
channel results are retained. Integrity and interactive challenges still stop
the scan.

Campaign source channels are added to the normal miner channel candidates, so a
broadcaster-specific drop found by the scan is not lost when its streamer falls
outside the ordinary game directory results. Inventory ACL data remains the
authoritative channel restriction when present.

This is deliberately a partial discovery method. It cannot prove that every
active Twitch campaign has been found: it only sees selected games and the
channels queried for those games, plus explicitly named channels. For a full
account dashboard, use a valid WEB identity and the matching live browser
integrity session. The Linux Muse run should first validate this SMARTBOX
fallback read-only; it must not start watching or claiming during that check.

## Research sources

- [Twitch Drops campaign report, issue #1165](https://github.com/DevilXD/TwitchDropsMiner/issues/1165)
- [Closed, unmerged SMARTBOX fallback PR #1174](https://github.com/DevilXD/TwitchDropsMiner/pull/1174)
- [GrubDrops channel-first implementation](https://github.com/aalejandrofer/GrubDrops/blob/master/internal/platform/twitch/chandisc.go)
- [KickDropsMiner API client](https://github.com/HyperBeats/KickDropsMiner/blob/main/core/api.py) uses Kick-specific campaign endpoints and browser handling; it does not expose a Twitch query that can be reused here.
