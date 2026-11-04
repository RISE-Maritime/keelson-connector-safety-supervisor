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
- **Never less inhibited by accident:**
  - a lost, corrupt or unknown store is RECOVERY_REQUIRED, never CLEAR;
  - local recovery gives STOPPED, never CLEAR;
  - a RESET re-arms nothing;
  - with no adopted policy a STOP still lands.
  - silence on an input (the truth record) never releases a lockout;
  - a legacy stop latches on the EDGE only, and a RESET clears it (README).
- **The Slipway actuator is SIL only**: refused outside `test` and without `--sim-truth`; a release
  never calls anything.
- **The decision loop never waits on a vessel**: actuation runs on `ThreadedActuator`'s worker.
- **Never moving while STOPPED**: her latch released while inhibiting means she is stopped again.
- **Simulated inputs (`--sim-truth`, `--sim-inputs`) are refused outside `test`.**
- **One transaction.** The state and its ledger entry commit together. Publish after the commit;
  mark published after publishing; garbage-collect only published entries past the retention.
- **Pure decisions.** `safety_supervisor/model.py` has no clock, no store and no Zenoh. Every
  decision is tested there.
- **Tests never touch a network.** The e2e runs on loopback with multicast scouting off on both
  sides, and nothing runs against a vessel or the shared Zenoh network.
- **keelson is an exact published pin** (README, "Developing before the alpha"). Never commit a
  path or a local build.

## Commands

```bash
.venv/bin/python -m pytest -q        # all
.venv/bin/python -m pytest -m e2e    # the CLI against an isolated loopback peer
.venv/bin/ruff check .
```
