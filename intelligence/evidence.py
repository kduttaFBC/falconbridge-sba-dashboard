"""Structured financial evidence records (M1).

An evidence record states one value that a validated application already produced, with where it came from, which
calculation version produced it, how far it has been validated and whether its methodology is approved. Records are
immutable; their identifiers are stable (derived from the source analysis and the metric, never from position or time).

Kinds keep facts, derived interpretations and hypotheses apart:
  FACT        a value read unchanged from a frozen source (e.g. a Saved Analysis payload)
  DERIVED     a deterministic restatement or rule-based flag over FACTs (cites the records or rule it rests on)
  HYPOTHESIS  a proposed interpretation (future AI / analyst); always UNVERIFIED and must cite evidence
"""
import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from enum import Enum


class Kind(str, Enum):
    FACT = "FACT"
    DERIVED = "DERIVED"
    HYPOTHESIS = "HYPOTHESIS"


class ValidationStatus(str, Enum):
    INDEPENDENTLY_VERIFIED = "INDEPENDENTLY_VERIFIED"      # recomputed independently from source data, within scope
    RECONCILED_TO_TEMPLATE = "RECONCILED_TO_TEMPLATE"      # reproduces FalconBridge's template; not independently redone
    PRELIMINARY_METHODOLOGY = "PRELIMINARY_METHODOLOGY"    # numerically checked; methodology explicitly preliminary
    PENDING_CLIENT_APPROVAL = "PENDING_CLIENT_APPROVAL"    # application-defined measure awaiting FalconBridge approval
    UNVERIFIED = "UNVERIFIED"                              # no sufficient validation evidence (or a hypothesis)


class Methodology(str, Enum):
    CLIENT_APPROVED = "CLIENT_APPROVED"                        # written FalconBridge approval on record (none yet)
    CLIENT_DIRECTED_PRELIMINARY = "CLIENT_DIRECTED_PRELIMINARY"  # FalconBridge directed its use, subject to confirmation
    UNCONFIRMED_WORKBOOK_METHOD = "UNCONFIRMED_WORKBOOK_METHOD"  # FalconBridge's workbook definition, not confirmed
    APPLICATION_RULE = "APPLICATION_RULE"                      # defined by this application, not by FalconBridge
    NOT_APPLICABLE = "NOT_APPLICABLE"


class Classification(str, Enum):
    """Confidentiality class; decides what may ever leave the machine (see egress.py)."""
    PUBLIC_IDENTITY = "PUBLIC_IDENTITY"                            # lender identity as published in SBA FOIA
    PUBLIC_FOIA_AGGREGATE = "PUBLIC_FOIA_AGGREGATE"                # aggregates of public FOIA data, no client input
    FOIA_AGGREGATE_CLIENT_CORRECTED = "FOIA_AGGREGATE_CLIENT_CORRECTED"  # depends on FalconBridge's data corrections
    CLIENT_METHOD_DERIVED = "CLIENT_METHOD_DERIVED"                # computed with FalconBridge's methodology
    APPLICATION_DERIVED = "APPLICATION_DERIVED"                    # application rules / flags over the above
    CLIENT_CONFIDENTIAL = "CLIENT_CONFIDENTIAL"                    # FalconBridge text, labels, rules, workbook content
    INTERNAL = "INTERNAL"                                          # machine paths, owners, job ids


UNITS = {"count", "usd", "ratio", "months", "year", "flag", "text"}
ADAPTER_VERSION_KEY = "adapter_version"


class EvidenceError(ValueError):
    """Missing, inconsistent or invalid evidence. Raised instead of guessing or recomputing."""


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def sha256_of(obj):
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def make_id(source_id, kind, metric_key):
    """Stable identifier: a function of the source analysis, the kind and the metric key only."""
    if not (source_id and metric_key):
        raise EvidenceError("evidence id needs a source id and a metric key")
    return "EV-" + hashlib.sha256(f"{source_id}|{Kind(kind).value}|{metric_key}".encode()).hexdigest()[:24]


@dataclass(frozen=True)
class Source:
    system: str                 # e.g. "foia-saved-analysis"
    source_id: str              # e.g. the Saved Analysis id
    payload_sha256: str         # fingerprint of the frozen source payload the value was read from
    payload_path: str = ""      # JSON pointer inside the source payload ("" for DERIVED / HYPOTHESIS)
    recorded_at: str = ""       # when the source was frozen (Saved Analysis 'created')


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    kind: Kind
    metric_key: str             # canonical, e.g. "bank.full.total.vintage.2007.cum_def"
    label: str                  # neutral description written by this application (no client text)
    value: object
    unit: str
    as_of: str                  # reporting period (SBA FOIA AsOfDate)
    bank: tuple                 # (bank_name, bank_state, fdic_cert)
    calculation_version: str
    validation_status: ValidationStatus
    methodology: Methodology
    classification: Classification
    source: Source
    numeric_basis: str = ""     # what checked the number (e.g. "validation record VR-...")
    numerically_verified: bool = False   # True only if an applicable validation record confirmed this exact value
    definition_ref: str = ""    # application definition reference (e.g. "PerformanceSummary!S" column definition)
    rule: str = ""              # DERIVED: the deterministic rule applied
    derived_from: tuple = ()
    limitations: tuple = ()

    def to_dict(self):
        d = asdict(self)
        for k in ("kind", "validation_status", "methodology", "classification"):
            d[k] = getattr(self, k).value
        return d


