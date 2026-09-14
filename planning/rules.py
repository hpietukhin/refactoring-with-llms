"""Typed planning rules with an explicit raw rule declaration."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final, Literal

from planning.ast.relations import AstRelations

LONG_METHOD: Final = "Long Method"
LARGE_CLASS: Final = "Large Class"
LONG_PARAMETER_LIST: Final = "Long Parameter List"
DUPLICATED_CODE: Final = "Duplicated Code"
DIVERGENT_CHANGE: Final = "Divergent Change"
SHOTGUN_SURGERY: Final = "Shotgun Surgery"
FEATURE_ENVY: Final = "Feature Envy"
DATA_CLUMPS: Final = "Data Clumps"
PRIMITIVE_OBSESSION: Final = "Primitive Obsession"
SWITCH_STATEMENT: Final = "Switch Statement"
PARALLEL_INHERITANCE: Final = "Parallel Inheritance Hierarchies"
LAZY_CLASS: Final = "Lazy Class"
SPECULATIVE_GENERALITY: Final = "Speculative Generality"
TEMPORARY_FIELD: Final = "Temporary Field"
MESSAGE_CHAINS: Final = "Message Chains"
MIDDLE_MAN: Final = "Middle Man"
INAPPROPRIATE_INTIMACY: Final = "Inappropriate Intimacy"
ALTERNATIVE_CLASSES: Final = "Alternative Classes with Different Interfaces"
INCOMPLETE_LIBRARY_CLASS: Final = "Incomplete Library Class"
DATA_CLASS: Final = "Data Class"
REFUSED_BEQUEST: Final = "Refused Bequest"
COMMENTS: Final = "Comments"

COMPLEX_METHOD: Final = "Complex Method"
CONDITIONAL_COMPLEXITY: Final = "Conditional Complexity"
GOD_CLASS: Final = "God Class"
BAD_CLASS_CONTENT: Final = "Bad Class Content"
BAD_INHERITANCE: Final = "Bad Inheritance"
NEEDLESS_PART: Final = "Needless Part"
DUPLICATED_CONDITIONS: Final = "Duplicated Conditions"
PRINT_STATEMENTS: Final = "Print Statements"

COMPLEX_CLASS: Final = "Complex Class"
SPAGHETTI_CODE: Final = "Spaghetti Code"
CLASS_DATA_SHOULD_BE_PRIVATE: Final = "Class Data Should Be Private"
BRAIN_METHOD: Final = "Brain Method"
BRAIN_CLASS: Final = "Brain Class"
INTENSIVE_COUPLING: Final = "Intensive Coupling"
DISPERSED_COUPLING: Final = "Dispersed Coupling"

type SmellType = Literal[
    "Long Method",
    "Large Class",
    "Long Parameter List",
    "Duplicated Code",
    "Divergent Change",
    "Shotgun Surgery",
    "Feature Envy",
    "Data Clumps",
    "Primitive Obsession",
    "Switch Statement",
    "Parallel Inheritance Hierarchies",
    "Lazy Class",
    "Speculative Generality",
    "Temporary Field",
    "Message Chains",
    "Middle Man",
    "Inappropriate Intimacy",
    "Alternative Classes with Different Interfaces",
    "Incomplete Library Class",
    "Data Class",
    "Refused Bequest",
    "Comments",
    "Complex Method",
    "Conditional Complexity",
    "God Class",
    "Bad Class Content",
    "Bad Inheritance",
    "Needless Part",
    "Duplicated Conditions",
    "Print Statements",
    "Complex Class",
    "Spaghetti Code",
    "Class Data Should Be Private",
    "Brain Method",
    "Brain Class",
    "Intensive Coupling",
    "Dispersed Coupling",
]

HIGH_SEVERITY_TYPES: Final[frozenset[SmellType]] = frozenset(
    {
        LONG_METHOD,
        LARGE_CLASS,
        COMPLEX_METHOD,
        CONDITIONAL_COMPLEXITY,
        GOD_CLASS,
        COMPLEX_CLASS,
        SPAGHETTI_CODE,
        BRAIN_METHOD,
        BRAIN_CLASS,
    }
)


@dataclass(frozen=True, order=True, slots=True)
class PlanningSmell:
    """A smell instance represented in the planner state."""

    element_id: str
    line: int
    type: SmellType
    id: str
    severity: int

    @classmethod
    def predicted(
        cls,
        smell_type: SmellType,
        element_id: str,
    ) -> PlanningSmell:
        """Create a stable hypothetical smell introduced by a dependency rule."""
        payload = f"{element_id}\0{smell_type}".encode()
        return cls(
            element_id=element_id,
            line=0,
            type=smell_type,
            id=f"predicted:{hashlib.sha256(payload).hexdigest()}",
            severity=3 if smell_type in HIGH_SEVERITY_TYPES else 2,
        )


type State = tuple[PlanningSmell, ...]

@dataclass(frozen=True, slots=True)
class Rule:
    """Effects of resolving one smell."""

    target: SmellType
    positive: frozenset[SmellType] = frozenset()
    negative: frozenset[SmellType] = frozenset()

    def apply(
        self,
        state: State,
        target: PlanningSmell,
        relations: AstRelations,
    ) -> State:
        """Apply this rule to one smell and its direct AST dependencies."""
        if target not in state:
            raise ValueError(f"Rule target is not present: {target.id}")
        if target.type != self.target:
            raise ValueError(
                f"Rule for {self.target!r} cannot resolve {target.type!r}"
            )

        remaining = [
            smell
            for smell in state
            if smell != target
            and not (
                relations.related(smell.element_id, target.element_id)
                and smell.type in self.positive
            )
        ]
        present = {
            smell.type
            for smell in remaining
            if relations.related(smell.element_id, target.element_id)
        }
        for smell_type in sorted(self.negative):
            if smell_type not in present:
                remaining.append(
                    PlanningSmell.predicted(
                        smell_type,
                        target.element_id,
                    )
                )
                present.add(smell_type)
        return tuple(sorted(remaining))


rules: dict[SmellType, Rule] = {
    LONG_METHOD: Rule(
        target=LONG_METHOD,
        positive=frozenset({DUPLICATED_CODE, COMMENTS, FEATURE_ENVY, DIVERGENT_CHANGE, SWITCH_STATEMENT, LONG_PARAMETER_LIST, COMPLEX_METHOD, CONDITIONAL_COMPLEXITY, BRAIN_METHOD}),
        negative=frozenset({LONG_PARAMETER_LIST, MESSAGE_CHAINS}),
    ),
    LARGE_CLASS: Rule(
        target=LARGE_CLASS,
        positive=frozenset({DATA_CLUMPS, FEATURE_ENVY, INAPPROPRIATE_INTIMACY, DUPLICATED_CODE, BAD_CLASS_CONTENT, GOD_CLASS}),
        negative=frozenset({SHOTGUN_SURGERY, MESSAGE_CHAINS, DATA_CLASS}),
    ),
    LONG_PARAMETER_LIST: Rule(
        target=LONG_PARAMETER_LIST,
        positive=frozenset({DATA_CLUMPS, PRIMITIVE_OBSESSION, TEMPORARY_FIELD}),
        negative=frozenset({DATA_CLASS}),
    ),
    GOD_CLASS: Rule(
        target=GOD_CLASS,
        positive=frozenset({DATA_CLUMPS, FEATURE_ENVY, BAD_CLASS_CONTENT, INAPPROPRIATE_INTIMACY, DUPLICATED_CODE, LARGE_CLASS, BRAIN_CLASS}),
        negative=frozenset({SHOTGUN_SURGERY, MESSAGE_CHAINS, DATA_CLASS}),
    ),
    COMPLEX_METHOD: Rule(
        target=COMPLEX_METHOD,
        positive=frozenset({SWITCH_STATEMENT, FEATURE_ENVY, DUPLICATED_CODE, DIVERGENT_CHANGE, COMMENTS, LONG_PARAMETER_LIST, LONG_METHOD, CONDITIONAL_COMPLEXITY, BRAIN_METHOD}),
        negative=frozenset({LONG_PARAMETER_LIST, MESSAGE_CHAINS}),
    ),
    CONDITIONAL_COMPLEXITY: Rule(
        target=CONDITIONAL_COMPLEXITY,
        positive=frozenset({SWITCH_STATEMENT, DUPLICATED_CODE, COMMENTS, LONG_METHOD, COMPLEX_METHOD, BRAIN_METHOD}),
        negative=frozenset({LARGE_CLASS}),
    ),
    BRAIN_CLASS: Rule(
        target=BRAIN_CLASS,
        positive=frozenset({GOD_CLASS, LARGE_CLASS, DATA_CLUMPS, FEATURE_ENVY}),
        negative=frozenset({SHOTGUN_SURGERY, MESSAGE_CHAINS, DATA_CLASS}),
    ),
    BRAIN_METHOD: Rule(
        target=BRAIN_METHOD,
        positive=frozenset({LONG_METHOD, DUPLICATED_CODE, COMMENTS, FEATURE_ENVY}),
        negative=frozenset({LONG_PARAMETER_LIST, MESSAGE_CHAINS}),
    ),
    COMPLEX_CLASS: Rule(
        target=COMPLEX_CLASS,
        positive=frozenset({LARGE_CLASS, COMPLEX_METHOD, DUPLICATED_CODE}),
        negative=frozenset({SHOTGUN_SURGERY, MESSAGE_CHAINS}),
    ),
    SPAGHETTI_CODE: Rule(
        target=SPAGHETTI_CODE,
        positive=frozenset({LONG_METHOD, COMPLEX_METHOD, DUPLICATED_CODE, DIVERGENT_CHANGE}),
        negative=frozenset({LONG_PARAMETER_LIST, MESSAGE_CHAINS, SHOTGUN_SURGERY}),
    ),
    FEATURE_ENVY: Rule(
        target=FEATURE_ENVY,
        positive=frozenset({MESSAGE_CHAINS, INAPPROPRIATE_INTIMACY, LONG_METHOD, DATA_CLASS}),
        negative=frozenset({SHOTGUN_SURGERY, DIVERGENT_CHANGE}),
    ),
    DUPLICATED_CODE: Rule(
        target=DUPLICATED_CODE,
        positive=frozenset({LONG_METHOD, LARGE_CLASS, DIVERGENT_CHANGE, PARALLEL_INHERITANCE}),
        negative=frozenset({LONG_PARAMETER_LIST, SHOTGUN_SURGERY}),
    ),
    DIVERGENT_CHANGE: Rule(
        target=DIVERGENT_CHANGE,
        positive=frozenset({SHOTGUN_SURGERY, LARGE_CLASS, FEATURE_ENVY}),
        negative=frozenset({SHOTGUN_SURGERY}),
    ),
    SHOTGUN_SURGERY: Rule(
        target=SHOTGUN_SURGERY,
        positive=frozenset({DIVERGENT_CHANGE, INAPPROPRIATE_INTIMACY, FEATURE_ENVY}),
        negative=frozenset({LARGE_CLASS}),
    ),
    SWITCH_STATEMENT: Rule(
        target=SWITCH_STATEMENT,
        positive=frozenset({DUPLICATED_CODE, FEATURE_ENVY, DIVERGENT_CHANGE}),
        negative=frozenset({LARGE_CLASS, PARALLEL_INHERITANCE}),
    ),
    COMMENTS: Rule(
        target=COMMENTS,
        positive=frozenset({LONG_METHOD, NEEDLESS_PART}),
        negative=frozenset(),
    ),
    DISPERSED_COUPLING: Rule(
        target=DISPERSED_COUPLING,
        positive=frozenset({SHOTGUN_SURGERY, FEATURE_ENVY, INAPPROPRIATE_INTIMACY}),
        negative=frozenset({LARGE_CLASS, MESSAGE_CHAINS}),
    ),
    INTENSIVE_COUPLING: Rule(
        target=INTENSIVE_COUPLING,
        positive=frozenset({FEATURE_ENVY, MESSAGE_CHAINS, INAPPROPRIATE_INTIMACY}),
        negative=frozenset({SHOTGUN_SURGERY, DIVERGENT_CHANGE}),
    ),
    REFUSED_BEQUEST: Rule(
        target=REFUSED_BEQUEST,
        positive=frozenset({INAPPROPRIATE_INTIMACY, ALTERNATIVE_CLASSES}),
        negative=frozenset({MESSAGE_CHAINS, FEATURE_ENVY}),
    ),
    PARALLEL_INHERITANCE: Rule(
        target=PARALLEL_INHERITANCE,
        positive=frozenset({SHOTGUN_SURGERY, DUPLICATED_CODE}),
        negative=frozenset({LARGE_CLASS}),
    ),
    ALTERNATIVE_CLASSES: Rule(
        target=ALTERNATIVE_CLASSES,
        positive=frozenset({DUPLICATED_CODE}),
        negative=frozenset(),
    ),
    BAD_INHERITANCE: Rule(
        target=BAD_INHERITANCE,
        positive=frozenset({REFUSED_BEQUEST, INAPPROPRIATE_INTIMACY}),
        negative=frozenset({MESSAGE_CHAINS, FEATURE_ENVY, DATA_CLASS}),
    ),
    SPECULATIVE_GENERALITY: Rule(
        target=SPECULATIVE_GENERALITY,
        positive=frozenset({LAZY_CLASS, NEEDLESS_PART, COMMENTS}),
        negative=frozenset(),
    ),
    LAZY_CLASS: Rule(
        target=LAZY_CLASS,
        positive=frozenset({NEEDLESS_PART}),
        negative=frozenset(),
    ),
    NEEDLESS_PART: Rule(
        target=NEEDLESS_PART,
        positive=frozenset({COMMENTS, LAZY_CLASS, DUPLICATED_CODE}),
        negative=frozenset(),
    ),
    PRINT_STATEMENTS: Rule(
        target=PRINT_STATEMENTS,
        positive=frozenset({NEEDLESS_PART}),
        negative=frozenset({DATA_CLASS, LAZY_CLASS}),
    ),
    DATA_CLUMPS: Rule(
        target=DATA_CLUMPS,
        positive=frozenset({LONG_PARAMETER_LIST, PRIMITIVE_OBSESSION, TEMPORARY_FIELD}),
        negative=frozenset({DATA_CLASS}),
    ),
    TEMPORARY_FIELD: Rule(
        target=TEMPORARY_FIELD,
        positive=frozenset({DATA_CLUMPS, LARGE_CLASS}),
        negative=frozenset({DATA_CLASS}),
    ),
    PRIMITIVE_OBSESSION: Rule(
        target=PRIMITIVE_OBSESSION,
        positive=frozenset({DATA_CLUMPS, LONG_PARAMETER_LIST}),
        negative=frozenset({DATA_CLASS, LAZY_CLASS}),
    ),
    CLASS_DATA_SHOULD_BE_PRIVATE: Rule(
        target=CLASS_DATA_SHOULD_BE_PRIVATE,
        positive=frozenset({PRIMITIVE_OBSESSION, DATA_CLUMPS}),
        negative=frozenset({DATA_CLASS}),
    ),
    DATA_CLASS: Rule(
        target=DATA_CLASS,
        positive=frozenset({FEATURE_ENVY, BAD_CLASS_CONTENT}),
        negative=frozenset({INAPPROPRIATE_INTIMACY, MESSAGE_CHAINS}),
    ),
    BAD_CLASS_CONTENT: Rule(
        target=BAD_CLASS_CONTENT,
        positive=frozenset({DATA_CLASS, FEATURE_ENVY, LAZY_CLASS}),
        negative=frozenset({SHOTGUN_SURGERY}),
    ),
    INCOMPLETE_LIBRARY_CLASS: Rule(
        target=INCOMPLETE_LIBRARY_CLASS,
        positive=frozenset({DUPLICATED_CODE, FEATURE_ENVY}),
        negative=frozenset({DATA_CLASS}),
    ),
    INAPPROPRIATE_INTIMACY: Rule(
        target=INAPPROPRIATE_INTIMACY,
        positive=frozenset({FEATURE_ENVY, MESSAGE_CHAINS, DIVERGENT_CHANGE}),
        negative=frozenset({MESSAGE_CHAINS, SHOTGUN_SURGERY}),
    ),
    MIDDLE_MAN: Rule(
        target=MIDDLE_MAN,
        positive=frozenset({INAPPROPRIATE_INTIMACY}),
        negative=frozenset({MESSAGE_CHAINS, INAPPROPRIATE_INTIMACY}),
    ),
    MESSAGE_CHAINS: Rule(
        target=MESSAGE_CHAINS,
        positive=frozenset({MIDDLE_MAN, INAPPROPRIATE_INTIMACY}),
        negative=frozenset({MIDDLE_MAN, DATA_CLASS}),
    ),
    DUPLICATED_CONDITIONS: Rule(
        target=DUPLICATED_CONDITIONS,
        positive=frozenset({DIVERGENT_CHANGE, SHOTGUN_SURGERY}),
        negative=frozenset({LARGE_CLASS, BAD_INHERITANCE}),
    ),
}



__all__ = [
    "rules",
    "SmellType",
    "PlanningSmell",
    "State",
    "Rule",
    "HIGH_SEVERITY_TYPES",
    "LONG_METHOD",
    "LARGE_CLASS",
    "LONG_PARAMETER_LIST",
    "DUPLICATED_CODE",
    "DIVERGENT_CHANGE",
    "SHOTGUN_SURGERY",
    "FEATURE_ENVY",
    "DATA_CLUMPS",
    "PRIMITIVE_OBSESSION",
    "SWITCH_STATEMENT",
    "PARALLEL_INHERITANCE",
    "LAZY_CLASS",
    "SPECULATIVE_GENERALITY",
    "TEMPORARY_FIELD",
    "MESSAGE_CHAINS",
    "MIDDLE_MAN",
    "INAPPROPRIATE_INTIMACY",
    "ALTERNATIVE_CLASSES",
    "INCOMPLETE_LIBRARY_CLASS",
    "DATA_CLASS",
    "REFUSED_BEQUEST",
    "COMMENTS",
    "COMPLEX_METHOD",
    "CONDITIONAL_COMPLEXITY",
    "GOD_CLASS",
    "BAD_CLASS_CONTENT",
    "BAD_INHERITANCE",
    "NEEDLESS_PART",
    "DUPLICATED_CONDITIONS",
    "PRINT_STATEMENTS",
    "COMPLEX_CLASS",
    "SPAGHETTI_CODE",
    "CLASS_DATA_SHOULD_BE_PRIVATE",
    "BRAIN_METHOD",
    "BRAIN_CLASS",
    "INTENSIVE_COUPLING",
    "DISPERSED_COUPLING",
]
