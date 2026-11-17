"""Offline reachability precheck of a session script against a mock scenario.

This module never opens a socket and never changes collection behavior.  It
replays the exact client semantics of :class:`termharvest.session.TerminalSession`
(start ``contains`` expectation, ordered step keys, reconnect + replay of
previously accepted steps, ``attempts``/``backoff_ms``/``timeout_seconds``)
against the deterministic mock-terminal semantics of
:class:`termharvest.mockhost.MockTerminalServer` (start-screen push, per-key
``drop_keys``/``delay_ms``, transition table, ``unknown_key_screen``).

Two proof modes are offered:

* ``nominal``   - follow only the fault-free trajectory (scenario-fixed
  ``drop_keys``/``delay_ms`` still apply: they are deterministic mock
  behavior, not injected faults).
* ``guarantee`` - additionally inject hypothetical faults under a total
  budget (by default at most one key loss **or** one response timeout per
  whole trajectory, not per step) and require *every* allowed trajectory to
  collect all frames.

The bounded model state is: current mock screen id, number of completed
steps, connection phase (disconnected / replaying prior step j / ready for
the current step), 1-based attempt number for the current step, and the
already spent injected drop/timeout budget.  A screen only counts as
collected when the client accepts its frame at the correct step; a target
screen that merely *appears* somewhere in the scenario graph (or during a
replay that drifted to another screen) never marks a step complete.

If the explored state space exceeds the configured limit the result is
``unproven`` - never a success.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .config_types import SessionContract
from .errors import E_FRAME_TIMEOUT, E_SCREEN_UNEXPECTED, E_STEP_RETRY_EXHAUSTED
from .mock_scenario import MockScenario
from .session import backoff_seconds, missing_fragments

MODE_NOMINAL = "nominal"
MODE_GUARANTEE = "guarantee"
MODES = (MODE_NOMINAL, MODE_GUARANTEE)

STATUS_REACHABLE = "reachable"      # nominal: the fault-free trajectory collects every frame
STATUS_UNREACHABLE = "unreachable"  # nominal: the fault-free trajectory fails
STATUS_GUARANTEED = "guaranteed"    # guarantee: every in-budget trajectory collects every frame
STATUS_VIOLATED = "violated"        # guarantee: some in-budget trajectory fails
STATUS_UNPROVEN = "unproven"        # state-space limit hit; nothing proven either way

# Failure categories reported in counterexamples.
CATEGORY_START_MISMATCH = "start_mismatch"
CATEGORY_MISSING_TRANSITION = "missing_transition"
CATEGORY_UNKNOWN_KEY_SCREEN = "unknown_key_screen"
CATEGORY_UNEXPECTED_SCREEN = "unexpected_screen"
CATEGORY_KEY_DROPPED = "key_dropped"
CATEGORY_RESPONSE_TIMEOUT = "response_timeout"

# Fault labels used on trace entries.
FAULT_SCENARIO_DROP = "scenario_drop"
FAULT_SCENARIO_DELAY = "scenario_delay"
FAULT_INJECTED_DROP = "injected_drop"
FAULT_INJECTED_TIMEOUT = "injected_timeout"

PROOF_SCOPE = (
    "证明范围：仅覆盖给定 mock 场景 JSON 的确定性行为与 TerminalSession 的客户端语义"
    "（起始屏 contains 校验、按序发键、失败后重连并从 start 重放已接受步骤、attempts/backoff/timeout 一致）。",
    "预检全程不打开 socket、不连接真实终端；原采集行为不受影响。",
    "故障模型 = 场景固定 drop_keys/delay_ms（每次尝试都生效）+ 预检注入预算"
    "（整段轨迹总共最多一次按键丢失或一次响应超时，而非每步一次）。",
    "未覆盖的真实故障：连接建立失败或被重置、start 帧推送延迟、网络粘包/拆包/乱序、帧损坏、"
    "TLS/鉴权、服务器跨连接状态、真实主机行为漂移。",
    "通过本预检不构成对任何生产主机的可达性保证；mock 证明不能冒充生产保证。",
)

_VIA_CATEGORY = {
    "transition": CATEGORY_UNEXPECTED_SCREEN,
    "missing_transition": CATEGORY_MISSING_TRANSITION,
    "unknown_key_screen": CATEGORY_UNKNOWN_KEY_SCREEN,
}

# Connection phases inside one attempt of the current step.
_DISCONNECTED = "disconnected"
_READY = "ready"
_REPLAY = "replay:"

@dataclass(frozen=True)
class FaultBudget:
    """Injected-fault budget for ``guarantee`` mode, per whole trajectory."""

    max_drops: int = 1
    max_timeouts: int = 1
    max_total: int = 1

    def __post_init__(self) -> None:
        if min(self.max_drops, self.max_timeouts, self.max_total) < 0:
            raise ValueError("fault budget values must be >= 0")

@dataclass(frozen=True)
class PrecheckOptions:
    mode: str = MODE_NOMINAL
    state_limit: int = 20000
    budget: FaultBudget = FaultBudget()

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if isinstance(self.state_limit, bool) or self.state_limit < 1:
            raise ValueError("state_limit must be a positive integer")

@dataclass(frozen=True)
class TraceEntry:
    """One client-observable action on a witness or counterexample trace."""

    seq: int
    action: str           # connect | read_start | send_key | receive | timeout | backoff | close
    step_id: str          # "start", a step id, or "-" when not step-bound
    attempt: int
    phase: str            # collect | retry | replay | step
    screen: str | None    # mock screen of the open connection before the action
    key: str | None
    expected_screen: str | None
    actual_screen: str | None
    fault: str | None
    accepted: bool | None
    elapsed_ms: int
    note: str

@dataclass(frozen=True)
class FailureInfo:
    """Terminal failure of a trajectory (always the client's own error)."""

    code: str             # E_STEP_RETRY_EXHAUSTED, or E_SCREEN_UNEXPECTED for a start mismatch
    last_code: str        # code of the final attempt's cause
    category: str
    step_id: str          # step the client was trying to complete (matches the live error context)
    failed_step_id: str   # step whose key/read actually failed ("start", or a prior step during replay)
    attempt: int
    phase: str
    expected_screen: str | None
    actual_screen: str | None
    fault: str | None
    message: str

@dataclass(frozen=True)
class PrecheckResult:
    mode: str
    status: str
    proven: bool
    session_id: str
    scenario_id: str
    state_limit: int
    budget: FaultBudget
    states_explored: int
    transitions_explored: int
    key_send_points: int
    injected_branches: int
    replay_injected_branches: int
    max_faults_used: int
    elapsed_ms: int
    trace: tuple[TraceEntry, ...]   # success path (reviewable) or shortest counterexample
    failure: FailureInfo | None
    scope: tuple[str, ...]
    summary: str

@dataclass(frozen=True)
class _State:
    """Bounded model state (see module docstring)."""

    screen: str | None    # mock screen of the open connection; None when disconnected
    completed: int        # number of session steps accepted so far
    attempt: int          # 1-based attempt number for the current step
    phase: str            # disconnected | ready | replay:<j>
    drops_used: int
    timeouts_used: int

@dataclass(frozen=True)
class _Cause:
    """The reason one attempt failed, carried into retry/exhaustion handling."""

    category: str
    code: str
    step_id: str
    phase: str
    expected: str | None
    actual: str | None
    fault: str | None

def _entry(
    seq: int,
    action: str,
    *,
    step_id: str = "-",
    attempt: int = 0,
    phase: str,
    screen: str | None = None,
    key: str | None = None,
    expected: str | None = None,
    actual: str | None = None,
    fault: str | None = None,
    accepted: bool | None = None,
    elapsed_ms: int = 0,
    note: str = "",
) -> TraceEntry:
    return TraceEntry(
        seq=seq, action=action, step_id=step_id, attempt=attempt, phase=phase, screen=screen, key=key,
        expected_screen=expected, actual_screen=actual, fault=fault, accepted=accepted, elapsed_ms=elapsed_ms, note=note,
    )

class _Explorer:
    """Breadth-first exploration of the bounded session/scenario state space."""

    def __init__(self, session: SessionContract, scenario: MockScenario, options: PrecheckOptions) -> None:
        self.session = session
        self.scenario = scenario
        self.options = options
        self.timeout_ms = int(round(session.retry.timeout_seconds * 1000))
        self.step_count = len(session.steps)
        self.transitions_explored = 0
        self.key_send_points = 0
        self.injected_branches = 0
        self.replay_injected_branches = 0
        self.max_faults_used = 0

    def _missing(self, screen_id: str, expectation) -> list[str]:
        return missing_fragments(self.scenario.frame(screen_id), expectation)

    # -- transitions ---------------------------------------------------------

    def _expand(self, state: _State, elapsed: int, trace: tuple[TraceEntry, ...]) -> list[tuple]:
        if state.phase == _DISCONNECTED:
            return self._expand_disconnected(state, elapsed, trace)
        if state.phase.startswith(_REPLAY):
            return self._send_key(state, elapsed, trace, int(state.phase[len(_REPLAY):]), replay=True)
        return self._send_key(state, elapsed, trace, state.completed, replay=False)

    def _expand_disconnected(self, state: _State, elapsed: int, trace: tuple[TraceEntry, ...]) -> list[tuple]:
        """Reconnect and re-check the start screen (TerminalSession._prepare)."""
        start = self.scenario.start_screen
        connect = _entry(
            len(trace), "connect", attempt=state.attempt, phase="retry", elapsed_ms=elapsed,
            note=f"reconnect for attempt {state.attempt}; mock resets to start screen {start!r}",
        )
        missing = self._missing(start, self.session.start)
        read_start = _entry(
            len(trace) + 1, "read_start", step_id="start", attempt=state.attempt, phase="retry", screen=start,
            expected=self.session.start.screen_id, actual=start, accepted=not missing, elapsed_ms=elapsed,
            note="start expectation satisfied" if not missing else f"start expectation missing fragments: {missing}",
        )
        new_trace = trace + (connect, read_start)
        if missing:
            cause = _Cause(CATEGORY_START_MISMATCH, E_SCREEN_UNEXPECTED, "start", "retry", self.session.start.screen_id, start, None)
            return self._attempt_failed(state, elapsed, new_trace, cause, state.drops_used, state.timeouts_used)
        phase = f"{_REPLAY}0" if state.completed else _READY
        return [("state", _State(start, state.completed, state.attempt, phase, state.drops_used, state.timeouts_used), new_trace, None)]

    def _send_key(self, state: _State, elapsed: int, trace: tuple[TraceEntry, ...], step_index: int, *, replay: bool) -> list[tuple]:
        """Send one key and branch over every response the budget allows."""
        step = self.session.steps[step_index]
        expect = step.expect
        screen = state.screen
        phase = "replay" if replay else "step"
        self.key_send_points += 1
        outcomes: list[tuple] = []
        plan = self.scenario.plan_response(screen, step.key)
        send = _entry(
            len(trace), "send_key", step_id=step.step_id, attempt=state.attempt, phase=phase, screen=screen,
            key=step.key, elapsed_ms=elapsed,
            note=f"send key {step.key!r} on screen {screen!r}" + (" (replay of accepted step)" if replay else ""),
        )
        # Fault-free branch: the mock's deterministic response.
        if plan.kind == "drop":
            timeout = _entry(
                len(trace) + 1, "timeout", step_id=step.step_id, attempt=state.attempt, phase=phase, screen=screen,
                key=step.key, fault=FAULT_SCENARIO_DROP, elapsed_ms=elapsed + self.timeout_ms,
                note=f"key {step.key!r} is listed in scenario drop_keys; client waits {self.timeout_ms}ms and times out",
            )
            cause = _Cause(CATEGORY_KEY_DROPPED, E_FRAME_TIMEOUT, step.step_id, phase, expect.screen_id, None, FAULT_SCENARIO_DROP)
            outcomes.extend(self._attempt_failed(state, elapsed + self.timeout_ms, trace + (send, timeout), cause, state.drops_used, state.timeouts_used))
        elif plan.delay_ms > self.timeout_ms:
            timeout = _entry(
                len(trace) + 1, "timeout", step_id=step.step_id, attempt=state.attempt, phase=phase, screen=screen,
                key=step.key, fault=FAULT_SCENARIO_DELAY, elapsed_ms=elapsed + self.timeout_ms,
                note=f"mock answers key {step.key!r} after {plan.delay_ms}ms, beyond the {self.timeout_ms}ms client timeout",
            )
            cause = _Cause(CATEGORY_RESPONSE_TIMEOUT, E_FRAME_TIMEOUT, step.step_id, phase, expect.screen_id, None, FAULT_SCENARIO_DELAY)
            outcomes.extend(self._attempt_failed(state, elapsed + self.timeout_ms, trace + (send, timeout), cause, state.drops_used, state.timeouts_used))
        else:
            actual = plan.screen
            missing = self._missing(actual, expect)
            received_elapsed = elapsed + plan.delay_ms
            if missing:
                receive = _entry(
                    len(trace) + 1, "receive", step_id=step.step_id, attempt=state.attempt, phase=phase, screen=screen,
                    key=step.key, expected=expect.screen_id, actual=actual, accepted=False, elapsed_ms=received_elapsed,
                    note=f"frame from {actual!r} missing fragments: {missing}" + ("" if plan.via == "transition" else f" (key resolved via {plan.via})"),
                )
                cause = _Cause(_VIA_CATEGORY[plan.via], E_SCREEN_UNEXPECTED, step.step_id, phase, expect.screen_id, actual, None)
                outcomes.extend(self._attempt_failed(state, received_elapsed, trace + (send, receive), cause, state.drops_used, state.timeouts_used))
            else:
                receive = _entry(
                    len(trace) + 1, "receive", step_id=step.step_id, attempt=state.attempt, phase=phase, screen=screen,
                    key=step.key, expected=expect.screen_id, actual=actual, accepted=True, elapsed_ms=received_elapsed,
                    note="replay frame accepted" if replay else "frame accepted for step",
                )
                new_trace = trace + (send, receive)
                if replay:
                    next_index = step_index + 1
                    next_phase = f"{_REPLAY}{next_index}" if next_index < state.completed else _READY
                    outcomes.append(("state", _State(actual, state.completed, state.attempt, next_phase, state.drops_used, state.timeouts_used), new_trace, None))
                else:
                    completed = state.completed + 1
                    if completed == self.step_count:
                        outcomes.append(("success", None, new_trace, None))
                    else:
                        outcomes.append(("state", _State(actual, completed, 1, _READY, state.drops_used, state.timeouts_used), new_trace, None))
        # Injected-fault branches (guarantee mode only, while budget remains).
        if self.options.mode == MODE_GUARANTEE:
            budget = self.options.budget
            spent = state.drops_used + state.timeouts_used
            for fault, category, drops_used, timeouts_used in (
                (FAULT_INJECTED_DROP, CATEGORY_KEY_DROPPED, state.drops_used + 1, state.timeouts_used),
                (FAULT_INJECTED_TIMEOUT, CATEGORY_RESPONSE_TIMEOUT, state.drops_used, state.timeouts_used + 1),
            ):
                allowed = (drops_used <= budget.max_drops) and (timeouts_used <= budget.max_timeouts) and (spent < budget.max_total)
                if not allowed:
                    continue
                self.injected_branches += 1
                if replay:
                    self.replay_injected_branches += 1
                kind = "key lost" if fault == FAULT_INJECTED_DROP else "response delayed past timeout"
                timeout = _entry(
                    len(trace) + 1, "timeout", step_id=step.step_id, attempt=state.attempt, phase=phase, screen=screen,
                    key=step.key, fault=fault, elapsed_ms=elapsed + self.timeout_ms,
                    note=(
                        f"injected fault: {kind} (drop budget {drops_used}/{budget.max_drops}, "
                        f"timeout budget {timeouts_used}/{budget.max_timeouts}, total {drops_used + timeouts_used}/{budget.max_total}); "
                        f"client waits {self.timeout_ms}ms and times out"
                    ),
                )
                cause = _Cause(category, E_FRAME_TIMEOUT, step.step_id, phase, expect.screen_id, None, fault)
                outcomes.extend(self._attempt_failed(state, elapsed + self.timeout_ms, trace + (send, timeout), cause, drops_used, timeouts_used))
        return outcomes

    def _attempt_failed(self, state: _State, elapsed: int, trace: tuple[TraceEntry, ...], cause: _Cause, drops_used: int, timeouts_used: int) -> list[tuple]:
        """Close the connection, then either back off and retry or exhaust."""
        self.max_faults_used = max(self.max_faults_used, drops_used + timeouts_used)
        close = _entry(
            len(trace), "close", step_id=cause.step_id, attempt=state.attempt, phase=cause.phase, elapsed_ms=elapsed,
            note="client closes the connection after the failed attempt",
        )
        if state.attempt < self.session.retry.attempts:
            backoff_ms = int(round(backoff_seconds(self.session.retry, state.attempt) * 1000))
            backoff = _entry(
                len(trace) + 1, "backoff", step_id=cause.step_id, attempt=state.attempt, phase=cause.phase,
                elapsed_ms=elapsed + backoff_ms,
                note=f"retry backoff {backoff_ms}ms (backoff_ms * failed attempt {state.attempt}) before attempt {state.attempt + 1}",
            )
            next_state = _State(None, state.completed, state.attempt + 1, _DISCONNECTED, drops_used, timeouts_used)
            return [("state", next_state, trace + (close, backoff), None)]
        failure = FailureInfo(
            code=E_STEP_RETRY_EXHAUSTED,
            last_code=cause.code,
            category=cause.category,
            step_id=self.session.steps[state.completed].step_id,
            failed_step_id=cause.step_id,
            attempt=state.attempt,
            phase=cause.phase,
            expected_screen=cause.expected,
            actual_screen=cause.actual,
            fault=cause.fault,
            message=(
                f"step {self.session.steps[state.completed].step_id!r} exhausted {state.attempt} attempt(s); "
                f"last failure: {cause.category} at {cause.step_id!r} ({cause.phase})"
            ),
        )
        return [("failure", None, trace + (close,), failure)]

    # -- exploration ---------------------------------------------------------

    def run(self) -> PrecheckResult:
        session = self.session
        start = self.scenario.start_screen
        # collect(): open the first connection and check the start expectation.
        # A mismatch here is terminal - the live client does not retry it.
        connect = _entry(0, "connect", phase="collect", elapsed_ms=0, note=f"open connection; mock pushes start screen {start!r}")
        missing = self._missing(start, session.start)
        read_start = _entry(
            1, "read_start", step_id="start", phase="collect", screen=start, expected=session.start.screen_id,
            actual=start, accepted=not missing, elapsed_ms=0,
            note="start expectation satisfied" if not missing else f"start expectation missing fragments: {missing}",
        )
        base_trace = (connect, read_start)
        if missing:
            failure = FailureInfo(
                code=E_SCREEN_UNEXPECTED, last_code=E_SCREEN_UNEXPECTED, category=CATEGORY_START_MISMATCH,
                step_id="start", failed_step_id="start", attempt=0, phase="collect", expected_screen=session.start.screen_id,
                actual_screen=start, fault=None,
                message="start screen does not satisfy the session start expectation; collect() fails without retry",
            )
            status = STATUS_UNREACHABLE if self.options.mode == MODE_NOMINAL else STATUS_VIOLATED
            return self._result(status, base_trace, failure, states_explored=0)
        initial = _State(start, 0, 1, _READY, 0, 0)
        visited = {initial}
        queue: deque[tuple[_State, tuple[TraceEntry, ...]]] = deque([(initial, base_trace)])
        best_success: tuple[TraceEntry, ...] | None = None
        while queue:
            state, trace = queue.popleft()
            elapsed = trace[-1].elapsed_ms
            for kind, next_state, outcome_trace, failure in self._expand(state, elapsed, trace):
                self.transitions_explored += 1
                if kind == "success":
                    if self.options.mode == MODE_NOMINAL:
                        return self._result(STATUS_REACHABLE, outcome_trace, None, len(visited))
                    if best_success is None:
                        best_success = outcome_trace
                    continue
                if kind == "failure":
                    # Breadth-first with a fixed branch order (fault-free, then
                    # injected drop, then injected timeout) makes this the
                    # deterministic shortest counterexample.
                    status = STATUS_UNREACHABLE if self.options.mode == MODE_NOMINAL else STATUS_VIOLATED
                    return self._result(status, outcome_trace, failure, len(visited))
                if next_state not in visited:
                    if len(visited) >= self.options.state_limit:
                        return self._result(STATUS_UNPROVEN, (), None, len(visited))
                    visited.add(next_state)
                    queue.append((next_state, outcome_trace))
        if self.options.mode == MODE_GUARANTEE:
            return self._result(STATUS_GUARANTEED, best_success or (), None, len(visited))
        # Nominal mode explores a single deterministic trajectory and always
        # terminates with success or failure above; reaching here is a defect.
        return self._result(STATUS_UNPROVEN, (), None, len(visited))

    def _result(self, status: str, trace: tuple[TraceEntry, ...], failure: FailureInfo | None, states_explored: int) -> PrecheckResult:
        proven = status in (STATUS_REACHABLE, STATUS_GUARANTEED)
        elapsed_ms = trace[-1].elapsed_ms if trace else 0
        if status == STATUS_REACHABLE:
            summary = f"nominal trajectory collects the start frame plus all {self.step_count} step frame(s)"
        elif status == STATUS_GUARANTEED:
            summary = f"every trajectory within the injected-fault budget collects the start frame plus all {self.step_count} step frame(s)"
        elif status in (STATUS_UNREACHABLE, STATUS_VIOLATED) and failure is not None:
            summary = f"shortest counterexample: {failure.category} at step {failure.step_id!r} (attempt {failure.attempt})"
        else:
            summary = f"state space exceeded the configured limit ({self.options.state_limit}); reachability not proven"
        return PrecheckResult(
            mode=self.options.mode,
            status=status,
            proven=proven,
            session_id=self.session.session_id,
            scenario_id=self.scenario.scenario_id,
            state_limit=self.options.state_limit,
            budget=self.options.budget,
            states_explored=states_explored,
            transitions_explored=self.transitions_explored,
            key_send_points=self.key_send_points,
            injected_branches=self.injected_branches,
            replay_injected_branches=self.replay_injected_branches,
            max_faults_used=self.max_faults_used,
            elapsed_ms=elapsed_ms,
            trace=trace,
            failure=failure,
            scope=PROOF_SCOPE,
            summary=summary,
        )

def check_session_reachability(session: SessionContract, scenario: MockScenario, options: PrecheckOptions | None = None) -> PrecheckResult:
    """Run the bounded reachability precheck; never opens a connection."""
    return _Explorer(session, scenario, options or PrecheckOptions()).run()

def format_result(result: PrecheckResult) -> str:
    """Human-readable report: verdict, witness/counterexample, proof scope."""
    lines = [
        f"precheck session={result.session_id} scenario={result.scenario_id} mode={result.mode}",
        f"status={result.status} proven={str(result.proven).lower()}",
        (
            f"explored states={result.states_explored} transitions={result.transitions_explored} "
            f"key_send_points={result.key_send_points} injected_branches={result.injected_branches} "
            f"replay_injected_branches={result.replay_injected_branches} max_faults_used={result.max_faults_used} "
            f"state_limit={result.state_limit}"
        ),
        f"summary: {result.summary}",
    ]
    if result.failure is not None:
        failure = result.failure
        lines.append(
            f"failure code={failure.code} last_code={failure.last_code} category={failure.category} "
            f"step={failure.step_id} failed_step={failure.failed_step_id} attempt={failure.attempt} phase={failure.phase} "
            f"expected={failure.expected_screen} actual={failure.actual_screen} fault={failure.fault}"
        )
    if result.trace:
        lines.append("trace:")
        for entry in result.trace:
            line = f"  #{entry.seq:02d} t={entry.elapsed_ms}ms {entry.action} step={entry.step_id} attempt={entry.attempt} phase={entry.phase}"
            if entry.screen is not None:
                line += f" screen={entry.screen}"
            if entry.key is not None:
                line += f" key={entry.key}"
            if entry.expected_screen is not None:
                line += f" expected={entry.expected_screen}"
            if entry.actual_screen is not None:
                line += f" actual={entry.actual_screen}"
            if entry.fault is not None:
                line += f" fault={entry.fault}"
            if entry.accepted is not None:
                line += f" accepted={entry.accepted}"
            if entry.note:
                line += f" :: {entry.note}"
            lines.append(line)
    lines.append("proof scope / 证明范围:")
    for item in result.scope:
        lines.append(f"  - {item}")
    return "\n".join(lines)
