"""Offline screen-contract satisfiability precheck (bounded, reviewable).

Before any real collection happens this module answers one question per
screen contract: can one 24x80 screen realize the contract's locators and
the declarative validation rules *at the same time*?  The three entry
points that used to check things independently (locator bounds in
``config_screen``, per-field extraction in ``extract`` and rule parsing in
``validate_config``) are folded into a single bounded model here:

* 24x80 character slots and absolute/label/multiline locators;
* field normalization (including case/fullwidth folding back to raw chars);
* the primary key and the supported field rules: enums, literal patterns,
  bounded integer ranges, and cross-field record/batch comparisons.

Verdicts:

* ``SAT``     - a synthetic witness screen was built *and re-verified* with
                the real ``extract_screen()`` / ``validate_records()`` code.
* ``UNSAT``   - a proof was found that no screen can satisfy the model
                (empty finite domain, forced character-slot conflict,
                pigeonhole on unique keys, or exhaustive search over a fully
                finite model).  A minimal conflict set is attached.
* ``UNKNOWN`` - the model left the provable subset (arbitrary regex,
                unbounded character domains, pinned fingerprints) or the
                search budget was exhausted.  UNKNOWN is never treated as
                SAT and never masquerades as UNSAT.

Two explicit purposes are supported:

* ``deployment`` feasibility - at least one synthetic screen satisfies the
  contract and the validation rules;
* ``regression`` adaptation - every frame of a local mock scenario that the
  session script claims to belong to a screen_id satisfies the same
  extraction and validation rules.

Sensitive fields participate only through their position and an anonymized
placeholder; their content is never synthesized, read, or written to logs.
"""

from __future__ import annotations

import itertools
import math
import re
import string as _string
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator

from .config_screen import locator_spans
from .config_types import Column, ListRegion, Locator, ScreenContract, ScreenField, SessionContract, Span
from .errors import E_CONFIG_SCHEMA, E_CONTRACT_PRECHECK_UNKNOWN, E_CONTRACT_REGRESSION_MISMATCH, E_CONTRACT_UNSAT, HarvestError
from .extract import Extraction, extract_screen, validate_extraction
from .extract_pages import extract_pages
from .mock_scenario import MockScenario, load_scenario
from .screen import FRAME_COLS, FRAME_ROWS, ScreenFrame
from .validate import validate_records
from .validate_config import FieldRule, ValidationConfig, screen_validation_config
from .validate_fields import validate_field

SAT = "SAT"
UNSAT = "UNSAT"
UNKNOWN = "UNKNOWN"
VERDICTS = frozenset({SAT, UNSAT, UNKNOWN})

PURPOSE_DEPLOYMENT = "deployment"
PURPOSE_REGRESSION = "regression"

SENSITIVE_PLACEHOLDER = "<REDACTED>"

# ---------------------------------------------------------------------------
# Budget


@dataclass(frozen=True)
class SatBudget:
    """Hard bounds that keep the model decidable and the run deterministic."""

    max_assignments: int = 20000
    max_pattern_expansion: int = 256
    max_domain_values: int = 512
    max_layout_attempts: int = 40000
    max_minimization_checks: int = 24

    @classmethod
    def from_overrides(cls, overrides: dict[str, int] | tuple[tuple[str, int], ...] | None) -> "SatBudget":
        budget = cls()
        if not overrides:
            return budget
        items = dict(overrides)
        unknown = sorted(set(items) - set(cls.__dataclass_fields__))
        if unknown:
            raise HarvestError(E_CONFIG_SCHEMA, f"unknown precheck budget keys: {unknown}", context={"field": "precheck.budget"})
        values = {name: getattr(budget, name) for name in cls.__dataclass_fields__}
        for key, value in items.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise HarvestError(E_CONFIG_SCHEMA, f"precheck budget {key} must be a positive integer", context={"field": f"precheck.budget.{key}"})
            values[key] = value
        return cls(**values)


# ---------------------------------------------------------------------------
# Result types


@dataclass
class ContractSatResult:
    screen_id: str
    purpose: str
    verdict: str
    reasons: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    minimal_conflict: list[str] = field(default_factory=list)
    witness_rows: list[str] | None = None
    witness_sha256: str | None = None
    witness_values: dict[str, Any] | None = None
    non_adapting_frames: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_witness: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "screen_id": self.screen_id,
            "purpose": self.purpose,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "conflicts": self.conflicts,
            "minimal_conflict": list(self.minimal_conflict),
            "non_adapting_frames": self.non_adapting_frames,
            "stats": self.stats,
        }
        if self.witness_sha256 is not None:
            data["witness_sha256"] = self.witness_sha256
        if include_witness and self.witness_rows is not None:
            data["witness"] = {"rows": list(self.witness_rows), "sha256": self.witness_sha256, "normalized_values": self.witness_values}
        return data


# ---------------------------------------------------------------------------
# Literal-pattern subset: parse, expand, positional char sets, synthesis.
#
# Supported: literal chars, escapes (\d \w \s \n \t and escaped punctuation),
# character classes with ranges (no negation), grouping (including (?:...)),
# alternation, bounded repetition {m} {m,n} and ?.  Anything unbounded or
# backtracking-only (`.`, `*`, `+`, lookaround, backreferences) is rejected
# so the caller can degrade to UNKNOWN instead of guessing.

_SUPPORTED_CLASS_ESCAPES = {
    "d": frozenset(_string.digits),
    "w": frozenset(_string.ascii_letters + _string.digits + "_"),
    "s": frozenset(" \t"),
}
_LITERAL_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "f": "\f", "v": "\v"}


class _UnsupportedPattern(Exception):
    pass


def _parse_pattern(pattern: str) -> tuple | None:
    try:
        node, pos = _parse_alternation(pattern, 0)
    except _UnsupportedPattern:
        return None
    if pos != len(pattern):
        return None
    return node


def _parse_alternation(pattern: str, pos: int) -> tuple[tuple, int]:
    parts = []
    node, pos = _parse_concat(pattern, pos)
    parts.append(node)
    while pos < len(pattern) and pattern[pos] == "|":
        node, pos = _parse_concat(pattern, pos + 1)
        parts.append(node)
    if len(parts) == 1:
        return parts[0], pos
    return ("alt", tuple(parts)), pos


def _parse_concat(pattern: str, pos: int) -> tuple[tuple, int]:
    nodes = []
    while pos < len(pattern) and pattern[pos] not in "|)":
        node, pos = _parse_repeat(pattern, pos)
        nodes.append(node)
    if not nodes:
        return ("lit", ""), pos
    if len(nodes) == 1:
        return nodes[0], pos
    return ("cat", tuple(nodes)), pos


def _parse_repeat(pattern: str, pos: int) -> tuple[tuple, int]:
    node, pos = _parse_atom(pattern, pos)
    while pos < len(pattern):
        ch = pattern[pos]
        if ch == "?":
            node = ("rep", node, 0, 1)
            pos += 1
        elif ch == "{":
            end = pattern.find("}", pos)
            if end == -1:
                raise _UnsupportedPattern
            body = pattern[pos + 1 : end]
            if not re.fullmatch(r"[0-9]+(,[0-9]*)?|,[0-9]+", body):
                raise _UnsupportedPattern
            if "," in body:
                lo_text, hi_text = body.split(",", 1)
                lo = int(lo_text) if lo_text else 0
                if hi_text == "":
                    raise _UnsupportedPattern  # {m,} is unbounded
                hi = int(hi_text)
            else:
                lo = hi = int(body)
            if hi < lo:
                raise _UnsupportedPattern
            node = ("rep", node, lo, hi)
            pos = end + 1
        elif ch in "*+":
            raise _UnsupportedPattern
        else:
            break
    return node, pos


