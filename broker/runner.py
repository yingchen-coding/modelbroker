"""Run a task: pick the strongest available provider, invoke it, and on a quota error cool it down
and fail over to the next — so work keeps moving instead of stalling on one model's limit."""
from __future__ import annotations

import shlex
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import Config, Provider
from .router import plan
from .state import State, now

# An executor takes (argv, stdin_text) and returns (exit_code, combined_output).
Executor = Callable[[list[str], str | None], "tuple[int, str]"]


_CMD_NOT_FOUND = 127        # provider CLI is missing/uninstalled
_TIMEOUT = 124              # _subprocess_executor's timeout exit code — always transient
_UNAVAILABLE_COOLDOWN = 60  # short cooldown for a missing CLI (not a quota window)
_TRANSIENT_COOLDOWN = 30    # brief cooldown for a retryable provider-side fault


@dataclass
class Attempt:
    provider: str
    exit_code: int
    quota_hit: bool
    seconds: float = 0.0
    unavailable: bool = False   # the provider CLI was missing (exit 127) — failed over like quota
    transient: bool = False     # a retryable fault (timeout/network/5xx/crash) — failed over
    refusal: bool = False       # a policy refusal — failed over without cooling the provider

    def label(self) -> str:
        """Provider name annotated with why it failed over (for status/trace lines)."""
        if self.quota_hit:
            return f"{self.provider}(quota)"
        if self.unavailable:
            return f"{self.provider}(unavailable)"
        if self.transient:
            return f"{self.provider}(transient)"
        if self.refusal:
            return f"{self.provider}(refusal)"
        return self.provider


def _classify_transient(provider: Provider, code: int, output: str) -> bool:
    """A non-quota, non-127 nonzero exit that we should fail over on rather than return as-is:
    a timeout, a crash (negative code = killed by signal), or output matching a transient marker
    (network / 5xx). Everything else — a generic exit 1, a content/policy refusal, a bad prompt —
    is a TERMINAL client error and is returned honestly so the caller sees the real failure."""
    if code == 0:
        return False
    return code == _TIMEOUT or code < 0 or provider.matches_transient_error(output)


@dataclass
class RunResult:
    provider: str | None          # provider that ultimately handled it, or None if none could
    exit_code: int
    output: str
    attempts: list[Attempt] = field(default_factory=list)
    exhausted: bool = False       # every candidate was cooled down / hit quota


def _argv_and_stdin(provider: Provider, prompt: str) -> tuple[list[str], str | None]:
    """Build argv from the command template. A literal `{prompt}` token becomes the prompt as a
    single argument (no shell interpolation); otherwise the prompt is piped on stdin."""
    tokens = shlex.split(provider.command)
    if "{prompt}" in tokens:
        return [prompt if tok == "{prompt}" else tok for tok in tokens], None
    return tokens, prompt


def probe_provider(provider: Provider) -> tuple[bool, str]:
    """Check whether a provider's CLI is installed/reachable WITHOUT running a prompt.

    Returns (ok, detail): (True, resolved-path) if the command's executable is on PATH,
    else (False, reason). Lets `broker doctor` catch a missing model before a run fails over to it.
    """
    tokens = shlex.split(provider.command)
    if not tokens:
        return False, "empty command"
    resolved = shutil.which(tokens[0])
    if resolved is None:
        return False, f"{tokens[0]!r} not on PATH"
    return True, resolved


def _subprocess_executor(timeout: float | None) -> Executor:
    def run(argv: list[str], stdin_text: str | None) -> tuple[int, str]:
        try:
            proc = subprocess.run(  # noqa: S603 - argv from operator config, prompt passed as data
                argv, input=stdin_text, capture_output=True, text=True, timeout=timeout
            )
        except FileNotFoundError:
            return 127, f"command not found: {argv[0]}"
        except subprocess.TimeoutExpired:
            return 124, "timeout"
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    return run


