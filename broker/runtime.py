"""Runtime throughput and token-factory metrics from broker traces."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RuntimeReport:
    runs: int
    total_tokens: int
    total_seconds: float
    estimated_cost_usd: float
    tokens_per_second: float
    cost_per_1k_tokens_usd: float
    cost_per_hour_usd: float
    error_rate: float
    by_provider: dict[str, dict[str, float | int]]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "runs": self.runs,
            "total_tokens": self.total_tokens,
            "total_seconds": self.total_seconds,
            "estimated_cost_usd": self.estimated_cost_usd,
            "tokens_per_second": self.tokens_per_second,
            "cost_per_1k_tokens_usd": self.cost_per_1k_tokens_usd,
            "cost_per_hour_usd": self.cost_per_hour_usd,
            "error_rate": self.error_rate,
            "by_provider": self.by_provider,
            "warnings": self.warnings,
        }

    def render(self) -> str:
        if self.runs == 0:
            return "no runtime traces yet"
        lines = [
            f"runs: {self.runs}",
            f"total_tokens: {self.total_tokens}",
            f"total_seconds: {self.total_seconds:.2f}",
            f"tokens_per_second: {self.tokens_per_second:.2f}",
            f"estimated_cost: ${self.estimated_cost_usd:.4f}",
            f"cost_per_1k_tokens: ${self.cost_per_1k_tokens_usd:.4f}",
            f"cost_per_hour: ${self.cost_per_hour_usd:.4f}",
            f"error_rate: {self.error_rate:.1%}",
        ]
        if self.by_provider:
            lines.append("by_provider:")
            for provider, row in sorted(self.by_provider.items()):
                lines.append(
                    f"  {provider:<12} runs={row['runs']} tokens={row['tokens']} "
                    f"tokens/s={row['tokens_per_second']:.2f} "
                    f"cost/1k=${row['cost_per_1k_tokens_usd']:.4f}"
                )
        if self.warnings:
            lines.append("warnings:")
            lines.extend(f"  {warning}" for warning in self.warnings)
        return "\n".join(lines)


def runtime_report(path: str | Path) -> RuntimeReport:
    rows = _load_rows(Path(path))
    runs = len(rows)
    total_tokens = sum(_tokens(row) for row in rows)
    total_seconds = round(sum(_num(row.get("seconds")) for row in rows), 6)
    total_cost = round(sum(_num(row.get("estimated_cost_usd")) for row in rows), 6)
    errors = sum(1 for row in rows if _is_error(row))
    by_provider_raw: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        provider = str(row.get("provider") or "unresolved")
        by_provider_raw.setdefault(provider, []).append(row)

    by_provider: dict[str, dict[str, float | int]] = {}
    for provider, provider_rows in by_provider_raw.items():
        provider_tokens = sum(_tokens(row) for row in provider_rows)
        provider_seconds = sum(_num(row.get("seconds")) for row in provider_rows)
        provider_cost = sum(_num(row.get("estimated_cost_usd")) for row in provider_rows)
        by_provider[provider] = {
            "runs": len(provider_rows),
            "tokens": provider_tokens,
            "seconds": round(provider_seconds, 6),
            "estimated_cost_usd": round(provider_cost, 6),
            "tokens_per_second": _ratio(provider_tokens, provider_seconds),
            "cost_per_1k_tokens_usd": _cost_per_1k(provider_cost, provider_tokens),
        }

    warnings = []
    if runs and total_tokens == 0:
        warnings.append("Trace has no token fields; cannot validate token-factory throughput claims.")
    if _ratio(errors, runs) > 0.1:
        warnings.append("Error rate above 10%; do not scale this runtime without reliability work.")
    if total_seconds and total_tokens and _ratio(total_tokens, total_seconds) < 10:
        warnings.append("Token throughput is low; check provider latency, context size, or routing.")

    return RuntimeReport(
        runs=runs,
        total_tokens=total_tokens,
        total_seconds=total_seconds,
        estimated_cost_usd=total_cost,
        tokens_per_second=_ratio(total_tokens, total_seconds),
        cost_per_1k_tokens_usd=_cost_per_1k(total_cost, total_tokens),
        cost_per_hour_usd=round(total_cost / total_seconds * 3600, 6) if total_seconds else 0.0,
        error_rate=_ratio(errors, runs),
        by_provider=by_provider,
        warnings=warnings,
    )


def _load_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _num(value: Any) -> float:
    """Coerce a trace field to float, tolerating the garbage real trace files contain (None, '',
    a non-numeric string from a truncated/buggy provider line). A single bad field must never crash
    the whole report — we already skip malformed JSON *lines*; this gives malformed *fields* the same
    grace instead of letting one row take down `broker cost`."""
    if value is None:
        return 0.0
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    # NaN/inf slip through float() ("NaN", "inf") but blow up int() and poison every sum they touch;
    # treat them as missing, not as data.
    return result if math.isfinite(result) else 0.0


def _int(value: Any) -> int:
    return int(_num(value))


def _tokens(row: dict[str, Any]) -> int:
    if row.get("total_tokens") is not None:
        return _int(row.get("total_tokens"))
    if row.get("tokens") is not None:
        return _int(row.get("tokens"))
    return _int(row.get("input_tokens")) + _int(row.get("output_tokens"))


def _is_error(row: dict[str, Any]) -> bool:
    if row.get("provider") is None:
        return True
    exit_code = row.get("exit_code")
    return isinstance(exit_code, int) and exit_code != 0


def _ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 6)


def _cost_per_1k(cost: float, tokens: int) -> float:
    if tokens <= 0:
        return 0.0
    return round(cost / tokens * 1000, 6)