def _parse_atom(pattern: str, pos: int) -> tuple[tuple, int]:
    ch = pattern[pos]
    if ch in "^$":
        return ("lit", ""), pos + 1
    if ch == "(":
        if pattern.startswith("(?:", pos):
            node, end = _parse_alternation(pattern, pos + 3)
        elif pattern.startswith("(?", pos):
            raise _UnsupportedPattern
        else:
            node, end = _parse_alternation(pattern, pos + 1)
        if end >= len(pattern) or pattern[end] != ")":
            raise _UnsupportedPattern
        return node, end + 1
    if ch == "[":
        return _parse_class(pattern, pos)
    if ch == ".":
        raise _UnsupportedPattern
    if ch == "\\":
        if pos + 1 >= len(pattern):
            raise _UnsupportedPattern
        esc = pattern[pos + 1]
        if esc in _SUPPORTED_CLASS_ESCAPES:
            return ("cls", _SUPPORTED_CLASS_ESCAPES[esc]), pos + 2
        if esc in _LITERAL_ESCAPES:
            return ("lit", _LITERAL_ESCAPES[esc]), pos + 2
        if esc.isalnum():
            raise _UnsupportedPattern  # \b \A backreferences etc.
        return ("lit", esc), pos + 2
    return ("lit", ch), pos + 1


def _parse_class(pattern: str, pos: int) -> tuple[tuple, int]:
    pos += 1
    if pos < len(pattern) and pattern[pos] == "^":
        raise _UnsupportedPattern  # negated class: domain not enumerable
    chars: set[str] = set()
    first = True
    while True:
        if pos >= len(pattern):
            raise _UnsupportedPattern
        ch = pattern[pos]
        if ch == "]" and not first:
            if not chars:
                raise _UnsupportedPattern
            return ("cls", frozenset(chars)), pos + 1
        first = False
        if ch == "\\":
            if pos + 1 >= len(pattern):
                raise _UnsupportedPattern
            esc = pattern[pos + 1]
            if esc in _SUPPORTED_CLASS_ESCAPES:
                chars |= _SUPPORTED_CLASS_ESCAPES[esc]
            elif esc in _LITERAL_ESCAPES:
                chars.add(_LITERAL_ESCAPES[esc])
            elif esc.isalnum():
                raise _UnsupportedPattern
            else:
                chars.add(esc)
            pos += 2
            continue
        if pos + 2 < len(pattern) and pattern[pos + 1] == "-" and pattern[pos + 2] != "]":
            hi = pattern[pos + 2]
            if ord(ch) > ord(hi):
                raise _UnsupportedPattern
            chars.update(chr(code) for code in range(ord(ch), ord(hi) + 1))
            pos += 3
            continue
        chars.add(ch)
        pos += 1


def _expand_pattern(node: tuple, budget: int) -> set[str] | None:
    """All strings in the language, or None when the language exceeds budget."""
    kind = node[0]
    if kind == "lit":
        return {node[1]}
    if kind == "cls":
        if len(node[1]) > budget:
            return None
        return set(node[1])
    if kind == "cat":
        acc = {""}
        for part in node[1]:
            options = _expand_pattern(part, budget)
            if options is None:
                return None
            acc = {prefix + option for prefix in acc for option in options}
            if len(acc) > budget:
                return None
        return acc
    if kind == "alt":
        acc: set[str] = set()
        for part in node[1]:
            options = _expand_pattern(part, budget)
            if options is None:
                return None
            acc |= options
            if len(acc) > budget:
                return None
        return acc
    if kind == "rep":
        _, inner, lo, hi = node
        base = _expand_pattern(inner, budget)
        if base is None:
            return None
        acc = {""}
        result: set[str] = set()
        for count in range(1, hi + 1):
            acc = {prefix + option for prefix in acc for option in base}
            if len(acc) > budget:
                return None
            if count >= lo:
                result |= acc
                if len(result) > budget:
                    return None
        if lo == 0:
            result.add("")
        return result
    return None


def _positional_pattern(node: tuple) -> tuple[int, int, tuple[frozenset[str], ...]] | None:
    """(min_len, max_len, per-position char sets); an over-approximation.

    Returns None when variable-length parts would make positions shift, in
    which case the caller must not use positional information for proofs.
    """
    kind = node[0]
    if kind == "lit":
        text = node[1]
        return len(text), len(text), tuple(frozenset({ch}) for ch in text)
    if kind == "cls":
        return 1, 1, (node[1],)
    if kind == "cat":
        mins = 0
        maxs = 0
        sets: list[frozenset[str]] = []
        parts = [_positional_pattern(part) for part in node[1]]
        if any(part is None for part in parts):
            return None
        for index, part in enumerate(parts):
            assert part is not None
            if part[0] != part[1] and index < len(parts) - 1:
                return None  # variable-length part shifts later positions
            mins += part[0]
            maxs += part[1]
            sets.extend(part[2])
        return mins, maxs, tuple(sets)
    if kind == "alt":
        parts = [_positional_pattern(part) for part in node[1]]
        if any(part is None for part in parts):
            return None
        lo = min(part[0] for part in parts if part is not None)
        hi = max(part[1] for part in parts if part is not None)
        sets = []
        for index in range(hi):
            chars: set[str] = set()
            for part in parts:
                assert part is not None
                if index < len(part[2]):
                    chars |= part[2][index]
            sets.append(frozenset(chars))
        return lo, hi, tuple(sets)
    if kind == "rep":
        _, inner, lo, hi = node
        part = _positional_pattern(inner)
        if part is None:
            return None
        ilo, ihi, isets = part
        if ilo != ihi:
            return None
        if ihi == 0:
            return 0, 0, ()
        sets = []
        for _ in range(hi):
            sets.extend(isets)
        return lo * ilo, hi * ihi, tuple(sets)
    return None


def _synthesize_pattern(node: tuple) -> str:
    """One concrete member of the language (deterministic, minimal)."""
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "cls":
        return sorted(node[1])[0] if node[1] else ""
    if kind == "cat":
        return "".join(_synthesize_pattern(part) for part in node[1])
    if kind == "alt":
        return _synthesize_pattern(node[1][0])
    if kind == "rep":
        return _synthesize_pattern(node[1]) * node[2]
    return ""


# ---------------------------------------------------------------------------
# Field domains


@dataclass(frozen=True)
class Domain:
    """Candidate normalized values for a field.

    ``finite=True`` means ``values`` is the *complete* feasible set, which is
    what licenses UNSAT proofs; otherwise ``values`` is a probe prefix that
    is only good enough for constructive SAT attempts.
    """

    values: tuple[str | None, ...]
    finite: bool
    reasons: tuple[str, ...] = ()


def _passes_field(screen_id: str, name: str, rule: FieldRule, value: str) -> bool:
    record = {"screen_id": screen_id, "normalized_values": {name: value}, "record_key": "sat-domain-probe"}
    return not validate_field(screen_id, record, name, rule)


def _renderable_value(value: str | None) -> bool:
    """Values the extraction layer could ever produce for one locator slice.

    ``_locate`` always strips the slice, so normalized values never carry
    leading/trailing whitespace, and a single slice can never contain a
    newline.  Anything else is infeasible no matter the screen content.
    """
    if value is None:
        return True
    if value != value.strip():
        return False
    return "\n" not in value and "\r" not in value


def _allow_none(values: list[str], rule: FieldRule) -> tuple[str | None, ...]:
    result: list[str | None] = list(values)
    if not rule.required:
        result.append(None)
    return tuple(result)


def _probe_values(rule: FieldRule, ast: tuple | None) -> list[str]:
    probes: list[str] = []
    if ast is not None:
        probes.append(_synthesize_pattern(ast))
    if rule.type == "integer":
        candidates: list[int] = []
        if rule.minimum is not None:
            candidates.extend([math.ceil(rule.minimum), math.ceil(rule.minimum) + 1])
        if rule.maximum is not None:
            candidates.append(math.floor(rule.maximum))
        if not candidates:
            candidates.append(0)
        probes.extend(str(candidate) for candidate in candidates)
    elif rule.type == "decimal":
        candidates = []
        if rule.minimum is not None:
            candidates.extend([str(rule.minimum), str(rule.minimum + 1)])
        if rule.maximum is not None:
            candidates.append(str(rule.maximum))
        if not candidates:
            candidates.extend(["0", "1"])
        probes.extend(candidates)
    elif rule.type == "date":
        probes.extend([date(2026, 1, 1).strftime(rule.date_format), date(2026, 12, 31).strftime(rule.date_format)])
    elif ast is None:
        probes.append("X")
    seen: set[str] = set()
    unique: list[str] = []
    for probe in probes:
        if probe not in seen:
            seen.add(probe)
            unique.append(probe)
    return unique