def run_task(
    config: Config,
    state: State,
    prompt: str,
    *,
    task: str | None = None,
    executor: Executor | None = None,
    now_fn: Callable[[], float] = now,
    timeout: float | None = None,
) -> RunResult:
    """Try providers in routing order, failing over on quota errors. Mutates + (the caller) saves state."""
    if not prompt.strip():
        # cost control starts here: never spend a provider call on an empty prompt
        return RunResult(provider=None, exit_code=2, output="empty prompt — nothing to run")

    exec_fn = executor or _subprocess_executor(timeout)
    started = now_fn()  # one logical timestamp per invocation — keeps routing decisions consistent
    p = plan(config, state, task, started)
    result = RunResult(provider=None, exit_code=1, output="")
    last_refusal: tuple[str, int, str] | None = None  # (provider, exit_code, output) of last refusal

    if not p.order:
        result.output = f"no providers configured for task {task!r}"
        return result

    for name in p.order:
        provider = config.providers[name]
        if not state.get(name).available(started):
            continue  # still cooling down — skip
        argv, stdin_text = _argv_and_stdin(provider, prompt)
        before = time.monotonic()  # latency uses a monotonic clock, independent of the cooldown now_fn
        code, output = exec_fn(argv, stdin_text)
        elapsed = round(time.monotonic() - before, 3)
        quota = code != 0 and provider.matches_quota_error(output)
        unavailable = code == _CMD_NOT_FOUND
        # a transient fault is classified only after ruling out quota/missing-CLI, so those keep
        # their own (longer) cooldowns and labels
        refusal = provider.matches_refusal(output)
        transient = (not quota and not unavailable and not refusal
                     and _classify_transient(provider, code, output))
        result.attempts.append(Attempt(provider=name, exit_code=code, quota_hit=quota,
                                       seconds=elapsed, unavailable=unavailable,
                                       transient=transient, refusal=refusal))
        if quota or unavailable or transient or refusal:
            # quota-exhausted, missing-CLI, OR a retryable provider-side fault: cool the provider
            # down and fail over to the next, so one model's hiccup doesn't end the whole run. A
            # generic/terminal nonzero (bad prompt, policy refusal) falls through and is returned.
            if refusal:
                last_refusal = (name, code, output)
            else:
                cooldown = (provider.reset_seconds if quota
                            else _UNAVAILABLE_COOLDOWN if unavailable
                            else _TRANSIENT_COOLDOWN)
                state.cool_down(name, started + cooldown)
            continue
        state.record_run(name, started)
        result.provider, result.exit_code, result.output = name, code, output
        return result

    # No provider succeeded. The candidates split into ones genuinely on cooldown (quota / missing /
    # transient — they free up later) and ones that *refused* (still "available", but would just
    # refuse the same prompt again). A refused provider stays available, so plan()'s soonest can't be
    # trusted here — key off the real cooldown set instead.
    cooling = [n for n in p.order if not state.get(n).available(started)]
    if not cooling and last_refusal is not None:
        # Nothing is on cooldown — the run ended only because every provider that answered *refused*
        # the prompt. That's a terminal content/policy issue, not exhaustion; surface the actual
        # refusal (and the provider's own message) so the caller sees the real cause, not a
        # misleading "out of quota". (exhausted stays False: no quota window to wait on.)
        refused = [a.provider for a in result.attempts if a.refusal]
        _, rcode, routput = last_refusal
        result.provider, result.exit_code = None, rcode or 1
        detail = f"\n{routput.strip()}" if routput.strip() else ""
        result.output = (
            f"all providers refused this prompt (policy refusal, not a quota issue): "
            f"{', '.join(refused)}{detail}"
        )
        return result

    # at least one provider is genuinely cooled down / out of quota this pass
    result.exhausted = True
    if cooling:
        soonest = min(cooling, key=lambda n: state.get(n).cooldown_until)
        eta = int(state.get(soonest).cooldown_remaining(started))
        result.output = f"all providers exhausted; {soonest} frees up in {eta}s"
    else:
        result.output = "all providers exhausted"
    return result
