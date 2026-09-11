# Uproot with motion-attestation

A minimal [Uproot](https://uproot.science/) project that runs
[motion-attestation](https://github.com/libcaptcha/motion-attestation) as a
loopback-only Node sidecar. The experiment is the standard prisoner's dilemma;
the only additions are the attestation integration and three exported summary
fields.

## Architecture

```text
participant page
  collector.js
      |
      | same-origin, player-authenticated uproot.api2()
      v
prisoners_dilemma.api2()
  Python bridge
      |
      | loopback HTTP + per-process random key
      v
Node motion-attestation sidecar
```

The browser never talks to the sidecar directly. Uproot authenticates the
participant, the Python bridge cryptographically binds each challenge to that
participant and app, and only the Python process knows the sidecar key. The
binding is stateless: no challenge is written to participant storage.

The integration has deliberately narrow seams:

- [`ProjectBody.html`](ProjectBody.html) loads one project-wide browser adapter
  on participant pages.
- [`_static/motion-attestation.js`](_static/motion-attestation.js) collects
  signals and uses Uproot's `beginSubmit()` and `api2()` APIs to attest before
  every Uproot page submission.
- [`prisoners_dilemma/__init__.py`](prisoners_dilemma/__init__.py) delegates its
  authenticated `api2()` endpoint to the Python bridge. Nothing in the game or
  payoff logic depends on attestation.
- [`motion_attestation.py`](motion_attestation.py) is the reusable Python
  bridge and typed Appendmuch ledger API. [`motion-attestation/`](motion-attestation/)
  contains the small Node-side adapter.
- [`run.sh`](run.sh) creates an ephemeral proxy key, starts the sidecar, waits
  for readiness, starts Uproot, and shuts both processes down together.

## Run locally

Install Python 3.12 or newer, [uv](https://docs.astral.sh/uv/), and Node.js 18
or newer. Then run:

```bash
uv sync
npm ci
./run.sh
```

Any arguments after `./run.sh` are passed unchanged to `uproot run`; for
example, `./run.sh --host 127.0.0.1 --port 9000`. The wrapper does not choose
Uproot host or port defaults.

Debian 13's standard `nodejs` 20 and `npm` 9 packages are sufficient; this
example does not require NodeSource, nvm, or another third-party Node build.

Open <http://127.0.0.1:8000/room/test/>. The prisoner's dilemma needs two
participants, so open the room in a second private browsing session as well.

`npm ci` installs the exact version and tarball recorded in
[`package-lock.json`](package-lock.json). At startup, `prepare-client.mjs`
copies the package's browser-only collector into Uproot's generated project
static directory after checking its SHA-256 digest. That generated file and
`node_modules/` are intentionally ignored by Git.

## What is stored

The bridge does not retain raw mouse positions, keystroke timings, touch data,
scroll data, or sensor readings. Raw signals pass through Uproot to the local
analyzer and are discarded.

Each valid analyzer verdict becomes one immutable `MotionAttestationEntry` in
a session-wide Uproot model. The model identifier is created lazily in the
session field `motion_attestation_log`; before the first valid verdict that
field is absent. Uproot's model API stores each entry as a separate Appendmuch
event, so recording a new page does not rewrite or duplicate earlier pages.

No attestation result, counter, score, or challenge field is written to the
player. The typed ledger is the single source of truth. Pending challenges are
returned as HMAC-authenticated envelopes bound to the session, participant,
and app; Python validates and unwraps an envelope before proxying it to the
sidecar. The sidecar remains responsible for challenge expiry and one-use
consumption.

`assessment()` and `assessments()` dynamically reduce ledger entries into an
immutable `Assessment` value:

| Assessment attribute | Type | Meaning |
| --- | --- | --- |
| `checks` | `int` | Number of completed analyzer verdicts with a valid score |
| `failed_checks` | `int` | Number of those verdicts for which `cleared` was false |
| `flagged` | `bool` | True when at least one verdict did not clear; a derived property |
| `last_score` | `float \| None` | Most recent score, or `None` when there is no verdict |
| `mean_score` | `float \| None` | Mean score across all verdicts, or `None` when there is no verdict |

`cleared` means that the score met
`MOTION_ATTESTATION_SCORE_THRESHOLD`. Despite its name,
`failed_checks` counts suspicious analyzer verdicts, not technical failures. A
sidecar outage, timeout, malformed response, or page without a completed verification
does not appear in the ledger. Therefore, zero checks means **no verdict was recorded**;
it must not be interpreted as a successful attestation.

Each ledger entry contains:

```python
MotionAttestationEntry(
    pid=PlayerIdentifier(...),        # participant identity
    app_name="prisoners_dilemma",
    page_index=2,                     # zero-based index in player.page_order
    cleared=True,                     # score met the configured threshold
    score=0.875,
    flags=[],                         # analyzer reasons, if any
    duration_ms=5123.0,               # browser-reported collection duration
    signal_counts={                   # counts only; never the raw signals
        "m": 120, "c": 2, "k": 0, "s": 4, "tc": 0,
        "ac": 0, "gy": 0, "or": 0, "ev": 128, "bc": 2,
    },
)
```

The ledger query returns `(entry_id, verified_at, entry)` tuples. `entry_id` is
a UUID and `verified_at` is Appendmuch's server-side Unix timestamp in seconds;
ordering is determined by the append log, not by a browser clock.

Assess one participant when only one result is needed:

```python
from motion_attestation import assessment


result = assessment(session, player, app_name="prisoners_dilemma")
print(result.checks, result.failed_checks, result.flagged, result.mean_score)
```

For a digest or pipeline, scan the ledger once and look up each participant:

```python
from motion_attestation import Assessment, assessments
from uproot.types import PlayerIdentifier


by_player = assessments(session, app_name="prisoners_dilemma")
for player in session.players:
    pid = PlayerIdentifier(sname=session.name, uname=player.name)
    result = by_player.get(pid, Assessment())
```

Read the immutable page-level records through the integration helper. Both
filters are optional, so the same helper can read the whole session ledger:

```python
from motion_attestation import read_entries


for entry_id, verified_at, entry in read_entries(
    session,
    app_name="prisoners_dilemma",
    player=player,
):
    print(entry.page_index, entry.score, entry.cleared, verified_at)
```

The example's `pipeline()` exports the dynamically derived check count,
failed-check count, and mean score alongside the ordinary
prisoner's-dilemma data. Those export columns are not player fields; the
append-only ledger remains the sole persisted attestation data.

The session's Digest view is a live example of reading these fields. Its
[`digest()`](prisoners_dilemma/__init__.py) projection and
[`AdminDigest.html`](prisoners_dilemma/AdminDigest.html) display only the
participant identifier and motion-attestation summaries—never the
prisoner's-dilemma choices or payoffs. “No verdict” is shown separately from
“Cleared” and “Flagged.”

Attestation is observational and fail-open in this example. A timeout, sidecar
failure, or suspicious score is recorded when possible, but never changes page
progression, matching, or payment. Decide and document a different policy
explicitly if your study requires one.

## Use the pattern in another Uproot project

1. Copy `motion_attestation.py`, `motion-attestation/`,
   `_static/motion-attestation.js`, `ProjectBody.html`, and `run.sh`.
2. Add the npm dependency and scripts from `package.json`, retain the npm
   lockfile, and add `httpx` to the Python dependencies.
3. Delegate each participating app's modern `api2()` endpoint:

   ```python
   from fastapi import Request
   from fastapi.responses import Response

   from motion_attestation import handle_request as handle_motion_attestation


   async def api2(
       session: SessionType,
       request: Request,
       player: PlayerType | None = None,
   ) -> Response:
       return await handle_motion_attestation(__name__, session, request, player)
   ```

4. Use `assessment()` for one participant or `assessments()` for a one-scan
   digest/pipeline projection. Keep attestation out of the experimental
   treatment and payoff code unless that coupling is intentional.

If an app already uses `api2()`, route requests bearing the
`X-Motion-Attestation-Action` header (or for which `is_request(request)` is
true) to `handle_motion_attestation()` and leave the app's other API behavior
unchanged.

## Configuration

| Variable | Default | Purpose |
| --- | ---: | --- |
| `MOTION_ATTESTATION_SCORE_THRESHOLD` | `0.5` | Minimum score considered cleared |
| `MOTION_ATTESTATION_CHALLENGE_TTL_MS` | `60000` | One-use challenge lifetime |
| `MOTION_ATTESTATION_PORT` | `35001` | Loopback sidecar port |

`MOTION_ATTESTATION_URL` and `MOTION_ATTESTATION_PROXY_KEY` are internal
process settings created by `run.sh`; they are not browser configuration.

The sidecar keeps challenges in memory. Deploy Uproot and its sidecar together
as one supervised web process, as the included `Procfile` does. If traffic is
spread across multiple instances, requests between challenge creation and
verification need instance affinity or a shared challenge store.

## Security, privacy, and licensing

Behavioral attestation is probabilistic evidence, not proof of identity or
humanity. Participants should be told what interaction and device signals are
processed, and collection should be covered by the study's consent and data
governance process.

The sidecar listens only on `127.0.0.1`, requires a timing-safe comparison of a
random per-process key, accepts bounded payloads through a player-authenticated
Uproot endpoint, and never exposes motion-attestation's signed token to the
browser. Responses are marked `no-store`.

This example's own code is licensed under the
[0BSD license](LICENSE). `motion-attestation` is an external MIT-licensed npm
dependency; its notice is retained in
[`licenses/motion-attestation.txt`](licenses/motion-attestation.txt). A Git
submodule is intentionally not used: the dependency is published through npm,
whose lockfile pins both its version and package integrity, while this repo
contains only the Uproot-specific adapters.
