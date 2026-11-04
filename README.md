# keelson-connector-safety-supervisor

The onboard **safety supervisor**. It turns `emergency_request` into `safety_state`: a STOP
latches, a RESET is fenced, the local lockout sits above every lease, and a processed-request
ledger guarantees that a request never executes twice.

- **Protocol:** keelson `protocols/emergency_request.yaml` and `messages/payloads/SafetyState.proto`
  (keelson PR #332).
- **Design:** crowsnest-dev `docs/COMMAND-ARCHITECTURE.md` §3.

> **Status: core only. It actuates nothing.** The safety actuation interface (propulsion inhibit,
> safe state, local hold, shutdown, a physical output; MAVLink force-disarm for the drone case) is
> an interface with a logging implementation. So is the observer of legacy e-stops. Each real one
> is its own change, with its own safety review. Never run this against a real vessel or a shared
> network.

## What it does, and only that

Minimal, and simpler than what it protects (protocol inv. 15):

- **STOP** is unconditional on state. Each valid STOP adds a `SAFETY_TRIP` cause, latches
  STOPPED and bumps the generation. Who may trip comes from the supervisor's own policy:
  humans at a site by default, processes not.
- **RESET** works by `command_profile`:
  - `operational`: only the OVERALL_COMMAND assignee may RESET;
  - `test` / `legacy_single_operator`: the conn holder may RESET;
  - in every profile it must name the current fence (`expected_stopped_fence`), or it is
    REFUSED_STALE_FENCE;
  - it clears only the causes marked `clearable_remotely`, and only when none of the other
    kind remain. Otherwise it answers PARTIAL, clears nothing and moves no fence;
  - **a RESET re-arms nothing.**
- **The ledger.** The state and the ledger entry commit in **one** sqlite transaction. The
  result is published after the commit and marked published after that. A seen `request_id`
  returns its recorded result. At boot, every unpublished result is republished and every
  retained request without a result is processed (I8).
- **A lost store is never CLEAR.** Missing, corrupt, a schema it does not know, or a state that
  fails its hash all mean RECOVERY_REQUIRED with `SAFETY_STATE_LOST` (I17). The way out is the
  local `--recover-locally`, which gives STOPPED, never CLEAR; then a RESET.
- **The fence.** `safety_generation` is a `(term, generation)` pair minted here and only here:
  - the term is a random 128-bit value drawn when the store is initialised or recovered, and
    **persisted**, so a restart with a valid store keeps it;
  - the generation bumps on every change of the cause set.

  (crowsnest-dev #82 says the term "bumps on every restart". The protocol and `Fence.proto` say a
  term changes only when the store is reinitialised, and the protocol is normative.)
- **The physical stop** adds `PHYSICAL_STOP`. While pressed, no RESET clears it. Releasing it
  makes it clearable, and then a RESET clears it.
- **The local lockout** adds `LOCKOUT` and sets `local_lockout`. No remote role clears it.
  Clearing it locally removes its own cause and re-arms nothing (I14). In this cut the stop and
  the lockout are **simulated** from a local file, and the state says `lockout_simulated`.
- **The command_assignment projection** (operational RESET) **fails closed** with
  REFUSED_ASSIGNMENT_PROJECTION_UNVERIFIED unless all of these hold:
  - the feed was heard within `feed_ttl_seconds` on this process's monotonic clock;
  - the manager's liveliness token is present;
  - the issuer is the manager the policy names;
  - the RESET names the advertised assignment id and revision.

  A restart starts not-live until the feed is heard again. A STOP never reads it (I7′).

It **never** reads the gate, the navigator or mavlink2keelson, and it never asks the authority
manager or a station anything.

**Independence level L1:** its own process and Zenoh session, sharing the router and the box with
what it protects (§3.4). Nothing here claims more.

## Keys

| Reads | Writes |
| --- | --- |
| `{realm}/@v0/{vessel}/pubsub/emergency_request/{vessel}/*` (live + a boot GET of the retained ones) | `…/safety_state/{vessel}` (on every transition, and a heartbeat) |
| `…/command_assignment/{vessel}` (operational profile) | `…/safety_processed_request/{vessel}/{request_id}` |
| `…/command_authority/{vessel}` (the conn lease; test and legacy profiles) | liveliness: source + `safety_state`, `safety_processed_request` |
| the authority manager's source liveliness token | |

The retained requests need a **persistent** router storage on `emergency_request/**`. A `memory`
storage does not answer wildcard queries, so a STOP published while the supervisor was down would
never be seen (protocol `not_solved`).

## Policy: a maintenance act

`SafetyPolicySnapshot` is a local JSON file (`config/policy.example.json`). It is adopted into the
store only with `--accept-policy`; otherwise the persisted copy rules, and a different file is
logged and ignored. With no adopted policy, a STOP still lands on the default policy and every
RESET is refused.

## Running (development only)

```bash
safety-supervisor2keelson -r rise -e <vessel> --state-dir ./state \
  --policy config/policy.json --accept-policy --recover-locally "<who>@vessel" \
  --sim-inputs ./inputs.json --connect tcp/<router>:7447
```

## Developing before the alpha

The pin is `keelson==0.6.0a332.devN`: the alpha of keelson #332, still to be cut. Until it is
published, `uv sync` cannot resolve it. Develop in a plain venv against the #332 worktree's SDK
**without committing that**:

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e ../keelson.worktrees/emergency-request/sdks/python pytest ruff
.venv/bin/python -m pytest -q          # unit + e2e (e2e: loopback, multicast scouting off)
.venv/bin/ruff check .
```

When the alpha exists: set the exact version in `pyproject.toml`, run `uv lock`, and use
`uv sync --locked --extra dev`.

## Not in this cut

- The real safety actuators and the legacy `vehicle_lifecycle/v1 emergency_stop` observer
  (`EXTERNAL_LEGACY_ESTOP`).
- Hardware stop and lockout inputs: the `operational` profile's lockout is a hardware inhibit
  that this only observes.
- Envelope trips.
- Deleting the retained request keys once answered.
- `maintenance_state`.