def _build_domain(screen_id: str, name: str, rule: FieldRule | None, budget: SatBudget) -> Domain:
    if rule is None:
        return Domain(("X", None), False, (f"unbounded_domain:{name}",))
    reasons: list[str] = []
    exact: list[str] | None = None
    ast: tuple | None = None
    if rule.enum is not None:
        exact = [str(value) for value in rule.enum]
    if rule.pattern is not None:
        ast = _parse_pattern(rule.pattern)
        if ast is None:
            if exact is not None:
                exact = [value for value in exact if re.fullmatch(rule.pattern, value)]
            else:
                reasons.append(f"pattern_not_supported:{name}")
        else:
            expanded = _expand_pattern(ast, budget.max_pattern_expansion)
            if expanded is not None:
                exact = sorted(expanded) if exact is None else [value for value in exact if value in expanded]
            elif exact is not None:
                exact = [value for value in exact if re.fullmatch(rule.pattern, value)]
            else:
                reasons.append(f"pattern_not_literal:{name}")
    if exact is None and rule.type == "integer" and rule.minimum is not None and rule.maximum is not None:
        lo = math.ceil(rule.minimum)
        hi = math.floor(rule.maximum)
        if hi - lo + 1 <= 0:
            exact = []
        elif hi - lo + 1 <= budget.max_domain_values:
            exact = [str(value) for value in range(lo, hi + 1)]
        else:
            reasons.append(f"domain_too_large:{name}")
    if exact is not None:
        values = [value for value in exact if _renderable_value(value) and _passes_field(screen_id, name, rule, value)]
        if len(values) > budget.max_domain_values:
            reasons.append(f"domain_too_large:{name}")
            return Domain(_allow_none(values[: budget.max_domain_values], rule), False, tuple(reasons))
        return Domain(_allow_none(values, rule), True, ())
    probes = [value for value in _probe_values(rule, ast) if _renderable_value(value) and _passes_field(screen_id, name, rule, value)]
    if rule.required and not probes:
        reasons.append(f"no_feasible_value:{name}")
    if not reasons:
        reasons.append(f"unbounded_domain:{name}")
    return Domain(_allow_none(probes, rule), False, tuple(reasons))


# ---------------------------------------------------------------------------
# Field models and locator feasibility


@dataclass
class _FieldModel:
    name: str
    source: str  # "field" | "summary" | "column"
    locator: Locator | None
    column: Column | None
    normalize: tuple[str, ...]
    domain: Domain


def _locator_max_length(locator: Locator) -> int:
    data = locator.data
    if locator.kind == "absolute":
        return data["length"]
    if locator.kind == "multiline":
        return data["length"]
    if locator.kind == "label":
        if data["direction"] == "right":
            if data["length"] is not None:
                return data["length"]
            return FRAME_COLS - (len(data["label"]) + 1)
        if data["length"] is not None:
            return data["length"]
        return FRAME_COLS - (data["col"] or 0)
    return 0


def _multiline_splittable(value: str, locator: Locator) -> bool:
    data = locator.data
    join = data["join"]
    if not join or join not in value:
        return False
    pieces = value.split(join)
    if len(pieces) > len(data["rows"]):
        return False
    return all(0 < len(piece) <= data["length"] for piece in pieces)


def _value_fits_locator(value: str | None, locator: Locator | None, column: Column | None) -> bool:
    if value is None:
        return True
    if column is not None:
        return len(value) <= column.end - column.start
    assert locator is not None
    if locator.kind == "multiline":
        if len(value) <= locator.data["length"]:
            return True
        return _multiline_splittable(value, locator)
    if len(value) > _locator_max_length(locator):
        return False
    if locator.kind == "label" and locator.data["direction"] == "right" and value[:1] in (":", "\t"):
        return False  # the extractor skips leading " :\t" after the label
    return True


def _filter_domain_by_locator(domain: Domain, locator: Locator | None, column: Column | None, name: str) -> Domain:
    values = tuple(value for value in domain.values if _value_fits_locator(value, locator, column))
    reasons = list(domain.reasons)
    if not values and domain.values:
        reasons.append(f"locator_too_small:{name}")
    return Domain(values, domain.finite, tuple(reasons))


class _ScreenModel:
    """All model facts for one screen contract (deterministic order)."""

    def __init__(self, contract: ScreenContract, validation: ValidationConfig, budget: SatBudget) -> None:
        self.contract = contract
        self.validation = validation
        self.scoped = screen_validation_config(validation, contract.screen_id)
        self.budget = budget
        rules = validation.fields.get(contract.screen_id, {})
        self.rules = rules
        self.record_checks = self.scoped.record_checks
        self.batch_checks = self.scoped.batch_checks
        self.fields: list[_FieldModel] = []
        self.summary: list[_FieldModel] = []
        self.columns: list[_FieldModel] = []
        self.sensitive: list[ScreenField] = []
        for item in contract.fields:
            self._add_field(item, self.fields)
        for item in contract.summary_fields:
            self._add_field(item, self.summary)
        if contract.list_region is not None:
            for column in contract.list_region.columns:
                domain = _build_domain(contract.screen_id, column.name, rules.get(column.name), budget)
                domain = _filter_domain_by_locator(domain, None, column, column.name)
                self.columns.append(_FieldModel(column.name, "column", None, column, ("trim", "collapse_space", "fullwidth_digits"), domain))
        self.value_fields = self.fields + self.summary + self.columns
        self._collapse_free_fields()
        self.reasons: list[str] = []
        for model in self.value_fields:
            self.reasons.extend(model.domain.reasons)
        self.finite = all(model.domain.finite for model in self.value_fields)

    def _add_field(self, item: ScreenField, target: list[_FieldModel]) -> None:
        if item.sensitive:
            self.sensitive.append(item)
            return
        domain = _build_domain(self.contract.screen_id, item.name, self.rules.get(item.name), self.budget)
        domain = _filter_domain_by_locator(domain, item.locator, None, item.name)
        target.append(_FieldModel(item.name, "field" if target is self.fields else "summary", item.locator, None, tuple(item.normalize), domain))

    def _collapse_free_fields(self) -> None:
        """Fields no rule and no check references cannot affect satisfiability.

        They are collapsed to a single concrete probe value so the model stays
        finite; layout failures are tracked separately, so this never turns an
        UNKNOWN into a wrong UNSAT.
        """
        referenced = _referenced_names(self.record_checks, self.batch_checks, self.fields, self.columns)
        for model in self.value_fields:
            if self.rules.get(model.name) is None and model.name not in referenced:
                model.domain = Domain(("X",), True)

    def summary_model(self, name: str | None) -> _FieldModel | None:
        for model in self.summary:
            if model.name == name:
                return model
        return None


def _referenced_names(record_checks: tuple, batch_checks: tuple, fields: list[_FieldModel], columns: list[_FieldModel]) -> set[str]:
    names: set[str] = set()
    for check in record_checks:
        names.add(check.left)
        if check.right_field is not None:
            names.add(check.right_field)
    for check in batch_checks:
        names.update(check.key)
        if check.field_name is not None:
            names.add(check.field_name)
        if check.fact is not None:
            names.add(check.fact)
        if check.check == "duplicate_record":
            names.update(item.name for item in fields)
            names.update(item.name for item in columns)
    return names


# ---------------------------------------------------------------------------
# Layout engine: a 24x80 character grid with deterministic placement.
#
# Every locator slice is modelled exactly the way ``_locate`` reads it: the
# raw text plus space padding up to the end of the slice.  Any foreign
# non-space character inside the slice would change the extracted value, so
# placements reserve the whole slice, not just the text length.


