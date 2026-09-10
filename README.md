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
participant, the Python bridge associates each challenge and result with that
participant, and only the Python process knows the sidecar key.

The integration has deliberately narrow seams:

- [`ProjectBody.html`](ProjectBody.html) loads one project-wide browser adapter
  on real participant pages, but not simulated pages.
- [`_static/motion-attestation.js`](_static/motion-attestation.js) collects
  signals and uses Uproot's `beginSubmit()` and `api2()` APIs to attest before
  every Uproot page submission.
- [`prisoners_dilemma/__init__.py`](prisoners_dilemma/__init__.py) delegates its
  authenticated `api2()` endpoint to the Python bridge. Nothing in the game or
  payoff logic depends on attestation.
- [`motion_attestation/`](motion_attestation/) contains the Python bridge and
  the small Node-side adapter. It is the reusable integration boundary.
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
analyzer and are discarded. Each participant instead receives:

- `motion_attestation_checks`
- `motion_attestation_failed_checks`
- `motion_attestation_flagged`
- `motion_attestation_last_score`
- `motion_attestation_min_score`
- `motion_attestation_records`, containing bounded page-level summaries with
  score, flags, duration, and counts by signal type

The example's `pipeline()` exports the check count, failed-check count, and
minimum score alongside the ordinary prisoner's-dilemma data. The complete
bounded summaries remain available in player data.

Attestation is observational and fail-open in this example. A timeout, sidecar
failure, or suspicious score is recorded when possible, but never changes page
progression, matching, or payment. Decide and document a different policy
explicitly if your study requires one.

## Use the pattern in another Uproot project

1. Copy `motion_attestation/`, `_static/motion-attestation.js`,
   `ProjectBody.html`, and `run.sh`.
2. Add the npm dependency and scripts from `package.json`, retain the npm
   lockfile, and add `httpx` to the Python dependencies.
3. Delegate each participating app's modern `api2()` endpoint:

   ```python
   from fastapi import Request
   from fastapi.responses import Response

   from motion_attestation import handle_request as handle_motion_attestation


   async def api2(
       request: Request,
       player: PlayerType | None = None,
   ) -> Response:
       return await handle_motion_attestation(__name__, request, player)
   ```

4. Add whichever aggregate fields you need to your pipeline or digest. Keep
   attestation out of the experimental treatment and payoff code unless that
   coupling is an intentional part of the design.

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
| `UPROOT_HOST` | `0.0.0.0` | Uproot bind address |
| `PORT` | `8000` | Uproot HTTP port |

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
