"""Auditable, non-backtracking matcher for the controlled field-pattern subset.

Field ``pattern`` values in validation contracts are matched against screen
text.  Python's :mod:`re` engine uses backtracking: a legal pattern such as
``(A+)+B`` applied to a long run of ``A`` followed by ``C`` takes exponential
time to report "no match".  A timeout that hides that failure cannot be used
as evidence that the data is valid.

This module implements an explicit, bounded semantic instead:

1. ``pattern`` text is parsed by a recursive-descent parser into an AST whose
   nodes carry the source position of every construct.
2. The AST is compiled with Thompson's construction into an epsilon-NFA whose
   state count is hard-capped by a configurable budget.  Repetition never
   expands per input character: ``+``/``*``/``{m,}`` are single loop edges and
   finite ``{m,n}`` ranges are unrolled only while the budget allows.
3. Matching simulates the NFA with state-set (subset) propagation.  Every
   input character costs at most ``states * predicates`` work, so total work
   is linear in both the input length and the compiled state count.  There is
   no recursion over the input and no backtracking.

Supported subset (defined to agree with ``re.fullmatch`` on every input that
uses only these constructs):

* anchors: ``^`` and ``$`` (fullmatch semantics; ``$`` also matches before a
  trailing newline exactly like Python's default ``$``);
* literal characters, including escaped punctuation;
* character classes: ``[...]``, negation, ranges, and the shorthands
  ``\\d \\D \\w \\W \\s \\S`` both inside and outside classes;
* groups: plain ``(...)`` and non-capturing ``(?:...)``;
* alternation ``|``;
* quantifiers: ``?``, ``*``, ``+``, ``{m}``, ``{m,n}``, ``{m,}`` (greedy and
  non-greedy spellings are both accepted; greediness cannot change whether a
  fullmatch exists in an NFA);
* the dot ``.`` (any character except newline).

Unsupported constructs (backreferences, look-around, named groups, inline
flags, possessive quantifiers, octal/named Unicode escapes, ...) are rejected
at parse time with the pattern source position.  Compilation that would exceed
the state budget is rejected with the position of the offending construct.
Neither condition ever falls back to :mod:`re`; callers receive a
:class:`PatternRejection` describing an *undecidable* pattern.

The pattern text only ever originates from local synthetic contracts; this
module performs no I/O and executes no external code.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Public result types and reason codes
# ---------------------------------------------------------------------------

#: Parse-time rejection: the pattern uses a construct outside the subset.
REASON_UNSUPPORTED = "unsupported_construct"
#: Parse-time rejection: the pattern text is not syntactically valid.
REASON_SYNTAX = "syntax_error"
#: Compile-time rejection: the epsilon-NFA would exceed the state budget.
REASON_BUDGET = "state_budget_exceeded"

#: Default upper bound on epsilon-NFA states for one pattern.
DEFAULT_STATE_BUDGET = 256

#: Match verdicts.
MATCH = "match"
NO_MATCH = "no_match"


@dataclass(frozen=True)
class PatternRejection:
    """A pattern that cannot be decided under the bounded semantic.

    ``position`` is the zero-based character offset in the pattern text where
    the offending construct starts (or where the budget was exhausted);
    ``line``/``column`` are the human-readable 1-based equivalents.
    """

    reason: str
    message: str
    position: int
    line: int
    column: int

    def describe(self) -> str:
        return f"{self.reason} at line {self.line} column {self.column}: {self.message}"


@dataclass(frozen=True)
class PatternProgram:
    """A compiled, budget-bounded epsilon-NFA ready for state-set matching."""

    pattern: str
    states: int
    budget: int
    # Consuming edges: state -> ((target, predicate, anchor_kind_or_none), ...)
    transitions: tuple[tuple[tuple[int, Callable[[str], bool], Optional[str]], ...], ...]
    epsilons: tuple[tuple[int, ...], ...]
    start: int
    accept: int

    def fullmatch(self, text: str) -> str:
        """Return :data:`MATCH` or :data:`NO_MATCH`, never raising on input."""
        return simulate(self, text)


# ---------------------------------------------------------------------------
# Parser-level exception (internal; converted into PatternRejection)
# ---------------------------------------------------------------------------


class _PatternError(Exception):
    def __init__(self, reason: str, message: str, position: int) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.position = position


# ---------------------------------------------------------------------------
# Character predicates
# ---------------------------------------------------------------------------


def _is_digit(ch: str) -> bool:
    # Python \d in a str pattern matches Unicode decimal (category Nd) code
    # points; str.isdigit() additionally includes superscripts and fractions.
    return len(ch) == 1 and unicodedata.category(ch) == "Nd"


def _is_word(ch: str) -> bool:
    # Python \w: Unicode word characters plus the underscore.
    return ch == "_" or (len(ch) == 1 and ch.isalnum())


def _is_space(ch: str) -> bool:
    # Python \s: Unicode whitespace (includes \t \n \r \f \v and more).
    return len(ch) == 1 and ch.isspace()


def _negate(predicate: Callable[[str], bool]) -> Callable[[str], bool]:
    return lambda ch, p=predicate: not p(ch)


def _literal(value: str) -> Callable[[str], bool]:
    return lambda ch, v=value: ch == v


def _in_range(low: str, high: str) -> Callable[[str], bool]:
    return lambda ch, lo=low, hi=high: lo <= ch <= hi


# ---------------------------------------------------------------------------
# AST (each node records the source position of its leading construct)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Node:
    position: int


@dataclass(frozen=True)
class _Char(_Node):
    predicate: Callable[[str], bool]
    label: str


@dataclass(frozen=True)
class _Anchor(_Node):
    kind: str  # "start" or "end"


@dataclass(frozen=True)
class _Concat(_Node):
    children: tuple["_Node", ...]


@dataclass(frozen=True)
class _Alternation(_Node):
    alternatives: tuple["_Node", ...]


@dataclass(frozen=True)
class _Repeat(_Node):
    child: "_Node"
    minimum: int
    maximum: Optional[int]  # None means unbounded


# ---------------------------------------------------------------------------
# Recursive-descent parser
# ---------------------------------------------------------------------------

# Escapes whose meaning is defined both standalone and inside character classes.
_CLASS_ESCAPES: dict[str, tuple[Callable[[str], bool], str]] = {
    "d": (_is_digit, r"\d"),
    "D": (_negate(_is_digit), r"\D"),
    "w": (_is_word, r"\w"),
    "W": (_negate(_is_word), r"\W"),
    "s": (_is_space, r"\s"),
    "S": (_negate(_is_space), r"\S"),
}

# Escapes that stand for a literal character.
_LITERAL_ESCAPES: dict[str, str] = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "f": "\f",
    "v": "\v",
    "0": "\0",
    "a": "\a",
    "b": "\b",
}

# Constructs Python supports but the bounded subset deliberately excludes.
_UNSUPPORTED_GROUP = frozenset(
    {
        "P",  # Unicode property: \P{...}
        "p",  # Unicode property: \p{...}
        "N",  # named Unicode character: \N{...}
        "u",  # \uXXXX
        "U",  # \UXXXXXXXX
        "x",  # \xHH
        "k",  # named backreference: \k<name>
        "g",  # group reference/conditional: \g<...>
    }
)


class _Parser:
    def __init__(self, pattern: str) -> None:
        self.text = pattern
        self.length = len(pattern)
        self.index = 0

    # -- helpers -----------------------------------------------------------

    def _fail(self, reason: str, message: str, position: Optional[int] = None) -> None:
        raise _PatternError(reason, message, self.index if position is None else position)

    def _eof(self) -> bool:
        return self.index >= self.length

    def _peek(self) -> str:
        return self.text[self.index] if not self._eof() else ""

    def _advance(self) -> str:
        ch = self.text[self.index]
        self.index += 1
        return ch

    @staticmethod
    def _is_quantifier_start(ch: str) -> bool:
        return ch in "?*+{"

    # -- entry point -------------------------------------------------------

    def parse(self) -> _Node:
        node = self._parse_alternation(0)
        if not self._eof():
            ch = self._peek()
            if ch == ")":
                self._fail(REASON_SYNTAX, "unbalanced closing parenthesis")
            self._fail(REASON_SYNTAX, f"unexpected character {ch!r}")
        return node

    # -- grammar -----------------------------------------------------------

    def _parse_alternation(self, position: int) -> _Node:
        alternatives = [self._parse_concatenation()]
        while not self._eof() and self._peek() == "|":
            self._advance()
            alternatives.append(self._parse_concatenation())
        if len(alternatives) == 1:
            return alternatives[0]
        return _Alternation(position, tuple(alternatives))

    def _parse_concatenation(self) -> _Node:
        start = self.index
        children: list[_Node] = []
        while not self._eof() and self._peek() not in "|)":
            children.append(self._parse_repeat())
        if len(children) == 1:
            return children[0]
        return _Concat(start, tuple(children))

    def _parse_repeat(self) -> _Node:
        atom = self._parse_atom()
        if self._eof() or not self._is_quantifier_start(self._peek()):
            return atom
        quantifier_position = self.index
        token = self._advance()
        if token == "?":
            minimum, maximum = 0, 1
        elif token == "*":
            minimum, maximum = 0, None
        elif token == "+":
            minimum, maximum = 1, None
        else:
            minimum, maximum = self._parse_brace_repeat(quantifier_position)
        # Greedy versus lazy cannot change whether an NFA fullmatch exists;
        # the lazy marker is accepted so contract spellings stay familiar.
        if not self._eof() and self._peek() == "?":
            self._advance()
        elif not self._eof() and self._peek() == "+":
            self._fail(
                REASON_UNSUPPORTED,
                "possessive quantifiers are not supported by the bounded matcher",
                quantifier_position,
            )
        return _Repeat(quantifier_position, atom, minimum, maximum)

    def _parse_brace_repeat(self, position: int) -> tuple[int, Optional[int]]:
        # Current char is just after the opening '{'.
        low_text = self._read_decimal()
        if low_text is None:
            # A '{' that does not begin a valid quantifier is a literal in
            # Python's re; back up and treat it as such.
            self.index = position
            self._fail(REASON_SYNTAX, "literal '{' must be escaped as '\\{'", position)
        low = int(low_text)
        ch = self._peek()
        if ch == "}":
            self._advance()
            return low, low
        if ch != ",":
            self._fail(REASON_SYNTAX, "expected ',' or '}' inside bounded quantifier", position)
        self._advance()  # consume ','
        if self._peek() == "}":
            self._advance()
            return low, None
        high_text = self._read_decimal()
        if high_text is None or self._peek() != "}":
            self._fail(REASON_SYNTAX, "malformed bounded quantifier", position)
        self._advance()
        high = int(high_text)
        if high < low:
            self._fail(REASON_SYNTAX, "quantifier upper bound must be >= lower bound", position)
        return low, high

    def _read_decimal(self) -> Optional[str]:
        start = self.index
        while not self._eof() and self._peek().isdigit():
            self._advance()
        if self.index == start:
            return None
        return self.text[start : self.index]

    def _parse_atom(self) -> _Node:
        position = self.index
        ch = self._advance()
        if ch == "(":
            return self._parse_group(position)
        if ch == "[":
            return self._parse_class(position)
        if ch == ".":
            return _Char(position, _negate(_literal("\n")), ".")
        if ch == "^":
            return _Anchor(position, "start")
        if ch == "$":
            return _Anchor(position, "end")
        if ch == "\\":
            return self._parse_escape(position, in_class=False)
        if ch in "?*+":
            self._fail(REASON_SYNTAX, f"quantifier {ch!r} with nothing to repeat", position)
        if ch in "{":
            self._fail(REASON_SYNTAX, "literal '{' must be escaped as '\\{'", position)
        if ch in "}":
            self._fail(REASON_SYNTAX, "literal '}' must be escaped as '\\}'", position)
        return _Char(position, _literal(ch), ch)

    def _parse_group(self, position: int) -> _Node:
        if not self._eof() and self._peek() == "?":
            marker_position = self.index
            self._advance()
            kind = self._peek()
            if kind == ":":
                self._advance()
            else:
                self._fail(
                    REASON_UNSUPPORTED,
                    "only plain and non-capturing (?:...) groups are supported; "
                    "look-around, named groups and inline flags are not",
                    marker_position,
                )
        body = self._parse_alternation(position)
        if self._eof() or self._peek() != ")":
            self._fail(REASON_SYNTAX, "unclosed group", position)
        self._advance()
        return body

    def _parse_class(self, position: int) -> _Char:
        negated = False
        if not self._eof() and self._peek() == "^":
            negated = True
            self._advance()
        predicates: list[Callable[[str], bool]] = []
        labels: list[str] = []
        first = True
        while first or (not self._eof() and self._peek() != "]"):
            first = False
            if self._eof():
                self._fail(REASON_SYNTAX, "unclosed character class", position)
            item_position = self.index
            ch = self._advance()
            if ch == "\\":
                node = self._parse_escape(item_position, in_class=True)
                if not isinstance(node, _Char):
                    self._fail(REASON_UNSUPPORTED, "only character escapes are allowed inside classes", item_position)
                predicates.append(node.predicate)
                labels.append(node.label)
                continue
            # A range: read the '-' and the upper bound.
            if not self._eof() and self._peek() == "-":
                dash_position = self.index
                self._advance()
                if self._eof():
                    self._fail(REASON_SYNTAX, "malformed character range", dash_position)
                if self._peek() == "]":
                    # Trailing '-' is a literal.
                    predicates.append(_literal(ch))
                    labels.append(ch)
                    predicates.append(_literal("-"))
                    labels.append("-")
                    break
                upper_position = self.index
                upper = self._advance()
                if upper == "\\":
                    upper_node = self._parse_escape(upper_position, in_class=True)
                    if not isinstance(upper_node, _Char) or upper_node.label not in _LITERAL_ESCAPES.values():
                        self._fail(REASON_SYNTAX, "range endpoints must be literal characters", upper_position)
                    upper_value = upper_node.label
                else:
                    upper_value = upper
                if upper_value < ch:
                    self._fail(REASON_SYNTAX, "character range is reversed", item_position)
                predicates.append(_in_range(ch, upper_value))
                labels.append(f"{ch}-{upper_value}")
                continue
            predicates.append(_literal(ch))
            labels.append(ch)
        if self._eof():
            self._fail(REASON_SYNTAX, "unclosed character class", position)
        self._advance()  # consume ']'
        if not predicates:
            self._fail(REASON_SYNTAX, "empty character class", position)

        def class_predicate(ch: str, preds=tuple(predicates)) -> bool:
            return any(pred(ch) for pred in preds)

        if negated:
            return _Char(position, _negate(class_predicate), "[^" + "".join(labels) + "]")
        return _Char(position, class_predicate, "[" + "".join(labels) + "]")

    def _parse_escape(self, position: int, *, in_class: bool) -> _Node:
        if self._eof():
            self._fail(REASON_SYNTAX, "dangling backslash at end of pattern", position)
        ch = self._advance()
        if ch in _CLASS_ESCAPES:
            predicate, label = _CLASS_ESCAPES[ch]
            return _Char(position, predicate, label)
        if ch.isdigit():
            if ch == "0" and not self._eof() and self._peek().isdigit():
                self._fail(
                    REASON_UNSUPPORTED,
                    "octal escapes are not supported",
                    position,
                )
            self._fail(
                REASON_UNSUPPORTED,
                "backreferences and octal escapes are not supported",
                position,
            )
        if ch in _UNSUPPORTED_GROUP:
            self._fail(
                REASON_UNSUPPORTED,
                f"escape \\{ch} is outside the supported pattern subset",
                position,
            )
        if ch in _LITERAL_ESCAPES:
            return _Char(position, _literal(_LITERAL_ESCAPES[ch]), "\\" + ch)
        # Escaped punctuation is the literal punctuation (e.g. \. \( \\ \-).
        return _Char(position, _literal(ch), "\\" + ch)


# ---------------------------------------------------------------------------
# Thompson construction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Fragment:
    start: int
    accept: int


class _Compiler:
    def __init__(self, budget: int) -> None:
        self.budget = budget
        # Consuming edges per state: (target, predicate, anchor_kind or None).
        self.transitions: list[list[tuple[int, Callable[[str], bool], Optional[str]]]] = []
        self.epsilons: list[list[int]] = []

    def _new_state(self, position: int) -> int:
        if len(self.transitions) >= self.budget:
            raise _PatternError(
                REASON_BUDGET,
                f"pattern needs more than {self.budget} NFA states; "
                "simplify the pattern or raise the configured budget",
                position,
            )
        state = len(self.transitions)
        self.transitions.append([])
        self.epsilons.append([])
        return state

    def _epsilon(self, source: int, target: int) -> None:
        self.epsilons[source].append(target)

    def _empty(self, position: int) -> _Fragment:
        state = self._new_state(position)
        return _Fragment(state, state)

    def compile(self, node: _Node) -> _Fragment:
        return self._node(node)

    def _node(self, node: _Node) -> _Fragment:
        if isinstance(node, _Char):
            start = self._new_state(node.position)
            accept = self._new_state(node.position)
            self.transitions[start].append((accept, node.predicate, None))
            return _Fragment(start, accept)
        if isinstance(node, _Anchor):
            start = self._new_state(node.position)
            accept = self._new_state(node.position)
            # Zero-width anchor: the epsilon edge carries the positional
            # constraint (evaluated during closure); the parallel marked
            # consuming edge always fails so anchors never eat a character.
            self._epsilon(start, accept)
            self.transitions[start].append((accept, _anchor_predicate(node.kind), node.kind))
            return _Fragment(start, accept)
        if isinstance(node, _Concat):
            if not node.children:
                return self._empty(node.position)
            first = self._node(node.children[0])
            current = first.accept
            for child in node.children[1:]:
                fragment = self._node(child)
                self._epsilon(current, fragment.start)
                current = fragment.accept
            return _Fragment(first.start, current)
        if isinstance(node, _Alternation):
            start = self._new_state(node.position)
            accept = self._new_state(node.position)
            for child in node.alternatives:
                fragment = self._node(child)
                self._epsilon(start, fragment.start)
                self._epsilon(fragment.accept, accept)
            return _Fragment(start, accept)
        if isinstance(node, _Repeat):
            return self._repeat(node)
        raise _PatternError(REASON_SYNTAX, f"unknown AST node {type(node).__name__}", node.position)

    def _repeat(self, node: _Repeat) -> _Fragment:
        try:
            return self._repeat_body(node)
        except _PatternError as exc:
            if exc.reason == REASON_BUDGET:
                # Attribute the exhaustion to the quantifier for auditability.
                raise _PatternError(REASON_BUDGET, exc.message, node.position) from exc
            raise

    def _repeat_body(self, node: _Repeat) -> _Fragment:
        # Unroll the mandatory minimum copies, chained by epsilon edges.
        current_start: Optional[int] = None
        current_accept: Optional[int] = None
        for _ in range(node.minimum):
            fragment = self._node(node.child)
            if current_start is None:
                current_start = fragment.start
            else:
                self._epsilon(current_accept, fragment.start)  # type: ignore[arg-type]
            current_accept = fragment.accept
        if node.maximum is not None:
            # Finite range: append (maximum - minimum) optional copies.  The
            # skip edge lives on the previous accept state, never on the
            # entry of a mandatory copy.
            for _ in range(node.maximum - node.minimum):
                fragment = self._node(node.child)
                if current_start is None:
                    current_start = self._new_state(node.position)
                    current_accept = current_start
                self._epsilon(current_accept, fragment.start)  # type: ignore[arg-type]
                self._epsilon(current_accept, fragment.accept)  # type: ignore[arg-type]
                current_accept = fragment.accept
            if current_start is None:
                # {0,0}: matches the empty string.
                return self._empty(node.position)
            return _Fragment(current_start, current_accept)  # type: ignore[arg-type]
        # Unbounded tail: mandatory minimum copies act as a prefix, then one
        # child fragment is reused as the loop body (never per-iteration
        # expansion).  A fresh loop entry owns the zero-iteration skip edge
        # only when no mandatory copy is present; otherwise the prefix is
        # joined to the loop entry by an epsilon edge.
        accept = self._new_state(node.position)
        loop_start = self._new_state(node.position)
        if current_start is None:
            self._epsilon(loop_start, accept)  # zero iterations at all
        else:
            self._epsilon(current_accept, loop_start)  # type: ignore[arg-type]
        fragment = self._node(node.child)
        self._epsilon(loop_start, fragment.start)
        # Zero extra iterations: the prefix (if any) already satisfied the
        # minimum, so skipping the tail body is legal here as well.
        self._epsilon(loop_start, accept)
        self._epsilon(fragment.accept, fragment.start)  # iterate once more
        self._epsilon(fragment.accept, accept)  # leave the loop
        return _Fragment(current_start if current_start is not None else loop_start, accept)


def _anchor_predicate(kind: str) -> Callable[[str], bool]:
    # Anchors are zero-width: represented as a marked consuming edge that
    # always fails, so only the parallel epsilon edge can traverse it.  The
    # simulation evaluates the real positional check during closure.
    return lambda ch: False


# ---------------------------------------------------------------------------
# State-set simulation
# ---------------------------------------------------------------------------


def _epsilon_closure(
    program: PatternProgram,
    seeds: frozenset[int],
    *,
    position: int,
    text: str,
) -> frozenset[int]:
    """Close over epsilon edges, honouring ``^``/``$`` zero-width anchors.

    Anchor constraints are attached to edges (not states) and must be
    (re)evaluated whenever the edge is traversed, including anchors nested
    inside alternations reached only after consuming characters.
    """
    seen: set[int] = set(seeds)
    stack = list(seeds)
    while stack:
        state = stack.pop()
        for target in program.epsilons[state]:
            if target in seen:
                continue
            anchor = _anchor_kind(program, state, target)
            if anchor is not None and not _anchor_holds(anchor, position, text):
                continue
            seen.add(target)
            stack.append(target)
    return frozenset(seen)


def _anchor_kind(program: PatternProgram, source: int, target: int) -> Optional[str]:
    for edge_target, _predicate, anchor in program.transitions[source]:
        if edge_target == target and anchor is not None:
            return anchor
    return None


def _anchor_holds(kind: str, position: int, text: str) -> bool:
    if kind == "start":
        # re.fullmatch anchors the whole match at position 0; ^ may also
        # match after a newline with MULTILINE, which is not enabled here.
        return position == 0
    # Python's $ (without MULTILINE) holds at end of string, or immediately
    # before a single trailing newline.  At position == len(text) it always
    # holds; the fullmatch requirement that every character be consumed
    # separately rejects patterns whose body cannot consume that newline, so
    # re.fullmatch("a$", "a\n") fails at the consumption step rather than here.
    if position == len(text):
        return True
    return position == len(text) - 1 and text[position] == "\n"

def simulate(program: PatternProgram, text: str) -> str:
    """Run the epsilon-NFA with state-set propagation.

    Work per character is bounded by ``states`` successor edges; total work
    is ``O(len(text) * states)`` and memory is ``O(states)``.

    A fullmatch requires two things: every input character was consumed along
    some path, and the accept state is reachable in the final closure.  A
    trailing ``$`` before a final newline reaches accept in the closure taken
    at position ``len(text) - 1``; the newline itself then has no consuming
    successor, reproducing Python's trailing-newline ``$`` semantics without
    letting an anchor eat a character.
    """
    current = _epsilon_closure(program, frozenset({program.start}), position=0, text=text)
    for index, ch in enumerate(text):
        nxt: set[int] = set()
        for state in current:
            for target, predicate, anchor in program.transitions[state]:
                # Anchor edges are zero-width and never consume a character.
                if anchor is not None:
                    continue
                if predicate(ch):
                    nxt.add(target)
        if not nxt:
            return NO_MATCH
        current = _epsilon_closure(program, frozenset(nxt), position=index + 1, text=text)
        if not current:
            return NO_MATCH
    return MATCH if program.accept in current else NO_MATCH


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compile_pattern(pattern: str, *, state_budget: int = DEFAULT_STATE_BUDGET) -> PatternProgram | PatternRejection:
    """Compile ``pattern`` into a bounded NFA or return a rejection.

    The result is never an exception and never involves :mod:`re`.  Callers
    must treat a :class:`PatternRejection` as *undecidable*, not as a match
    or a non-match.
    """
    if not isinstance(pattern, str):
        return PatternRejection(REASON_SYNTAX, "pattern must be a string", 0, 1, 1)
    if state_budget <= 0:
        return PatternRejection(REASON_BUDGET, "state budget must be a positive integer", 0, 1, 1)
    try:
        ast = _Parser(pattern).parse()
        compiler = _Compiler(state_budget)
        fragment = compiler.compile(ast)
    except _PatternError as exc:
        line = pattern.count("\n", 0, exc.position) + 1
        last_break = pattern.rfind("\n", 0, exc.position)
        column = exc.position + 1 if last_break < 0 else exc.position - last_break
        return PatternRejection(exc.reason, exc.message, exc.position, line, column)
    transitions = tuple(tuple(edges) for edges in compiler.transitions)
    epsilons = tuple(tuple(sorted(set(edges))) for edges in compiler.epsilons)
    return PatternProgram(
        pattern=pattern,
        states=len(compiler.transitions),
        budget=state_budget,
        transitions=transitions,
        epsilons=epsilons,
        start=fragment.start,
        accept=fragment.accept,
    )


def bounded_fullmatch(program: PatternProgram, text: str) -> str:
    """Convenience wrapper keeping the tri-state contract explicit."""
    return program.fullmatch(text)
