"""Command-line interface for modelbroker."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from . import config as cfgmod
from . import state as statemod
from . import trace as tracemod
from .config import ConfigError
from .router import plan
from .runner import probe_provider, run_task

_DEFAULT_TOML = """\
# modelbroker config — quota-aware multi-model routing.
[budget]
state_file = ".broker-state.json"

# A {prompt} token in `command` is replaced with the prompt as one argument (no shell).
[providers.claude]
command = "claude -p {prompt}"
strengths = ["reasoning", "architecture", "refactor", "debugging", "writing", "review"]
reset = "5h"                       # cool-down when Claude hits its usage limit
quota_markers = ["usage limit", "rate limit", "429", "quota", "resets at", "too many requests", "exceeded"]

[providers.codex]
command = "codex exec --skip-git-repo-check {prompt}"
strengths = ["codegen", "boilerplate", "tests", "quick-edit", "scripts"]
reset = "1h"
quota_markers = ["rate limit", "429", "quota", "usage limit", "too many requests", "exceeded"]

[routing]
default = ["claude", "codex"]      # global fail-over order

[routing.tasks]                    # route by task to the model that's strongest at it
codegen = ["codex", "claude"]
boilerplate = ["codex", "claude"]
tests = ["codex", "claude"]
reasoning = ["claude", "codex"]
architecture = ["claude", "codex"]
refactor = ["claude", "codex"]
review = ["claude", "codex"]
writing = ["claude", "codex"]
debugging = ["claude", "codex"]
"""


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="broker", description="Quota-aware multi-model task router.")
    p.add_argument("--version", action="version", version=f"modelbroker {__version__}")
    p.add_argument("-c", "--config", default=cfgmod.DEFAULT_CONFIG, help="path to broker.toml")
    # Accept -c AFTER the subcommand too (`broker run -c X`), not just before it. SUPPRESS default so
    # a subcommand only overrides the global value when -c is explicitly given — never resets it.
    cfg_after = argparse.ArgumentParser(add_help=False)
    cfg_after.add_argument("-c", "--config", default=argparse.SUPPRESS, help="path to broker.toml")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", parents=[cfg_after],
                         help="run a prompt on the strongest available model (with fail-over)")
    run.add_argument("prompt", help="the prompt / task text")
    run.add_argument("-t", "--task", default=None, help="task type (codegen, reasoning, tests, ...)")
    run.add_argument("--timeout", type=float, default=None, help="per-provider timeout (seconds)")

    route = sub.add_parser("route", parents=[cfg_after],
                           help="show which model a task would go to (no execution)")
    route.add_argument("-t", "--task", default=None)

    sub.add_parser("status", parents=[cfg_after],
                   help="show each provider's availability / cooldown / usage")
    sub.add_parser("doctor", parents=[cfg_after],
                   help="check each provider's CLI is installed/on PATH (no prompt run)")
    sub.add_parser("trace", parents=[cfg_after],
                   help="summarize the run trace (routing, failovers, quota events, time)")
    sub.add_parser("init", parents=[cfg_after], help="write a starter broker.toml")
    return p


def _trace_path(cfg: cfgmod.Config) -> str:
    # trace lives next to the state file
    base = Path(cfg.state_file).parent
    return str(base / tracemod.DEFAULT_TRACE)


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = cfgmod.load(args.config)
    state = statemod.load(cfg.state_file)
    result = run_task(cfg, state, args.prompt, task=args.task, timeout=args.timeout)
    state.save()
    tracemod.append(_trace_path(cfg), {
        "task": args.task,
        "provider": result.provider,
        "exit_code": result.exit_code,
        "exhausted": result.exhausted,
        "seconds": round(sum(a.seconds for a in result.attempts), 3),
        "attempts": [
            {"provider": a.provider, "exit_code": a.exit_code, "quota_hit": a.quota_hit,
             "unavailable": a.unavailable, "transient": a.transient, "refusal": a.refusal,
             "seconds": a.seconds}
            for a in result.attempts
        ],
    })

    if result.provider is None:
        print(f"broker: {result.output}", file=sys.stderr)
        if result.attempts:
            print(f"  tried: {', '.join(a.label() for a in result.attempts)}", file=sys.stderr)
        return 1
    failovers = [a.label() for a in result.attempts
                 if a.quota_hit or a.unavailable or a.transient or a.refusal]
    if failovers:
        print(f"broker: {', '.join(failovers)} → used {result.provider}", file=sys.stderr)
    print(result.output, end="" if result.output.endswith("\n") else "\n")
    return result.exit_code


def _cmd_route(args: argparse.Namespace) -> int:
    cfg = cfgmod.load(args.config)
    state = statemod.load(cfg.state_file)
    pl = plan(cfg, state, args.task, statemod.now())
    print(f"task: {args.task or '(default)'}  order: {' → '.join(pl.order)}")
    if pl.chosen:
        print(f"  → would use: {pl.chosen}")
    else:
        print(f"  → all cooled down; {pl.soonest} frees up in {int(pl.soonest_eta)}s")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    cfg = cfgmod.load(args.config)
    state = statemod.load(cfg.state_file)
    nowt = statemod.now()
    for name, prov in cfg.providers.items():
        st = state.get(name)
        tag = "available" if st.available(nowt) else f"cooldown {int(st.cooldown_remaining(nowt))}s"
        print(f"{name:<10} {tag:<16} runs={st.runs} fails={st.failures}  strengths: {', '.join(prov.strengths)}")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    cfg = cfgmod.load(args.config)
    all_ok = True
    for name, prov in cfg.providers.items():
        ok, detail = probe_provider(prov)
        mark = "ok " if ok else "MISSING"
        print(f"{name:<10} {mark:<8} {detail}")
        all_ok = all_ok and ok
    if not all_ok:
        print("broker: some provider CLIs are not installed — those models will be skipped on run",
              file=sys.stderr)
    return 0 if all_ok else 1


def _cmd_trace(args: argparse.Namespace) -> int:
    cfg = cfgmod.load(args.config)
    print(tracemod.summarize(_trace_path(cfg)).render())
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    dest = Path(args.config)
    if dest.exists():
        print(f"broker: {dest} already exists", file=sys.stderr)
        return 2
    dest.write_text(_DEFAULT_TOML, encoding="utf-8")
    print(f"wrote {dest}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    dispatch = {"run": _cmd_run, "route": _cmd_route, "status": _cmd_status,
                "doctor": _cmd_doctor, "trace": _cmd_trace, "init": _cmd_init}
    try:
        return dispatch[args.command](args)
    except ConfigError as exc:
        print(f"broker: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
