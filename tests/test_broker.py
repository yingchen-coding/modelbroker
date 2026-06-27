import json

import pytest

from broker import config as cfgmod
from broker import state as statemod
from broker.config import ConfigError, parse_duration
from broker.router import plan
from broker.runner import run_task

TOML = """\
[budget]
state_file = ".broker-state.json"
[providers.claude]
command = "claude -p {prompt}"
strengths = ["reasoning"]
reset = "5h"
quota_markers = ["usage limit", "429"]
cost_per_run_usd = 0.03
[providers.codex]
command = "codex exec {prompt}"
strengths = ["codegen"]
reset = "1h"
quota_markers = ["rate limit", "429"]
cost_per_run_usd = 0.01
[routing]
default = ["claude", "codex"]
[routing.tasks]
codegen = ["codex", "claude"]
"""


def _cfg(tmp_path):
    p = tmp_path / "broker.toml"
    p.write_text(TOML)
    return cfgmod.load(p)


def _state(tmp_path):
    return statemod.State(path=tmp_path / "state.json")


def test_parse_duration():
    assert parse_duration("5h") == 18000
    assert parse_duration("30m") == 1800
    assert parse_duration("weekly") == 7 * 86400
    with pytest.raises(ConfigError):
        parse_duration("soon")


def test_routing_order_default_and_by_task(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.order_for(None) == ["claude", "codex"]
    assert cfg.order_for("codegen") == ["codex", "claude"]   # routed to its strength
    assert cfg.order_for("unknown-task") == ["claude", "codex"]  # falls back to default
    assert cfg.providers["claude"].cost_per_run_usd == 0.03
    assert cfg.providers["codex"].cost_per_run_usd == 0.01


def test_cost_ceiling_skips_expensive_provider(tmp_path):
    config_text = TOML.replace(
        '[budget]\nstate_file = ".broker-state.json"',
        '[budget]\nstate_file = ".broker-state.json"\nmax_cost_per_run_usd = 0.02',
    )
    path = tmp_path / "broker.toml"
    path.write_text(config_text)
    cfg = cfgmod.load(path)
    state = _state(tmp_path)

    routed = plan(cfg, state, "reasoning", now=1000.0)
    assert routed.order == ["codex"]
    assert routed.chosen == "codex"


def test_cost_strategy_cheapest_reorders_candidates(tmp_path):
    config_text = TOML.replace(
        '[budget]\nstate_file = ".broker-state.json"',
        '[budget]\nstate_file = ".broker-state.json"\ncost_strategy = "cheapest"',
    )
    path = tmp_path / "broker.toml"
    path.write_text(config_text)
    cfg = cfgmod.load(path)
    routed = plan(cfg, _state(tmp_path), "reasoning", now=1000.0)
    assert routed.order == ["codex", "claude"]
    assert routed.chosen == "codex"


def test_cost_strategy_balanced_keeps_task_fit_then_cost(tmp_path):
    config_text = TOML.replace(
        '[budget]\nstate_file = ".broker-state.json"',
        '[budget]\nstate_file = ".broker-state.json"\ncost_strategy = "balanced"',
    )
    path = tmp_path / "broker.toml"
    path.write_text(config_text)
    cfg = cfgmod.load(path)
    assert plan(cfg, _state(tmp_path), "reasoning", now=1000.0).chosen == "claude"
    assert plan(cfg, _state(tmp_path), "codegen", now=1000.0).chosen == "codex"


def test_config_rejects_unknown_provider_in_routing(tmp_path):
    bad = TOML.replace('default = ["claude", "codex"]', 'default = ["claude", "ghost"]')
    p = tmp_path / "bad.toml"
    p.write_text(bad)
    with pytest.raises(ConfigError, match="ghost"):
        cfgmod.load(p)


def test_failover_when_primary_hits_quota(tmp_path):
    cfg, state = _cfg(tmp_path), _state(tmp_path)
    calls = []

    def executor(argv, stdin):
        calls.append(argv[0])
        if argv[0] == "claude":
            return 1, "Error: usage limit reached, resets at 3pm"
        return 0, "codex did the work"

    result = run_task(cfg, state, "build X", task="reasoning", executor=executor, now_fn=lambda: 1000.0)
    assert calls == ["claude", "codex"]               # tried claude, failed over to codex
    assert result.provider == "codex"
    assert [a.provider for a in result.attempts] == ["claude", "codex"]
    assert result.attempts[0].quota_hit is True
    # claude is now cooled down for 5h
    assert state.get("claude").available(1000.0) is False
    assert state.get("claude").cooldown_remaining(1000.0) == 18000


def test_configured_policy_refusal_fails_over_without_cooldown(tmp_path):
    config_text = TOML.replace(
        'quota_markers = ["usage limit", "429"]',
        'quota_markers = ["usage limit", "429"]\nrefusal_markers = ["policy risk"]',
    )
    path = tmp_path / "broker.toml"
    path.write_text(config_text)
    cfg, state = cfgmod.load(path), _state(tmp_path)

    def executor(argv, stdin):
        if argv[0] == "claude":
            return 1, "Blocked because this request may present a policy risk"
        return 0, "answered by fallback"

    result = run_task(cfg, state, "Explain Selmer groups", executor=executor,
                      now_fn=lambda: 1000.0)

    assert result.provider == "codex"
    assert result.output == "answered by fallback"
    assert result.attempts[0].refusal is True
    assert result.attempts[0].label() == "claude(refusal)"
    assert state.get("claude").available(1000.0) is True


def test_all_providers_refuse_reports_refusal_not_exhaustion(tmp_path):
    # When every provider answers with a policy refusal, the run must NOT be reported as
    # "all providers exhausted" (nothing was cooled down / out of quota) — it must surface the
    # refusal honestly so the caller sees the prompt is the problem, not a quota window.
    config_text = (
        TOML.replace(
            'quota_markers = ["usage limit", "429"]',
            'quota_markers = ["usage limit", "429"]\nrefusal_markers = ["policy risk"]',
        ).replace(
            'quota_markers = ["rate limit", "429"]',
            'quota_markers = ["rate limit", "429"]\nrefusal_markers = ["policy risk"]',
        )
    )
    path = tmp_path / "broker.toml"
    path.write_text(config_text)
    cfg, state = cfgmod.load(path), _state(tmp_path)

    def executor(argv, stdin):
        return 1, f"{argv[0]}: blocked, this request may present a policy risk"

    result = run_task(cfg, state, "do the bad thing", executor=executor, now_fn=lambda: 1000.0)

    assert result.provider is None
    assert result.exhausted is False              # nothing was exhausted — all refused
    assert result.exit_code == 1
    assert "refused this prompt" in result.output
    assert "policy risk" in result.output         # the provider's own message is surfaced
    assert [a.refusal for a in result.attempts] == [True, True]
    # no healthy provider was cooled down by a refusal
    assert state.get("claude").available(1000.0) is True
    assert state.get("codex").available(1000.0) is True


def test_refusal_plus_cooled_provider_still_reports_exhaustion(tmp_path):
    # Boundary for the all-refusal branch: if one provider is genuinely cooling down (quota) while
    # another refuses, the run is NOT pure-refusal — a provider will free up, so it must still report
    # "exhausted" with the ETA, not "all refused".
    config_text = TOML.replace(
        'quota_markers = ["rate limit", "429"]',
        'quota_markers = ["rate limit", "429"]\nrefusal_markers = ["policy risk"]',
    )
    path = tmp_path / "broker.toml"
    path.write_text(config_text)
    cfg, state = cfgmod.load(path), _state(tmp_path)
    state.cool_down("claude", until=5000.0)  # claude on a real quota cooldown

    def executor(argv, stdin):  # only codex runs; it refuses
        return 1, "codex: blocked, this request may present a policy risk"

    result = run_task(cfg, state, "task", executor=executor, now_fn=lambda: 1000.0)

    assert result.provider is None
    assert result.exhausted is True
    assert "exhausted" in result.output and "claude" in result.output  # names the freeing provider
    assert "refused this prompt" not in result.output


def test_refusal_detection_is_opt_in(tmp_path):
    cfg, state = _cfg(tmp_path), _state(tmp_path)
    result = run_task(cfg, state, "x", executor=lambda a, s: (1, "I cannot help with that"),
                      now_fn=lambda: 1000.0)
    assert result.provider == "claude"
    assert result.attempts[0].refusal is False


def test_cooled_down_provider_is_skipped(tmp_path):
    cfg, state = _cfg(tmp_path), _state(tmp_path)
    state.cool_down("claude", until=5000.0)           # claude unavailable until t=5000
    used = []

    def executor(argv, stdin):
        used.append(argv[0])
        return 0, "ok"

    result = run_task(cfg, state, "x", task="reasoning", executor=executor, now_fn=lambda: 1000.0)
    assert used == ["codex"]                          # claude skipped entirely, no wasted call
    assert result.provider == "codex"


def test_resume_after_cooldown_expires(tmp_path):
    cfg, state = _cfg(tmp_path), _state(tmp_path)
    state.cool_down("claude", until=2000.0)
    result = run_task(cfg, state, "x", task="reasoning", executor=lambda a, s: (0, "ok"),
                      now_fn=lambda: 2001.0)           # past cooldown
    assert result.provider == "claude"                # back to the preferred model


def test_all_exhausted(tmp_path):
    cfg, state = _cfg(tmp_path), _state(tmp_path)
    result = run_task(cfg, state, "x", task="reasoning",
                      executor=lambda a, s: (1, "429 too many requests"), now_fn=lambda: 1000.0)
    assert result.provider is None and result.exhausted is True
    assert "exhausted" in result.output


def test_prompt_passed_as_single_arg_not_shell(tmp_path):
    cfg, state = _cfg(tmp_path), _state(tmp_path)
    seen = {}

    def executor(argv, stdin):
        seen["argv"] = argv
        return 0, "ok"

    run_task(cfg, state, "rm -rf / ; echo pwned", task="reasoning", executor=executor, now_fn=lambda: 1.0)
    assert seen["argv"] == ["claude", "-p", "rm -rf / ; echo pwned"]   # one arg, no injection


def test_state_round_trips(tmp_path):
    state = _state(tmp_path)
    state.cool_down("claude", until=9999.0)
    state.record_run("codex", now=100.0)
    state.save()
    reloaded = statemod.load(state.path)
    assert reloaded.get("claude").cooldown_until == 9999.0
    assert reloaded.get("codex").runs == 1
    assert json.loads(state.path.read_text())  # valid JSON
