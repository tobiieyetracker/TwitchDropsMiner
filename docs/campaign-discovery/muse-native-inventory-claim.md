# Native inventory claim validation

Status: candidate, not yet accepted by Twitch on Muse. This is a bounded test of
one already-earned reward, not unattended claiming or a deployed miner.

First Muse run, `75e9ec5`: all 239 focused offline tests passed. The live run
stopped during Python identity validation with `ClientConnectionError`, before
browser launch, Inventory, clicking or a claim request. Child exit was 1;
the wrapper reported exit 0. The parent cleaned its relay/processes, old cookies
and journals were unchanged, and no native journal was created. The archived
report is `native-claim-75e9ec5-20260930-2346.json` on Muse. This is not evidence
of Twitch rejecting the native claim path.

The runner now permits separate transport proxy parameters: Python reads can
use the platform upstream already known to work, while Chromium uses the relay
to that same upstream. This removes an unnecessary Python relay dependency;
it does not establish why the first connection failed or change the egress.

The previous Python attempts had no accepted integrity context. The latest
source-derived hash returned an explicit integrity challenge. Changing a hash
again does not address that result. The official inventory page has its own
claim button and ordinary challenge recovery, independent of a successful
ViewerDropsDashboard response. This test exercises that path.

## Scope and prerequisites

- Muse's existing WEB cookie file; the file and both old claim journals remain
  unchanged. Cookies are imported with their original domains and paths.
- The account, campaign and drop must match both existing journals in
  `~/.local/state/twitchdropsminer/finish-drop/`.
- Fresh Python Inventory and the webpage's successful Inventory must agree on
  the target and real server-issued claim instance. Full watched minutes,
  server-confirmed account link and claim readiness are required.
- No dashboard success is needed. No watching, generated integrity tokens,
  SDK replacement, certificate bypass, stealth settings, or proxy rotation.
- The existing local relay is an explicitly bounded diagnostic dependency;
  this change does not establish that it is a supported permanent egress path.

## What the script does

`check_native_claim.py` opens `/drops/inventory` once in a temporary browser
context. It matches the campaign's exact official link, then a unique reward
name/image derived from the same drop's Inventory benefits. It clicks one
native **Claim Now** button. DOM ambiguity stops the test.

The browser retains its own transport, request headers and payload. The guard
allows source-confirmed read operations and only the one exact claim instance;
it blocks unknown persisted operations and unrelated mutations. It does not
replace browser requests with `route.fetch()` or Python mutations.

Before forwarding a claim, the script durably reserves its budget in the fixed
`native-inventory-claim-v1.json`, under the original process lock. The first
request and at most one website-initiated recovery must have identical payloads
and matching OAuth, Client-Id, User-Agent, X-Device-Id, Client-Session-Id and
Client-Version. Recovery additionally requires an explicit first-response
integrity challenge with no non-null mutation result, plus a newly issued,
unexpired token actually observed after that response. The retry must use that
exact token. Unknown outcomes never authorize a resend.

An existing native journal makes all later invocations read-only reconciliation.
Never delete or rename any journal to obtain another attempt. A crash after
budget reservation intentionally consumes the attempt even if sending is
uncertain.

The test stops for a relevant SDK/GQL HTTP 429 and closes the browser promptly.
It does not ignore 429, reload the page, or keep retrying. A blocked operation or
incomplete page is a test limitation, not proof that the server rejected a claim.

After an attempted claim, the browser must close successfully before up to
three read-only Inventory confirmations. No confirmation is issued after 429
or an unconfirmed shutdown. Only the exact target's returned `isClaimed:true`
produces `claim_confirmed`. A successful HTTP response, native success message,
token issuance or response status alone is insufficient.

## One controlled Muse run

First fast-forward to the commit containing this document, preserving all
untracked/local files. Run the focused offline tests. Do not install a cron job.

Using the previously checked parent/relay (read current proxy credentials from
the existing environment, never write them into this command):

```bash
DISPLAY=:99 DIAG_PROBE_TIMEOUT=200 DIAG_PARENT_BUDGET=240 \
  python3 diag_probe_parent.py <existing-venv-python> check_native_claim.py \
  --cookie-file cookies.jar --campaign-name 'Rust Isles AR' \
  --channel chrome --proxy-env BROWSER_PROXY --python-proxy-env HTTPS_PROXY \
  --seconds 60
```

The runner has a 180-second internal total budget. The parent must retain its
finite deadline and process-group/relay cleanup. No simultaneous Twitch probe,
miner or extra inventory-page session should be started for this run.
The parent retains the original `HTTPS_PROXY` environment for the child and
adds `BROWSER_PROXY` for Chromium. If the selected variable is missing, the
script fails before network access. It never prints either proxy value.

Save the complete redacted JSON under `docs/campaign-discovery/probe-reports/`.
Report the exact commit, exit code, `state`, `phase`, error codes,
`website_inventory`, `native_claim_responses`, `claim`, final inventory checks,
`rate_limits`, `blocked_operations`, browser shutdown and parent cleanup. Do not
return cookies, headers, tokens, raw claim IDs, full URLs or browser storage.

Stop after this invocation. Preserve every journal and original report. A failed
or absent native mutation must not be described as successful claiming.

## Offline checks

```bash
<existing-venv-python> -m pytest -q \
  tests/test_native_claim_state.py tests/test_native_claim_dom.py \
  tests/test_native_claim_check.py tests/test_web_claim.py \
  tests/test_finish_channel_drop.py tests/test_campaign_auth_check.py \
  tests/test_browser_cookie_import.py
```

These tests cover durable budgets, restart reconciliation, identity/instance
binding, response/token timing races, wrong or ambiguous UI targets, 429 and
shutdown behavior, and post-mutation confirmation. Website transports/DOM are
simulated. They cannot establish Twitch acceptance or round-the-clock operation.