class _Layout:
    def __init__(self) -> None:
        self.cells = [bytearray(FRAME_COLS) for _ in range(FRAME_ROWS)]
        self.owners: dict[tuple[int, int], str] = {}
        self.labels: list[tuple[str, int, int, int, int]] = []  # label, first, last, row, col

    def row_text(self, row: int) -> str:
        return "".join(chr(cell) if cell else " " for cell in self.cells[row])

    def row_text_with(self, row: int, writes: dict[tuple[int, int], str]) -> str:
        overlay = {col: ch for (r, col), ch in writes.items() if r == row}
        return "".join(overlay.get(col) or (chr(self.cells[row][col]) if self.cells[row][col] else " ") for col in range(FRAME_COLS))

    def text(self) -> str:
        return "\n".join(self.row_text(row) for row in range(FRAME_ROWS))

    def can_write(self, writes: dict[tuple[int, int], str]) -> bool:
        for (row, col), ch in writes.items():
            if not (0 <= row < FRAME_ROWS and 0 <= col < FRAME_COLS):
                return False
            current = self.cells[row][col]
            if current != 0 and current != ord(ch):
                return False
        return True

    def apply(self, writes: dict[tuple[int, int], str], owner: str) -> bool:
        if not self.can_write(writes):
            return False
        for (row, col), ch in writes.items():
            self.cells[row][col] = ord(ch)
            self.owners[(row, col)] = owner
        return True

    def labels_resolve_with(self, extra: dict[tuple[int, int], str] | None = None) -> bool:
        """Every placed label must still be found at its own position."""
        extra = extra or {}
        for label, first, last, row, col in self.labels:
            found = None
            for candidate in range(max(0, first), min(last, FRAME_ROWS - 1) + 1):
                text = self.row_text_with(candidate, extra)
                if label in text:
                    found = (candidate, text.find(label))
                    break
            if found != (row, col):
                return False
        return True


def _span_writes(span: Span, raw: str, offset: int) -> dict[tuple[int, int], str]:
    content = " " * offset + raw
    content = content + " " * (span.length - len(content))
    return {(span.row, span.col + index): ch for index, ch in enumerate(content)}


def _merge_writes(base: dict[tuple[int, int], str], extra: dict[tuple[int, int], str]) -> dict[tuple[int, int], str] | None:
    """Merge two write sets; None when they disagree on a shared slot."""
    for key, ch in extra.items():
        if key in base and base[key] != ch:
            return None
        base[key] = ch
    return base


def _place_span(layout: _Layout, span: Span, raws: list[str], owner: str, state: dict[str, int]) -> bool:
    for raw in raws:
        if len(raw) > span.length:
            continue
        for offset in range(span.length - len(raw) + 1):
            state["attempts"] += 1
            if state["attempts"] > state["budget"]:
                return False
            if layout.apply(_span_writes(span, raw, offset), owner):
                return True
    return False


def _label_writes_right(row: int, col: int, label: str, value: str, length: int | None) -> dict[tuple[int, int], str] | None:
    start = col + len(label) + 1
    if start > FRAME_COLS:
        return None
    end = FRAME_COLS if length is None else min(FRAME_COLS, start + length)
    if len(value) > end - start:
        return None
    writes: dict[tuple[int, int], str] = {}
    for index, ch in enumerate(label):
        writes[(row, col + index)] = ch
    writes[(row, col + len(label))] = " "
    for index in range(end - start):
        writes[(row, start + index)] = value[index] if index < len(value) else " "
    return writes


def _place_label_field(layout: _Layout, locator: Locator, value: str | None, owner: str, state: dict[str, int], avoid_rows: frozenset[int]) -> bool:
    data = locator.data
    label = data["label"]
    first, last = data["search_rows"]
    value_text = "" if value is None else value
    rows = [row for row in range(first, last + 1) if row not in avoid_rows]
    rows += [row for row in range(first, last + 1) if row in avoid_rows]
    for row in rows:
        if not 0 <= row < FRAME_ROWS:
            continue
        if data["direction"] == "right":
            for col in range(0, FRAME_COLS - len(label) + 1):
                writes = _label_writes_right(row, col, label, value_text, data["length"])
                if writes is None:
                    if data["length"] is None:
                        break  # wider columns only shrink the remaining row
                    continue
                state["attempts"] += 1
                if state["attempts"] > state["budget"]:
                    return False
                if not layout.can_write(writes):
                    continue
                candidate = layout.labels + [(label, first, last, row, col)]
                resolved = _labels_resolve(layout, writes, candidate)
                if resolved and layout.apply(writes, owner):
                    layout.labels.append((label, first, last, row, col))
                    return True
        else:
            target = row + data["row_offset"]
            if target >= FRAME_ROWS:
                continue
            vcol = data["col"] or 0
            length = data["length"]
            vend = FRAME_COLS if length is None else min(FRAME_COLS, vcol + length)
            if len(value_text) > vend - vcol:
                return False
            for col in range(0, FRAME_COLS - len(label) + 1):
                writes = {(row, col + index): ch for index, ch in enumerate(label)}
                for index in range(vend - vcol):
                    writes[(target, vcol + index)] = value_text[index] if index < len(value_text) else " "
                state["attempts"] += 1
                if state["attempts"] > state["budget"]:
                    return False
                if not layout.can_write(writes):
                    continue
                candidate = layout.labels + [(label, first, last, row, col)]
                if _labels_resolve(layout, writes, candidate) and layout.apply(writes, owner):
                    layout.labels.append((label, first, last, row, col))
                    return True
    return False


def _labels_resolve(layout: _Layout, writes: dict[tuple[int, int], str], placements: list[tuple[str, int, int, int, int]]) -> bool:
    for label, first, last, row, col in placements:
        found = None
        for candidate in range(max(0, first), min(last, FRAME_ROWS - 1) + 1):
            text = layout.row_text_with(candidate, writes)
            if label in text:
                found = (candidate, text.find(label))
                break
        if found != (row, col):
            return False
    return True


def _place_fragment(layout: _Layout, fragment: str, state: dict[str, int], avoid_rows: frozenset[int]) -> bool:
    if fragment in layout.text():
        return True
    for row in range(FRAME_ROWS):
        if row in avoid_rows:
            continue
        for col in range(0, FRAME_COLS - len(fragment) + 1):
            writes = {(row, col + index): ch for index, ch in enumerate(fragment)}
            state["attempts"] += 1
            if state["attempts"] > state["budget"]:
                return False
            if layout.can_write(writes) and layout.labels_resolve_with(writes):
                layout.apply(writes, "match")
                return True
    return False


def _place_sensitive(layout: _Layout, item: ScreenField, state: dict[str, int], avoid_rows: frozenset[int]) -> None:
    """Best-effort anonymized placeholder; never fails the layout."""
    locator = item.locator
    owner = f"sensitive:{item.name}"
    if locator.kind in {"absolute", "multiline"}:
        for span in locator_spans(locator):
            placeholder = SENSITIVE_PLACEHOLDER[: span.length]
            layout.apply(_span_writes(span, placeholder, 0), owner)
        return
    data = locator.data
    length = data["length"]
    placeholder = SENSITIVE_PLACEHOLDER if length is None else SENSITIVE_PLACEHOLDER[:length]
    _place_label_field(layout, locator, placeholder, owner, state, avoid_rows)


# ---------------------------------------------------------------------------
# Forced character slots (sound UNSAT evidence)
#
# A field's slots are *forced* only when every feasible value renders at the
# exact same length as its fixed span (tight), the field cannot be empty, and
# every value is ASCII.  Character variants come from the inverse of the
# normalization rules actually configured (upper/fullwidth_digits), so
# normalization folding different raw text into the same value is handled.


def _char_variants(ch: str, normalize: tuple[str, ...]) -> frozenset[str]:
    variants = {ch}
    if "upper" in normalize and ch.isascii():
        variants |= {ch.lower(), ch.upper()}
    if "fullwidth_digits" in normalize and ch in _string.digits:
        variants.add(chr(0xFF10 + int(ch)))
    if "collapse_space" in normalize and ch == " ":
        variants.add("\t")
    return frozenset(variants)


def _raw_variants(value: str, normalize: tuple[str, ...], cap: int = 32) -> list[str]:
    """Raw renderings of a normalized value, identity first, bounded.

    Normalization folds many raw strings into one value (case, fullwidth
    digits, whitespace runs), so construction may need a non-identity
    rendering to stay consistent with a shared character slot.
    """
    options = [[ch] + sorted(_char_variants(ch, normalize) - {ch}) for ch in value]
    variants = ["".join(combo) for combo in itertools.islice(itertools.product(*options), cap)]
    return variants


