# Campaign discovery compatibility patch

Status: candidate patch. Authenticated live validation of the Python campaign
transport, discovery, details, integrity recovery and refresh passed on 2026-09-30
using an authorized Codex in-app browser session. The standalone Playwright/Edge
sign-in flow did not complete: the user reported being unable to sign in. This
is not yet a verified end-to-end fix for the normal application startup flow.

For the next Linux run, follow [the Muse validation handoff](LINUX_VALIDATION.md).
The intended delivery is now unattended discovery, progress and claims on the
user's Xvfb-only Linux host; see [cloud operation requirements](CLOUD_OPERATION.md).
That is not implemented or verified end to end by this candidate.
The subsequent [pure Python integrity test](PYTHON_AUTH_VALIDATION.md) received
tokens from a direct HTTP integrity request, but those tokens were rejected by
the dashboard. The same Python transport succeeded with the normal web page's
token. Browser-free issuance and ongoing refresh remain unverified.
The later [SMARTBOX / ANDROID_APP comparison](docs/campaign-discovery/twitch-android-campaign-validation.md)
still returned null with SMARTBOX-issued OAuth, including with fresh integrity.
WEB-issued OAuth plus matching web identity and integrity returned campaigns with
ANDROID_APP. The candidate browser mode uses the captured WEB client identity;
it does not implement a SMARTBOX fix or switch the web session to ANDROID_APP.

## What changed on Twitch

The public web bundles retrieved on 2026-09-30 still use `ViewerDropsDashboard`
and `currentUser.dropCampaigns`. There is no replacement campaign endpoint in
those bundles. The current web transport handles
`extensions.challenge.type == "integrity"` by fetching an integrity token and
replaying the challenged operation with `Client-Integrity`.

The web integrity manager calls `https://gql.twitch.tv/integrity` using the
same OAuth token, client ID, device ID, session ID and client version as the
web session. It refreshes at 90% of the returned token lifetime. The site's
own fetch wrapper handles its browser checks. An arbitrary string in the
`Client-Integrity` header does not provide this session.

Sources:

