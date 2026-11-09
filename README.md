# keelson-connector-safety-supervisor

The onboard **safety supervisor**. It turns `emergency_request` into `safety_state`: a STOP
latches, a RESET is fenced, the local lockout sits above every lease, and a processed-request
ledger guarantees that a request never executes twice.

- **Protocol:** keelson `protocols/emergency_request.yaml` and `messages/payloads/SafetyState.proto`
  (keelson PR #332).
- **Design:** crowsnest-dev `docs/COMMAND-ARCHITECTURE.md` §3.

> **Status: a real vessel is out of scope; a Slipway vessel is not.**
> - **Actuation:** a logging actuator, or a **SIL** one: `--actuator slipway` stops a simulated
>   vessel, `--actuator mavlink` force-disarms the SITL drone over the supervisor's own link. Each
>   real interface (propulsion inhibit, safe state, local hold, shutdown, a physical output; MAVLink
>   force-disarm on a real vehicle) is its own change, with its own safety review.
> - **Inputs:** a real stop and lockout can be read over Modbus TCP (`--hw-inputs`), for a bench.
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
  Clearing it locally removes its own cause and re-arms nothing (I14). Both are read from real hardware
  with `--hw-inputs` (below). Otherwise they are **simulated**, and the state says `lockout_simulated`.
  The simulated ones come from one of three sources:
  - a local file (`--sim-inputs`);
  - **Slipway's truth record** (`--sim-truth`), which carries her `set_lockout` switch (Slipway
    ADR-0140). In SIL the simulator is the hardware: this is what the `operational` profile's
    hardware inhibit stands in for. Silence never releases it. The last reading stands, and a
    silent record is logged.
  - **The SITL drone's key switch** (`--sim-mavlink URL`, crowsnest-dev#268): her RC9 (`--sim-mavlink-rc-channel`),
    at or above 1700 µs (`--sim-mavlink-rc-high-us`) is the key turned. It is read over the supervisor's
    **own** MAVLink link to the autopilot (the SITL's spare serial port, e.g. `tcp:127.0.0.1:5763`; its
    own system id 250), never through mavlink2keelson: the SIL stand-in for a key wired into the
    supervisor. A "no RC" value (0, 65535, or RC9 absent before any RC input) is no reading, and silence
    never releases it. Needs the `sitl` extra (pymavlink).

  **Every simulated input is refused outside the `test` profile.** The truth record is an
  ordinary key, so anyone who can publish on the bus can forge `lockout: false`. "No remote role
  clears it" (I14) therefore holds here only as far as the bus is trusted. The real guarantee is
  the operational profile's hardware inhibit.