def _forced_slots(model: _FieldModel) -> dict[int, frozenset[str]] | None:
    """Per-column forced character sets, or None when not fully forced."""
    domain = model.domain
    if not domain.finite or not domain.values:
        return None
    if any(value is None for value in domain.values):
        return None
    values = [value for value in domain.values if value is not None]
    if any(not value.isascii() for value in values):
        return None
    if model.column is not None:
        width = model.column.end - model.column.start
    elif model.locator is not None and model.locator.kind == "absolute":
        width = model.locator.data["length"]
    else:
        return None  # multiline pieces and labels can move: never forced
    if any(len(value) != width for value in values):
        return None
    slots: dict[int, frozenset[str]] = {}
    for index in range(width):
        chars: set[str] = set()
        for value in values:
            chars |= _char_variants(value[index], model.normalize)
        col = model.column.start + index if model.column is not None else model.locator.data["col"] + index
        slots[col] = frozenset(chars)
    return slots


def _slot_conflict_pair(name_a: str, slots_a: dict[int, frozenset[str]], name_b: str, slots_b: dict[int, frozenset[str]], row: int | None) -> dict[str, Any] | None:
    for col in sorted(set(slots_a) & set(slots_b)):
        if slots_a[col].isdisjoint(slots_b[col]):
            return {
                "type": "slot_conflict",
                "row": row,
                "col": col,
                "fields": [name_a, name_b],
                "chars": {name_a: sorted(slots_a[col]), name_b: sorted(slots_b[col])},
            }
    return None


# ---------------------------------------------------------------------------
# Synthetic rule checks (real validation engine, screen-scoped)


def _synthetic_record(screen_id: str, values: dict[str, str | None], key: str) -> dict[str, Any]:
    return {"screen_id": screen_id, "normalized_values": values, "raw_values": {}, "record_key": key, "primary_key": {}}


def _records_ok(model: _ScreenModel, records: list[dict[str, Any]], facts: dict[str, str | None]) -> bool:
    result = validate_records(records, model.scoped, facts_by_screen={model.contract.screen_id: facts})
    return result.ok


def _decimal_text(value: Decimal) -> str:
    return str(value)


def _sum_of(values: list[str | None]) -> Decimal:
    total = Decimal("0")
    for value in values:
        if value is None:
            continue
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            continue
        if number.is_finite():
            total += number
    return total


# ---------------------------------------------------------------------------
# Single-record screen solver


def _derived_facts_single(model: _ScreenModel, summary_values: dict[str, str | None], record_values: dict[str, str | None]) -> dict[str, str | None] | None:
    """Fill count/sum facts for a one-record screen; None when impossible."""
    derived = dict(summary_values)
    for check in model.batch_checks:
        if check.check == "count_equals":
            if model.summary_model(check.fact) is None:
                return None
            derived[check.fact] = "1"
        elif check.check == "sum_equals":
            if model.summary_model(check.fact) is None or check.field_name not in record_values:
                return None
            derived[check.fact] = _decimal_text(_sum_of([record_values[check.field_name]]))
    for name, value in derived.items():
        summary = model.summary_model(name)
        if summary is not None and not _value_fits_locator(value, summary.locator, None):
            return None
    return derived


def _layout_single(model: _ScreenModel, values: dict[str, str | None], state: dict[str, int]) -> _Layout | None:
    layout = _Layout()
    avoid_rows = frozenset()
    for item in model.fields + model.summary:
        if item.locator is None or item.locator.kind == "label":
            continue
        raw = "" if values.get(item.name) is None else str(values[item.name])
        spans = locator_spans(item.locator)
        if item.locator.kind == "multiline":
            pieces = _multiline_pieces(raw, item.locator)
            if pieces is None:
                return None
            for span, piece in zip(spans, pieces):
                if not _place_span(layout, span, piece, item.name, state):
                    return None
        else:
            if not _place_span(layout, spans[0], raw, item.name, state):
                return None
    labels = sorted(
        (item for item in model.fields + model.summary if item.locator is not None and item.locator.kind == "label"),
        key=lambda item: item.locator.data["search_rows"][1] - item.locator.data["search_rows"][0],
    )
    for item in labels:
        if not _place_label_field(layout, item.locator, values.get(item.name), item.name, state, avoid_rows):
            return None
    for fragment in model.contract.match:
        if not _place_fragment(layout, fragment, state, avoid_rows):
            return None
    for item in model.sensitive:
        _place_sensitive(layout, item, state, avoid_rows)
    return layout


def _multiline_pieces(value: str, locator: Locator) -> list[str] | None:
    data = locator.data
    rows = data["rows"]
    if len(value) <= data["length"]:
        return [value] + [""] * (len(rows) - 1)
    join = data["join"]
    if join and join in value:
        pieces = value.split(join)
        if len(pieces) <= len(rows) and all(0 < len(piece) <= data["length"] for piece in pieces):
            return pieces + [""] * (len(rows) - len(pieces))
    return None


def _solve_single_record(model: _ScreenModel) -> ContractSatResult:
    contract = model.contract
    result = ContractSatResult(contract.screen_id, PURPOSE_DEPLOYMENT, UNKNOWN)
    if contract.fingerprint is not None:
        result.reasons.append("fingerprint_pinned")
        return result
    for item in model.value_fields:
        if item.domain.finite and not item.domain.values:
            result.verdict = UNSAT
            result.conflicts.append({"type": "empty_domain", "field": item.name, "source": item.source})
            result.minimal_conflict = [f"field:{contract.screen_id}.{item.name}"]
            return result
    conflict = _absolute_slot_conflict(model)
    if conflict is not None:
        result.verdict = UNSAT
        result.conflicts.append(conflict)
        result.minimal_conflict = [f"field:{contract.screen_id}.{name}" for name in conflict["fields"]] + [f"slot:{conflict['row']}:{conflict['col']}"]
        return result

    enum_fields = [item for item in model.fields]
    summary_fields = [item for item in model.summary]
    summary_names = {item.name for item in summary_fields}
    domains = [item.domain.values for item in enum_fields + summary_fields]
    names = [item.name for item in enum_fields + summary_fields]
    attempts = 0
    layout_failures = 0
    verification_failures = 0
    state = {"attempts": 0, "budget": model.budget.max_layout_attempts}
    for combo in itertools.product(*domains):
        attempts += 1
        if attempts > model.budget.max_assignments:
            result.reasons.append("budget_exceeded:assignments")
            result.stats["assignments"] = attempts - 1
            return result
        values = dict(zip(names, combo))
        summary_values = {name: values[name] for name in names if name in summary_names}
        record_values = {name: values[name] for name in names if name not in summary_names}
        facts = _derived_facts_single(model, summary_values, record_values)
        if facts is None:
            continue
        if not _records_ok(model, [_synthetic_record(contract.screen_id, record_values, "sat-probe")], facts):
            continue
        layout = _layout_single(model, values, state)
        if layout is None:
            layout_failures += 1
            continue
        frame = ScreenFrame.from_rows([layout.row_text(row) for row in range(FRAME_ROWS)])
        extraction = extract_screen(frame, contract, 1)
        validation = validate_extraction(extraction, model.validation)
        if validation.ok:
            result.verdict = SAT
            result.witness_rows = list(frame.rows)
            result.witness_sha256 = frame.sha256
            result.witness_values = extraction.records[0]["normalized_values"] if extraction.records else {}
            result.stats["assignments"] = attempts
            return result
        verification_failures += 1
    result.stats["assignments"] = attempts
    if model.finite and layout_failures == 0 and verification_failures == 0:
        minimal, truncated = _minimize_conflict(model)
        result.verdict = UNSAT
        result.conflicts.append({"type": "joint_constraints", "detail": "no value assignment satisfies all field rules and record/batch checks"})
        result.minimal_conflict = minimal
        if truncated:
            result.reasons.append("minimization_budget_exceeded")
        return result
    if layout_failures:
        result.reasons.append("layout_unresolved")
    if verification_failures:
        result.reasons.append("witness_verification_failed")
    result.reasons.extend(reason for reason in model.reasons if reason not in result.reasons)
    return result


def _absolute_slot_conflict(model: _ScreenModel) -> dict[str, Any] | None:
    entries: list[tuple[str, int, dict[int, frozenset[str]]]] = []
    for item in model.fields + model.summary:
        if item.locator is None or item.locator.kind != "absolute":
            continue
        slots = _forced_slots(item)
        if slots:
            entries.append((item.name, item.locator.data["row"], slots))
    for index, (name_a, row_a, slots_a) in enumerate(entries):
        for name_b, row_b, slots_b in entries[index + 1 :]:
            if row_a != row_b:
                continue
            conflict = _slot_conflict_pair(name_a, slots_a, name_b, slots_b, row_a)
            if conflict is not None:
                return conflict
    return None


