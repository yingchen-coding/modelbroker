from broker import cli
from broker.runtime import runtime_report
from broker.trace import append


def test_runtime_report_computes_token_factory_metrics(tmp_path):
    trace = tmp_path / ".broker-trace.jsonl"
    append(
        trace,
        {
            "provider": "fast",
            "seconds": 2.0,
            "input_tokens": 100,
            "output_tokens": 300,
            "estimated_cost_usd": 0.04,
            "exit_code": 0,
        },
    )
    append(
        trace,
        {
            "provider": "slow",
            "seconds": 8.0,
            "total_tokens": 400,
            "estimated_cost_usd": 0.08,
            "exit_code": 0,
        },
    )

    report = runtime_report(trace)

    assert report.runs == 2
    assert report.total_tokens == 800
    assert report.tokens_per_second == 80.0
    assert report.cost_per_1k_tokens_usd == 0.15
    assert report.by_provider["fast"]["tokens_per_second"] == 200.0


def test_runtime_report_warns_when_tokens_missing(tmp_path):
    trace = tmp_path / ".broker-trace.jsonl"
    append(trace, {"provider": "fast", "seconds": 1.0, "estimated_cost_usd": 0.01})

    report = runtime_report(trace)

    assert report.total_tokens == 0
    assert any("no token fields" in warning for warning in report.warnings)


def test_cli_runtime_reads_trace_next_to_state_file(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "broker.toml"
    cfg.write_text(
        """\
[budget]
state_file = "state.json"
[providers.fast]
command = "fast {prompt}"
reset = "1h"
[routing]
default = ["fast"]
""",
        encoding="utf-8",
    )
    append(
        tmp_path / ".broker-trace.jsonl",
        {
            "provider": "fast",
            "seconds": 2.0,
            "tokens": 100,
            "estimated_cost_usd": 0.02,
        },
    )
    monkeypatch.chdir(tmp_path)

    assert cli.main(["-c", str(cfg), "runtime"]) == 0
    out = capsys.readouterr().out
    assert "tokens_per_second: 50.00" in out
    assert "cost_per_1k_tokens: $0.2000" in out


def test_runtime_report_survives_malformed_numeric_fields(tmp_path):
    # Real trace files contain garbage: a truncated line, a provider that wrote a string where a
    # number belongs. One bad field must not crash the whole report — the good rows still count.
    trace = tmp_path / ".broker-trace.jsonl"
    append(trace, {"provider": "a", "seconds": "oops", "total_tokens": "NaN",
                   "estimated_cost_usd": None, "exit_code": 0})
    append(trace, {"provider": "b", "seconds": 4.0, "total_tokens": 400,
                   "estimated_cost_usd": 0.08, "exit_code": 0})

    report = runtime_report(trace)  # must not raise

    assert report.runs == 2
    assert report.total_tokens == 400          # the bad row contributes 0, not a crash
    assert report.total_seconds == 4.0
    assert report.by_provider["a"]["tokens"] == 0
