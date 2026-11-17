from __future__ import annotations

from termharvest.validate_checks import run_ownership_checks

from ownership_helpers import bill_record, customer_record, make_ownership_check

SCREEN = "demo_synth_bills"


def run(records, check, *, facts=None, sources=None):
    errs, results = run_ownership_checks(
        records,
        None,
        (check,),
        facts_by_screen={SCREEN: facts or {}},
        fact_sources_by_screen={SCREEN: sources or {}},
    )
    return errs, results[0]


def xy_records():
    """X=80 OPEN can go A/B; Y=20 OPEN only A (the prompt's canonical page)."""
    return [
        bill_record("B000001", "80.00", "OPEN", "A,B", "k-x"),
        bill_record("B000002", "20.00", "OPEN", "A", "k-y"),
    ]


def edge_map(result):
    return {edge["bill_id"]: edge["bucket"] for edge in result["minimum_witness"]["edges"]}


def test_footer_pins_unique_feasible_assignment_and_proves_safe():
    """Greedy X->A would strand Y; footers force X->B, Y->A and prove A safe."""
    check = make_ownership_check()
    facts = {"a_count": "1", "a_total": "20.00", "b_count": "1", "b_total": "80.00"}
    errs, result = run(xy_records(), check, facts=facts)
    assert errs == []
    assert result["status"] == "proven"
    assert result["open_minimum"] == "20.00"
    assert result["open_maximum"] == "20.00"
    assignment = edge_map(result)
    assert assignment == {"B000001": "B", "B000002": "A"}
    assert result["maximum_witness"]["one_to_one"] is True
    assert result["credit_state"] == "safe"
    assert result["credit_action"] == "pass"
    # every bill is used exactly once across both witnesses
    for witness in (result["minimum_witness"], result["maximum_witness"]):
        buckets_used = [edge["bucket"] for edge in witness["edges"]]
        assert sorted(buckets_used) == ["A", "B"]
        assert len({edge["bill_id"] for edge in witness["edges"]}) == 2


def test_without_footers_bounds_span_20_to_100_and_compliance_holds():
    check = make_ownership_check(purpose="compliance")
    errs, result = run(xy_records(), check)
    assert errs == []
    assert result["status"] == "proven"
    assert result["open_minimum"] == "20.00"
    assert result["open_maximum"] == "100.00"
    # min witness keeps X out of A, max witness puts X into A
    assert edge_map(result)["B000001"] == "B"
    max_edges = {edge["bill_id"]: edge["bucket"] for edge in result["maximum_witness"]["edges"]}
    assert max_edges == {"B000001": "A", "B000002": "A"}
    # 50 lies inside [20, 100]: neither safety nor breach is provable
    assert result["credit_state"] == "inconclusive"
    assert result["credit_action"] == "hold_for_manual_review"


def test_manual_review_may_list_safe_witness_but_never_passes():
    review = make_ownership_check(purpose="manual_review")
    _, result = run(xy_records(), review)
    assert result["credit_state"] == "inconclusive"
    assert result["credit_action"] == "review_with_safe_witness"
    assert result["safe_witness_available"] is True
    safe_edges = {edge["bill_id"]: edge["bucket"] for edge in result["safe_witness"]["edges"]}
    assert safe_edges["B000001"] == "B"
    # possible compliance must not be reported as certain compliance
    assert result["credit_state"] != "safe"

    compliance = make_ownership_check(purpose="compliance")
    _, compliant = run(xy_records(), compliance)
    assert "safe_witness" not in compliant
    assert compliant["credit_action"] == "hold_for_manual_review"


