# CLAUDE.md

The onboard safety supervisor (README). **Safety-critical: every change is reviewed by a human
before it is merged or run anywhere but the isolated tests.** Every commit message carries a
`Safety-review: required` trailer naming what changed.

## Rules that must not be broken

- **Minimal.** STOP, RESET, the physical stop, the lockout, the state and the ledger. Nothing else.
  The operational fallbacks (ROC lost, a stale joystick, a stale navigator, a lapsed lease) belong
  to the gate, not here.
- **Independent.** Its own Zenoh session and its own store. It never reads the gate, the navigator
  or mavlink2keelson, and it never asks the authority manager or a station who may STOP or RESET.
  In the `test` profile it may hold its OWN MAVLink link to a SITL autopilot (`--sim-mavlink`): a
  second link to the vehicle, never mavlink2keelson (crowsnest-dev#268).
- **Never less inhibited by accident:**
  - a lost, corrupt or unknown store is RECOVERY_REQUIRED, never CLEAR;
  - local recovery gives STOPPED, never CLEAR;
  - a RESET re-arms nothing;
  - with no adopted policy a STOP still lands.
  - silence on a simulated input (the truth record, the drone's RC key) never releases a lockout, and a
    "no RC" value is no reading;
  - a legacy stop latches on the EDGE only, and a RESET clears it (README).
- **The Slipway actuator is SIL only**: refused outside `test` and without `--sim-truth`; a release
  never calls anything. **So is the MAVLink one**: refused outside `test` and without
  `--sim-mavlink`; a release never arms her.
- **The decision loop never waits on a vessel**: actuation runs on `ThreadedActuator`'s worker.
- **Never moving while STOPPED**: her latch released while inhibiting means she is stopped again.
- **Simulated inputs (`--sim-truth`, `--sim-inputs`, `--sim-mavlink`) are refused outside `test`.**
- **A real input's silence stops her** (`--hw-inputs`, #277): an unreadable device or a stale reading is a pressed
  stop and never a release, and it holds the lockout's last reading. Toward inhibited is immediate; toward released
  is debounced. A channel's polarity is never defaulted. The device is polled off the decision loop.
- **The start warning is beside the state, not in it** (#377): it mints no generation and is not persisted, so a
  restart is IDLE, which holds. No device is FAULT in `operational`, never NOT_FITTED. A simulated horn is `test` only.
- **One transaction.** The state and its ledger entry commit together. Publish after the commit;
  mark published after publishing; garbage-collect only published entries whose request key was deleted, past the retention counted from that delete (a sighting in storage voids it, #279).
- **An ENVELOPE cause never clears while its watchdog keeps re-sending** (#291): only after `envelope.hold_ms` of
  quiet, and then only by a RESET. A re-send refreshes the cause in place and moves no generation.
- **Pure decisions.** `safety_supervisor/model.py` has no clock, no store and no Zenoh. Every
  decision is tested there.
- **Tests never touch a network.** The e2e runs on loopback with multicast scouting off on both
  sides, and nothing runs against a vessel or the shared Zenoh network.
- **keelson is an exact published pin** (README, "The keelson pin"). Never commit a
  path or a local build.

## Commands

```bash
.venv/bin/python -m pytest -q        # all
.venv/bin/python -m pytest -m e2e    # the CLI against an isolated loopback peer
.venv/bin/ruff check .
```
