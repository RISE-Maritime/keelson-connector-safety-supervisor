"""The retained-request poll, off the decision loop (joint review of 130fd57, 2026-11-04).

The supervisor reads the retained `emergency_request` keys back from storage every `period_s`, so a
STOP published while it was partitioned -- alive, never hearing the live publish -- still lands (HIL
#86). Two rules make that safe:

- NEVER IN THE DECISION LOOP (review A2). The GET runs on this thread and only ENQUEUES, as the
  subscribers do. A slow router cannot delay a live STOP.
- NO GROWING LOAD (review A3). A request the loop has seen answered and published is remembered by
  id (`published`), and the poll never enqueues it again. The loop still decides everything else
  through the ledger.

`get()` and `enqueue()` are injected, so this has no Zenoh and the tests drive `poll_once`.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)


class RetainedPoll:
    def __init__(self, get, enqueue, request_id_of, period_s: float = 5.0):
        self._get = get  # () -> iterable of samples
        self._enqueue = enqueue  # (sample) -> None
        self._request_id_of = request_id_of  # (sample) -> str | None
        self.period_s = period_s
        self._published: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def mark_published(self, request_id: str) -> None:
        """Called by the decision loop once a request's result is in the ledger AND published."""
        with self._lock:
            self._published.add(request_id)

    def poll_once(self) -> int:
        n = 0
        for sample in self._get():
            try:
                rid = self._request_id_of(sample)
            except Exception:  # noqa: BLE001 - a request that does not decode is the loop's to refuse
                rid = None
            with self._lock:
                known = rid is not None and rid in self._published
            if not known:
                self._enqueue(sample)
                n += 1
        return n

    def start(self) -> RetainedPoll:
        def loop():
            while not self._stop.wait(self.period_s):
                try:
                    self.poll_once()
                except Exception:
                    log.exception("the retained-request poll failed")

        self._thread = threading.Thread(target=loop, name="retained-poll", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
