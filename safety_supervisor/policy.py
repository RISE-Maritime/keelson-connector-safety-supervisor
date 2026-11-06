"""The SafetyPolicySnapshot: who may STOP and RESET, decided from the supervisor's OWN persisted
policy and nothing else (inv. 10; COMMAND-ARCHITECTURE.md §3). Never by asking the authority
manager, a station or Crowsnest — or the independent path has regained a dependency.

The snapshot is a local JSON file. It is ADOPTED into the store only by a local maintenance act
(`--accept-policy`); otherwise the persisted copy rules, and a file that differs is logged and
ignored. Example (README):

    {
      "config_revision": 3,
      "command_profile": "test",                 # operational | test | legacy_single_operator
      "safety_trip": {"allow": ["*@*"], "deny": []},   # fnmatch over controller_id
      "manager": {"issuer_id": "authority-manager/0", "source_id": "authority-manager"},
      "conn_ttl_s": 30,
      "trust_roots": []
    }

`safety_trip.allow` defaults to humans at a site (`*@*`, the controller_id shape stations use) and
no process — the protocol's default policy: humans yes whatever role they hold, processes no.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .model import PROFILES

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Policy:
    config_revision: int
    command_profile: str
    trip_allow: tuple[str, ...] = ("*@*",)
    trip_deny: tuple[str, ...] = ()
    manager_issuer_id: str = ""
    manager_source_id: str = ""
    conn_ttl_s: float = 30.0
    trust_roots: tuple[str, ...] = field(default_factory=tuple)
    # The envelope watchdog (#291): the principals whose STOP is CAUSE_TYPE_ENVELOPE, how long after its
    # last re-sent STOP the cause becomes clearable, and the source id whose liveliness is watched.
    envelope_trippers: tuple[str, ...] = ()
    envelope_hold_ms: int = 3000
    envelope_watchdog_source_id: str = ""

    def is_envelope_tripper(self, controller_id: str) -> bool:
        return any(fnmatch.fnmatchcase(controller_id, p) for p in self.envelope_trippers)

    def may_trip(self, controller_id: str) -> bool:
        if any(fnmatch.fnmatchcase(controller_id, p) for p in self.trip_deny):
            return False
        # A named envelope watchdog is a process the policy deliberately lets trip; no other process is.
        return self.is_envelope_tripper(controller_id) or any(fnmatch.fnmatchcase(controller_id, p) for p in self.trip_allow)


def parse(d: dict) -> Policy:
    profile = d.get("command_profile")
    if profile not in PROFILES:
        raise ValueError(f"command_profile must be one of {PROFILES}, got {profile!r}")
    rev = d.get("config_revision")
    if not isinstance(rev, int) or rev < 1:
        raise ValueError("config_revision must be a positive integer")
    trip = d.get("safety_trip") or {}
    manager = d.get("manager") or {}
    envelope = d.get("envelope") or {}
    hold = int(envelope.get("hold_ms", 3000))
    if hold <= 0:
        raise ValueError("envelope.hold_ms must be positive: a cause that is clearable at once is no hold")
    if profile == "operational" and not manager.get("issuer_id"):
        raise ValueError("the operational profile names the authorised manager (manager.issuer_id)")
    return Policy(
        config_revision=rev,
        command_profile=profile,
        trip_allow=tuple(trip.get("allow", ["*@*"])),
        trip_deny=tuple(trip.get("deny", [])),
        manager_issuer_id=manager.get("issuer_id", ""),
        manager_source_id=manager.get("source_id", ""),
        conn_ttl_s=float(d.get("conn_ttl_s", 30)),
        trust_roots=tuple(d.get("trust_roots", [])),
        envelope_trippers=tuple(envelope.get("trippers", [])),
        envelope_hold_ms=hold,
        envelope_watchdog_source_id=str(envelope.get("watchdog_source_id", "")),
    )


def resolve(store, file: Path | None, accept: bool) -> Policy | None:
    """The policy in force. `accept` is the maintenance act that adopts the file."""
    stored = store.get_json("policy")
    if accept:
        if file is None:
            raise ValueError("--accept-policy needs --policy")
        d = json.loads(Path(file).read_text())
        p = parse(d)  # validate before adopting
        store.put_json("policy", d)
        log.warning("policy revision %s ADOPTED from %s (maintenance)", p.config_revision, file)
        return p
    if stored is None:
        if file is not None:
            log.error("no adopted policy; %s is not adopted without --accept-policy", file)
        return None
    p = parse(stored)
    if file is not None and json.loads(Path(file).read_text()) != stored:
        log.warning("%s differs from the adopted policy (revision %s); it is ignored until a maintenance --accept-policy", file, p.config_revision)
    return p
