"""The onboard inputs the supervisor reads directly: the physical stop and the local maintenance
lockout.

In this cut both are SIMULATED (the `test` profile): a local JSON file `{"physical_stop": bool,
"lockout": bool}` that a person on the vessel (or a test) edits. The state says so —
`lockout_simulated` — on every screen that shows it (COMMAND-ARCHITECTURE.md §3.3). In the
`operational` profile the lockout is a HARDWARE inhibit the supervisor only observes and reports;
that input does not exist yet, and nothing here pretends it does.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Reading:
    physical_stop: bool
    lockout: bool
    simulated: bool


class NoInputs:
    """No physical inputs wired: proves nothing, so adds no cause."""

    def read(self) -> Reading:
        return Reading(False, False, simulated=False)


class SimulatedInputs:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._last: Reading | None = None

    def read(self) -> Reading:
        try:
            d = json.loads(self.path.read_text()) if self.path.exists() else {}
        except (OSError, ValueError) as e:
            # An unreadable input file is not "released": keep the last reading.
            log.error("simulated inputs %s unreadable (%s); keeping the last reading", self.path, e)
            return self._last or Reading(False, False, simulated=True)
        self._last = Reading(bool(d.get("physical_stop", False)), bool(d.get("lockout", False)), simulated=True)
        return self._last
