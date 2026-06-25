# modelbroker

> **Run out of Claude quota? Keep working on Codex. Quota back? Switch back.**
> A quota-aware multi-model router: route each task to the model that's strongest at it, fail over
> when one runs out of quota, and resume the moment its window resets.

[![CI](https://github.com/yingchen-coding/modelbroker/actions/workflows/ci.yml/badge.svg)](https://github.com/yingchen-coding/modelbroker/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

You pay for two or three coding assistants (Claude Code, Codex, …), each with its own usage limit.
When one hits its limit you either stop, or babysit a manual switch. modelbroker does the switch
for you — and routes each kind of task to whichever model is best at it. Zero dependencies, no API
keys (it drives the CLIs you already have).

## Star This If

- You use multiple AI coding CLIs and lose time when one hits quota.
- You want task-based routing instead of one default model for every job.
- You need local traces showing which provider handled each task and why failover happened.

## What it does

- **Strength-based routing.** A `codegen` task goes to Codex; an `architecture` or `reasoning` task
  goes to Claude — you define the policy.
- **Quota-aware fail-over.** A provider that returns a rate-limit / usage-limit error is **cooled
  down** for its reset window and the task automatically retries on the next provider. No wasted
  call, no manual switch.
- **It remembers.** The cooldown is persisted, so the *next* command still knows Claude is limited
  until 4pm and keeps routing to Codex — then flips back when the window resets.
- **Safe by construction.** The prompt is passed as a single argument (or stdin), never interpolated
  into a shell — no injection from prompt text.

## Quickstart

```bash
pip install -e .          # or: pip install git+https://github.com/yingchen-coding/modelbroker
broker init              # write a starter broker.toml (claude + codex)

broker route -t codegen  # → would use: codex
broker route -t reasoning# → would use: claude
broker status            # availability / cooldown / usage per provider
broker doctor            # check each provider's CLI is installed / on PATH (no prompt run)

broker run -t codegen "write a quicksort in python"
# claude out of quota → uses codex; claude back in window → uses claude. automatically.

broker trace             # see your real routing / failover / cost behavior over time
# runs: 42 · failovers: 7 · quota events: 9 · unresolved: 0
#   claude  handled 31
#   codex   handled 11
```

## How a config looks

`broker.toml` — providers (with strengths + how they signal "out of quota") and a routing policy:

```toml
[providers.claude]
command = "claude -p {prompt}"     # {prompt} = the task, passed as one argument
strengths = ["reasoning", "architecture", "refactor", "review"]
reset = "5h"                       # cool down this long after a usage-limit error
quota_markers = ["usage limit", "rate limit", "429", "resets at"]
# Optional and deliberately provider-specific: retry this request elsewhere without marking
# Claude unhealthy or cooling it down.
refusal_markers = ["classified as a policy risk", "cannot assist with this request"]

[providers.codex]
command = "codex exec {prompt}"
strengths = ["codegen", "boilerplate", "tests"]
reset = "1h"

[routing]
default = ["claude", "codex"]      # global fail-over order
[routing.tasks]
codegen   = ["codex", "claude"]    # route by task to the model that's strongest at it
reasoning = ["claude", "codex"]
```

Any CLI works — add a `[providers.<name>]` with its command and quota markers (gemini, aider, a
local model via `ollama run`, …).

`refusal_markers` handles over-refusal separately from outages and quota. When a configured phrase
matches, the same prompt is tried on the next provider, the attempt is traced as `refusal`, and the
first provider remains available for unrelated requests. Keep the markers narrow: broad phrases
such as `cannot` can also occur in valid answers.

## How fail-over works

```
broker run -t reasoning "refactor this module"
  → claude (preferred for reasoning)
      └─ "Error: usage limit reached, resets at 4pm"   → cool down claude 5h
  → codex (next in order)
      └─ ok                                            → return codex's output
# every later command skips claude until its window resets, then routes back to it
```

## License

MIT © Ying Chen

## Local Review

```bash
scripts/pr_review_check.sh
```
