"""Trace logging + the `broker trace` summary — the observability surface that surfaced the codex
config bug during dogfooding."""
from broker import cli
from broker import trace as tracemod

THREE = """\
[providers.claude]
command = "claude -p {prompt}"
quota_markers = ["usage limit"]
[providers.codex]
command = "codex exec {prompt}"
reset = "1h"
quota_markers = ["rate limit"]
[routing]
default = ["claude", "codex"]
"""


def _fake_run(by_first_arg):
    class _Proc:
        def __init__(self, code, text):
            self.returncode, self.stdout, self.stderr = code, text, ""

    def runner(argv, **kwargs):
        return _Proc(*by_first_arg.get(argv[0], (0, "ok")))

    return runner


def test_append_and_summarize_roundtrip(tmp_path):
    t = tmp_path / "trace.jsonl"
    tracemod.append(t, {"provider": "codex", "seconds": 2.0, "attempts": [{"quota_hit": False}]})
    tracemod.append(t, {"provider": "claude", "seconds": 3.0,
                        "attempts": [{"quota_hit": True}, {"quota_hit": False}]})
    tracemod.append(t, {"provider": None, "seconds": 0.0, "attempts": [{"quota_hit": True}]})
    s = tracemod.summarize(t)
    assert s.runs == 3
    assert s.by_provider == {"codex": 1, "claude": 1}
    assert s.failovers == 2          # the claude run and the unresolved run each had a quota hit
    assert s.quota_events == 2
    assert s.unresolved == 1
    assert s.total_seconds == 5.0


def test_refusal_counts_as_failover(tmp_path):
    path = tmp_path / "trace.jsonl"
    tracemod.append(path, {
        "provider": "codex",
        "attempts": [
            {"provider": "claude", "refusal": True},
            {"provider": "codex", "refusal": False},
        ],
    })
    assert tracemod.summarize(path).failovers == 1


def test_summarize_missing_file_is_empty(tmp_path):
    s = tracemod.summarize(tmp_path / "nope.jsonl")
    assert s.runs == 0 and "no runs" in s.render()


def test_summarize_skips_corrupt_lines(tmp_path):
    t = tmp_path / "trace.jsonl"
    t.write_text('{"provider":"codex","attempts":[]}\nnot json\n\n', encoding="utf-8")
    assert tracemod.summarize(t).runs == 1   # corrupt + blank lines ignored, valid one counted


def test_run_writes_trace_and_trace_cmd_reads_it(monkeypatch, tmp_path, capsys):
    cfg = tmp_path / "broker.toml"
    cfg.write_text(THREE)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("broker.runner.subprocess.run", _fake_run({"claude": (0, "answer")}))
    assert cli.main(["-c", str(cfg), "run", "-t", "reasoning", "hi"]) == 0
    capsys.readouterr()
    assert cli.main(["-c", str(cfg), "trace"]) == 0
    out = capsys.readouterr().out
    assert "runs: 1" in out and "claude" in out