def test_multiple_attributions_same_amount_conclusion():
    """An extra candidate edge that the footers cannot use must not change the answer.

    With A pinned to one bill of 20 and B pinned to one bill of 80, letting Y
    also carry a B edge leaves the unique feasible joint assignment unchanged.
    """
    records = [
        bill_record("B000001", "80.00", "OPEN", "A,B", "k-x"),
        bill_record("B000002", "20.00", "OPEN", "A,B", "k-y"),
    ]
    facts = {"a_count": "1", "a_total": "20.00", "b_count": "1", "b_total": "80.00"}
    _, result = run(records, make_ownership_check(), facts=facts)
    assert result["status"] == "proven"
    assert result["open_minimum"] == "20.00"
    assert result["open_maximum"] == "20.00"
    assert edge_map(result) == {"B000001": "B", "B000002": "A"}
    assert result["credit_state"] == "safe"


def test_no_feasible_assignment_returns_conflict_core_not_zero():
    # A footer demands total 80 with one bill, but only Y (20) can enter A.
    facts = {"a_count": "1", "a_total": "80.00", "b_count": "1", "b_total": "20.00"}
    errs, result = run(xy_records(), make_ownership_check(), facts=facts)
    assert result["status"] == "infeasible"
    assert "open_minimum" not in result
    assert [error["code"] for error in errs] == ["E_OWNERSHIP_INFEASIBLE"]
    core = result["conflict_core"]
    assert core["bill_count"] == 2
    # the core must name something concrete: a footer violation or the joint conflict
    assert core["footer_violations"] or core.get("joint_footer_conflict")
    # it must never pretend exposure is zero
    assert result.get("open_minimum") not in ("0", "0.00", 0)


def test_paid_and_void_bills_are_assigned_but_excluded_from_open_exposure():
    records = [
        bill_record("B000001", "80.00", "PAID", "A,B", "k-x"),
        bill_record("B000002", "20.00", "VOID", "A", "k-y"),
    ]
    facts = {"a_total": "20.00", "b_total": "80.00"}
    errs, result = run(records, make_ownership_check(), facts=facts)
    assert errs == []
    assert result["open_minimum"] == "0"
    assert result["open_maximum"] == "0"
    assert result["credit_state"] == "safe"
    # both non-OPEN bills still had to be placed one-to-one
    assert edge_map(result) == {"B000001": "B", "B000002": "A"}


def test_duplicate_bill_id_is_invalid_and_blocks_assignment():
    records = [
        bill_record("B000001", "80.00", "OPEN", "A,B", "k-1"),
        bill_record("B000001", "20.00", "OPEN", "A", "k-2"),
    ]
    errs, result = run(records, make_ownership_check())
    assert result["status"] == "invalid"
    codes = {error["code"] for error in errs}
    assert codes == {"E_OWNERSHIP_DUPLICATE_BILL"}
    assert "open_minimum" not in result


def test_unknown_bucket_in_mask_is_invalid_with_frame_provenance():
    records = [
        bill_record("B000001", "80.00", "OPEN", "C", "k-x"),
        bill_record("B000002", "20.00", "OPEN", "A", "k-y"),
    ]
    errs, result = run(records, make_ownership_check())
    assert result["status"] == "invalid"
    assert [error["code"] for error in errs] == ["E_OWNERSHIP_INVALID_MASK"]
    assert errs[0]["field"] == "customer_ref"


def test_contradictory_footer_totals_make_the_feasible_region_empty():
    # Both buckets claim 100.00 but only 100.00 of bills exist in total.
    facts = {"a_count": "1", "a_total": "100.00", "b_count": "1", "b_total": "100.00"}
    errs, result = run(xy_records(), make_ownership_check(), facts=facts)
    assert result["status"] == "infeasible"
    assert any(error["code"] == "E_OWNERSHIP_INFEASIBLE" for error in errs)
    assert result["conflict_core"]["footer_violations"]


def test_search_budget_exhaustion_returns_unproven_not_a_guess():
    records = xy_records()
    _, result = run(records, make_ownership_check(budget=1))
    assert result["status"] == "unproven"
    assert "budget" in result["reason"]
    assert "open_minimum" not in result
    assert result.get("credit_state") is None


def test_max_bills_guard_returns_unproven():
    _, result = run(xy_records(), make_ownership_check(max_bills=1))
    assert result["status"] == "unproven"
    assert "max_bills" in result["reason"]


