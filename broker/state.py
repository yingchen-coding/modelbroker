"""Persistent provider state across invocations: when a provider is cooled down (out of quota) and
how many runs it's served. The whole point of cost control is that this survives between commands —
the broker must *remember* that Claude is rate-limited until 3pm so it keeps routing to Codex."""
from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ProviderState:
    cooldown_until: float = 0.0   # epoch seconds; 0 = available
    runs: int = 0
    failures: int = 0
    last_used: float = 0.0

    def available(self, now: float) -> bool:
        return now >= self.cooldown_until

    def cooldown_remaining(self, now: float) -> float:
        return max(0.0, self.cooldown_until - now)


@dataclass
class State:
    path: Path
    providers: dict[str, ProviderState] = field(default_factory=dict)

    def get(self, name: str) -> ProviderState:
        return self.providers.setdefault(name, ProviderState())

    def cool_down(self, name: str, until: float) -> None:
        st = self.get(name)
        st.cooldown_until = max(st.cooldown_until, until)
        st.failures += 1

    def record_run(self, name: str, now: float) -> None:
        st = self.get(name)
        st.runs += 1
        st.last_used = now

    def _reconcile_from_disk(self) -> None:
        """Fold the on-disk state back into memory before writing, so a concurrent broker process
        can't erase what another just recorded. Cooldown is the one invariant that must never be
        lost — if process A cools down Claude and process B (which loaded earlier) then saves, a
        naive overwrite would wipe the cooldown and route the next call straight back to the
        rate-limited provider. We take the *max* of every monotonic field: a cooldown survives, and
        run/failure counters never regress."""
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return  # unreadable/corrupt on-disk state: keep memory as-is, our atomic write repairs it
        if not isinstance(data, dict):
            return
        for name, s in data.items():
            if not isinstance(s, dict):
                continue
            disk = ProviderState(
                cooldown_until=float(s.get("cooldown_until", 0) or 0),
                runs=int(s.get("runs", 0) or 0),
                failures=int(s.get("failures", 0) or 0),
                last_used=float(s.get("last_used", 0) or 0),
            )
            cur = self.providers.get(name)
            if cur is None:
                self.providers[name] = disk
                continue
            cur.cooldown_until = max(cur.cooldown_until, disk.cooldown_until)
            cur.runs = max(cur.runs, disk.runs)
            cur.failures = max(cur.failures, disk.failures)
            cur.last_used = max(cur.last_used, disk.last_used)

    def save(self) -> None:
        self._reconcile_from_disk()
        payload = {
            name: {
                "cooldown_until": s.cooldown_until,
                "runs": s.runs,
                "failures": s.failures,
                "last_used": s.last_used,
            }
            for name, s in self.providers.items()
        }
        text = json.dumps(payload, indent=2) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: a same-directory temp file + os.replace means a reader (or a kill mid-write)
        # never sees a half-written state file. os.replace is atomic on POSIX and Windows.
        tmp = self.path.with_name(f"{self.path.name}.tmp.{os.getpid()}")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.path)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()


def load(path: str | Path) -> State:
    p = Path(path)
    state = State(path=p)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return state  # a corrupt state file should not block routing; start fresh
        for name, s in data.items():
            if isinstance(s, dict):
                state.providers[name] = ProviderState(
                    cooldown_until=float(s.get("cooldown_until", 0) or 0),
                    runs=int(s.get("runs", 0) or 0),
                    failures=int(s.get("failures", 0) or 0),
                    last_used=float(s.get("last_used", 0) or 0),
                )
    return state


def now() -> float:
    return time.time()
