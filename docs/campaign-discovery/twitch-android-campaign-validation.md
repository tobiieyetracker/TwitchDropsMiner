# ANDROID_APP campaign query verification — 2026-09-30

The requested combination of a SMARTBOX-issued OAuth token and the ANDROID_APP
client ID was reproduced against live Twitch, using the same account as the
successful browser session. It returned a null campaign list with both the old
repository query and the query currently used by the website. Adding an integrity
token issued for that SMARTBOX OAuth / ANDROID_APP identity still returned null.
No working request satisfying that exact token constraint was found.

A working alternative retains the ANDROID_APP client ID, but uses a **WEB-issued
OAuth token**, a freshly issued integrity token for the Android client ID and that
web OAuth, and the complete matching web-session identity headers. This returned
160 campaigns using both hashes, including through Python aiohttp. The tests ran
on Windows, not on the user's Linux runtime or network.

## Verified token provenance

Both tokens were checked at Twitch's `/oauth2/validate` endpoint. They belonged to
the same user. The endpoint reported these issuer client IDs:

- SMARTBOX: `ue6666qo983tsx6so1t0vnawi233wa`.
- WEB: `kimne78kx3ncx6brgo4mv6wki5h1ko`.
- GQL client ID held constant in the successful/unsuccessful Android comparison:
  `kd1unb4b3q4t58fwlpcbzcbnm76a8fp`.

The backend reason for the token distinction was not exposed. The evidence points
to the OAuth session's issuing client/authorization type; it does not establish a
specific missing OAuth scope or a Linux-specific problem.

## Current request body

The live page sent `fetchRewardCampaigns: true`. Using `false` was also verified
and omits the unrelated reward-campaign query. The body is available in
[twitch-android-campaigns-query.json](twitch-android-campaigns-query.json):

```json
{
  "operationName": "ViewerDropsDashboard",
  "variables": {"fetchRewardCampaigns": false},
  "extensions": {
    "persistedQuery": {
      "version": 1,
      "sha256Hash": "69750554e0a81492f2d343558f84bdf3e324767650a2dbb6e79a3c629b4548cf"
    }
  }
}
```

POST to `https://gql.twitch.tv/gql`; read `data.currentUser.dropCampaigns`.
The old `c16bb890cc8ce7647a96ee69cd313d423a378a3dedadf630a1017cde18975feb`
hash also returned 160 campaigns with the successful identity. A hash change was
not needed to turn that identity into a successful request.

## Sufficient headers, verified together

```http
Client-Id: kd1unb4b3q4t58fwlpcbzcbnm76a8fp
Authorization: OAuth <WEB-issued OAuth token>
Client-Integrity: <token issued for this Android client ID and web identity>
X-Device-Id: <device ID from that web session>
Client-Session-Id: <session ID from that web session>
Client-Version: <build ID from that web session>
User-Agent: <actual user agent of that browser>
Content-Type: application/json
```

Do not mix identity fields from different sessions. An ablation retaining only
Client-Id, OAuth, Client-Integrity, User-Agent and Content-Type failed integrity.
The remaining fields were tested together; no claim is made that each individual
one is independently necessary.

## Integrity issuance observed in the web bundle

The token is returned by Twitch's server, not calculated from the persisted-query
hash. The site's integrity manager calls `POST https://gql.twitch.tv/integrity`
through its normal browser fetch implementation, using Authorization, Client-Id,
X-Device-Id, Client-Session-Id, Client-Version and a fresh Client-Request-Id. The
browser supplies User-Agent and runs the site's integrity SDK handling.

For the working Android variant, copy the web identity's headers, retain its
WEB-issued OAuth, and set Client-Id to the Android client ID before obtaining the
integrity token. Inside the already authorized Twitch page, the tested operation is:

```js
// identityHeaders contains Authorization, Client-Id, X-Device-Id,
// Client-Session-Id and Client-Version, all from the identity described above.
const response = await window.fetch("https://gql.twitch.tv/integrity", {
  method: "POST",
  headers: {
    ...identityHeaders,
    "Client-Request-Id": crypto.randomUUID().replaceAll("-", "")
  }
});
const integrity = await response.json();
// Keep integrity.token in memory and use it as Client-Integrity.
// integrity.expiration is an epoch timestamp in milliseconds.
```

The manager refreshes at approximately `0.9 * (expiration - Date.now())` and fetches
a new token when GraphQL returns `extensions.challenge.type == "integrity"`.
An arbitrary standalone HTTP request to `/integrity`, or a token copied across
identities, is not established as sufficient by these tests.

Sources inspected:

- [Drops page bundle](https://assets.twitch.tv/assets/pages.drops.components.drops-root-4318b7132d246a556b3c.js),
  module 474137 contains the ViewerDropsDashboard query AST, the optional Boolean
  fetchRewardCampaigns variable and currentUser.dropCampaigns selection.
- [Web transport bundle](https://assets.twitch.tv/assets/21956-b5a5c32dd4e02f095dd2.js),
  rawFetchIntegrityResponse, fetchAndStoreIntegrityToken and the integrity challenge
  handler. The currently loaded page's script URL matched this bundle.
- The current query hash and the true-valued variables were captured from the
  authorized live page's own network request, not inferred from a name or an old
  issue comment.

## Reproducibility and limits

The sanitized Python results are saved in
[twitch-android-http-check-result.json](twitch-android-http-check-result.json).
The one-shot local adapter performed real aiohttp requests, returned no
credentials and was closed after its run. That environment-specific diagnostic
adapter is not distributed; use the source-run instructions in
[LINUX_VALIDATION.md](../../LINUX_VALIDATION.md) for the candidate implementation.
The successful detail query also returned the five expected Sonic Rumble Party
watch requirements (15, 30, 60, 90 and 120 minutes) and an unlinked-account state.

The user explicitly approved the test device authorization. The new test access
token was revoked through `/oauth2/revoke` (200); its subsequent validation returned
401. No existing Twitch for TV connection was disconnected. OAuth/refresh/integrity
tokens and device codes were kept in memory and were not written to these files.

The successful wire request is reproducible, but this does not certify a Linux
browser login, a browser-free integrity generator or cross-host credential reuse.
The verification did not change the miner candidate implementation. These
sanitized records were subsequently added to its branch for the Linux handoff.
