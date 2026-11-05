# keelson-connector-safety-supervisor

The onboard **safety supervisor**. It turns `emergency_request` into `safety_state`: a STOP
latches, a RESET is fenced, the local lockout sits above every lease, and a processed-request
ledger guarantees that a request never executes twice.

- **Protocol:** keelson `protocols/emergency_request.yaml` and `messages/payloads/SafetyState.proto`
  (keelson PR #332).
- **Design:** crowsnest-dev `docs/COMMAND-ARCHITECTURE.md` §3.

> **Status: a real vessel is out of scope; a Slipway vessel is not.**
> - **Actuation:** a logging actuator, or the **SIL** one (`--actuator slipway`), which stops a
>   simulated vessel. Each real interface (propulsion inhibit, safe state, local hold, shutdown, a
>   physical output; MAVLink force-disarm for the drone case) is its own change, with its own
>   safety review.
> - **Never** run this against a real vessel or a shared network.

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
  Clearing it locally removes its own cause and re-arms nothing (I14). Both the stop and the lockout
  are **simulated**, and the state says `lockout_simulated`. They come from one of two sources:
  - a local file (`--sim-inputs`);
  - **Slipway's truth record** (`--sim-truth`), which carries her `set_lockout` switch (Slipway
    ADR-0140). In SIL the simulator is the hardware: this is what the `operational` profile's
    hardware inhibit stands in for. Silence never releases it. The last reading stands, and a
    silent record is logged.

  **Both simulated inputs are refused outside the `test` profile.** The truth record is an
  ordinary key, so anyone who can publish on the bus can forge `lockout: false`. "No remote role
  clears it" (I14) therefore holds here only as far as the bus is trusted. The real guarantee is
  the operational profile's hardware inhibit.
- **A legacy stop converges** (`--legacy-observe slipway` or `mavlink:<source_id>`). A direct
  `vehicle_lifecycle/v1 emergency_stop` is seen in the vehicle's own telemetry: her truth-record
  latch, or `vehicle_armed` going false. It latches `EXTERNAL_LEGACY_ESTOP`, with these rules:
  - **Only an edge latches.** A vessel that is simply disarmed, at the berth or at boot, is not an
    emergency stop. The last value is persisted, so an edge that happened while the supervisor was
    down is caught at boot.
  - **A RESET clears it**, like a STOP's own cause (Ted, 2026-11-04). A cause that could not clear
    while she reports herself stopped would deadlock against the gate, which refuses every arm
    unless CLEAR. After the RESET she stays stopped until somebody arms her.
  - **Nothing is added while a STOP-class cause is present**, so the stop the supervisor itself
    actuated, echoed back, never doubles.
  - **On MAVLink every disarm is a stop.** ArduPilot's own auto-disarm after inactivity included:
    `vehicle_armed` cannot tell an emergency stop from a routine disarm. It fails toward more
    inhibited, and a RESET is needed before she is armed again.
  - **CLEAR means "arming permitted", not "not physically stopped".** After a RESET she can be
    CLEAR and still latched until somebody arms her (keelson #332's wording says so).
- **The SIL actuator** (`--actuator slipway`): an inhibit calls her `vehicle_lifecycle/v1
  emergency_stop` on responder `sim`.
  - **It runs on its own thread** (`ThreadedActuator`). The decision loop only records the latest
    "inhibit wanted", so `safety_state` and every result are published without waiting on a
    vessel that does not answer.
  - The worker retries every second until a reply is ACCEPTED.
  - A release calls nothing: a RESET re-arms nothing.
  - **Armed while it inhibits, she is stopped again.** Her truth record may show her latch released
    while the supervisor inhibits; somebody armed her on the legacy path, which nothing gates yet.
    The worker calls `emergency_stop` again and publishes an `alarm`. She is never left moving
    while `safety_state` says STOPPED.
  - It refuses to start outside the `test` profile, or without `--sim-truth`.
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

**The retained requests are read back every `--retained-poll-s`** (default 5 s), not only at boot.
A supervisor cut off by a partition, alive and not restarted, never hears a STOP published during the
partition; the storage keeps it, and the poll answers it once the link heals. The HIL suite found
this (crowsnest-dev #86). Ted and I reviewed it on 2026-11-04 and settled these points:
- **Off the decision loop.** The GET runs on its own thread and only enqueues, so a slow router
  cannot delay a live STOP.
- **No growing load.** A request seen answered and published is never enqueued again.
- **The ledger is never collected by default** (`--retention-days 0`). Storage keeps every request,
  because nothing deletes an answered one yet. A ledger row collected before its request leaves
  storage would make that request look new: a STOP from last week would execute again. Set a
  retention only once answered requests are deleted from storage.
- **A RESET lost in a partition can land when it heals,** minutes later. That is the protocol's
  "any order, against the current fence": a STOP in between makes it stale, and a RESET re-arms
  nothing, so she turns CLEAR and stays stopped until somebody arms her.

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

# against a Slipway vessel (test profile): her lockout switch, her stop latch, and her emergency_stop
safety-supervisor2keelson -r rise -e slipway-usv-alpha --state-dir ./state --policy config/policy.json \
  --sim-truth --legacy-observe slipway --actuator slipway --connect tcp/<local router>:7447
```

## The keelson pin

`keelson==0.6.0.dev21` is keelson's **experimental** channel, `0.6.0-experimental.21` (PyPI spells
it as a dev release). It is `dev` plus every open keelson PR whose CI passed, and the types this
uses (`EmergencyRequest`, `SafetyState`, `SafetyRequestResult`) exist only on one of them, #332
(at `d7d9512` in that build). The build's tag message is its manifest.

An experimental tree stops existing the moment any of its PRs lands or is rebased, so the pin is
tracked by crowsnest-dev #82. When #332 merges, repin to the `-pre.N` that carries it. To move it:

```bash
# set the exact version in pyproject.toml, then
uv lock --refresh-package keelson
uv sync --locked --extra dev --python 3.13
uv run --locked pytest -q && uv run --locked ruff check .
```

To try a build without moving the pin, `uv pip install keelson==<version>` into the venv and
`uv sync --locked` restores it.

## Not in this cut

- The real safety actuators for a real vessel.
- Hardware stop and lockout inputs: the `operational` profile's lockout is a hardware inhibit
  that this only observes.
- Slipway's side of I6 is Slipway's: its kernel refuses remote orders while this `safety_state`
  is not CLEAR, or is silent once heard (SLIPWAY ADR-0141).
- Envelope trips.
- Deleting the retained request keys once answered.
- `maintenance_state`.