- **The real inputs** (`--hw-inputs FILE`, crowsnest-dev#277; `config/hw-inputs.example.json`) are one digital
  channel each, read over Modbus TCP from a coil, a discrete input or a holding register: a safety PLC, a
  remote I/O module, or a LabJack T-series on Ethernet. They are allowed in every profile, and
  `lockout_simulated` is false.
  - **Wire both normally closed**, with `released` set to the raw value of a healthy, released loop. A pulled
    wire then reads as a press, or a turned key, with no fault logic at all. `released` has no default.
  - **Toward inhibited is immediate.** Toward released takes `release_ms` of unbroken released samples.
  - **Silence stops her**, unlike a simulated input. No good sample for `fault_ms`, or none yet, reads as a
    pressed stop, with `physical-stop-input-fault` as the cause's source, and no RESET clears it until the input
    reads released again. The lockout holds its last reading (nobody turned a key because a cable fell out). With
    no reading since boot, it counts as engaged.
  - The device is polled on its own thread, so the decision loop never waits on it. At start the supervisor waits
    up to `boot_wait_ms` for a first reading, and a device still silent boots her STOPPED.
  - **What this is not.** One channel per function, not a safety-rated input path. In the `operational` profile the
    enforcement is the L5 hardware inhibit, which this only reports (COMMAND-ARCHITECTURE.md §3.3).
  - **Modbus has no authentication.** Anyone who can reach the device can answer for it, so it needs its own network
    segment. The `operational` profile with no `--hw-inputs` logs an error at start and still runs: a supervisor
    that refused to start would be no STOP path at all.
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
- **The SITL drone's actuator** (`--actuator mavlink`, crowsnest-dev#268): an inhibit **force-disarms**
  her over the same own link (`MAV_CMD_COMPONENT_ARM_DISARM`, param2 21196: what mavlink2keelson's
  `emergency_stop` sends a boat) and waits for her `COMMAND_ACK`. The same contract as the Slipway one:
  its own thread, retried until ACCEPTED, a release calls nothing. Her armed flag in HEARTBEAT, over the
  same link, is her latch: **armed while it inhibits, she is disarmed again** and an `alarm` is published.
  It refuses to start outside the `test` profile, or without `--sim-mavlink`.
- **The command_assignment projection** (operational RESET) **fails closed** with
  REFUSED_ASSIGNMENT_PROJECTION_UNVERIFIED unless all of these hold:
  - the feed was heard within `feed_ttl_seconds` on this process's monotonic clock;
  - the manager's liveliness token is present;
  - the issuer is the manager the policy names;
  - the RESET names the advertised assignment id and revision.

  A restart starts not-live until the feed is heard again. A STOP never reads it (I7′).

It **never** reads the gate, the navigator or mavlink2keelson, and it never asks the authority
manager or a station anything. Its own MAVLink link to a SITL autopilot (test profile only) is not
mavlink2keelson: it is a second, independent link to the vehicle, standing in for the key and the
output a real vessel wires into the supervisor.

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
- **An answered request leaves storage** (the protocol's `gc`, crowsnest-dev#279). Once its result is
  committed, published and marked published, the supervisor deletes its `emergency_request` key, and
  storage keeps a tombstone. Ted decided the details on 2026-11-06:
  - **Deleted means the call returned.** Zenoh acknowledges no delete, and an empty storage GET looks
    exactly like a timed-out one, so absence is never "confirmed". Instead it **self-heals**: the
    retained poll still reads storage, and any answered request it finds there is deleted again, with
    its retention clock voided.
  - **A delete that raised is never marked deleted.** It is retried at every heartbeat, and its row
    is never collected. Read back meanwhile, the request is answered from the ledger and never
    executed again.
  - **The retention counts from the delete** (`--retention-days`), which is "replayed after its
    tombstone" in the protocol's words. A ledger row is collected only once it is published, its key
    deleted, and the retention has passed since.
  - **The default is still 0, never collect**, because the retention is deployment configuration.
    A deployment sets it longer than any partition the vessel may sail through: **7 days** is a
    reasonable start. A collected row whose request then reappears would execute again, and the
    self-heal makes that need a key that lingers unseen for the whole retention.
  - A store from before this keeps its state (no schema bump). Its answered rows have no delete
    recorded, so the first heartbeat deletes their keys.
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

## Envelope trips (crowsnest-dev#291)

A separate **envelope watchdog** (`keelson-processor-envelope-watchdog`) watches her hard limits from her
own sensors, never the navigator or `envelope_exceedance`, and sends an `EmergencyRequest` STOP like any
other tripper. The supervisor itself measures nothing, so it stays simpler than what it protects.
`EmergencyRequest` has no cause field: the cause follows from who sent it.

- **The policy names the watchdog:**

  ```json
  "envelope": {"trippers": ["envelope-watchdog/*@*"], "hold_ms": 3000, "watchdog_source_id": "envelope-watchdog"}
  ```

  A STOP from a named tripper is **`CAUSE_TYPE_ENVELOPE`**. Anyone else's STOP stays `SAFETY_TRIP`. A
  tripper is let STOP even though it is a process, and `safety_trip.deny` still refuses it.
- **It does not clear while she is still out** (Ted, 2026-11-06). The watchdog re-sends its STOP while
  the limit is breached. Each re-send refreshes the one ENVELOPE cause for that watchdog in place: the
  reason names the latest breach, it stays non-clearable, and no generation moves. A RESET is PARTIAL.
  Once no re-send has arrived for `hold_ms`, the cause becomes clearable (not cleared), and a RESET by
  the profile's holder clears it, as for a released physical stop. A re-send before that holds it again.
- **A dead watchdog is an alarm.** It too would stop re-sending. The supervisor watches the liveliness
  of `watchdog_source_id`, and raises `alarm/{source_id}` (`…/envelope-watchdog-lost`) when it goes.

## Running (development only)

```bash
safety-supervisor2keelson -r rise -e <vessel> --state-dir ./state \
  --policy config/policy.json --accept-policy --recover-locally "<who>@vessel" \
  --sim-inputs ./inputs.json --connect tcp/<router>:7447

# against a Slipway vessel (test profile): her lockout switch, her stop latch, and her emergency_stop
safety-supervisor2keelson -r rise -e slipway-usv-alpha --state-dir ./state --policy config/policy.json \
  --sim-truth --legacy-observe slipway --actuator slipway --connect tcp/<local router>:7447

# against the SITL drone (test profile; pip install '.[sitl]'): her RC9 key switch and a forced disarm,
# both over the supervisor's own MAVLink link to the SITL's spare serial port
safety-supervisor2keelson -r rise -e <drone> --state-dir ./state --policy config/policy.json \
  --sim-mavlink tcp:127.0.0.1:5763 --actuator mavlink --connect tcp/<local router>:7447
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
- `maintenance_state`.
