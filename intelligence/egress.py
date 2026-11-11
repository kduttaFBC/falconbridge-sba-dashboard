"""Egress boundary for any future outbound AI request (M1: policy and export only; nothing is sent anywhere).

Deny by default: the default policy approves no classification, so an export contains no evidence until a person
explicitly approves classes (and optionally metrics) in writing. CLIENT_CONFIDENTIAL and INTERNAL can never be
approved. An export carries only neutral fields (no payload paths, source identifiers, owners or machine paths) and
is scanned for forbidden markers before it is returned.
"""
from dataclasses import dataclass, field

from .evidence import Classification, EvidencePack, Kind, canonical_json, validate_pack

NEVER_EXPORTABLE = frozenset({Classification.CLIENT_CONFIDENTIAL, Classification.INTERNAL})
# Assembled at runtime: deploy/check_repo_contents.py refuses any committed file containing a literal local home path.
LOCAL_HOME = "/" + "Users" + "/"
FORBIDDEN_MARKERS = (LOCAL_HOME, "C:\\", "Y:\\", "\\\\", ".xlsx", ".xlsb", ".xlsm", ".csv", "source_data",
                     "reference/", "protected_config", "app_data", "FOIA!", "=SUM(", "=IFERROR(")
EXPORT_FIELDS = ("evidence_id", "kind", "metric_key", "label", "value", "unit", "validation_status", "methodology",
                 "numerically_verified", "derived_from", "limitations")


class EgressError(RuntimeError):
    pass


@dataclass(frozen=True)
class EgressPolicy:
    approved_classifications: frozenset = field(default_factory=frozenset)
    approved_metric_prefixes: tuple = ()      # empty = every metric of an approved class
    approved_by: str = ""
    approved_on: str = ""
    purpose: str = ""

    def __post_init__(self):
        bad = set(self.approved_classifications) & NEVER_EXPORTABLE
        if bad:
            raise EgressError(f"classifications that can never leave the machine: {sorted(c.value for c in bad)}")
        if self.approved_classifications and not (self.approved_by and self.approved_on):
            raise EgressError("an egress approval must name who approved it and when")

    def allows(self, record):
        if record.classification not in self.approved_classifications:
            return False
        return not self.approved_metric_prefixes or record.metric_key.startswith(self.approved_metric_prefixes)


DEFAULT_POLICY = EgressPolicy()          # approves nothing


def preview(pack, policy=DEFAULT_POLICY):
    """What an export under `policy` would contain, by classification (for the analyst pre-flight in M3)."""
    out = {}
    for r in pack.records:
        k = r.classification.value
        out.setdefault(k, {"included": 0, "excluded": 0})
        out[k]["included" if policy.allows(r) else "excluded"] += 1
    return {"policy_approved": sorted(c.value for c in policy.approved_classifications), "by_classification": out}


def export(pack, policy=DEFAULT_POLICY, extra_forbidden=()):
    """Allow-listed, neutral view of a validated pack. Hypotheses are never exported (they are AI or analyst output,
    not input). Raises EgressError if any forbidden marker would leave the machine."""
    if not isinstance(pack, EvidencePack):
        raise EgressError("export takes an EvidencePack")
    validate_pack(pack)
    h = pack.header
    approved = policy.approved_classifications
    doc = {
        "pack_fingerprint": pack.fingerprint(),
        "adapter_version": h["adapter_version"],
        "calculation_version": h.get("calculation_version_short", ""),
        "as_of": h["as_of"],
        "bank": list(h["bank"]) if Classification.PUBLIC_IDENTITY in approved else None,
        "policy": {"approved": sorted(c.value for c in approved), "metric_prefixes": list(policy.approved_metric_prefixes),
                   "approved_by": policy.approved_by, "approved_on": policy.approved_on, "purpose": policy.purpose},
        "records": [{k: d[k] for k in EXPORT_FIELDS} for d in
                    (r.to_dict() for r in pack.records if r.kind is not Kind.HYPOTHESIS and policy.allows(r))],
    }
    text = canonical_json(doc)
    internal = [h.get("source_id"), h.get("job_id"), h.get("owner"), h.get("release_folder")]
    internal = [str(m) for m in internal if m and len(str(m)) >= 8]     # short values (e.g. owner "local") are words
    for marker in tuple(FORBIDDEN_MARKERS) + tuple(internal) + tuple(extra_forbidden):
        if marker and marker in text:
            raise EgressError(f"export refused: forbidden content would leave the machine ({marker[:40]!r})")
    return doc
