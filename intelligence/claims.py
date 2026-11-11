"""Claim schema (M2): a statement about one evidence pack, written by an analyst or (later) an AI model, that the
deterministic verifier (verifier.py) must check before anyone relies on it.

A claim cites evidence records by identifier, declares every financial value it uses (each tied to a cited record),
optionally declares one calculation from an approved set of operations, states the reporting period and the
validation and methodology status it claims, and lists its caveats. The verifier's result travels with it.
"""
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional

from .evidence import Methodology, ValidationStatus


class ClaimType(str, Enum):
    OBSERVATION = "OBSERVATION"      # states cited values
    COMPARISON = "COMPARISON"        # compares cited values (needs the compared evidence and, for differences, a calc)
    CALCULATION = "CALCULATION"      # states the result of an approved operation on cited values
    LIMITATION = "LIMITATION"        # states a data limitation
    HYPOTHESIS = "HYPOTHESIS"        # a possible explanation; always UNVERIFIED, hedged, citing evidence
    QUESTION = "QUESTION"            # a question for analysts or the bank


class Caveat(str, Enum):
    PRELIMINARY_BENCHMARK = "PRELIMINARY_BENCHMARK"    # cites the preliminary industry-matched benchmark
    NOT_MIX_ADJUSTED = "NOT_MIX_ADJUSTED"              # bank vs benchmark not adjusted for vintage/size/term/industry
    SMALL_SAMPLE = "SMALL_SAMPLE"                      # cites a small sample or limited-data group
    PARTIAL_YEAR = "PARTIAL_YEAR"                      # cites the partial reporting year
    ORIGINAL_AMOUNTS = "ORIGINAL_AMOUNTS"              # amounts are original approvals, not balances
    PENDING_CLIENT_APPROVAL = "PENDING_CLIENT_APPROVAL"  # cites an application-defined measure
    UNVERIFIED = "UNVERIFIED"                          # cites unverified evidence


class Operation(str, Enum):
    """The only arithmetic a claim may perform on evidence (anything else is rejected)."""
    DIFFERENCE = "difference"        # operands[0] - operands[1]
    SUM = "sum"                      # operands[0] + operands[1] + ...
    RATIO = "ratio"                  # operands[0] / operands[1]


class Outcome(str, Enum):
    ACCEPTED = "ACCEPTED"
    NEEDS_ANALYST_REVIEW = "NEEDS_ANALYST_REVIEW"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class ValueUse:
    evidence_id: str
    value: float
    unit: str


@dataclass(frozen=True)
class Calculation:
    operation: str
    operands: tuple                  # evidence ids, in order
    result: float
    unit: str
    decimals: Optional[int] = None   # declared precision of `result` (decimal places); None = as written (m2-1.1)


@dataclass(frozen=True)
class Claim:
    claim_id: str
    claim_type: ClaimType
    source_id: str                   # the Saved Analysis the claim is about
    bank: tuple                      # (bank_name, bank_state, fdic_cert)
    as_of: str                       # reporting period
    text: str
    evidence_refs: tuple = ()
    values: tuple = ()               # ValueUse, one per financial value in the text
    calculation: Optional[Calculation] = None
    validation_status: ValidationStatus = ValidationStatus.UNVERIFIED
    methodology: Methodology = Methodology.NOT_APPLICABLE
    caveats: tuple = ()              # Caveat values
    exportable: bool = False         # intended to leave the machine (subject to the egress policy)

    def to_dict(self):
        d = asdict(self)
        for k in ("claim_type", "validation_status", "methodology"):
            d[k] = getattr(self, k).value
        d["caveats"] = [Caveat(c).value for c in self.caveats]
        return d


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str                    # "REJECT" or "REVIEW"
    message: str


@dataclass(frozen=True)
class Verification:
    claim_id: str
    outcome: Outcome
    findings: tuple = ()
    effective_validation_status: ValidationStatus = ValidationStatus.UNVERIFIED
    effective_methodology: Methodology = Methodology.NOT_APPLICABLE
    required_caveats: tuple = ()
    explanation: str = ""

    def to_dict(self):
        return {"claim_id": self.claim_id, "outcome": self.outcome.value,
                "findings": [asdict(f) for f in self.findings],
                "effective_validation_status": self.effective_validation_status.value,
                "effective_methodology": self.effective_methodology.value,
                "required_caveats": [Caveat(c).value for c in self.required_caveats],
                "explanation": self.explanation}


@dataclass(frozen=True)
class VerifiedClaim:
    claim: Claim
    verification: Verification = field(default=None)
