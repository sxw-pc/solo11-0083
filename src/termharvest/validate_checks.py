"""Record-level consistency and batch-level validation checks."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import E_BATCH_COUNT_MISMATCH, E_BATCH_DUPLICATE_KEY, E_BATCH_DUPLICATE_RECORD, E_BATCH_EMPTY, E_BATCH_SUM_MISMATCH, E_OWNERSHIP_DUPLICATE_BILL, E_OWNERSHIP_INVALID_MASK, E_OWNERSHIP_INFEASIBLE, E_RECORD_CONSISTENCY
from .validate_config import BatchCheck, BucketFooters, OwnershipCheck, RecordCheck, ValidationConfig
from .validate_fields import compare, convert_for_compare, issue

def run_record_checks(records: list[dict[str, Any]], config: ValidationConfig, checks: tuple[RecordCheck, ...]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for check in checks:
        for record in records:
            if record.get("screen_id") != check.screen_id:
                continue
            values = record.get("normalized_values", {})
            left = convert_for_compare(config, check.screen_id, check.left, values.get(check.left))
            if check.right_field is not None:
                right = convert_for_compare(config, check.screen_id, check.right_field, values.get(check.right_field))
            else:
                right = convert_for_compare(config, check.screen_id, check.left, check.right_value)
            if left is None or right is None:
                continue
            if not compare(left, check.op, right):
                errors.append(issue(E_RECORD_CONSISTENCY, rule_id=check.check_id, screen_id=check.screen_id, record_key=record.get("record_key"), field_name=check.left, message=check.message))
    return errors

def records_for(records: list[dict[str, Any]], screen_id: str) -> list[dict[str, Any]]:
    return [item for item in records if item.get("screen_id") == screen_id]

def _duplicate_key_errors(grouped: list[dict[str, Any]], check: BatchCheck) -> list[dict[str, Any]]:
    seen: set[tuple] = set()
    errors = []
    for record in grouped:
        values = record.get("normalized_values", {})
        key = tuple(values.get(name) for name in check.key)
        if key in seen:
            errors.append(
                issue(
                    E_BATCH_DUPLICATE_KEY,
                    rule_id=check.check_id,
                    screen_id=check.screen_id,
                    record_key=record.get("record_key"),
                    field_name=",".join(check.key),
                    message=f"duplicate business key {key!r}",
                )
            )
        seen.add(key)
    return errors

def _duplicate_record_errors(grouped: list[dict[str, Any]], check: BatchCheck) -> list[dict[str, Any]]:
    seen: set[str] = set()
    errors = []
    for record in grouped:
        canonical = json.dumps(record.get("normalized_values", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if canonical in seen:
            errors.append(
                issue(E_BATCH_DUPLICATE_RECORD, rule_id=check.check_id, screen_id=check.screen_id, record_key=record.get("record_key"), field_name=None, message="duplicate normalized record")
            )
        seen.add(canonical)
    return errors

def _sum_errors(grouped: list[dict[str, Any]], check: BatchCheck, facts: dict[str, Any]) -> list[dict[str, Any]]:
    if check.fact not in facts:
        return [issue(E_BATCH_SUM_MISMATCH, rule_id=check.check_id, screen_id=check.screen_id, record_key=None, field_name=check.field_name, message=f"fact {check.fact!r} is missing")]
    total = Decimal("0")
    for record in grouped:
        value = record.get("normalized_values", {}).get(check.field_name)
        if value is None:
            continue
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            continue
        if number.is_finite():
            total += number
    try:
        expected = Decimal(str(facts[check.fact]))
    except InvalidOperation:
        expected = Decimal("0")
    if abs(total - expected) > Decimal(str(check.tolerance)):
        return [
            issue(E_BATCH_SUM_MISMATCH, rule_id=check.check_id, screen_id=check.screen_id, record_key=None, field_name=check.field_name, message=f"sum {total} does not equal {check.fact}={expected}")
        ]
    return []

def _count_errors(grouped: list[dict[str, Any]], check: BatchCheck, facts: dict[str, Any]) -> list[dict[str, Any]]:
    if check.fact not in facts:
        return [issue(E_BATCH_COUNT_MISMATCH, rule_id=check.check_id, screen_id=check.screen_id, record_key=None, field_name=None, message=f"fact {check.fact!r} is missing")]
    try:
        expected = int(str(facts[check.fact]))
    except ValueError:
        expected = -1
    if len(grouped) != expected:
        return [
            issue(E_BATCH_COUNT_MISMATCH, rule_id=check.check_id, screen_id=check.screen_id, record_key=None, field_name=None, message=f"count {len(grouped)} does not equal {check.fact}={expected}")
        ]
    return []

def run_batch_checks(records: list[dict[str, Any]], facts_by_screen: dict[str, dict[str, Any]], checks: tuple[BatchCheck, ...]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for check in checks:
        grouped = records_for(records, check.screen_id)
        if check.check == "unique_key":
            errors.extend(_duplicate_key_errors(grouped, check))
        elif check.check == "duplicate_record":
            errors.extend(_duplicate_record_errors(grouped, check))
        elif check.check == "non_empty":
            if not grouped:
                errors.append(issue(E_BATCH_EMPTY, rule_id=check.check_id, screen_id=check.screen_id, record_key=None, field_name=None, message=check.message or "no records extracted"))
        elif check.check == "sum_equals":
            errors.extend(_sum_errors(grouped, check, facts_by_screen.get(check.screen_id, {})))
        elif check.check == "count_equals":
            errors.extend(_count_errors(grouped, check, facts_by_screen.get(check.screen_id, {})))
    return errors


# ---------------------------------------------------------------------------
# Masked bill ownership: global one-to-one bucket assignment with footer facts
# ---------------------------------------------------------------------------

WILDCARD_REF = "*"

@dataclass(frozen=True)
class _BillNode:
    bill_id: str
    amount: Decimal
    status: str
    is_open: bool
    candidates: tuple[str, ...]
    record_key: str
    frame_seq: int
    mask_value: str | None
    mask_source: dict[str, Any]

@dataclass
class _Budget:
    remaining: int

def _parse_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None

def _mask_candidates(raw_ref: str | None, buckets: tuple[str, ...]) -> tuple[tuple[str, ...], str | None]:
    """Expand a mask value to candidate buckets; ``None``/blank/``*`` = all buckets."""
    if raw_ref is None or not raw_ref.strip():
        return tuple(buckets), None
    token = raw_ref.strip()
    if token == WILDCARD_REF:
        return tuple(buckets), None
    names = [part.strip() for part in token.split(",") if part.strip()]
    ordered = [name for name in buckets if name in names]
    unknown = sorted(set(names) - set(buckets))
    return tuple(ordered), (unknown[0] if unknown else None)

def _footer_expected(facts: dict[str, Any], fact_name: str | None) -> Decimal | None:
    if fact_name is None:
        return None
    return _parse_decimal(facts.get(fact_name))

def _build_bill_nodes(check: OwnershipCheck, grouped: list[dict[str, Any]]) -> tuple[list[_BillNode] | None, list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    nodes: list[_BillNode] = []
    invalid_mask: list[dict[str, Any]] = []
    for record in grouped:
        values = record.get("normalized_values", {})
        bill_id = str(values.get(check.id_field) or "")
        if bill_id in seen_ids:
            errors.append(
                issue(
                    E_OWNERSHIP_DUPLICATE_BILL,
                    rule_id=check.check_id,
                    screen_id=check.screen_id,
                    record_key=record.get("record_key"),
                    field_name=check.id_field,
                    message=f"duplicate {check.id_field} {bill_id!r} cannot be assigned one-to-one",
                )
            )
        seen_ids.add(bill_id)
        amount = _parse_decimal(values.get(check.amount_field)) or Decimal("0")
        status = str(values.get(check.status_field) or "")
        mask_value = values.get(check.mask_field)
        candidates, unknown = _mask_candidates(mask_value, check.buckets)
        sources = record.get("field_sources", {})
        mask_source = sources.get(check.mask_field, {"row": record.get("frame_seq"), "col": 0, "frame_sha256": record.get("frame_sha256")})
        if unknown is not None or not candidates:
            invalid_mask.append(
                issue(
                    E_OWNERSHIP_INVALID_MASK,
                    rule_id=check.check_id,
                    screen_id=check.screen_id,
                    record_key=record.get("record_key"),
                    field_name=check.mask_field,
                    message=f"customer_ref {mask_value!r} names unknown bucket {unknown or mask_value!r}",
                )
            )
        nodes.append(
            _BillNode(
                bill_id=bill_id,
                amount=amount,
                status=status,
                is_open=status in check.open_statuses,
                candidates=candidates,
                record_key=str(record.get("record_key")),
                frame_seq=int(record.get("frame_seq", 0)),
                mask_value=None if mask_value is None else str(mask_value),
                mask_source=dict(mask_source),
            )
        )
    if errors or invalid_mask:
        return None, errors + invalid_mask
    return nodes, []

def _conflict_core(check: OwnershipCheck, nodes: list[_BillNode], facts: dict[str, Any], footer_lookup: dict[str, BucketFooters]) -> dict[str, Any]:
    """Small, explainable infeasibility core.

    Reports bills with no candidate edge and, per trusted footer, the
    candidate-edge reachable count/amount interval versus the fact value.
    Bounds are computed independently per bucket so they stay cheap while
    always naming at least one violated constraint when the global problem
    has no solution.
    """
    core: dict[str, Any] = {"unassignable_bills": [], "footer_violations": [], "bill_count": len(nodes)}
    for node in nodes:
        if not node.candidates:
            core["unassignable_bills"].append({"bill_id": node.bill_id, "record_key": node.record_key, "mask": node.mask_value})
    for footer in check.footers:
        bucket = footer.bucket
        eligible = [node for node in nodes if bucket in node.candidates]
        reachable_count = Decimal(len(eligible))
        reachable_max_total = sum((node.amount for node in eligible), Decimal("0"))
        expected_count = _footer_expected(facts, footer.count_fact)
        expected_total = _footer_expected(facts, footer.total_fact)
        if expected_count is not None and (expected_count > reachable_count or expected_count < 0):
            core["footer_violations"].append(
                {"bucket": bucket, "fact": footer.count_fact, "expected": str(expected_count), "reachable_max": str(reachable_count), "reason": "fewer bills carry an edge to this bucket than the trusted count requires"}
            )
        if expected_total is not None and (expected_total - reachable_max_total > check.tolerance or expected_total < 0):
            core["footer_violations"].append(
                {"bucket": bucket, "fact": footer.total_fact, "expected": str(expected_total), "reachable_max": str(reachable_max_total), "reason": "even assigning every eligible bill cannot reach the trusted total"}
            )
    if not core["unassignable_bills"] and not core["footer_violations"]:
        forced = sorted(
            (
                {"bill_id": node.bill_id, "bucket": node.candidates[0], "amount": str(node.amount), "record_key": node.record_key}
                for node in nodes
                if len(node.candidates) == 1
            ),
            key=lambda item: item["bill_id"],
        )
        core["joint_footer_conflict"] = {
            "reason": "each bill keeps a candidate edge in isolation, but no joint one-to-one assignment satisfies every trusted footer simultaneously",
            "forced_edges": forced,
        }
    return core

def _search(
    check: OwnershipCheck,
    nodes: list[_BillNode],
    facts: dict[str, Any],
    footer_lookup: dict[str, BucketFooters],
    *,
    minimize: bool,
    budget: _Budget,
) -> dict[str, Any] | None:
    """Branch-and-bound over global one-to-one assignments.

    Returns ``None`` when the budget is exhausted or no feasible full
    assignment exists; callers distinguish the two via ``budget.remaining``.
    """
    counts = {bucket: 0 for bucket in check.buckets}
    totals = {bucket: Decimal("0") for bucket in check.buckets}
    expected_counts = {bucket: _footer_expected(facts, footer_lookup[bucket].count_fact) for bucket in check.buckets}
    expected_totals = {bucket: _footer_expected(facts, footer_lookup[bucket].total_fact) for bucket in check.buckets}
    assignment: dict[str, str] = {}
    best: dict[str, Any] = {}
    exhausted = False

    def target_open() -> Decimal:
        return sum((node.amount for node in nodes if node.is_open and assignment.get(node.bill_id) == check.target_bucket), Decimal("0"))

    def acceptable(current: Decimal) -> bool:
        if "target_open" not in best:
            return True
        return current < best["target_open"] if minimize else current > best["target_open"]

    def dfs(index_order: list[int], depth: int) -> None:
        nonlocal exhausted
        if budget.remaining <= 0:
            # Only a hard stop before any complete feasible leaf is exhaustion;
            # running dry exactly on a leaf still leaves a proven witness.
            exhausted = "target_open" not in best
            return
        budget.remaining -= 1
        if depth == len(index_order):
            # A full assignment is feasible only when every trusted footer
            # count and total matches exactly (within tolerance). Partial
            # pruning alone cannot detect an under-filled bucket at the leaf.
            for bucket in check.buckets:
                expected_c = expected_counts[bucket]
                if expected_c is not None and abs(Decimal(counts[bucket]) - expected_c) > check.tolerance:
                    return
                expected_t = expected_totals[bucket]
                if expected_t is not None and abs(totals[bucket] - expected_t) > check.tolerance:
                    return
            current = target_open()
            if acceptable(current):
                best["target_open"] = current
                best["assignment"] = dict(assignment)
            return
        node = nodes[index_order[depth]]
        for bucket in node.candidates:
            expected_c = expected_counts[bucket]
            if expected_c is not None and counts[bucket] + 1 > expected_c:
                continue
            new_total = totals[bucket] + node.amount
            expected_t = expected_totals[bucket]
            if expected_t is not None and new_total - expected_t > check.tolerance:
                continue
            counts[bucket] += 1
            totals[bucket] = new_total
            assignment[node.bill_id] = bucket
            dfs(index_order, depth + 1)
            del assignment[node.bill_id]
            totals[bucket] -= node.amount
            counts[bucket] -= 1
            if exhausted:
                return

    order = sorted(range(len(nodes)), key=lambda i: (len(nodes[i].candidates), nodes[i].bill_id))
    dfs(order, 0)
    if exhausted:
        return None
    return best if best else None

def _witness(check: OwnershipCheck, nodes: list[_BillNode], assignment: dict[str, str]) -> dict[str, Any]:
    edges: list[dict[str, Any]] = []
    for node in sorted(nodes, key=lambda item: item.bill_id):
        edges.append(
            {
                "bill_id": node.bill_id,
                "amount": str(node.amount),
                "status": node.status,
                "bucket": assignment[node.bill_id],
                "record_key": node.record_key,
                "frame_seq": node.frame_seq,
                "mask": node.mask_value,
                "mask_source": node.mask_source,
            }
        )
    return {"one_to_one": True, "edges": edges}

def _resolve_credit_limit(check: OwnershipCheck, records: list[dict[str, Any]]) -> tuple[Decimal | None, dict[str, Any] | None]:
    if check.credit_limit_value is not None:
        return check.credit_limit_value, None
    for record in records:
        if record.get("screen_id") != check.credit_limit_screen:
            continue
        value = record.get("normalized_values", {}).get(check.credit_limit_field)
        number = _parse_decimal(value)
        source = record.get("field_sources", {}).get(check.credit_limit_field)
        provenance = {"screen_id": check.credit_limit_screen, "field": check.credit_limit_field, "record_key": record.get("record_key"), "source": source}
        return number, provenance
    return None, {"screen_id": check.credit_limit_screen, "field": check.credit_limit_field, "record_key": None, "source": None}

def run_ownership_checks(
    records: list[dict[str, Any]],
    config: ValidationConfig,
    checks: tuple[OwnershipCheck, ...],
    *,
    facts_by_screen: dict[str, dict[str, Any]] | None = None,
    fact_sources_by_screen: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (validation errors, structured ownership results)."""
    facts_by_screen = facts_by_screen or {}
    fact_sources_by_screen = fact_sources_by_screen or {}
    errors: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for check in checks:
        grouped = records_for(records, check.screen_id)
        facts = facts_by_screen.get(check.screen_id, {})
        footer_lookup = {footer.bucket: footer for footer in check.footers}
        result: dict[str, Any] = {
            "check_id": check.check_id,
            "screen_id": check.screen_id,
            "target_bucket": check.target_bucket,
            "purpose": check.purpose,
            "status": "unproven",
            "max_bills": check.max_bills,
            "search_budget": check.search_budget,
        }
        nodes, build_errors = _build_bill_nodes(check, grouped)
        if build_errors:
            errors.extend(build_errors)
            result["status"] = "invalid"
            result["message"] = "ownership input is ill-posed; no assignment was attempted"
            results.append(result)
            continue
        assert nodes is not None
        result["bill_count"] = len(nodes)
        result["candidate_edges"] = [
            {"bill_id": node.bill_id, "amount": str(node.amount), "status": node.status, "candidates": list(node.candidates), "record_key": node.record_key}
            for node in sorted(nodes, key=lambda item: item.bill_id)
        ]
        result["footer_facts"] = {
            footer.bucket: {
                "count_fact": None if footer.count_fact is None else {"name": footer.count_fact, "value": facts.get(footer.count_fact), "source": fact_sources_by_screen.get(check.screen_id, {}).get(footer.count_fact)},
                "total_fact": None if footer.total_fact is None else {"name": footer.total_fact, "value": facts.get(footer.total_fact), "source": fact_sources_by_screen.get(check.screen_id, {}).get(footer.total_fact)},
            }
            for footer in check.footers
        }
        if len(nodes) > check.max_bills:
            result["status"] = "unproven"
            result["reason"] = f"bill count {len(nodes)} exceeds max_bills {check.max_bills}"
            results.append(result)
            continue
        half = check.search_budget // 2
        min_budget = _Budget(half + check.search_budget % 2)
        max_budget = _Budget(half)
        minimum = _search(check, nodes, facts, footer_lookup, minimize=True, budget=min_budget)
        if minimum is None and min_budget.remaining <= 0:
            result["status"] = "unproven"
            result["reason"] = "search budget exhausted before a minimum witness was proven"
            results.append(result)
            continue
        maximum = _search(check, nodes, facts, footer_lookup, minimize=False, budget=max_budget)
        if maximum is None and max_budget.remaining <= 0:
            result["status"] = "unproven"
            result["reason"] = "search budget exhausted before a maximum witness was proven"
            results.append(result)
            continue
        if minimum is None or maximum is None:
            result["status"] = "infeasible"
            result["conflict_core"] = _conflict_core(check, nodes, facts, footer_lookup)
            errors.append(
                issue(
                    E_OWNERSHIP_INFEASIBLE,
                    rule_id=check.check_id,
                    screen_id=check.screen_id,
                    record_key=None,
                    field_name=check.mask_field,
                    message=check.message,
                )
            )
            results.append(result)
            continue
        result["open_minimum"] = str(minimum["target_open"])
        result["open_maximum"] = str(maximum["target_open"])
        result["minimum_witness"] = _witness(check, nodes, minimum["assignment"])
        result["maximum_witness"] = _witness(check, nodes, maximum["assignment"])
        result["status"] = "proven"
        limit, limit_source = _resolve_credit_limit(check, records)
        result["credit_limit"] = None if limit is None else str(limit)
        result["credit_limit_source"] = limit_source
        if limit is None:
            result["credit_state"] = "unproven"
            result["credit_action"] = "manual_review"
            results.append(result)
            continue
        low, high = minimum["target_open"], maximum["target_open"]
        if high <= limit:
            result["credit_state"] = "safe"
            result["credit_action"] = "pass"
        elif low > limit:
            result["credit_state"] = "exceeds"
            result["credit_action"] = "reject"
        else:
            result["credit_state"] = "inconclusive"
            if check.purpose == "compliance":
                result["credit_action"] = "hold_for_manual_review"
            else:
                result["credit_action"] = "review_with_safe_witness"
                safe_witness = minimum if minimum["target_open"] <= limit else None
                result["safe_witness_available"] = safe_witness is not None
                if safe_witness is not None:
                    result["safe_witness"] = _witness(check, nodes, safe_witness["assignment"])
        results.append(result)
    return errors, results