# ---------------------------------------------------------------------------
# List screen solver


def _region_rows(region: ListRegion) -> list[int]:
    return list(range(region.start_row, region.end_row, region.step))


def _count_candidates(model: _ScreenModel, region_rows: list[int]) -> tuple[list[int] | None, str | None]:
    """Feasible record counts, or (None, reason) when undecidable/unsat."""
    count_checks = [check for check in model.batch_checks if check.check == "count_equals"]
    non_empty = any(check.check == "non_empty" for check in model.batch_checks)
    low = 1 if non_empty else 0
    if not count_checks:
        return list(range(low, len(region_rows) + 1)), None
    candidate_sets: list[set[int]] = []
    pinned = True
    for check in count_checks:
        summary = model.summary_model(check.fact)
        if summary is None:
            return [], "count_fact_missing"
        values: set[int] = set()
        if summary.domain.finite:
            for value in summary.domain.values:
                if value is None:
                    continue
                try:
                    values.add(int(str(value)))
                except ValueError:
                    continue
            if not values:
                return [], "count_fact_no_integer"
        else:
            pinned = False
            values = set(range(0, len(region_rows) + 1))
        candidate_sets.append(values)
    intersection = set.intersection(*candidate_sets) if candidate_sets else set()
    candidates = sorted(n for n in intersection if low <= n <= len(region_rows))
    if not candidates:
        if pinned and intersection and all(n > len(region_rows) for n in intersection):
            return None, "count_exceeds_single_frame"
        return [], "count_checks_conflict"
    return candidates, None


def _key_options(model: _ScreenModel, key_fields: tuple[str, ...]) -> list[tuple[str | None, ...]] | None:
    domains = []
    for name in key_fields:
        item = next((entry for entry in model.columns if entry.name == name), None)
        if item is None or not item.domain.finite:
            return None
        domains.append(item.domain.values)
    options = list(itertools.product(*domains))
    return options if len(options) <= model.budget.max_assignments else None


def _pigeonhole_conflict(model: _ScreenModel, candidates: list[int]) -> dict[str, Any] | None:
    if not candidates:
        return None
    needed = min(candidates)
    for check in model.batch_checks:
        if check.check == "unique_key" and check.key:
            options = _key_options(model, check.key)
            if options is not None and needed > len(options):
                return {"type": "pigeonhole", "check": check.check_id, "key_fields": list(check.key), "distinct_keys": len(options), "required_records": needed}
        if check.check == "duplicate_record":
            options = _key_options(model, tuple(item.name for item in model.columns))
            if options is not None and needed > len(options):
                return {"type": "pigeonhole", "check": check.check_id, "key_fields": [item.name for item in model.columns], "distinct_keys": len(options), "required_records": needed}
    return None


def _list_slot_conflict(model: _ScreenModel, min_records: int, region_rows: list[int]) -> dict[str, Any] | None:
    forced: list[tuple[str, dict[int, frozenset[str]]]] = []
    for item in model.columns:
        slots = _forced_slots(item)
        if slots is None:
            return None  # one unforced column is enough to stay conservative here
        forced.append((item.name, slots))
    if min_records >= 1:
        for index, (name_a, slots_a) in enumerate(forced):
            for name_b, slots_b in forced[index + 1 :]:
                conflict = _slot_conflict_pair(name_a, slots_a, name_b, slots_b, None)
                if conflict is not None:
                    conflict["row"] = "list_region"
                    return conflict
    all_rows_forced = min_records >= len(region_rows) and len(region_rows) > 0
    if all_rows_forced:
        for item in model.summary:
            if item.locator is None or item.locator.kind != "absolute":
                continue
            row = item.locator.data["row"]
            if row not in region_rows:
                continue
            slots = _forced_slots(item)
            if not slots:
                continue
            for name_b, slots_b in forced:
                conflict = _slot_conflict_pair(item.name, slots, name_b, slots_b, row)
                if conflict is not None:
                    return conflict
    return None


def _build_rows_constructive(model: _ScreenModel, n: int, key_fields: tuple[str, ...]) -> list[dict[str, str | None]] | None:
    columns = model.columns
    key_options: list[tuple[str | None, ...]] = []
    if key_fields:
        domains = []
        for name in key_fields:
            item = next((entry for entry in columns if entry.name == name), None)
            if item is None:
                return None
            domains.append(item.domain.values)
        key_options = list(itertools.islice(itertools.product(*domains), model.budget.max_assignments))
        if len(key_options) < n:
            return None
    rows: list[dict[str, str | None]] = []
    for index in range(n):
        row: dict[str, str | None] = {}
        for column in columns:
            value = column.domain.values[0] if column.domain.values else None
            row[column.name] = value
        if key_fields:
            for pos, name in enumerate(key_fields):
                row[name] = key_options[index][pos]
        rows.append(row)
    return rows


def _adjust_sum_facts(model: _ScreenModel, rows: list[dict[str, str | None]], facts: dict[str, str | None]) -> bool:
    for check in model.batch_checks:
        if check.check != "sum_equals":
            continue
        summary = model.summary_model(check.fact)
        column = next((item for item in model.columns if item.name == check.field_name), None)
        if summary is None or column is None:
            return False
        total = _sum_of([row.get(check.field_name) for row in rows])
        if summary.domain.finite:
            targets = []
            for value in summary.domain.values:
                if value is None:
                    continue
                try:
                    targets.append((value, Decimal(str(value))))
                except InvalidOperation:
                    continue
            matched = False
            for text_value, target in targets:
                if rows and check.field_name:
                    needed = target - (total - _sum_of([rows[-1].get(check.field_name)]))
                    candidate = _decimal_text(needed)
                    rule = model.rules.get(check.field_name)
                    if rule is not None and not _passes_field(model.contract.screen_id, check.field_name, rule, candidate):
                        continue
                    if not _value_fits_locator(candidate, None, column.column):
                        continue
                    rows[-1][check.field_name] = candidate
                    facts[check.fact] = text_value
                    matched = True
                    break
                if abs(total - target) <= Decimal("0"):
                    facts[check.fact] = text_value
                    matched = True
                    break
            if not matched:
                return False
        else:
            facts[check.fact] = _decimal_text(total)
    return True


def _list_facts(model: _ScreenModel, n: int, rows: list[dict[str, str | None]]) -> dict[str, str | None] | None:
    facts: dict[str, str | None] = {}
    for item in model.summary:
        facts[item.name] = item.domain.values[0] if item.domain.values else None
    for check in model.batch_checks:
        if check.check == "count_equals":
            if model.summary_model(check.fact) is None:
                return None
            facts[check.fact] = str(n)
    if not _adjust_sum_facts(model, rows, facts):
        return None
    for name, value in facts.items():
        summary = model.summary_model(name)
        if summary is not None and not _value_fits_locator(value, summary.locator, None):
            return None
    return facts


def _layout_list(model: _ScreenModel, rows: list[dict[str, str | None]], facts: dict[str, str | None], state: dict[str, int]) -> _Layout | None:
    contract = model.contract
    region = contract.list_region
    assert region is not None
    region_rows = _region_rows(region)
    avoid_rows = frozenset(region_rows)
    layout = _Layout()
    for item in model.summary:
        if item.locator is None or item.locator.kind == "label":
            continue
        raw = "" if facts.get(item.name) is None else str(facts[item.name])
        spans = locator_spans(item.locator)
        if item.locator.kind == "multiline":
            pieces = _multiline_pieces(raw, item.locator)
            if pieces is None:
                return None
            for span, piece in zip(spans, pieces):
                if not _place_span(layout, span, piece, item.name, state):
                    return None
        else:
            if not _place_span(layout, spans[0], raw, item.name, state):
                return None
    used_rows: set[int] = set()
    for row_values in rows:
        placed = False
        for row in region_rows:
            if row in used_rows:
                continue
            writes: dict[tuple[int, int], str] = {}
            consistent = True
            for column in model.columns:
                assert column.column is not None
                raw = "" if row_values.get(column.name) is None else str(row_values[column.name])
                span = Span(row, column.column.start, column.column.end - column.column.start)
                if _merge_writes(writes, _span_writes(span, raw, 0)) is None:
                    consistent = False
                    break
            if not consistent:
                continue
            state["attempts"] += 1
            if state["attempts"] > state["budget"]:
                return None
            if layout.can_write(writes):
                layout.apply(writes, "list_row")
                used_rows.add(row)
                placed = True
                break
        if not placed:
            return None
    labels = sorted(
        (item for item in model.summary if item.locator is not None and item.locator.kind == "label"),
        key=lambda item: item.locator.data["search_rows"][1] - item.locator.data["search_rows"][0],
    )
    for item in labels:
        if not _place_label_field(layout, item.locator, facts.get(item.name), item.name, state, avoid_rows):
            return None
    for fragment in contract.match:
        if not _place_fragment(layout, fragment, state, avoid_rows):
            return None
    for item in model.sensitive:
        _place_sensitive(layout, item, state, avoid_rows)
    return layout


