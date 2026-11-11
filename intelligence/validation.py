"""Validation records: verifiable proof that specific values of one exact source were independently validated (M2).

A validation record binds independently recomputed values to ONE source (system + source id + payload SHA-256).
Its identifier is a hash of its own content, so any edit is detectable. Bank identity, reporting period or
calculation version alone are never evidence of validation: without an applicable record a value is not
INDEPENDENTLY_VERIFIED. A validation record says nothing about methodology approval.
"""
import math
from dataclasses import asdict, dataclass, replace

from .evidence import EvidenceError, sha256_of


@dataclass(frozen=True)
class ValidationRecord:
    record_id: str
    source_system: str
    source_id: str
    payload_sha256: str
    method: str                       # how the values were recomputed (independent of the application's engine)
    performed_on: str
    performed_by: str
    reference: str                    # where the procedure and results are documented
    values: tuple                     # ((metric_key, independently recomputed value), ...)
    abs_tolerance: float = 1e-9
    rel_tolerance: float = 1e-12
    input_fingerprints: tuple = ()    # ((input name, sha256), ...) of the data the recomputation read
    as_of: str = ""                   # reporting period the values describe (must equal the source's)
    population: str = ""              # the loan population that was recomputed (selection rule and counts)
    methodology_version: str = ""     # version of the restated definitions the recomputation used

    def content(self):
        d = asdict(self)
        d.pop("record_id")
        return d


def make_validation_record(source_system, source_id, payload_sha256, method, performed_on, performed_by, reference,
                           values, abs_tolerance=1e-9, rel_tolerance=1e-12, input_fingerprints=(), as_of="",
                           population="", methodology_version=""):
    vals = tuple(sorted((str(k), float(v)) for k, v in dict(values).items()))
    tmp = ValidationRecord("", source_system, source_id, payload_sha256, method, performed_on, performed_by, reference,
                           vals, abs_tolerance, rel_tolerance, tuple(sorted(tuple(x) for x in input_fingerprints)),
                           as_of, population, methodology_version)
    return check_integrity(replace(tmp, record_id="VR-" + sha256_of(tmp.content())[:24]))


def check_integrity(rec):
    """Raise EvidenceError if the record was altered or is incomplete."""
    if not isinstance(rec, ValidationRecord):
        raise EvidenceError("not a ValidationRecord")
    for f in ("source_system", "source_id", "payload_sha256", "method", "performed_on", "performed_by", "reference"):
        if not getattr(rec, f):
            raise EvidenceError(f"validation record {rec.record_id or '?'} is missing {f}")
    if not rec.values:
        raise EvidenceError(f"validation record {rec.record_id} validates no values")
    if any(not (isinstance(v, (int, float)) and math.isfinite(v)) for _, v in rec.values):
        raise EvidenceError(f"validation record {rec.record_id} holds a non-finite value")
    normalised = replace(rec, values=tuple(map(tuple, rec.values)),
                         input_fingerprints=tuple(map(tuple, rec.input_fingerprints)))
    expected = "VR-" + sha256_of(normalised.content())[:24]
    if rec.record_id != expected:
        raise EvidenceError(f"validation record {rec.record_id} failed its integrity check (content was altered)")
    return rec


def applies_to(rec, source_system, source_id, payload_sha256, as_of=None):
    """Why the record does not apply to this exact source, or '' if it does."""
    if rec.source_system != source_system:
        return f"different system ({rec.source_system})"
    if rec.source_id != source_id:
        return f"validates Saved Analysis {rec.source_id}, not {source_id}"
    if rec.payload_sha256 != payload_sha256:
        return "validates a different payload (the Saved Analysis content differs from what was validated)"
    if rec.as_of and as_of and rec.as_of != as_of:
        return f"validates reporting period {rec.as_of}, not {as_of}"
    return ""


def to_json(rec):
    return {"type": "validation_record", **asdict(rec)}


def from_json(d):
    """Rebuild a stored record; the integrity check runs, so an edited file is refused."""
    d = dict(d)
    d.pop("type", None)
    d["values"] = tuple(tuple(x) for x in d.get("values", ()))
    d["input_fingerprints"] = tuple(tuple(x) for x in d.get("input_fingerprints", ()))
    return check_integrity(ValidationRecord(**d))


def agrees(rec, validated, value):
    return abs(value - validated) <= max(rec.abs_tolerance, rec.rel_tolerance * abs(validated))