def validate_record(r):
    """Raise EvidenceError if the record is malformed or claims more than its kind allows."""
    try:
        kind, vs, meth, cls = Kind(r.kind), ValidationStatus(r.validation_status), Methodology(r.methodology), \
            Classification(r.classification)
    except ValueError as e:
        raise EvidenceError(f"{getattr(r, 'evidence_id', '?')}: invalid enum value: {e}") from None
    if r.evidence_id != make_id(r.source.source_id, kind, r.metric_key):
        raise EvidenceError(f"{r.evidence_id}: identifier does not match its source and metric")
    if r.unit not in UNITS:
        raise EvidenceError(f"{r.evidence_id}: unknown unit {r.unit!r}")
    if isinstance(r.value, bool):
        pass
    elif isinstance(r.value, (int, float)):
        if not math.isfinite(r.value):
            raise EvidenceError(f"{r.evidence_id}: non-finite value")
    elif not isinstance(r.value, str):
        raise EvidenceError(f"{r.evidence_id}: value must be a number, flag or short text")
    if not (r.as_of and r.calculation_version and r.source.payload_sha256 and len(r.bank) == 3):
        raise EvidenceError(f"{r.evidence_id}: missing period, bank, calculation version or source fingerprint")
    if cls in (Classification.CLIENT_CONFIDENTIAL, Classification.INTERNAL):
        raise EvidenceError(f"{r.evidence_id}: confidential or internal content is never stored as evidence")
    if kind is Kind.FACT:
        if not r.source.payload_path or r.derived_from or r.rule:
            raise EvidenceError(f"{r.evidence_id}: a FACT needs a payload path and no derivation")
    elif kind is Kind.DERIVED:
        if not r.rule:
            raise EvidenceError(f"{r.evidence_id}: a DERIVED record must name its rule")
        if vs is ValidationStatus.INDEPENDENTLY_VERIFIED:
            raise EvidenceError(f"{r.evidence_id}: only FACTs can be INDEPENDENTLY_VERIFIED")
    else:  # HYPOTHESIS
        if vs is not ValidationStatus.UNVERIFIED or r.numerically_verified:
            raise EvidenceError(f"{r.evidence_id}: a HYPOTHESIS is always UNVERIFIED")
        if not r.derived_from:
            raise EvidenceError(f"{r.evidence_id}: a HYPOTHESIS must cite the evidence it rests on")
    if vs is ValidationStatus.INDEPENDENTLY_VERIFIED and not r.numerically_verified:
        raise EvidenceError(f"{r.evidence_id}: INDEPENDENTLY_VERIFIED requires a validation record for this value")
    if vs is ValidationStatus.PRELIMINARY_METHODOLOGY and meth is Methodology.CLIENT_APPROVED:
        raise EvidenceError(f"{r.evidence_id}: preliminary methodology cannot be client-approved")
    return r


@dataclass(frozen=True)
class EvidencePack:
    header: dict
    records: tuple = field(default_factory=tuple)

    def ids(self):
        return [r.evidence_id for r in self.records]

    def get(self, evidence_id):
        for r in self.records:
            if r.evidence_id == evidence_id:
                return r
        raise KeyError(evidence_id)

    def by_key(self, metric_key, kind=Kind.FACT):
        return self.get(make_id(self.header["source_id"], kind, metric_key))

    def to_dict(self):
        return {"header": self.header, "records": [r.to_dict() for r in self.records]}

    def fingerprint(self):
        return sha256_of(self.to_dict())


def validate_pack(pack):
    """Every record valid, identifiers unique, every citation resolvable within the pack."""
    for k in ("source_id", "payload_sha256", "calculation_version", ADAPTER_VERSION_KEY, "as_of", "bank"):
        if not pack.header.get(k):
            raise EvidenceError(f"pack header missing {k}")
    ids = pack.ids()
    if len(ids) != len(set(ids)):
        raise EvidenceError("duplicate evidence identifiers")
    known = set(ids)
    for r in pack.records:
        validate_record(r)
        if r.source.payload_sha256 != pack.header["payload_sha256"]:
            raise EvidenceError(f"{r.evidence_id}: record and pack come from different source payloads")
        missing = [x for x in r.derived_from if x not in known]
        if missing:
            raise EvidenceError(f"{r.evidence_id}: cites unknown evidence {missing[:3]}")
    return pack


def make_hypothesis(pack, metric_key, label, statement, cites, limitations=()):
    """A hypothesis record (for future AI / analyst use): always UNVERIFIED, must cite existing evidence, never
    carries a number of its own. Returned separately; the pack itself is never modified."""
    if not cites or any(c not in set(pack.ids()) for c in cites):
        raise EvidenceError("a hypothesis must cite evidence that exists in the pack")
    h = pack.header
    r = EvidenceRecord(
        evidence_id=make_id(h["source_id"], Kind.HYPOTHESIS, metric_key), kind=Kind.HYPOTHESIS,
        metric_key=metric_key, label=label, value=str(statement), unit="text", as_of=h["as_of"],
        bank=tuple(h["bank"]), calculation_version=h["calculation_version"],
        validation_status=ValidationStatus.UNVERIFIED, methodology=Methodology.NOT_APPLICABLE,
        classification=Classification.APPLICATION_DERIVED,
        source=Source(h["system"], h["source_id"], h["payload_sha256"]), rule="hypothesis",
        derived_from=tuple(cites), limitations=tuple(limitations))
    return validate_record(r)