def test_tight_budget_that_still_reaches_a_leaf_is_proven():
    # Two bills, two buckets, no footers: a full DFS needs only a handful of
    # nodes; a small but sufficient budget must still return proven bounds.
    _, result = run(xy_records(), make_ownership_check(budget=32))
    assert result["status"] == "proven"
    assert result["open_minimum"] == "20.00"
    assert result["open_maximum"] == "100.00"


def test_minimum_above_limit_proves_breach():
    records = [
        bill_record("B000001", "80.00", "OPEN", "A", "k-x"),
        bill_record("B000002", "20.00", "OPEN", "A", "k-y"),
    ]
    _, result = run(records, make_ownership_check(limit="50"))
    assert result["open_minimum"] == "100.00"
    assert result["credit_state"] == "exceeds"
    assert result["credit_action"] == "reject"


def test_credit_limit_can_come_from_customer_screen_record():
    check = make_ownership_check(limit=None, limit_ref={"screen_id": "demo_customer", "field": "credit_limit"})
    records = xy_records() + [customer_record("50.00")]
    errs, result = run_ownership_checks(
        records, None, (check,), facts_by_screen={SCREEN: {}}, fact_sources_by_screen={SCREEN: {}}
    )
    result = result[0]
    assert errs == []
    assert result["credit_limit"] == "50.00"
    assert result["credit_limit_source"]["screen_id"] == "demo_customer"
    assert result["credit_limit_source"]["source"]["row"] == 8


def test_wildcard_and_missing_mask_mean_all_buckets():
    records = [
        bill_record("B000001", "80.00", "OPEN", "*", "k-x"),
        bill_record("B000002", "20.00", "OPEN", None, "k-y"),
    ]
    _, result = run(records, make_ownership_check())
    assert result["status"] == "proven"
    # every bill can avoid A entirely (minimum 0) or be forced into A (maximum 100)
    assert result["open_minimum"] == "0"
    assert result["open_maximum"] == "100.00"
    assert result["credit_state"] == "inconclusive"


def test_witness_edges_retain_record_identity_and_mask_source():
    facts = {"a_count": "1", "a_total": "20.00", "b_count": "1", "b_total": "80.00"}
    _, result = run(xy_records(), make_ownership_check(), facts=facts)
    edge = next(edge for edge in result["minimum_witness"]["edges"] if edge["bill_id"] == "B000001")
    assert edge["record_key"] == "k-x"
    assert edge["mask"] == "A,B"
    assert edge["mask_source"] == {"row": 5, "col": 50, "end_col": 70, "frame_sha256": "f" * 64}


def test_footer_facts_carry_frame_provenance():
    sources = {"a_total": {"row": 20, "col": 19, "frame_sha256": "f" * 64}}
    _, result = run(xy_records(), make_ownership_check(), facts={"a_total": "20.00"}, sources=sources)
    footer = result["footer_facts"]["A"]["total_fact"]
    assert footer["value"] == "20.00"
    assert footer["source"]["row"] == 20


def test_tolerance_allows_footer_rounding():
    records = [bill_record("B000001", "20.004", "OPEN", "A", "k-x")]
    check = make_ownership_check(tolerance="0.01")
    errs, result = run(records, check, facts={"a_total": "20.00", "b_count": "0", "b_total": "0.00"})
    assert errs == []
    assert result["status"] == "proven"


def test_one_bill_cannot_feed_two_buckets():
    # One bill, but both footers independently demand it: infeasible, never double counted.
    records = [bill_record("B000001", "30.00", "OPEN", "A,B", "k-x")]
    facts = {"a_count": "1", "a_total": "30.00", "b_count": "1", "b_total": "30.00"}
    errs, result = run(records, make_ownership_check(), facts=facts)
    assert result["status"] == "infeasible"
    assert any(error["code"] == "E_OWNERSHIP_INFEASIBLE" for error in errs)
    witness_bill_total = sum(1 for _ in records)
    assert witness_bill_total == 1