- [Drops page bundle](https://assets.twitch.tv/assets/pages.drops.components.drops-root-4318b7132d246a556b3c.js)
- [Web transport and integrity manager](https://assets.twitch.tv/assets/21956-b5a5c32dd4e02f095dd2.js)
- [Upstream investigation and unmerged fallback](https://github.com/DevilXD/TwitchDropsMiner/pull/1174)
- [Maintainer's explanation of the SMARTBOX limitation](https://github.com/DevilXD/TwitchDropsMiner/issues/1165#issuecomment-5757268242)

## Using the patch from source

Install the optional browser dependencies into the miner's environment:

```text
python -m pip install -r requirements-browser.txt
python main.py --browser-auth --check-campaigns
```

On Windows the default browser is installed Microsoft Edge. Elsewhere it is
installed Google Chrome. Override with `--browser-channel chrome`,
`--browser-channel msedge`, or `--browser-channel chromium`. For the last option,
install Playwright's browser first with `python -m playwright install chromium`.

Sign in directly on Twitch in the browser opened by the miner. Keep that browser
and its Twitch tab open. The new context is isolated from the default browser
profile and is not saved for reuse. It does not load or overwrite the existing
`cookies.jar`. Sign-in is needed again after restarting the miner.

Authenticated proxy URLs in `settings.proxy` are split into Playwright's `server`,
`username` and `password` fields. Previously the URL was passed only as `server`,
which lost embedded credentials during Playwright's proxy normalization. This fix
does not establish the cause of every Linux `ERR_EMPTY_RESPONSE`. To test the
actual proxy without login, use `check_browser_proxy.py` as described in the cloud
operation handoff. The miner reads `settings.proxy`; the probe explicitly reads
the environment variable selected with `--proxy-env`.

`--check-campaigns` fetches inventory, discovers campaigns and retrieves their
details once, then stops. It does not start maintenance, a watch loop, a websocket
or claims. Its messages also appear on stdout when a console is available, with
counts for in-progress, dashboard, newly discovered and account-linked campaigns.
The inventory tab and the reported campaign count can be inspected before
closing the application. Remove `--check-campaigns` to use the normal miner flow
after verification.

The default app/device authentication remains available without `--browser-auth`.
If Twitch rejects that client or its campaign queries, the error now points to
the web session option instead of showing `KeyError: device_code` or an empty
campaign list.
If a valid saved token belongs to a different client, authentication stops with
an explicit error and preserves `cookies.jar`, including during shutdown. It
does not replace that token by starting a device login. This protects imported
WEB credentials; it does not import them into the browser or provide integrity.

## Recovery and limits

- All GraphQL requests in browser mode use the same captured web identity.
- Expired tokens are refreshed through the signed-in Twitch page. Concurrent
  requests rejected with the same token share one refresh.
- Only challenged read operations are replayed, once. Successful entries in a
  batch, including claims, are not part of that integrity replay.
- A real empty campaign list remains a valid result. Null campaign data,
  missing user data and changed response schemas produce explicit errors.
- Interactive challenges are reported to the user. The patch does not complete
  CAPTCHA, change browser protections or implement Twitch's challenge scripts.
- Changing accounts or closing/navigating away from the browser stops use of
  that session; the miner does not silently switch accounts.
- This adds a browser dependency and browser memory overhead. Packaged builds
  need separate packaging work for Playwright; use the source build here.

## Live validation on 2026-09-30

The new chat passed the browser's normal site-access and CDP permission checks.
An already signed-in in-app tab provided the web identity. A temporary loopback
test adapter supplied only the browser page/cookie APIs and a GUI inventory sink;
the repository's `_AuthState`, aiohttp request transport, `gql_request`,
`recover_challenges`, `TwitchWebSession.refresh_integrity`, `fetch_inventory` and
campaign/drop constructors executed against live Twitch. API responses were not
replaced by fixtures. The adapter did not start or attach to any other browser.

- The existing dashboard hash in this repository was accepted. Twitch's current
  page used a different hash, but changing the repository hash was unnecessary.
- Inventory contained 0 in-progress campaigns. Dashboard returned 160 campaigns;
  124 ACTIVE/UPCOMING campaigns with a game reached the miner inventory and its
  GUI inventory sink. All 124 were new relative to Inventory.
- All 124 detail responses were checked against the constructed campaign account
  links and drop IDs/required minutes: 17 linked, 107 unlinked. The expanded Rust
  web UI also showed the matching campaign names and a successful account link;
  Sonic Rumble Party showed its expected account-link action and matching watch
  requirements (15, 30, 60, 90 and 120 minutes).
- Omitting `Client-Integrity` once and clearing the test session's cached token
  deliberately elicited a real `IntegrityCheckFailed`/integrity challenge. The
  patch fetched a new token and replayed the read successfully. A second complete
  inventory fetch again constructed 124 campaigns.
- Three integrity fetches succeeded in the final run. The last exercised the
  expiry branch by resetting the local refresh deadline; this was not an hour-long
  natural-expiry test. The run made 2 Inventory, 5 dashboard and 248 detail
  operations, and no watch/claim requests.

The [sanitized live result](docs/campaign-discovery/twitch-live-check-result.json)
contains counts and public campaign examples, not OAuth, cookies, integrity tokens
or user IDs. Its `state: passed` refers to the transport/adapter test described
above, not a completed standalone or Linux validation. The temporary local broker
was closed after verification. That diagnostic adapter is not distributed or a
supported Codex sign-in option shipped with the miner.

Run `python -m pytest -q tests` for the offline suite (63 passed, including cookie
preservation through failed authentication and shutdown). Remaining:
complete a supported standalone browser sign-in and verify the actual Tk inventory
rendering, plus a longer run through natural token expiry. No root cause was
established for the failed Edge sign-in; browser protections were not changed.
An account with no eligible campaigns can legitimately return `[]`.