def _row_assignments_exhaustive(model: _ScreenModel, n: int, key_fields: tuple[str, ...]) -> Iterator[list[dict[str, str | None]]] | None:
    """All row assignments for n records, or None when the space is too wide."""
    columns = model.columns
    names = [column.name for column in columns]
    domains = [column.domain.values for column in columns]
    width = math.prod((len(domain) for domain in domains), start=1) if domains else 1
    if width > model.budget.max_assignments:
        return None
    row_options = list(itertools.product(*domains)) if domains else [()]
    key_set: set[tuple[str | None, ...]] = set()

    def generate() -> Iterator[list[dict[str, str | None]]]:
        for combo in itertools.product(row_options, repeat=n):
            rows = [dict(zip(names, values)) for values in combo]
            if key_fields:
                keys = [tuple(row[name] for name in key_fields) for row in rows]
                if len(set(keys)) != len(keys):
                    continue
                signature = tuple(sorted(keys))
                if signature in key_set:
                    continue
                key_set.add(signature)
            yield rows

    return generate()


def _solve_list(model: _ScreenModel) -> ContractSatResult:
    contract = model.contract
    region = contract.list_region
    assert region is not None
    result = ContractSatResult(contract.screen_id, PURPOSE_DEPLOYMENT, UNKNOWN)
    if contract.fingerprint is not None:
        result.reasons.append("fingerprint_pinned")
        return result
    for item in model.value_fields:
        if item.domain.finite and not item.domain.values:
            result.verdict = UNSAT
            result.conflicts.append({"type": "empty_domain", "field": item.name, "source": item.source})
            result.minimal_conflict = [f"field:{contract.screen_id}.{item.name}"]
            return result
    region_rows = _region_rows(region)
    candidates, count_reason = _count_candidates(model, region_rows)
    if candidates is None:
        result.reasons.append(count_reason or "count_undecidable")
        return result
    if not candidates:
        result.verdict = UNSAT
        result.conflicts.append({"type": "count_checks_conflict", "detail": count_reason})
        result.minimal_conflict = [f"batch_check:{check.check_id}" for check in model.batch_checks if check.check == "count_equals"]
        return result
    min_records = min(candidates)
    pigeonhole = _pigeonhole_conflict(model, candidates)
    if pigeonhole is not None:
        result.verdict = UNSAT
        result.conflicts.append(pigeonhole)
        result.minimal_conflict = [f"batch_check:{pigeonhole['check']}"]
        return result
    conflict = _list_slot_conflict(model, min_records, region_rows)
    if conflict is not None:
        result.verdict = UNSAT
        result.conflicts.append(conflict)
        result.minimal_conflict = [f"field:{contract.screen_id}.{name}" for name in conflict["fields"]] + [f"slot:{conflict['row']}:{conflict['col']}"]
        return result

    key_fields: tuple[str, ...] = tuple(dict.fromkeys(key for check in model.batch_checks if check.check == "unique_key" for key in check.key))
    state = {"attempts": 0, "budget": model.budget.max_layout_attempts}
    attempts = 0
    layout_failures = 0
    verification_failures = 0
    rule_failure = False
    for n in candidates:
        constructive = _build_rows_constructive(model, n, key_fields)
        if constructive is not None:
            rows = constructive
            facts = _list_facts(model, n, rows)
            if facts is not None and _records_ok(model, [_synthetic_record(contract.screen_id, row, f"sat-probe-{i}") for i, row in enumerate(rows)], facts):
                layout = _layout_list(model, rows, facts, state)
                if layout is not None:
                    frame = ScreenFrame.from_rows([layout.row_text(row) for row in range(FRAME_ROWS)])
                    extraction = extract_screen(frame, contract, 1)
                    validation = validate_extraction(extraction, model.validation)
                    if validation.ok:
                        result.verdict = SAT
                        result.witness_rows = list(frame.rows)
                        result.witness_sha256 = frame.sha256
                        result.witness_values = {"records": len(extraction.records), "facts": extraction.facts}
                        result.stats["assignments"] = attempts + 1
                        return result
                    verification_failures += 1
                else:
                    layout_failures += 1
            else:
                rule_failure = True
        # Exhaustive pass over this n (only meaningful for finite models).
        if not model.finite:
            continue
        stream = _row_assignments_exhaustive(model, n, key_fields)
        if stream is None:
            result.reasons.append("budget_exceeded:assignments")
            result.stats["assignments"] = attempts
            return result
        for rows in stream:
            attempts += 1
            if attempts > model.budget.max_assignments:
                result.reasons.append("budget_exceeded:assignments")
                result.stats["assignments"] = attempts
                return result
            facts = _list_facts(model, n, [dict(row) for row in rows])
            if facts is None:
                rule_failure = True
                continue
            if not _records_ok(model, [_synthetic_record(contract.screen_id, row, f"sat-probe-{i}") for i, row in enumerate(rows)], facts):
                rule_failure = True
                continue
            layout = _layout_list(model, rows, facts, state)
            if layout is None:
                layout_failures += 1
                continue
            frame = ScreenFrame.from_rows([layout.row_text(row) for row in range(FRAME_ROWS)])
            extraction = extract_screen(frame, contract, 1)
            validation = validate_extraction(extraction, model.validation)
            if validation.ok:
                result.verdict = SAT
                result.witness_rows = list(frame.rows)
                result.witness_sha256 = frame.sha256
                result.witness_values = {"records": len(extraction.records), "facts": extraction.facts}
                result.stats["assignments"] = attempts
                return result
            verification_failures += 1
    result.stats["assignments"] = attempts
    if model.finite and layout_failures == 0 and verification_failures == 0:
        minimal, truncated = _minimize_conflict(model)
        result.verdict = UNSAT
        result.conflicts.append({"type": "joint_constraints", "detail": "no row assignment satisfies all column rules and record/batch checks"})
        result.minimal_conflict = minimal
        if truncated:
            result.reasons.append("minimization_budget_exceeded")
        return result
    if layout_failures:
        result.reasons.append("layout_unresolved")
    if verification_failures:
        result.reasons.append("witness_verification_failed")
    if not model.finite:
        result.reasons.extend(reason for reason in model.reasons if reason not in result.reasons)
    elif rule_failure:
        result.reasons.append("rules_unresolved")
    return result


# ---------------------------------------------------------------------------
# Minimal conflict sets (deletion-based, budget-capped)


def _restricted_model(model: _ScreenModel, keep: set[tuple[str, str]]) -> _ScreenModel:
    fields = {model.contract.screen_id: {name: rule for name, rule in model.rules.items() if ("field", name) in keep}}
    record_checks = tuple(check for check in model.validation.record_checks if check.screen_id != model.contract.screen_id or ("record_check", check.check_id) in keep)
    batch_checks = tuple(check for check in model.validation.batch_checks if check.screen_id != model.contract.screen_id or ("batch_check", check.check_id) in keep)
    restricted = ValidationConfig(fields=fields, record_checks=record_checks, batch_checks=batch_checks)
    return _ScreenModel(model.contract, restricted, model.budget)


