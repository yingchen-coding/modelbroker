"""Append-only trace of every run, so you can see your real routing/failover/cost behavior over
time — which model handled what, where quota failovers happened, how long each took. This is the
observability half of cost control: you can't manage what you can't see."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TRACE = ".broker-trace.jsonl"


def append(path: str | Path, record: dict[str, object]) -> None:
    """Append one JSON line. A trace write must never break a run, so failures are swallowed."""
    try:
        with Path(path).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


@dataclass
class TraceSummary:
    runs: int
    by_provider: dict[str, int]      # provider -> times it ultimately handled a run
    failovers: int                   # runs where a provider was abandoned, including policy refusal
    quota_events: int                # total provider-level quota hits
    unresolved: int                  # runs no provider could handle
    total_seconds: float

    def render(self) -> str:
        if self.runs == 0:
            return "no runs traced yet"
        lines = [f"runs: {self.runs}  ·  failovers: {self.failovers}  ·  quota events: "
                 f"{self.quota_events}  ·  unresolved: {self.unresolved}"]
        for prov, n in sorted(self.by_provider.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {prov:<10} handled {n}")
        if self.total_seconds:
            lines.append(f"total provider wall-time: {self.total_seconds:.1f}s")
        return "\n".join(lines)


def summarize(path: str | Path) -> TraceSummary:
    p = Path(path)
    runs = failovers = quota_events = unresolved = 0
    by_provider: dict[str, int] = {}
    total_seconds = 0.0
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            runs += 1
            attempts = rec.get("attempts") or []
            quota_hits = sum(1 for a in attempts if a.get("quota_hit"))
            quota_events += quota_hits
            # a failover is any run where a provider was abandoned mid-chain — for quota, a missing
            # CLI, transient fault, or configured policy refusal — not just quota
            if any(a.get("quota_hit") or a.get("unavailable") or a.get("transient")
                   or a.get("refusal") for a in attempts):
                failovers += 1
            prov = rec.get("provider")
            if prov:
                by_provider[prov] = by_provider.get(prov, 0) + 1
            else:
                unresolved += 1
            total_seconds += float(rec.get("seconds") or 0.0)
    return TraceSummary(runs, by_provider, failovers, quota_events, unresolved, round(total_seconds, 2))
