"""Regression: the M2 verifier must always evaluate a claim against the evidence pack it is given, even when Python
reuses the memory address of a freed pack (the earlier cache was keyed on id(pack) alone)."""
import gc
from dataclasses import replace

from intelligence import investigation as INV
from intelligence import verifier as VF
from intelligence.claims import Claim, ClaimType as T, Outcome, ValueUse
from intelligence.evidence import EvidencePack
from webapp import intel_adapter as IA
from webapp import synthetic_analysis as SY

SYN = IA.extract(SY.synthetic_saved())
KEY = "bank.fy2000.total.vintage.2008.loans"
EID = next(r.evidence_id for r in SYN.records if r.metric_key == KEY)


def altered(**values):
    return EvidencePack(SYN.header, tuple(replace(r, value=values[r.metric_key.replace(".", "_")])
                                          if r.metric_key.replace(".", "_") in values else r for r in SYN.records))


def test_index_belongs_to_the_pack_when_addresses_collide(monkeypatch):
    """Deterministic reproduction: every pack reports the same address (as when Python reuses a freed pack's
    memory). The old id()-keyed cache served the first pack's index to the second; the fix compares identity."""
    monkeypatch.setattr(VF, "id", lambda obj: 4242, raising=False)
    a = altered(bank_fy2000_total_vintage_2008_loans=11)
    assert VF.pack_index(a)[EID].value == 11
    b = altered(bank_fy2000_total_vintage_2008_loans=7)
    assert VF.pack_index(b)[EID].value == 7
    assert VF.pack_index(a)[EID].value == 11


def test_index_belongs_to_the_pack_even_when_its_address_is_reused():
    for _ in range(200):
        a = altered(bank_fy2000_total_vintage_2008_loans=11)
        VF.pack_index(a)
        old = id(a)
        del a
        gc.collect()
        b = altered(bank_fy2000_total_vintage_2008_loans=7)
        # before the fix, id(b) == old in almost every round and the freed pack's index (11) was returned
        assert VF.pack_index(b)[EID].value == 7 and VF.pack_index(b)[EID] is next(
            r for r in b.records if r.evidence_id == EID)
        assert old is not None


def test_claims_are_verified_against_the_pack_given():
    keys = ("bank.full.total.vintage.2008.cum_def", "benchmark.total.vintage.2008.cum_def")
    for _ in range(50):
        for loans, expected in ((7, Outcome.REJECTED), (10, Outcome.ACCEPTED)):
            pk = altered(bank_fy2000_total_vintage_2008_loans=loans)
            recs = [INV._resolve(pk, k) for k in keys]
            c = Claim("CL-c", T.COMPARISON, pk.header["source_id"], tuple(pk.header["bank"]), pk.header["as_of"],
                      "Loans first disbursed in 2008 have a % Cum. Defaulted of 46.67% against the benchmark's 21.00%.",
                      tuple(r.evidence_id for r in recs), tuple(ValueUse(r.evidence_id, r.value, r.unit) for r in recs),
                      None, VF.effective_status(recs), VF.effective_methodology(recs),
                      tuple(sorted(VF.required_caveats(pk, recs), key=lambda x: x.value)))
            assert VF.verify_claim(pk, c).outcome is expected
            del pk, recs, c
            gc.collect()
