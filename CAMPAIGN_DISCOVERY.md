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
The latest [bounded Python channel handoff](docs/campaign-discovery/python-watch-transport.md)
records official participating-channel candidates and the existing HTTP watch
transport. AvailableDrops null/missing data now reports unknown availability
instead of becoming an empty list; silent null does not infer an integrity challenge.
Neither that correction nor transport acceptance proves server-side watch progress.
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
web session's client ID, device ID, session ID and client version. OAuth is
included once the manager's auth token is initialized; the actual request
identity must be observed to establish a match. It refreshes at 90% of the returned
token lifetime. The site's
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

Without `--browser-auth`, the app first tries its existing device-login flow.
If Twitch returns no device code, the login panel now enables its Login button;
clicking it opens Chrome and starts an isolated WEB session. This is the
fallback intended for the Windows executable. To open the browser immediately
from the command line, use `--browser-auth --browser-channel chrome`.
If the device token is valid but Twitch still rejects a campaign query, the
miner reports that browser authentication is needed instead of showing
`KeyError: device_code` or an empty campaign list.
If a valid saved token belongs to a different client, authentication stops with
an explicit error and preserves `cookies.jar`, including during shutdown. It
does not replace that token by starting a device login. This protects imported
WEB credentials; it does not import them into the browser or provide integrity.

For an isolated, no-Tk diagnostic of a user-provided WEB cookie, use
`check_campaign_auth.py` as described in [the cloud handoff](CLOUD_OPERATION.md).
It leaves the selected cookie file untouched, observes the website's dashboard,
and performs one Python control read only after that website request succeeds
for the expected identity. It uses normal browser settings and retains certificate
verification. This is a diagnostic import, not persistent authentication in the miner.
Muse's `b7ea9ce` report confirmed SDK loading/readiness and two completed integrity
POSTs returning tokens. A dashboard with Client-Integrity still failed. The earlier
empty response list was an observation gap, not evidence that no request was sent.
The probe now passively correlates issued tokens with dashboard headers, compares
issuance/use identities and records request order independently of parsing order.
It records and stops on relevant SDK/GQL HTTP 429, including pending navigation,
without sending a new Python control after that signal or automatically retrying.
Token values remain in memory and are discarded at cleanup; only match results
and relative times are reported. Unknown/cancelled body parsing is not reported
as an absent token. The final SDK snapshot is explicitly timed after network freeze.
See [the binding evidence and next verification](docs/campaign-discovery/twitch-integrity-binding.md).
Muse also observed an SDK-domain 429 and an aborted fetch. Their effect on the
rejection is unproven. This update does not establish a fix or a server-side cause.
The subsequent Muse run following the `a623b25` handoff stopped at an SDK document
429 after about seven seconds, before any comparable dashboard/issuance responses
were captured. It confirms the stop condition, not campaign functionality. Do not
repeat the unchanged probe; preserve existing reports and establish supported
platform access and continuous-runtime conditions as described in the cloud handoff.
The earlier redacted JSON did not retain issuance identity or token equality data,
so it cannot retrospectively answer the correlation question.

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

Run `python -m pytest -q tests` for the offline suite (155 passed, including cookie
preservation and the standalone diagnostic's identity checks, read-only batch
extraction, network lifecycle observation, issuance/use correlation, cancellation,
429 stop conditions, redacted output, AvailableDrops null/empty handling, and the
Login-button fallback from an unavailable device code to Chrome).

The packaged Windows candidate (`4cbbb84`) was launched in read-only
`--check-campaigns` mode. Clicking Login opened the installed Chrome channel
(154.0.8037.93), but Twitch displayed “目前不支持您的浏览器”. No successful
dashboard response was captured, so this did not verify standalone login or
campaign discovery in the executable. The user then confirmed that a separately
and normally launched Chrome can open the campaigns page. This narrows the
failure to the Playwright-launched session path, but does not isolate whether
automation control, the temporary profile or another session difference triggers
Twitch's page. Do not change browser protections or disguise automation to get
past the page. The fallback opens Chrome and leaves the existing `cookies.jar`
untouched; this run does not establish a supported end-to-end login. Remaining:
identify a supported browser-session integration and verify actual Tk inventory
rendering, plus a longer run through natural token expiry. An account with no
eligible campaigns can legitimately return `[]`.
