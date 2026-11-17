"""Tests for the offline session reachability precheck (session_model)."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from termharvest.clock import FixedClock
from termharvest.config_session import load_session, summarize_session
from termharvest.config_types import Expectation, RetryConfig, SessionContract, SessionStep
from termharvest.errors import HarvestError
from termharvest.mock_scenario import MockScenario, ScenarioScreen, load_scenario
from termharvest.mockhost import MockTerminalServer
from termharvest.screen import FRAME_COLS, FRAME_ROWS, ScreenFrame
from termharvest.session import TerminalSession
from termharvest.session_model import (
    FAULT_INJECTED_DROP,
    FAULT_SCENARIO_DELAY,
    FAULT_SCENARIO_DROP,
    MODE_GUARANTEE,
    MODE_NOMINAL,
    STATUS_GUARANTEED,
    STATUS_REACHABLE,
    STATUS_UNPROVEN,
    STATUS_UNREACHABLE,
    STATUS_VIOLATED,
    FaultBudget,
    PrecheckOptions,
    check_session_reachability,
    format_result,
)

# ---------------------------------------------------------------------------
# builders


def make_frame(*lines: str) -> ScreenFrame:
    rows = [line.ljust(FRAME_COLS)[:FRAME_COLS] for line in lines]
    rows += [" " * FRAME_COLS] * (FRAME_ROWS - len(rows))
    return ScreenFrame.from_rows(rows)


def make_scenario(
    screens: dict[str, tuple[tuple[str, ...], dict[str, str]]],
    *,
    start: str = "menu",
    drop_keys=(),
    delay_ms=None,
    unknown_key_screen=None,
    scenario_id: str = "test_scenario",
) -> MockScenario:
    return MockScenario(
        scenario_id=scenario_id,
        start_screen=start,
        screens={sid: ScenarioScreen(sid, make_frame(*lines), dict(transitions)) for sid, (lines, transitions) in screens.items()},
        delay_ms=dict(delay_ms or {}),
        drop_keys=frozenset(drop_keys),
        unknown_key_screen=unknown_key_screen,
        source_file="<test>",
    )


def make_session(
    steps: list[tuple[str, str, str, tuple[str, ...]]],
    *,
    start_screen: str = "menu",
    start_contains=("MAIN MENU",),
    attempts: int = 2,
    timeout: float = 0.2,
    backoff_ms: int = 50,
    session_id: str = "test_session",
) -> SessionContract:
    return SessionContract(
        session_id=session_id,
        target_host="127.0.0.1",
        target_port=19771,
        start=Expectation(start_screen, tuple(start_contains)),
        steps=tuple(SessionStep(i, step_id, key, Expectation(expect_screen, tuple(contains))) for i, (step_id, key, expect_screen, contains) in enumerate(steps)),
        paging=None,
        retry=RetryConfig(attempts=attempts, timeout_seconds=timeout, backoff_ms=backoff_ms),
        source_file="<test>",
    )


# The three-step synthetic scenario from the task description: a clean linear
# menu -> s1 -> s2 -> s3 navigation with no scenario-fixed faults.
THREE_STEP_SCREENS = {
    "menu": (("MAIN MENU", "PICK AN OPTION"), {"K1": "s1"}),
    "s1": (("SCREEN ONE",), {"K2": "s2"}),
    "s2": (("SCREEN TWO",), {"K3": "s3"}),
    "s3": (("SCREEN THREE",), {}),
}
THREE_STEP_STEPS = [
    ("st1", "K1", "s1", ("SCREEN ONE",)),
    ("st2", "K2", "s2", ("SCREEN TWO",)),
    ("st3", "K3", "s3", ("SCREEN THREE",)),
]


def three_step_scenario(**kwargs) -> MockScenario:
    return make_scenario(THREE_STEP_SCREENS, **kwargs)


def three_step_session(**kwargs) -> SessionContract:
    return make_session(THREE_STEP_STEPS, **kwargs)


def actions(result) -> list[str]:
    return [entry.action for entry in result.trace]


# ---------------------------------------------------------------------------
# no-fault paths


def test_nominal_reachable_on_clean_scenario():
    result = check_session_reachability(three_step_session(), three_step_scenario(), PrecheckOptions(mode=MODE_NOMINAL))
    assert result.status == STATUS_REACHABLE
    assert result.proven
    assert result.failure is None
    # The reviewable success path: connect, start read, then send/receive per step.
    assert actions(result) == ["connect", "read_start", "send_key", "receive", "send_key", "receive", "send_key", "receive"]
    assert [entry.key for entry in result.trace if entry.action == "send_key"] == ["K1", "K2", "K3"]
    assert all(entry.accepted for entry in result.trace if entry.action == "receive")
    assert result.elapsed_ms == 0
    assert result.scope  # proof scope is always attached


def test_guarantee_passes_on_clean_scenario_with_attempts_2():
    result = check_session_reachability(three_step_session(attempts=2), three_step_scenario(), PrecheckOptions(mode=MODE_GUARANTEE))
    assert result.status == STATUS_GUARANTEED
    assert result.proven
    # The single injected fault is always recoverable by the one retry.
    assert result.max_faults_used == 1
    assert result.injected_branches > 0
    # The reported sample path is the fault-free one.
    assert not [entry for entry in result.trace if entry.fault]


def test_guarantee_violated_with_attempts_1_by_single_key_drop():
    session = three_step_session(attempts=1)
    result = check_session_reachability(session, three_step_scenario(), PrecheckOptions(mode=MODE_GUARANTEE))
    assert result.status == STATUS_VIOLATED
    assert not result.proven
    failure = result.failure
    assert failure.code == "E_STEP_RETRY_EXHAUSTED"
    assert failure.last_code == "E_FRAME_TIMEOUT"
    assert failure.category == "key_dropped"
    # Exactly one injected fault appears on the counterexample: with attempts=1
    # a single key loss kills the step and there is no reconnect+replay.
    faults = [entry for entry in result.trace if entry.fault]
    assert len(faults) == 1
    assert faults[0].fault == FAULT_INJECTED_DROP
    # BFS finds the shortest counterexample: the fault strikes the first key
    # send, so no reconnect/replay entries appear on the trace.
    assert "backoff" not in actions(result)
    assert failure.step_id == "st1"
    assert failure.attempt == 1


def test_task_contract_three_step_scenario_both_modes():
    scenario = three_step_scenario()
    # attempts=1: nominal passes, guarantee is violated by one injected drop.
    nominal = check_session_reachability(three_step_session(attempts=1), scenario, PrecheckOptions(mode=MODE_NOMINAL))
    assert nominal.status == STATUS_REACHABLE
    violated = check_session_reachability(three_step_session(attempts=1), scenario, PrecheckOptions(mode=MODE_GUARANTEE))
    assert violated.status == STATUS_VIOLATED
    assert violated.failure.category == "key_dropped"
    # attempts=2: the reconnect+replay path is consistent, so both modes pass.
    nominal2 = check_session_reachability(three_step_session(attempts=2), scenario, PrecheckOptions(mode=MODE_NOMINAL))
    guaranteed2 = check_session_reachability(three_step_session(attempts=2), scenario, PrecheckOptions(mode=MODE_GUARANTEE))
    assert nominal2.status == STATUS_REACHABLE
    assert guaranteed2.status == STATUS_GUARANTEED


# ---------------------------------------------------------------------------
# scenario-fixed faults: drops and delays


def test_nominal_unreachable_when_scenario_drops_step_key():
    scenario = three_step_scenario(drop_keys=["K2"])
    result = check_session_reachability(three_step_session(attempts=2), scenario, PrecheckOptions(mode=MODE_NOMINAL))
    assert result.status == STATUS_UNREACHABLE
    assert result.failure.code == "E_STEP_RETRY_EXHAUSTED"
    assert result.failure.category == "key_dropped"
    assert result.failure.step_id == "st2"
    assert result.failure.attempt == 2
    # The fixed drop strikes on every attempt: two scenario_drop timeouts.
    drops = [entry for entry in result.trace if entry.fault == FAULT_SCENARIO_DROP]
    assert len(drops) == 2
    # Deterministic timing: timeout(200) + backoff(50*1) + timeout(200) = 450ms.
    assert result.elapsed_ms == 450
    # The retry reconnected and replayed the accepted step K1 before resending K2.
    replay_sends = [entry for entry in result.trace if entry.action == "send_key" and entry.phase == "replay"]
    assert [entry.key for entry in replay_sends] == ["K1"]


def test_nominal_unreachable_when_delay_exceeds_timeout():
    scenario = three_step_scenario(delay_ms={"K2": 500})
    result = check_session_reachability(three_step_session(attempts=2, timeout=0.2), scenario, PrecheckOptions(mode=MODE_NOMINAL))
    assert result.status == STATUS_UNREACHABLE
    assert result.failure.category == "response_timeout"
    assert result.failure.last_code == "E_FRAME_TIMEOUT"
    delays = [entry for entry in result.trace if entry.fault == FAULT_SCENARIO_DELAY]
    assert len(delays) == 2  # fixed fault: strikes on both attempts
    assert all(entry.elapsed_ms % 250 == 200 for entry in delays)  # 200ms then 450ms


def test_delay_within_timeout_is_reachable_and_timed():
    scenario = three_step_scenario(delay_ms={"K2": 50})
    result = check_session_reachability(three_step_session(timeout=0.2), scenario, PrecheckOptions(mode=MODE_NOMINAL))
    assert result.status == STATUS_REACHABLE
    receives = [entry for entry in result.trace if entry.action == "receive"]
    assert [entry.elapsed_ms for entry in receives] == [0, 50, 50]


def test_fixed_faults_ignore_injected_budget_in_guarantee_mode():
    # The scenario drops K2 on every attempt; the injected budget cannot
    # prevent a scenario-fixed fault. The shortest counterexample: attempt 1
    # hits the fixed drop on K2, the retry's replay of K1 is then hit by the
    # single injected drop - a fault at a replay position.
    scenario = three_step_scenario(drop_keys=["K2"])
    result = check_session_reachability(three_step_session(attempts=2), scenario, PrecheckOptions(mode=MODE_GUARANTEE))
    assert result.status == STATUS_VIOLATED
    assert result.failure.code == "E_STEP_RETRY_EXHAUSTED"
    assert result.failure.step_id == "st2"          # step the client tried to complete
    assert result.failure.failed_step_id == "st1"   # its retry failed while replaying st1
    assert result.failure.phase == "replay"
    assert result.failure.fault == FAULT_INJECTED_DROP
    # The fixed drop struck attempt 1 without consuming the injected budget...
    assert any(entry.fault == FAULT_SCENARIO_DROP for entry in result.trace)
    # ...and the one allowed injected fault struck during the replay of K1.
    replay_timeouts = [entry for entry in result.trace if entry.phase == "replay" and entry.fault == FAULT_INJECTED_DROP]
    assert len(replay_timeouts) == 1
    assert result.max_faults_used == 1
    assert result.replay_injected_branches > 0


# ---------------------------------------------------------------------------
# transition table problems


def test_missing_transition_replays_current_screen():
    scenario = make_scenario(
        {
            "menu": (("MAIN MENU",), {}),  # no transition for K1 at all
            "s1": (("SCREEN ONE",), {}),
        }
    )
    session = make_session([("st1", "K1", "s1", ("SCREEN ONE",))], attempts=1)
    result = check_session_reachability(session, scenario, PrecheckOptions(mode=MODE_NOMINAL))
    assert result.status == STATUS_UNREACHABLE
    assert result.failure.category == "missing_transition"
    assert result.failure.last_code == "E_SCREEN_UNEXPECTED"
    # The mock stays on the current screen; the client re-reads "menu".
    assert result.failure.actual_screen == "menu"
    assert result.failure.expected_screen == "s1"


def test_unknown_key_screen_is_reported():
    scenario = make_scenario(
        {
            "menu": (("MAIN MENU",), {}),
            "oops": (("UNKNOWN KEY - BACK TO MENU",), {}),
        },
        unknown_key_screen="oops",
    )
    session = make_session([("st1", "K1", "s1", ("SCREEN ONE",))], attempts=1)
    result = check_session_reachability(session, scenario, PrecheckOptions(mode=MODE_NOMINAL))
    assert result.status == STATUS_UNREACHABLE
    assert result.failure.category == "unknown_key_screen"
    assert result.failure.actual_screen == "oops"


def test_start_mismatch_fails_without_retry():
    session = make_session([("st1", "K1", "s1", ("SCREEN ONE",))], start_contains=("NO SUCH BANNER",), attempts=3)
    result = check_session_reachability(session, three_step_scenario(), PrecheckOptions(mode=MODE_NOMINAL))
    assert result.status == STATUS_UNREACHABLE
    assert result.failure.code == "E_SCREEN_UNEXPECTED"
    assert result.failure.category == "start_mismatch"
    # collect() does not retry the initial start read: exactly connect+read.
    assert actions(result) == ["connect", "read_start"]


# ---------------------------------------------------------------------------
# appeared vs accepted / replay drift


def test_target_screen_appearing_is_not_acceptance():
    # "ledger" exists in the scenario and is even transition-reachable from the
    # menu, and its marker text appears on the start screen the client reads.
    # But the client's key sequence sends L from "profile", which has no such
    # transition, so step 2 is never accepted at the correct position.
    scenario = make_scenario(
        {
            "menu": (("MAIN MENU", "LEDGER"), {"C": "profile", "L": "ledger"}),
            "profile": (("PROFILE PAGE",), {}),
            "ledger": (("LEDGER",), {}),
        }
    )
    session = make_session(
        [
            ("open_profile", "C", "profile_screen", ("PROFILE PAGE",)),
            ("open_ledger", "L", "ledger_screen", ("LEDGER",)),
        ],
        attempts=2,
    )
    result = check_session_reachability(session, scenario, PrecheckOptions(mode=MODE_NOMINAL))
    assert result.status == STATUS_UNREACHABLE
    assert result.failure.step_id == "open_ledger"
    assert result.failure.category == "missing_transition"
    assert result.failure.actual_screen == "profile"
    # The model tracked the actual mock screen ("profile"), not the expected
    # screen id ("profile_screen"): the replay after reconnect re-accepts
    # step 1 on "profile" and the drifted L read stays on "profile".
    replay_receives = [entry for entry in result.trace if entry.phase == "replay" and entry.action == "receive"]
    assert replay_receives
    assert all(entry.actual_screen == "profile" and entry.expected_screen == "profile_screen" for entry in replay_receives)
    assert all(entry.accepted for entry in replay_receives)
    # The target screen was never delivered to the client on any receive.
    assert not [entry for entry in result.trace if entry.actual_screen == "ledger"]


def test_replay_position_is_a_fault_injection_point():
    # A scenario-fixed fault forces a reconnect+replay while the injected
    # budget is still unspent; the model then also injects faults while
    # replaying accepted steps, not only at the current step's own key.
    scenario = three_step_scenario(drop_keys=["K2"])
    result = check_session_reachability(three_step_session(attempts=2), scenario, PrecheckOptions(mode=MODE_GUARANTEE))
    assert result.status == STATUS_VIOLATED
    assert result.replay_injected_branches > 0
    # The shortest counterexample fails exactly at such a replay position.
    assert result.failure.phase == "replay"


# ---------------------------------------------------------------------------
# budget and state limit


def test_injected_budget_is_one_total_not_per_step():
    result = check_session_reachability(three_step_session(attempts=2), three_step_scenario(), PrecheckOptions(mode=MODE_GUARANTEE))
    assert result.status == STATUS_GUARANTEED
    # No explored state ever spent more than one fault in total.
    assert result.max_faults_used == 1


def test_zero_budget_guarantee_matches_nominal():
    options = PrecheckOptions(mode=MODE_GUARANTEE, budget=FaultBudget(max_drops=0, max_timeouts=0, max_total=0))
    result = check_session_reachability(three_step_session(), three_step_scenario(), options)
    assert result.status == STATUS_GUARANTEED
    assert result.injected_branches == 0


def test_state_limit_returns_unproven_not_success():
    session = three_step_session()
    scenario = three_step_scenario()
    nominal = check_session_reachability(session, scenario, PrecheckOptions(mode=MODE_NOMINAL, state_limit=1))
    assert nominal.status == STATUS_UNPROVEN
    assert not nominal.proven
    assert nominal.trace == ()
    guarantee = check_session_reachability(session, scenario, PrecheckOptions(mode=MODE_GUARANTEE, state_limit=3))
    assert guarantee.status == STATUS_UNPROVEN
    assert not guarantee.proven
    # A generous limit proves the same configuration.
    ok = check_session_reachability(session, scenario, PrecheckOptions(mode=MODE_GUARANTEE, state_limit=20000))
    assert ok.status == STATUS_GUARANTEED


def test_invalid_options_are_rejected():
    with pytest.raises(ValueError):
        PrecheckOptions(mode="sometimes")
    with pytest.raises(ValueError):
        PrecheckOptions(state_limit=0)
    with pytest.raises(ValueError):
        FaultBudget(max_total=-1)


# ---------------------------------------------------------------------------
# deterministic shortest counterexample


def test_counterexample_is_deterministic_and_shortest():
    session = three_step_session(attempts=1)
    scenario = three_step_scenario()
    options = PrecheckOptions(mode=MODE_GUARANTEE)
    first = check_session_reachability(session, scenario, options)
    second = check_session_reachability(session, scenario, options)
    assert first == second  # fully deterministic, including the trace
    # Shortest possible failure: connect, start read, first key send, the
    # injected timeout, close. No shorter failing trajectory exists.
    assert actions(first) == ["connect", "read_start", "send_key", "timeout", "close"]
    assert first.trace[-1].elapsed_ms == 200  # one client timeout, no backoff


def test_format_result_renders_trace_failure_and_scope():
    result = check_session_reachability(three_step_session(attempts=1), three_step_scenario(), PrecheckOptions(mode=MODE_GUARANTEE))
    text = format_result(result)
    assert "status=violated" in text
    assert "injected_drop" in text
    assert "E_STEP_RETRY_EXHAUSTED" in text
    assert "proof scope" in text
    assert "不连接真实终端" in text
    assert "生产" in text  # does not claim production-host guarantees


# ---------------------------------------------------------------------------
# shared semantics with the live client and the mock server


def test_plan_response_matches_mock_semantics():
    scenario = make_scenario(
        {
            "menu": (("MAIN MENU",), {"GO": "s1", "SLOW": "s1"}),
            "s1": (("SCREEN ONE",), {}),
            "oops": (("OOPS",), {}),
        },
        drop_keys=["DEAD"],
        delay_ms={"SLOW": 30},
        unknown_key_screen="oops",
    )
    plan = scenario.plan_response("menu", "GO")
    assert (plan.kind, plan.screen, plan.delay_ms, plan.via) == ("answer", "s1", 0, "transition")
    plan = scenario.plan_response("menu", "SLOW")
    assert (plan.kind, plan.screen, plan.delay_ms, plan.via) == ("answer", "s1", 30, "transition")
    plan = scenario.plan_response("menu", "DEAD")
    assert (plan.kind, plan.screen) == ("drop", None)
    plan = scenario.plan_response("menu", "WHAT")
    assert (plan.kind, plan.screen, plan.via) == ("answer", "oops", "unknown_key_screen")
    plan = scenario.plan_response("s1", "WHAT")
    # s1 has no transitions, but the scenario-wide unknown_key_screen applies.
    assert (plan.kind, plan.screen, plan.via) == ("answer", "oops", "unknown_key_screen")
    no_unknown = three_step_scenario()
    plan = no_unknown.plan_response("s3", "WHAT")
    assert (plan.kind, plan.screen, plan.via) == ("answer", "s3", "missing_transition")


# ---------------------------------------------------------------------------
# cross-validation against real loopback runs


def scenario_json(scenario: MockScenario) -> str:
    return json.dumps(
        {
            "scenario_id": scenario.scenario_id,
            "start_screen": scenario.start_screen,
            "screens": {sid: {"rows": list(screen.frame.rows), "transitions": screen.transitions} for sid, screen in scenario.screens.items()},
            "drop_keys": sorted(scenario.drop_keys),
            "delay_ms": scenario.delay_ms,
            **({"unknown_key_screen": scenario.unknown_key_screen} if scenario.unknown_key_screen else {}),
        }
    )


def run_live(session: SessionContract, scenario: MockScenario):
    server = MockTerminalServer(scenario, port=0, frame_timeout=2.0)
    server.start()
    try:
        live = replace(session, target_port=server.actual_port)
        return TerminalSession(live, clock=FixedClock()).collect()
    finally:
        server.stop()


def test_loopback_run_matches_nominal_reachable_prediction():
    session = three_step_session(attempts=2)
    scenario = three_step_scenario()
    predicted = check_session_reachability(session, scenario, PrecheckOptions(mode=MODE_NOMINAL))
    assert predicted.status == STATUS_REACHABLE
    frames = run_live(session, scenario)
    assert [frame.step_id for frame in frames] == ["start", "st1", "st2", "st3"]
    assert [frame.expected_screen for frame in frames] == ["menu", "s1", "s2", "s3"]
    # The frames the live client captured are exactly the screens the model
    # predicted on its success path.
    predicted_screens = ["menu"] + [entry.actual_screen for entry in predicted.trace if entry.action == "receive"]
    actual_screens = []
    for frame in frames:
        for sid, screen in scenario.screens.items():
            if screen.frame.sha256 == frame.frame.sha256:
                actual_screens.append(sid)
                break
    assert actual_screens == predicted_screens


def test_loopback_run_matches_unreachable_prediction():
    session = three_step_session(attempts=2)
    scenario = three_step_scenario(drop_keys=["K2"])
    predicted = check_session_reachability(session, scenario, PrecheckOptions(mode=MODE_NOMINAL))
    assert predicted.status == STATUS_UNREACHABLE
    assert predicted.failure.category == "key_dropped"
    with pytest.raises(HarvestError) as caught:
        run_live(session, scenario)
    assert caught.value.code == "E_STEP_RETRY_EXHAUSTED"
    assert caught.value.context["step_id"] == "st2"


def test_loopback_run_matches_delay_timeout_prediction():
    session = three_step_session(attempts=1, timeout=0.2)
    scenario = three_step_scenario(delay_ms={"K2": 500})
    predicted = check_session_reachability(session, scenario, PrecheckOptions(mode=MODE_NOMINAL))
    assert predicted.status == STATUS_UNREACHABLE
    assert predicted.failure.category == "response_timeout"
    with pytest.raises(HarvestError) as caught:
        run_live(session, scenario)
    assert caught.value.code == "E_STEP_RETRY_EXHAUSTED"
    assert caught.value.context["last_code"] == "E_FRAME_TIMEOUT"


# ---------------------------------------------------------------------------
# loading from real files and the CLI


def write_session_file(tmp_path, *, attempts: int, timeout: float = 0.2) -> str:
    path = tmp_path / "session.yaml"
    path.write_text(
        "\n".join(
            [
                "version: 1",
                "session_id: file_session",
                "target: {host: 127.0.0.1, port: 19771}",
                "start: {screen_id: menu, contains: [MAIN MENU]}",
                f"retry: {{attempts: {attempts}, timeout_seconds: {timeout}, backoff_ms: 50}}",
                "steps:",
                "  - {id: st1, key: K1, expect: {screen_id: s1, contains: [SCREEN ONE]}}",
                "  - {id: st2, key: K2, expect: {screen_id: s2, contains: [SCREEN TWO]}}",
                "  - {id: st3, key: K3, expect: {screen_id: s3, contains: [SCREEN THREE]}}",
            ]
        ),
        encoding="utf-8",
    )
    return str(path)


def test_precheck_from_loaded_files(tmp_path):
    session_path = write_session_file(tmp_path, attempts=2)
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(scenario_json(three_step_scenario()), encoding="utf-8")
    session = load_session(session_path)
    scenario = load_scenario(scenario_path)
    result = check_session_reachability(session, scenario, PrecheckOptions(mode=MODE_GUARANTEE))
    assert result.status == STATUS_GUARANTEED
    summary = summarize_session(session)
    assert summary["session_id"] == "file_session"
    assert [step["key"] for step in summary["steps"]] == ["K1", "K2", "K3"]


def test_cli_precheck_exit_codes_and_output(tmp_path, capsys):
    from termharvest.cli import main

    session_path = write_session_file(tmp_path, attempts=2)
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(scenario_json(three_step_scenario()), encoding="utf-8")

    assert main(["precheck", "--session", session_path, "--scenario", str(scenario_path), "--mode", "guarantee"]) == 0
    out = capsys.readouterr().out
    assert "status=guaranteed" in out
    assert "proof scope" in out

    assert main(["precheck", "--session", session_path, "--scenario", str(scenario_path), "--state-limit", "1"]) == 2
    out = capsys.readouterr().out
    assert "status=unproven" in out

    failing_session = write_session_file(tmp_path, attempts=1)
    assert main(["precheck", "--session", failing_session, "--scenario", str(scenario_path), "--mode", "guarantee", "--json"]) == 1
    out = capsys.readouterr().out
    payload = json.loads(out.split("PRECHECK_RESULT ", 1)[1])
    assert payload["result"]["status"] == STATUS_VIOLATED
    assert payload["result"]["failure"]["category"] == "key_dropped"
    assert payload["result"]["scope"]
    assert payload["session"]["retry"]["attempts"] == 1


def test_cli_precheck_rejects_bad_state_limit(tmp_path, capsys):
    from termharvest.cli import main

    session_path = write_session_file(tmp_path, attempts=2)
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(scenario_json(three_step_scenario()), encoding="utf-8")
    assert main(["precheck", "--session", session_path, "--scenario", str(scenario_path), "--state-limit", "0"]) == 1
    assert "E_CONFIG_SCHEMA" in capsys.readouterr().err
