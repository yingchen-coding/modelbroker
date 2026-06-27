# Changelog

## Unreleased

- Add `broker run --skill stop-slop ...` and `broker skills`. The built-in prompt skill wraps a
  request with a concrete, low-fluff quality contract before routing it through the normal provider
  policy.
- Add `--skill context-window` for long tasks: prefer smallest sufficient context, code-only context
  when possible, defer bulky data until the final responsible step, and compact/summarize before
  context-window drift.
- Add opt-in policy-refusal failover that retries the current task on another provider without
  incorrectly cooling down a healthy model.
- **Report an all-refusal run honestly instead of as "exhausted".** When every provider answers
  with a policy refusal, nothing was cooled down or out of quota — so the run no longer claims "all
  providers exhausted" (which implied a quota window to wait on). It now reports that every provider
  *refused* the prompt and surfaces the provider's own refusal message, so the real cause (the
  prompt, not quota) is visible. `exhausted` stays `False` in this case.

## 0.2.1

- **`-c/--config` is accepted after the subcommand too** (`broker run -t codegen -c X`), not only
  before it — matching how people actually type the command. A subcommand only overrides the global
  config when `-c` is explicitly given.
- **Fail over on transient faults, return terminal errors honestly.** A non-quota, non-127 nonzero
  exit used to be returned as-is — so a timeout, network blip, 5xx, or CLI crash on the first
  provider ended the whole run instead of trying the next, defeating the point of a router. Now a
  retryable fault (timeout exit 124, negative/killed code, or output matching per-provider
  `transient_markers`) cools the provider down briefly (30s) and fails over; a genuine terminal
  error (bad prompt, policy refusal, generic exit 1) is still returned honestly so you see the real
  cause. Trace/status label it `name(transient)`. The trace failover count now also reflects
  missing-CLI and transient failovers, not just quota.
- **`broker doctor`** — preflight that checks each provider's CLI is installed / on PATH (via
  `shutil.which`, no prompt run), so you catch a missing model before a run silently fails over to
  it. Exits non-zero if any are missing.
- **Fail over when a provider CLI is missing.** A provider that exits 127 (command not found —
  uninstalled/broken CLI) now cools down briefly and fails over to the next, instead of stalling the
  whole run. Found by dogfooding. Trace + status lines label it `name(unavailable)`.
- **Refuse empty prompts** before spending any provider call (cost control at the input).

## 0.2.0 — 2026-06-20

- **`broker trace`** — every `run` now appends a JSONL trace (provider, exit, quota events,
  per-attempt latency); `broker trace` summarizes routing / failover / quota / wall-time so you can
  see real cost behavior over time.
- **Fix:** default `codex` command now passes `--skip-git-repo-check`, so `broker run` works outside
  a trusted git dir (found by dogfooding — codex aborted otherwise).
- Per-attempt latency recorded via a monotonic clock, independent of the cooldown clock.

## 0.1.0 — 2026-06-20

Initial release. Quota-aware multi-model router (`broker`):

- **Strength routing** — per-task provider order (`[routing.tasks]`) over a global fail-over default.
- **Quota fail-over** — a provider whose output matches its `quota_markers` on a nonzero exit is
  cooled down for its `reset` window; the task automatically retries on the next provider.
- **Persisted cooldown state** (`.broker-state.json`) — later commands keep routing around a
  limited provider until its window resets, then flip back.
- **Safe invocation** — the prompt is passed as a single argv token (or stdin), never shell-interpolated.
- **CLI** — `broker run | route | status | init`. Zero runtime dependencies, Python ≥3.11.