def _rule_level_satisfiable(model: _ScreenModel) -> bool | None:
    """True/False when decidable within budget, None otherwise (no layout)."""
    if not model.finite:
        return None
    contract = model.contract
    if contract.list_region is None:
        names = [item.name for item in model.fields + model.summary]
        summary_names = {item.name for item in model.summary}
        domains = [item.domain.values for item in model.fields + model.summary]
        attempts = 0
        for combo in itertools.product(*domains):
            attempts += 1
            if attempts > model.budget.max_assignments:
                return None
            values = dict(zip(names, combo))
            summary_values = {name: values[name] for name in names if name in summary_names}
            record_values = {name: values[name] for name in names if name not in summary_names}
            facts = _derived_facts_single(model, summary_values, record_values)
            if facts is None:
                continue
            if _records_ok(model, [_synthetic_record(contract.screen_id, record_values, "sat-probe")], facts):
                return True
        return False
    region_rows = _region_rows(contract.list_region)
    candidates, _ = _count_candidates(model, region_rows)
    if candidates is None:
        return None
    key_fields: tuple[str, ...] = tuple(dict.fromkeys(key for check in model.batch_checks if check.check == "unique_key" for key in check.key))
    attempts = 0
    for n in candidates:
        stream = _row_assignments_exhaustive(model, n, key_fields)
        if stream is None:
            return None
        for rows in stream:
            attempts += 1
            if attempts > model.budget.max_assignments:
                return None
            facts = _list_facts(model, n, [dict(row) for row in rows])
            if facts is None:
                continue
            if _records_ok(model, [_synthetic_record(contract.screen_id, row, f"sat-probe-{i}") for i, row in enumerate(rows)], facts):
                return True
    return False


def _minimize_conflict(model: _ScreenModel) -> tuple[list[str], bool]:
    constraints: list[tuple[str, str]] = []
    for name in model.rules:
        constraints.append(("field", name))
    constraints.extend(("record_check", check.check_id) for check in model.record_checks)
    constraints.extend(("batch_check", check.check_id) for check in model.batch_checks)
    current = list(constraints)
    checks_done = 0
    for candidate in list(current):
        if checks_done >= model.budget.max_minimization_checks:
            return [f"{kind}:{name}" for kind, name in current], True
        trial = [item for item in current if item != candidate]
        checks_done += 1
        restricted = _restricted_model(model, set(trial))
        verdict = _rule_level_satisfiable(restricted)
        if verdict is False:
            current = trial
    return [f"{kind}:{name}" for kind, name in current], False


# ---------------------------------------------------------------------------
# Public solver entry points


def solve_screen_contract(contract: ScreenContract, validation: ValidationConfig, *, budget: SatBudget | None = None) -> ContractSatResult:
    """Deployment feasibility: does one synthetic screen satisfy everything?"""
    budget = budget or SatBudget()
    model = _ScreenModel(contract, validation, budget)
    if contract.list_region is not None:
        return _solve_list(model)
    return _solve_single_record(model)


def walk_scenario_frames(session: SessionContract, scenario: MockScenario) -> list[tuple[int, str, str, ScreenFrame]]:
    """(seq, step_id, claimed screen_id, frame) pairs, mirroring live navigation."""
    pairs = [(0, "start", session.start.screen_id, scenario.frame(scenario.start_screen))]
    current = scenario.start_screen
    for index, step in enumerate(session.steps, start=1):
        current = scenario.next_screen(current, step.key)
        pairs.append((index, step.step_id, step.expect.screen_id, scenario.frame(current)))
    return pairs


def check_regression_adaptation(session: SessionContract, screens: dict[str, ScreenContract], validation: ValidationConfig, scenario: MockScenario) -> list[ContractSatResult]:
    """Regression adaptation: every claimed frame must extract and validate."""
    pairs = walk_scenario_frames(session, scenario)
    claimed_order: list[str] = []
    for _, _, screen_id, _ in pairs:
        if screen_id in screens and screen_id not in claimed_order:
            claimed_order.append(screen_id)
    results: list[ContractSatResult] = []
    for screen_id in claimed_order:
        contract = screens[screen_id]
        result = ContractSatResult(screen_id, PURPOSE_REGRESSION, SAT)
        claimed = [(seq, step_id, frame) for seq, step_id, claim, frame in pairs if claim == screen_id]
        failures: dict[int, list[dict[str, Any]]] = {}
        usable: list[tuple[int, ScreenFrame]] = []
        step_of: dict[int, str] = {}
        for seq, step_id, frame in claimed:
            step_of[seq] = step_id
            try:
                extract_screen(frame, contract, seq)
                usable.append((seq, frame))
            except HarvestError as exc:
                failures.setdefault(seq, []).append({"code": exc.code, "message": exc.message})
        if usable:
            if contract.list_region is not None:
                paging = session.paging if session.paging is not None and session.paging.screen_id == screen_id else None
                paged = extract_pages(
                    usable,
                    contract,
                    max_pages=paging.max_pages if paging else None,
                    stop_marker=paging.stop_marker if paging else None,
                    stop_on_repeated_page=(paging.stop_on_repeated_page if paging else True),
                )
                extraction = paged.extraction
            else:
                extraction = extract_screen(usable[0][1], contract, usable[0][0])
            validation_result = validate_extraction(extraction, validation)
            by_key = {record.get("record_key"): record for record in extraction.records}
            for error in validation_result.errors:
                record = by_key.get(error.get("record_key"))
                seq = record.get("frame_seq") if record else None
                entry = {"code": error["code"], "message": error["message"], "rule_id": error["rule_id"]}
                if seq is None:
                    for known_seq in step_of:
                        failures.setdefault(known_seq, []).append({**entry, "scope": "screen"})
                else:
                    failures.setdefault(seq, []).append(entry)
        for seq in sorted(failures):
            result.non_adapting_frames.append({"frame_seq": seq, "step_id": step_of.get(seq), "screen_id": screen_id, "errors": failures[seq][:8]})
        if result.non_adapting_frames:
            result.verdict = UNSAT
        else:
            result.stats["frames_checked"] = len(claimed)
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# Session-level orchestration used by the pipeline


@dataclass
class PrecheckOutcome:
    results: list[ContractSatResult]
    refusal: HarvestError | None

    def event_context(self) -> dict[str, Any]:
        return {
            "screens": [
                {"screen_id": result.screen_id, "purpose": result.purpose, "verdict": result.verdict, "reasons": result.reasons[:4]}
                for result in self.results
            ]
        }


def run_session_precheck(session: SessionContract, screens: dict[str, ScreenContract], validation: ValidationConfig) -> PrecheckOutcome:
    """Evaluate the session's precheck config; never silently passes UNKNOWN."""
    precheck = session.precheck
    assert precheck is not None
    budget = SatBudget.from_overrides(dict(precheck.budget))
    results: list[ContractSatResult] = []
    if precheck.deployment:
        for screen_id in sorted(screens):
            results.append(solve_screen_contract(screens[screen_id], validation, budget=budget))
        for result in results:
            if result.verdict == UNSAT:
                return PrecheckOutcome(
                    results,
                    HarvestError(
                        E_CONTRACT_UNSAT,
                        f"screen contract {result.screen_id!r} is provably not satisfiable",
                        context={"screen_id": result.screen_id, "verdict": result.verdict, "conflicts": result.conflicts[:8], "minimal_conflict": result.minimal_conflict},
                    ),
                )
        for result in results:
            if result.verdict == UNKNOWN:
                return PrecheckOutcome(
                    results,
                    HarvestError(
                        E_CONTRACT_PRECHECK_UNKNOWN,
                        f"screen contract {result.screen_id!r} satisfiability is unknown; refusing to treat it as SAT",
                        context={"screen_id": result.screen_id, "verdict": result.verdict, "reasons": result.reasons[:8]},
                    ),
                )
    if precheck.regression_scenario:
        scenario = load_scenario(precheck.regression_scenario)
        regression = check_regression_adaptation(session, screens, validation, scenario)
        results.extend(regression)
        bad = [result for result in regression if result.verdict != SAT]
        if bad:
            frames = [frame for result in bad for frame in result.non_adapting_frames]
            return PrecheckOutcome(
                results,
                HarvestError(
                    E_CONTRACT_REGRESSION_MISMATCH,
                    "mock scenario frames do not satisfy the extraction and validation rules",
                    context={"screens": [result.screen_id for result in bad], "frames": frames[:8]},
                ),
            )
    return PrecheckOutcome(results, None)
