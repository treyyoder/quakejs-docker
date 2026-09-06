"""One way to follow a growing file, for both log tailers.

The engine's console log and its game log are read the same way: whatever was
appended since last time, split into whole lines, with a partial last line
held back for the next pass. A file that shrank was truncated by a restart and
is read from the start again. A file whose opening bytes changed is a different
file altogether - the container was recreated, or the engine opened a new log
underneath us - and is treated the same way. Where to resume after a restart
is the caller's business: offset and fingerprint are plain attributes it may
persist and hand back.
"""

import hashlib
import time

FINGERPRINT_BYTES = 64


class Follower:
    def __init__(self, path, offset=0, fingerprint=""):
        self.path = path
        self.offset = offset
        self.fingerprint = fingerprint
        self.pending = ""
        self.reset = False   # the last poll found a truncated or different file

    def poll(self):
        """Whole lines appended since the previous poll; [] when there are none."""
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        with self.path.open("rb") as handle:
            head = handle.read(FINGERPRINT_BYTES)
            # A file still shorter than the fingerprint has no fingerprint yet;
            # comparing one taken then against one taken later would look like
            # a different file and replay the start.
            fingerprint = (hashlib.sha256(head).hexdigest()[:16]
                           if len(head) >= FINGERPRINT_BYTES else "")
            replaced = bool(fingerprint and self.fingerprint and fingerprint != self.fingerprint)
            self.reset = replaced or size < self.offset
            if self.reset:
                self.offset = 0
                self.pending = ""
            if fingerprint:
                self.fingerprint = fingerprint
            if size <= self.offset:
                return []
            handle.seek(self.offset)
            self.pending += handle.read(size - self.offset).decode("latin-1")
            self.offset = handle.tell()
        lines = self.pending.split("\n")
        self.pending = lines.pop()     # keep any partial line for next pass
        return [line.rstrip("\r") for line in lines]

    def step(self, on_lines, tag, on_tick=None):
        """One pass: deliver new lines, then tick. A handler that raises is
        logged and its batch is not retried - the offset has already moved past
        it - so a bad line costs one batch, never the thread. An unreadable
        file is routine; anything else here is a bug in a parser, and it must
        be seen rather than quietly end the tailer.
        """
        try:
            lines = self.poll()
            if lines:
                on_lines(lines)
            if on_tick:
                on_tick()
        except Exception as exc:
            print(f"[{tag}] {type(exc).__name__}: {exc}", flush=True)

    def run(self, on_lines, tag, interval=1.0, on_tick=None):
        """Follow forever on the calling thread."""
        while True:
            self.step(on_lines, tag, on_tick)
            time.sleep(interval)
