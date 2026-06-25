"""Parse the broker config: providers (with their strengths, quota markers, and reset window) and
the routing policy that maps a task type to an ordered list of preferred providers."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG = "broker.toml"
DEFAULT_STATE = ".broker-state.json"


class ConfigError(Exception):
    """Raised on a malformed or missing config. Fails loud — never a silent default."""


def parse_duration(text: str) -> int:
    """'5h' -> 18000 seconds. Supports s/m/h/d and the words 'weekly' (7d) and 'daily' (1d)."""
    t = text.strip().lower()
    words = {"weekly": 7 * 86400, "daily": 86400, "hourly": 3600}
    if t in words:
        return words[t]
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if t and t[-1] in units and t[:-1].replace(".", "", 1).isdigit():
        return int(float(t[:-1]) * units[t[-1]])
    raise ConfigError(f"bad duration {text!r} (use e.g. 30s, 15m, 5h, 1d, weekly)")


@dataclass
class Provider:
    name: str
    command: str                       # argv template; {prompt} token is replaced with the prompt
    strengths: list[str] = field(default_factory=list)
    reset_seconds: int = 3600          # how long a quota-exhausted provider stays cooled down
    quota_markers: list[str] = field(default_factory=list)  # substrings that mean "out of quota"
    transient_markers: list[str] = field(default_factory=list)  # substrings that mean "retry elsewhere"
    refusal_markers: list[str] = field(default_factory=list)  # policy refusal: retry, but do not cool down

    def matches_quota_error(self, output: str) -> bool:
        low = output.lower()
        return any(marker.lower() in low for marker in self.quota_markers)

    def matches_transient_error(self, output: str) -> bool:
        """A retryable provider-side fault (timeout, network, 5xx, crash) — fail over rather than
        return as-is. Quota is handled separately; terminal client errors must NOT match here."""
        low = output.lower()
        return any(marker.lower() in low for marker in self.transient_markers)

    def matches_refusal(self, output: str) -> bool:
        """Return whether output represents a policy refusal rather than provider failure."""
        low = output.lower()
        return any(marker.lower() in low for marker in self.refusal_markers)


@dataclass
class Config:
    providers: dict[str, Provider]
    default_order: list[str]
    task_order: dict[str, list[str]]
    state_file: str = DEFAULT_STATE

    def order_for(self, task: str | None) -> list[str]:
        """The ordered candidate providers for a task (task override, else default)."""
        if task and task in self.task_order:
            return list(self.task_order[task])
        return list(self.default_order)


def _coerce_str_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def load(path: str | Path = DEFAULT_CONFIG) -> Config:
    p = Path(path)
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"no config at {p} (run `broker init` to create one)") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot parse {p}: {exc}") from exc

    raw_providers = data.get("providers", {})
    if not isinstance(raw_providers, dict) or not raw_providers:
        raise ConfigError(f"{p}: at least one [providers.<name>] is required")

    providers: dict[str, Provider] = {}
    for name, spec in raw_providers.items():
        if not isinstance(spec, dict) or "command" not in spec:
            raise ConfigError(f"provider {name!r} needs a `command`")
        providers[name] = Provider(
            name=name,
            command=str(spec["command"]),
            strengths=_coerce_str_list(spec.get("strengths")),
            reset_seconds=parse_duration(str(spec.get("reset", "1h"))),
            quota_markers=_coerce_str_list(spec.get("quota_markers"))
            or ["rate limit", "usage limit", "quota", "429", "too many requests"],
            transient_markers=_coerce_str_list(spec.get("transient_markers"))
            or ["timeout", "timed out", "connection reset", "connection refused", "network",
                "temporarily unavailable", "503", "502", "504", "500", "bad gateway"],
            # Empty by default: refusal wording varies by provider and broad defaults can turn a
            # legitimate answer containing words such as "cannot" into an unwanted retry.
            refusal_markers=_coerce_str_list(spec.get("refusal_markers")),
        )

    routing = data.get("routing", {})
    default_order = _coerce_str_list(routing.get("default")) or list(providers)
    task_order_raw = routing.get("tasks", {})
    task_order = {
        str(task): _coerce_str_list(order)
        for task, order in (task_order_raw.items() if isinstance(task_order_raw, dict) else [])
    }

    for name in [*default_order, *(n for order in task_order.values() for n in order)]:
        if name not in providers:
            raise ConfigError(f"routing references unknown provider {name!r}")

    state_file = str(data.get("budget", {}).get("state_file", DEFAULT_STATE))
    return Config(providers, default_order, task_order, state_file)
