"""Deterministic claim verifier (M2). Offline, no model calls, no new financial calculation.

A claim is ACCEPTED only if every check passes; NEEDS_ANALYST_REVIEW if it is numerically supported but makes an
interpretation that FalconBridge's methodology does not (yet) allow the system to accept on its own; REJECTED with
specific findings otherwise. Language checks are evidence-based, not a blanket word ban: a comparative or evaluative
term is accepted only when the cited evidence supports its direction, the population is not a small sample, and the
methodology of every cited record is client-approved.
"""
import math
from dataclasses import replace
from decimal import Decimal
import re

from . import egress as EG
from .claims import Calculation, Caveat, Claim, ClaimType, Finding, Operation, Outcome, ValueUse, Verification
from .evidence import EvidencePack, Kind, Methodology as ME, ValidationStatus as VS, make_id

VS_RANK = {VS.INDEPENDENTLY_VERIFIED: 4, VS.RECONCILED_TO_TEMPLATE: 3, VS.PRELIMINARY_METHODOLOGY: 2,
           VS.PENDING_CLIENT_APPROVAL: 2, VS.UNVERIFIED: 0}
LOSS_METRICS = ("cum_def", "cum_loss", "severity", "chgoff")
I = re.IGNORECASE

ID_TOKEN = re.compile(r"\b(?:EV|CL|VR)-[A-Za-z0-9._-]+")
PROGRAM = re.compile(r"\b7\s?\(a\)|\b504\b", I)
MONTH_NAMES = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
DATE_LONG = re.compile(rf"\b({MONTH_NAMES})\s+(\d{{1,2}}),?\s+(\d{{4}})\b", I)
DATE_US = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
FY = re.compile(r"\bFY\s?(\d{4})\+?", I)
PP = re.compile(r"[-−]?(\d[\d,]*(?:\.\d+)?)\s*(pp\b|percentage[- ]points?\b|basis[- ]points?\b|bps\b)", I)
PCT = re.compile(r"[-−]?(\d[\d,]*(?:\.\d+)?)\s*%")
MONEY = re.compile(r"[-−]?\$\s*(\d[\d,]*(?:\.\d+)?)(?:\s*(billion|bn|million|mm|thousand)\b)?", I)
MONTHS = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*months?\b", I)
NUMBER = re.compile(r"(?<![\w.])[-−]?(\d[\d,]*(?:\.\d+)?)(?![\w])")
SCALE = {"billion": 1e9, "bn": 1e9, "million": 1e6, "mm": 1e6, "thousand": 1e3}

DIR_NEG = re.compile(r"\b(lower|less|smaller)\s+than\b|\bbelow\s+(?:the\s+)?(?:sba|benchmark|industry|peer|average)", I)
DIR_POS = re.compile(r"\b(higher|more|greater|larger)\s+than\b|\b(above|exceed\w*)\s+(?:the\s+)?"
                     r"(?:sba|benchmark|industry|peer|average)", I)
EVAL_POS = re.compile(r"\b(outperform\w*|better|stronger|strong|superior|exceptional\w*|excellent|healthier|safer|"
                      r"high[- ]quality|sound)\b", I)
EVAL_NEG = re.compile(r"\b(underperform\w*|worse|weaker|weak|inferior|poor(?:ly)?|riskier|low[- ]quality|"
                      r"deteriorat\w*)\b", I)
RANKING = re.compile(r"\b(rank(?:s|ed|ing)?|top[- ](?:tier|quartile|decile|ranked)|bottom[- ](?:tier|quartile|decile)|"
                     r"best|worst|leading|percentile)\b|#\s?\d+", I)
CAUSAL = re.compile(r"\b(because|due to|driven by|caused by|as a result of|attributable to|owing to|thanks to|"
                    r"explain(?:s|ed)?|leads? to|led to|reflects?)\b", I)
SIGNIFICANCE = re.compile(r"\bstatistically\b|\bsignificant(?:ly)?\b|\bp[- ]?value\b|\bconfidence interval\b", I)
OVERREACH = re.compile(r"\b(consistently|always|never|clearly|demonstrat\w*|proves?|proven|reliabl\w*|trend\w*|"
                       r"systematic\w*|robust)\b", I)
# Approval language about a METHOD or RESULT ("approved benchmark", "FalconBridge-approved", "the methodology is
# confirmed"); plain SBA usage such as "loans approved in FY2000" is not approval of a methodology.
_SUBJECT = r"(?:benchmark|methodology|method|measure|comparison|conclusion|finding|figures?|results?|rates?)"
APPROVAL = re.compile(rf"\b(?:falconbridge|client)[- ](?:approved|confirmed|endorsed)\b|"
                      rf"\b(?:approved|confirmed|endorsed|certified|official|definitive|final(?:i[sz]ed)?)\s+"
                      rf"(?:by\s+falconbridge|(?:sba\s+)?(?:industry[- ]matched\s+)?{_SUBJECT})\b|"
                      rf"\b{_SUBJECT}\s+(?:is|has\s+been|was|are|were|have\s+been)\s+"
                      rf"(?:approved|confirmed|endorsed|certified|final(?:i[sz]ed)?)\b", I)
NEGATED_APPROVAL = re.compile(r"\b(?:not|un|pending|awaiting)\s*(?:yet\s+)?(?:been\s+)?"
                              r"(?:approved|confirmed|endorsed|certified|finali[sz]ed)\b", I)
CRITERIA = re.compile(r"\b(eligib\w*|qualif(?:y|ies|ied|ying)|disqualif\w*|criteri(?:a|on)|investment[- ]grade|"
                      r"recommend\w*|should\s+(?:not\s+)?(?:invest|approve|reject|partner|lend|purchase|buy|include|"
                      r"exclude|pursue|target)|pass(?:es|ed)?\s+(?:the\s+)?(?:screen|review|test)|"
                      r"fail(?:s|ed)?\s+(?:the\s+)?(?:screen|review|test)|acceptable\s+(?:for|to)\s+falconbridge|"
                      r"falconbridge(?:'s)?\s+(?:requires|policy|standards?|threshold|minimum|would|will))\b", I)
HEDGE = re.compile(r"\b(may|might|could|possibly|perhaps|suggests?|hypothes\w*|appears?|potentially)\b", I)
SHEET_CELL = re.compile(r"[A-Za-z_&][A-Za-z0-9_&]*!\$?[A-Z]{1,3}\$?\d*")


VERIFIER_VERSION = "m2-1.3"          # m2-1.0 / 1.1 / 1.2: earlier rules, kept so records made with them replay
VERSIONS = ("m2-1.0", "m2-1.1", "m2-1.2", "m2-1.3")


def _r(rule, msg, severity="REJECT"):
    return Finding(rule, severity, msg)


def effective_status(records):
    if not records:
        return VS.UNVERIFIED
    lo = min(VS_RANK[r.validation_status] for r in records)
    cands = {r.validation_status for r in records if VS_RANK[r.validation_status] == lo}
    return VS.PRELIMINARY_METHODOLOGY if VS.PRELIMINARY_METHODOLOGY in cands else sorted(cands, key=lambda s: s.value)[0]


def effective_methodology(records):
    ms = {r.methodology for r in records}
    if not ms:
        return ME.NOT_APPLICABLE
    if ms == {ME.CLIENT_APPROVED}:
        return ME.CLIENT_APPROVED
    for m in (ME.CLIENT_DIRECTED_PRELIMINARY, ME.APPLICATION_RULE, ME.UNCONFIRMED_WORKBOOK_METHOD):
        if m in ms:
            return m
    return ME.NOT_APPLICABLE


def required_caveats(pack, records):
    req = set()
    partial = None
    for r in pack.records:
        if r.metric_key == "limitation.partial_year" and isinstance(r.value, int):
            partial = r.value
    for r in records:
        lims = " | ".join(r.limitations)
        if r.validation_status is VS.PRELIMINARY_METHODOLOGY or r.methodology is ME.CLIENT_DIRECTED_PRELIMINARY:
            req.add(Caveat.PRELIMINARY_BENCHMARK)
        if "Not adjusted for vintage" in lims:
            req.add(Caveat.NOT_MIX_ADJUSTED)
        if "Small sample" in lims or "Limited data" in lims:
            req.add(Caveat.SMALL_SAMPLE)
        if "Original amounts" in lims:
            req.add(Caveat.ORIGINAL_AMOUNTS)
        if r.validation_status is VS.PENDING_CLIENT_APPROVAL or r.methodology is ME.APPLICATION_RULE:
            req.add(Caveat.PENDING_CLIENT_APPROVAL)
        if r.validation_status is VS.UNVERIFIED:
            req.add(Caveat.UNVERIFIED)
        if partial and (f".{partial}." in r.metric_key or r.metric_key == "limitation.partial_year"):
            req.add(Caveat.PARTIAL_YEAR)
    keys = [r.metric_key for r in records]
    if any(k.startswith("bank.") for k in keys) and any(k.startswith("benchmark.") for k in keys):
        req.add(Caveat.NOT_MIX_ADJUSTED)
    return req


def _decimals(lit):
    return len(lit.split(".")[1]) if "." in lit else 0


def _num(lit):
    return float(lit.replace(",", ""))


def _close(shown, true, decimals, scale=1.0):
    return abs(abs(shown) - abs(true)) <= 0.5 * 10 ** (-decimals) * scale + 1e-9 * max(1.0, abs(true))


def _years_in(text):
    return {int(y) for y in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", text)}


class _Ctx:
    """Candidate numbers a claim's text may use, all taken from the claim's declared, verified values."""
    def __init__(self, pack, claim, cited, calc_result):
        self.levels, self.diffs, self.usd, self.months, self.counts = [], [], [], [], []
        for vu in claim.values:
            r = pack_index(pack).get(vu.evidence_id)
            if r is None or not isinstance(vu.value, (int, float)):
                continue
            self._add(vu.unit, vu.value, r.metric_key.endswith(".diff"))
        if calc_result is not None:
            unit, value, is_diff = calc_result
            self._add(unit, value, is_diff)
        self.plain = set()                                   # numbers written in cited labels / limitations / keys
        for r in cited:
            for t in (r.label, r.metric_key, *r.limitations):
                self.plain.update(_num(x) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", t) if not x.endswith(","))
        th = pack.header.get("threshold")
        if isinstance(th, (int, float)):
            self.usd.append(float(th))
        self.years = {int(pack.header["as_of"][:4])}
        for r in cited:
            self.years |= _years_in(r.metric_key) | _years_in(r.label)
            if r.unit == "year" and isinstance(r.value, int):
                self.years.add(r.value)
        self.fy2000 = any(".fy2000." in r.metric_key or "FY2000" in r.label for r in cited)

    def _add(self, unit, value, is_diff):
        if unit == "ratio":
            (self.diffs if is_diff else self.levels).append(value)
        elif unit == "usd":
            self.usd.append(value)
        elif unit == "months":
            self.months.append(value)
        elif unit in ("count", "year"):
            self.counts.append(value)


_INDEX = {"pack": None, "index": None}


def pack_index(pack):
    """Evidence id -> record for THIS pack. The cache holds the pack object itself and compares identity (`is`), so
    a different pack can never be served a stale index, even if Python reuses a freed pack's memory address."""
    if _INDEX["pack"] is not pack:
        _INDEX["pack"], _INDEX["index"] = pack, {r.evidence_id: r for r in pack.records}
    return _INDEX["index"]


def _check_numbers(text, ctx, as_of, findings):
    t = ID_TOKEN.sub(" ", text)
    t = PROGRAM.sub(" ", t)
    for rx in (DATE_ISO, DATE_LONG, DATE_US):
        for m in rx.finditer(t):
            if rx is DATE_ISO:
                iso = m.group(0)
            elif rx is DATE_LONG:
                mon = MONTH_NAMES.lower().split("|").index(m.group(1).lower()) + 1
                iso = f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(2)):02d}"
            else:
                iso = f"{int(m.group(3)):04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
            if iso != as_of:
                findings.append(_r("reporting_period", f"date {m.group(0)!r} differs from the reporting period {as_of}"))
        t = rx.sub(" ", t)
    for m in FY.finditer(t):
        y = int(m.group(1))
        if not (y == 2000 and ctx.fy2000) and y not in ctx.years:
            findings.append(_r("reporting_period", f"'{m.group(0)}' is not a period of the cited evidence"))
    t = FY.sub(" ", t)
    for m in PP.finditer(t):
        lit, unit = m.group(1), m.group(2).lower()
        shown = _num(lit) / (100.0 if unit.startswith(("basis", "bps")) else 1.0)
        dec = _decimals(lit) + (2 if unit.startswith(("basis", "bps")) else 0)
        if not any(_close(shown, d * 100, dec) for d in ctx.diffs):
            hint = " (it matches a level, not a difference)" if any(_close(shown, v * 100, dec) for v in ctx.levels) \
                else ""
            findings.append(_r("value_unsupported", f"'{m.group(0).strip()}' does not equal any declared difference{hint}"))
    t = PP.sub(" ", t)
    for m in PCT.finditer(t):
        lit = m.group(1)
        shown, dec = _num(lit), _decimals(lit)
        if any(_close(shown, v * 100, dec) for v in ctx.levels):
            continue
        if any(_close(shown, d * 100, dec) for d in ctx.diffs):
            findings.append(_r("percentage_format", f"'{m.group(0)}' states a difference between two percentages as a "
                               "percent; it must be written in percentage points"))
        elif any(_close(shown, v, dec) or _close(shown, v * 10000, dec) for v in ctx.levels + ctx.diffs):
            findings.append(_r("percentage_format", f"'{m.group(0)}' is a correctly sourced ratio with the wrong "
                               "percentage scaling (a ratio of 0.025 is 2.5%)"))
        else:
            findings.append(_r("value_unsupported", f"'{m.group(0)}' does not equal any declared value"))
    t = PCT.sub(" ", t)
    for m in MONEY.finditer(t):
        lit, sc = m.group(1), SCALE.get((m.group(2) or "").lower(), 1.0)
        if not any(_close(_num(lit) * sc, v, _decimals(lit), sc) for v in ctx.usd):
            findings.append(_r("value_unsupported", f"'{m.group(0).strip()}' does not equal any declared dollar amount"))
    t = MONEY.sub(" ", t)
    for m in MONTHS.finditer(t):
        lit = m.group(1)
        if not any(_close(_num(lit), v, _decimals(lit)) for v in ctx.months):
            findings.append(_r("value_unsupported", f"'{m.group(0)}' does not equal any declared value in months"))
    t = MONTHS.sub(" ", t)
    for m in NUMBER.finditer(t):
        lit = m.group(1)
        if lit.endswith(","):
            lit = lit[:-1]
        v = _num(lit)
        if re.fullmatch(r"(19|20)\d{2}", lit):
            if int(lit) not in ctx.years:
                findings.append(_r("reporting_period", f"year {lit} is not a period of the cited evidence"))
            continue
        cands = ctx.counts + ctx.months + ctx.usd + list(ctx.plain)
        if not any(_close(v, c, _decimals(lit)) for c in cands):
            findings.append(_r("value_unsupported", f"number {lit!r} does not equal any declared value"))


def _declared_decimals(c):
    """Precision the calculation declares: the explicit `decimals`, else the decimal places of the result as written
    (shortest round-trip form, e.g. 0.2567 -> 4)."""
    if getattr(c, "decimals", None) is not None:
        return int(c.decimals)
    exp = Decimal(repr(float(c.result))).normalize().as_tuple().exponent
    return max(0, -exp)


def _calculation_finding(op, c, true, unit, version):
    """m2-1.0: the declared result must equal the recomputed one (relative 1e-9). m2-1.1: a rounded result is
    accepted when it equals the recomputed value at the precision it declares, and that precision is adequate
    (ratios: >= 4 decimal places, i.e. 0.01 percentage point; counts: exact; dollars: whole dollars or finer)."""
    exact = abs(c.result - true) <= max(1e-12, 1e-9 * abs(true))
    if version == "m2-1.0" or exact:
        return None if exact else _r("calculation", f"declared {op.value} {c.result!r} does not equal the recomputed "
                                                    f"{true!r} from the cited evidence")
    d = _declared_decimals(c)
    if getattr(c, "decimals", None) is not None and _declared_decimals(replace(c, decimals=None)) > d:
        return _r("calculation_precision", f"declared result {c.result!r} has more decimal places than the declared "
                                           f"precision ({d})")
    if unit == "count":
        return _r("calculation", f"declared {op.value} {c.result!r} is not the exact count {true!r}")
    if abs(c.result - true) > 0.5 * 10 ** (-d) + 1e-12:              # wrong even at its own precision
        return _r("calculation", f"declared {op.value} {c.result!r} does not equal the recomputed {true!r} at its "
                                 f"declared precision ({d} decimal places)")
    minimum = {"ratio": 4, "usd": 0}.get(unit, 4)
    if d < minimum:                                                  # right only because it is too coarse
        return _r("calculation_precision", f"declared {op.value} {c.result!r} is rounded to {d} decimal place(s); "
                                           f"{unit} results need at least {minimum}")
    return None


FY2000_TEXT = re.compile(r"\bFY\s?2000\b|\bsince 2000\b|\bbenchmark period\b", I)
FULL_TEXT = re.compile(r"\bfull[- ]history\b|\ball (?:of )?the bank'?s loans\b|\bsince inception\b|\blifetime\b|"
                       r"\bentire portfolio\b", I)
AGE_TEXT = re.compile(r"\bage\b|\baged\b", I)
OUTSTANDING_TEXT = re.compile(r"\boutstanding\b|\bactive\b", I)


def _scope_findings(text, cited):
    """m2-1.1 population and denominator scope (values that coincide do not make different populations equal)."""
    out = []
    hist = {r.metric_key.split(".")[1] for r in cited if r.metric_key.startswith("bank.")}
    if any(r.metric_key.startswith("benchmark.") for r in cited) and "full" in hist:
        out.append(_r("population_mismatch", "the SBA benchmark covers loans approved FY2000 onward; it must be "
                      "compared with the bank's FY2000+ figures (bank.fy2000...), not its full history, even where "
                      "the two values coincide"))
    if FY2000_TEXT.search(text) and "full" in hist and "fy2000" not in hist:
        out.append(_r("population_mismatch", f"the text describes '{FY2000_TEXT.search(text).group(0)}' but cites "
                      "the bank's full-history evidence"))
    if FULL_TEXT.search(text) and "fy2000" in hist and "full" not in hist:
        out.append(_r("population_mismatch", f"the text describes '{FULL_TEXT.search(text).group(0)}' but cites the "
                      "bank's FY2000+ evidence"))
    if any(r.metric_key.endswith(".age") for r in cited) and AGE_TEXT.search(text) and \
            not OUTSTANDING_TEXT.search(text):
        out.append(_r("denominator_scope", "average loan age is measured over the bank's estimated outstanding "
                      "(active) loans only; the statement must say so rather than describe the whole portfolio"))
    return out


# ------------------------------------------------------------------------------- m2-1.2: population alignment
# A bank-vs-benchmark comparison must compare the SAME measure for the SAME segment (loan size group and total /
# vintage year / industry) and the SAME loan population. The SBA benchmark covers loans approved FY2000 onward; the
# bank's FY2000+ evidence is built by filtering its full-history loans by approval year (threshold_analysis
# hist_rows), so it is a subset of the full-history population. A full-history bank figure is therefore accepted
# only when the evidence shows the FY2000+ population of the same segment has the same loan count AND the same
# original amount (a subset with equal count and amount is the same set of loans). Equal rates prove nothing.
_BANK = re.compile(r"^bank\.(full|fy2000)\.(total|above|below)\.(total|vintage\.\d{4}|industry\..+|subprogram\..+)"
                   r"\.([a-z_0-9]+)$")
_BENCH = re.compile(r"^benchmark\.(total|above|below|noncre|cre)\.(total|vintage\.\d{4}|industry\..+)\.([a-z_0-9]+)$")
_CMP = re.compile(r"^compare\.(total|above|below)\.(total|industry\..+)\.([a-z_0-9]+)\.diff$")
RATE_METRICS = ("cum_def", "cum_loss", "severity", "purch_pct")
GROUP_TEXT = {"total": "all loan sizes", "above": "loans above the threshold", "below": "loans at or below the "
              "threshold", "noncre": "the SBA term <= 10 years group", "cre": "the SBA term > 10 years group"}


def _segment_text(group, seg):
    if seg == "total":
        what = "all vintages and industries"
    elif seg.startswith("vintage."):
        what = f"the {seg[8:]} vintage"
    elif seg.startswith("industry."):
        what = f"the industry {seg[9:]}"
    else:
        what = f"the subprogram {seg[11:]}"
    return f"{what}, {GROUP_TEXT[group]}"


def _parse(key):
    m = _BANK.match(key)
    if m:
        return ("bank", m.group(1), m.group(2), m.group(3), m.group(4))
    m = _BENCH.match(key)
    if m:
        return ("benchmark", "fy2000", m.group(1), m.group(2), m.group(3))
    m = _CMP.match(key)
    if m:
        return ("compare", "fy2000", m.group(1), m.group(2), m.group(3))
    return None


def _population(pack, hist, group, seg):
    idx, sid = pack_index(pack), pack.header["source_id"]
    get = lambda m: idx.get(make_id(sid, Kind.FACT, f"bank.{hist}.{group}.{seg}.{m}"))     # noqa: E731
    n, o = get("loans"), get("orig")
    return (n.value if n is not None else None), (o.value if o is not None else None)


def _identity(pack, group, seg):
    """'' if the full-history and FY2000+ bank populations of this segment are proven identical, else the reason."""
    n_full, o_full = _population(pack, "full", group, seg)
    n_fy, o_fy = _population(pack, "fy2000", group, seg)
    where = _segment_text(group, seg)
    if n_full is None or o_full is None:
        return f"{where}: the full-history population (loan count and original amount) is not in the evidence"
    if not n_fy:
        return (f"{where}: the bank's full-history population has {n_full:,} loans (${o_full:,.0f}) and none were "
                "approved FY2000 onward, the only years the SBA benchmark covers")
    if o_fy is None or n_fy > n_full or o_fy > o_full + 0.5:
        return f"{where}: the FY2000+ population is not a subset of the full-history population in the evidence"
    if n_fy != n_full or abs(o_fy - o_full) > 0.5:
        return (f"{where}: the bank's full-history population has {n_full:,} loans (${o_full:,.0f}) but its FY2000+ "
                f"population, the one comparable with the SBA benchmark, has {n_fy:,} loans (${o_fy:,.0f}); the "
                "difference is loans approved before FY2000")
    return ""


def _alignment_findings(text, cited, pack, calc_ops):
    out = []
    parsed = [(r, _parse(r.metric_key)) for r in cited]
    banks = [(r, p) for r, p in parsed if p and p[0] == "bank"]
    benches = [(r, p) for r, p in parsed if p and p[0] == "benchmark"]
    compares = [(r, p) for r, p in parsed if p and p[0] == "compare"]
    proven = set()
    if benches and banks:                      # a bank-vs-benchmark comparison (benchmark-only statements are not)
        bank_keys = {(p[2], p[3], p[4]) for _, p in banks}
        bench_keys = {(p[2], p[3], p[4]) for _, p in benches}
        for r, p in benches:                                  # every benchmark figure needs its bank counterpart
            if p[2] in ("noncre", "cre"):
                out.append(_r("segment_mismatch", f"{r.metric_key}: the bank is not split by loan term, so "
                              f"{GROUP_TEXT[p[2]]} has no comparable bank figure"))
            elif (p[2], p[3], p[4]) not in bank_keys:
                out.append(_r("segment_mismatch", f"the SBA benchmark's {p[4]} for {_segment_text(p[2], p[3])} is "
                              "cited without the bank's same measure for the same segment"))
        for r, p in banks:                                    # every bank rate needs its benchmark counterpart
            if p[4] in RATE_METRICS and (p[2], p[3], p[4]) not in bench_keys:
                out.append(_r("segment_mismatch", f"the bank's {p[4]} for {_segment_text(p[2], p[3])} is compared "
                              "with SBA benchmark figures for a different segment or measure"))
        for r, p in compares:
            if (p[2], p[3], p[4]) not in bench_keys:
                out.append(_r("segment_mismatch", f"{r.metric_key} describes a different segment or measure from "
                              "the SBA benchmark figures cited with it"))
        seen = set()
        for r, p in banks:                                    # population identity for full-history figures
            if p[1] == "full" and (p[2], p[3]) not in seen:
                seen.add((p[2], p[3]))
                why = _identity(pack, p[2], p[3])
                if why:
                    out.append(_r("population_mismatch", why + "; cite the bank's FY2000+ figure instead"))
                else:
                    proven.add((p[2], p[3]))
    if calc_ops and len(calc_ops) == 2:                       # a declared bank-minus-benchmark must pair like with like
        a, b = (_parse(o.metric_key) if o is not None else None for o in calc_ops)
        if a and b and {a[0], b[0]} == {"bank", "benchmark"} and (a[2], a[3], a[4]) != (b[2], b[3], b[4]):
            out.append(_r("segment_mismatch", "the declared difference subtracts figures for different segments or "
                          "measures"))
    hist = {p[1] for _, p in banks}
    full_unproven = any(p[1] == "full" and (p[2], p[3]) not in proven for _, p in banks)
    if FY2000_TEXT.search(text) and "full" in hist and "fy2000" not in hist and full_unproven:
        out.append(_r("population_mismatch", f"the text describes '{FY2000_TEXT.search(text).group(0)}' but cites "
                      "full-history evidence whose population is not shown to be the FY2000+ population"))
    if FULL_TEXT.search(text) and "fy2000" in hist and "full" not in hist:
        fy_segments = {(p[2], p[3]) for _, p in banks if p[1] == "fy2000"}
        if any(_identity(pack, g, s) for g, s in fy_segments):
            out.append(_r("population_mismatch", f"the text describes '{FULL_TEXT.search(text).group(0)}' but cites "
                          "FY2000+ evidence whose population is not the full history"))
    if any(r.metric_key.endswith(".age") for r in cited) and AGE_TEXT.search(text) and \
            not OUTSTANDING_TEXT.search(text):
        out.append(_r("denominator_scope", "average loan age is measured over the bank's estimated outstanding "
                      "(active) loans only; the statement must say so rather than describe the whole portfolio"))
    return out


# ------------------------------------------------------------------------------- m2-1.3: benchmark identity
# Benchmark Intelligence Phase A (approved 2026-11-09) adds two INVESTIGATIVE SBA comparisons next to the approved,
# preliminary benchmark. Their values can be close to each other, so a statement must name the comparison it cites
# and must not borrow another comparison's label or population:
#   B0  benchmark.* and bench_intel.B0.*  SBA loans in the industries where the bank has lent, at the SBA's own mix
#   B1  bench_intel.B1.*                  all SBA 7(a) loans, every industry (not industry-matched)
#   B2  bench_intel.B2.*                  B0's SBA loans weighted to the bank's FY2000+ industry mix
# Every comparison covers loans approved FY2000 onward, so it pairs with the bank's FY2000+ figures only.
B1_TEXT = re.compile(r"\b(?:every|all) industr(?:y|ies)\b|\bentire SBA\b|\bwhole SBA\b|\bunmatched\b|"
                     r"\bnot industry[- ]matched\b|\ball[- ]SBA comparison\b", I)
B2_TEXT = re.compile(r"\bweighted (?:to|by) (?:the )?bank'?s industry (?:mix|distribution|weights?)\b|"
                     r"\bbank'?s industry mix\b|\bindustry[- ]mix[- ]weighted\b|\bindustry[- ]weighted\b", I)
B0_TEXT = re.compile(r"\bbank'?s (?:own )?industries\b|\bindustry[- ](?:matched|selected)\b|"
                     r"\bSBA'?s own (?:industry )?mix\b", I)
ALL_SBA = re.compile(r"\ball[- ]SBA\b", I)
BENCH_NAMES = {"B1": "the all-SBA comparison (every industry)",
               "B2": "the comparison weighted to the bank's industry mix"}


def _bench_kind(key):
    if key.startswith(("benchmark.", "bench_intel.B0.")):
        return "B0"
    if key.startswith("bench_intel.B1."):
        return "B1"
    if key.startswith("bench_intel.B2."):
        return "B2"
    return None


def _benchmark_identity_findings(text, cited, calc_ops):
    out = []
    kinds = {k for k in (_bench_kind(r.metric_key) for r in cited) if k}
    for k, rx in (("B1", B1_TEXT), ("B2", B2_TEXT)):
        if k in kinds and not rx.search(text):
            out.append(_r("benchmark_label", f"a {k} figure is cited; the statement must name it as {BENCH_NAMES[k]} "
                          "so that it cannot be read as the approved SBA benchmark"))
        if k not in kinds and rx.search(text):
            out.append(_r("benchmark_label", f"the text describes '{rx.search(text).group(0)}' ({BENCH_NAMES[k]}) "
                          f"but cites no {k} figure"))
    if "B0" in kinds and "B1" not in kinds and ALL_SBA.search(text) and not B0_TEXT.search(text):
        out.append(_r("benchmark_label", "the SBA benchmark covers only SBA loans in the industries where the bank has "
                      "lent; it must not be described as all SBA loans"))
    if kinds and any(r.metric_key.startswith("bench_intel.bank_full.") for r in cited):
        out.append(_r("population_mismatch", "every SBA comparison covers loans approved FY2000 onward; compare it "
                      "with the bank's FY2000+ figure, not its full history"))
    if any(r.metric_key.startswith("bench_intel.bank_fy2000.") for r in cited) and FULL_TEXT.search(text):
        out.append(_r("population_mismatch", f"the text describes '{FULL_TEXT.search(text).group(0)}' but cites the "
                      "bank's FY2000+ evidence"))
    if calc_ops and len(calc_ops) == 2 and all(o is not None for o in calc_ops):
        keys = [o.metric_key for o in calc_ops]
        if any(k.startswith("bench_intel.") for k in keys) and keys[0].rsplit(".", 1)[-1] != keys[1].rsplit(".", 1)[-1]:
            out.append(_r("segment_mismatch", "the declared difference subtracts two different measures"))
    return out


def verify_claim(pack, claim, policy=EG.DEFAULT_POLICY, confidential_terms=(), registry=None, approved_rules=(),
                 version=None):
    """Verify one claim against one evidence pack. `registry` (optional): {evidence_id: (source_id, bank)} from other
    packs, used only to explain cross-analysis substitutions. `approved_rules`: approved FalconBridge business rules
    (none exist yet), which alone could allow criteria statements."""
    version = version or VERIFIER_VERSION
    if version not in VERSIONS:
        raise ValueError(f"unknown verifier version {version!r}")
    f = []
    if not isinstance(pack, EvidencePack) or not isinstance(claim, Claim):
        raise TypeError("verify_claim(EvidencePack, Claim)")
    idx = pack_index(pack)
    h = pack.header
    try:
        ctype = ClaimType(claim.claim_type)
    except ValueError:
        return Verification(claim.claim_id, Outcome.REJECTED, (_r("structure", f"unknown claim type {claim.claim_type!r}"),),
                            explanation="unknown claim type")
    if not re.fullmatch(r"CL-[A-Za-z0-9._-]{1,64}", claim.claim_id or ""):
        f.append(_r("structure", "claim identifier must look like CL-<id>"))
    if not claim.text or not claim.text.strip() or len(claim.text) > 2000:
        f.append(_r("structure", "claim text is empty or too long"))
    if ctype is ClaimType.QUESTION and not claim.text.rstrip().endswith("?"):
        f.append(_r("structure", "a QUESTION must be phrased as a question"))
    if ctype not in (ClaimType.QUESTION,) and not claim.evidence_refs:
        f.append(_r("evidence_missing", f"a {ctype.value} must cite evidence"))

    # 1-2: evidence exists and belongs to this Saved Analysis, bank and period
    if claim.source_id != h["source_id"]:
        f.append(_r("source_mismatch", f"claim is about Saved Analysis {claim.source_id}, but this evidence pack is "
                    f"{h['source_id']}"))
    if tuple(claim.bank) != tuple(h["bank"]):
        f.append(_r("source_mismatch", f"claim names bank {tuple(claim.bank)}, but the evidence is for {tuple(h['bank'])}"))
    if claim.as_of != h["as_of"]:
        f.append(_r("reporting_period", f"claim reporting period {claim.as_of} differs from the evidence ({h['as_of']})"))
    cited = []
    for eid in claim.evidence_refs:
        r = idx.get(eid)
        if r is None:
            other = (registry or {}).get(eid)
            f.append(_r("evidence_unknown", f"{eid} belongs to Saved Analysis {other[0]} ({', '.join(other[1])}), not "
                        f"this one: cross-analysis evidence substitution" if other else
                        f"{eid} does not exist in this Saved Analysis's evidence (fabricated or mistyped identifier)"))
        elif r.kind is Kind.HYPOTHESIS:
            f.append(_r("evidence_kind", f"{eid} is a hypothesis and cannot support a claim"))
        else:
            cited.append(r)

    # 3-4: declared values, units, calculation
    for vu in claim.values:
        if vu.evidence_id not in claim.evidence_refs:
            f.append(_r("value_unsupported", f"value {vu.value!r} cites {vu.evidence_id}, which the claim does not list"))
            continue
        r = idx.get(vu.evidence_id)
        if r is None:
            continue
        if vu.unit != r.unit:
            f.append(_r("unit_mismatch", f"value for {vu.evidence_id} is declared in {vu.unit!r}; the evidence is in "
                        f"{r.unit!r}"))
        elif not (isinstance(r.value, (int, float)) and isinstance(vu.value, (int, float))
                  and abs(vu.value - r.value) <= max(1e-12, 1e-9 * abs(r.value))):
            f.append(_r("value_mismatch", f"declared value {vu.value!r} for {vu.evidence_id} does not match the evidence "
                        f"value {r.value!r}"))
    calc_result = None
    c = claim.calculation
    if c is not None:
        try:
            op = Operation(c.operation)
        except ValueError:
            op = None
            f.append(_r("operation_not_approved", f"operation {c.operation!r} is not an approved operation "
                        f"({', '.join(o.value for o in Operation)})"))
        ops = [idx.get(e) for e in c.operands]
        if op is not None:
            if any(e not in claim.evidence_refs for e in c.operands) or any(o is None for o in ops):
                f.append(_r("calculation", "calculation operands must be cited evidence of this Saved Analysis"))
            elif any(not isinstance(o.value, (int, float)) or isinstance(o.value, bool) for o in ops):
                f.append(_r("calculation", "calculation operands must be numeric"))
            elif (op in (Operation.DIFFERENCE, Operation.RATIO) and len(ops) != 2) or len(ops) < 2:
                f.append(_r("calculation", f"{op.value} needs {'exactly ' if op is not Operation.SUM else 'at least '}2 "
                            "operands"))
            elif op is not Operation.RATIO and len({o.unit for o in ops}) != 1:
                f.append(_r("unit_mismatch", f"{op.value} of values in different units "
                            f"({', '.join(sorted({o.unit for o in ops}))})"))
            elif op is Operation.RATIO and ops[1].value == 0:
                f.append(_r("calculation", "ratio with a zero denominator"))
            else:
                true = (ops[0].value - ops[1].value if op is Operation.DIFFERENCE else
                        sum(o.value for o in ops) if op is Operation.SUM else ops[0].value / ops[1].value)
                unit = "ratio" if op is Operation.RATIO else ops[0].unit
                if c.unit != unit:
                    f.append(_r("unit_mismatch", f"calculation result declared in {c.unit!r}; {op.value} of these "
                                f"operands is in {unit!r}"))
                else:
                    finding = _calculation_finding(op, c, true, unit, version)
                    if finding:
                        f.append(finding)
                calc_result = (unit, true, op is Operation.DIFFERENCE)

    # 3-4: every number in the text is a declared value (right scale, right period)
    _check_numbers(claim.text, _Ctx(pack, claim, cited, calc_result), h["as_of"], f)

    text = claim.text
    small = any("Small sample" in " ".join(r.limitations) or "Limited data" in " ".join(r.limitations) for r in cited)
    eff_vs, eff_me = effective_status(cited), effective_methodology(cited)
    diff_sign, diff_loss = None, False
    if calc_result and calc_result[2]:
        diff_sign = math.copysign(1, calc_result[1]) if calc_result[1] else 0
        diff_loss = any(m in idx[e].metric_key for e in c.operands if e in idx for m in LOSS_METRICS)
    else:
        for r in cited:
            if r.metric_key.endswith(".diff") and isinstance(r.value, (int, float)):
                diff_sign = math.copysign(1, r.value) if r.value else 0
                diff_loss = any(m in r.metric_key for m in LOSS_METRICS)
                break
    if ctype is not ClaimType.QUESTION:
        # 10: direction words must agree with a cited difference
        neg, pos = bool(DIR_NEG.search(text)), bool(DIR_POS.search(text))
        if neg or pos:
            if diff_sign is None:
                f.append(_r("comparison_unsupported", "the claim compares values but cites no difference (declare a "
                            "'difference' calculation or cite a difference record)"))
            elif neg and pos:
                f.append(_r("comparison_unsupported", "the claim uses contradictory direction words"))
            elif (neg and diff_sign > 0) or (pos and diff_sign < 0):
                f.append(_r("direction_contradicts_evidence", f"the text says {'lower' if neg else 'higher'}, but the "
                            f"cited difference is {'positive' if diff_sign > 0 else 'negative'}"))
        # 5: rankings need ranking evidence (no pack holds any)
        if RANKING.search(text) and not any(".rank" in r.metric_key for r in cited):
            f.append(_r("ranking_unsupported", f"'{RANKING.search(text).group(0)}' implies a ranking or peer position; "
                        "no cited evidence ranks this bank against others"))
        # 9: significance claims need a significance test (none exists)
        if SIGNIFICANCE.search(text):
            f.append(_r("significance_unsupported", f"'{SIGNIFICANCE.search(text).group(0)}': no cited evidence tests "
                        "statistical significance; differences are arithmetic only"))
        # 7: small populations
        if small and OVERREACH.search(text):
            f.append(_r("small_sample_overinterpretation", f"'{OVERREACH.search(text).group(0)}' generalises from a "
                        "small sample or limited-data population"))
        # 6: unapproved methodology presented as approved
        cleaned = NEGATED_APPROVAL.sub(" ", text)
        if APPROVAL.search(cleaned) and eff_me is not ME.CLIENT_APPROVED:
            f.append(_r("approval_misrepresented", f"'{APPROVAL.search(cleaned).group(0)}' presents evidence whose "
                        f"methodology is {eff_me.value} as approved"))
    if ctype is not ClaimType.QUESTION and version != "m2-1.0":
        if version == "m2-1.1":
            f.extend(_scope_findings(text, cited))
        else:
            ops = [idx.get(e) for e in c.operands] if c else ()
            f.extend(_alignment_findings(text, cited, pack, ops))
            if version not in ("m2-1.2",):                     # m2-1.3: benchmark identity (B0 / B1 / B2)
                f.extend(_benchmark_identity_findings(text, cited, ops))
    if ctype not in (ClaimType.QUESTION, ClaimType.HYPOTHESIS):
        # 10: causal explanations are hypotheses, not observations
        if CAUSAL.search(text):
            f.append(_r("causal_unsupported", f"'{CAUSAL.search(text).group(0)}' asserts a cause the evidence does not "
                        "establish; state it as a HYPOTHESIS citing evidence"))
        # 10: evaluative terms need supporting evidence AND approved methodology
        ev_pos, ev_neg = EVAL_POS.search(text), EVAL_NEG.search(text)
        if ev_pos or ev_neg:
            term = (ev_pos or ev_neg).group(0)
            if diff_sign is None or not diff_loss:
                f.append(_r("conclusion_unsupported", f"'{term}' is a performance conclusion; no cited loss-rate "
                            "difference supports it"))
            elif (ev_pos and diff_sign >= 0) or (ev_neg and diff_sign <= 0):
                f.append(_r("direction_contradicts_evidence", f"'{term}' contradicts the cited loss-rate difference "
                            f"({'positive' if diff_sign > 0 else 'zero or negative'})"))
            elif small:
                f.append(_r("small_sample_overinterpretation", f"'{term}' draws a performance conclusion from a small "
                            "sample or limited-data population"))
            elif eff_me is not ME.CLIENT_APPROVED:
                f.append(_r("methodology_not_approved", f"'{term}' is supported by the numbers, but the cited "
                            f"methodology is {eff_me.value}; an analyst must review this interpretation", "REVIEW"))
        # 8: no invented FalconBridge criteria
        m = CRITERIA.search(text)
        if m and not approved_rules:
            f.append(_r("criteria_invented", f"'{m.group(0)}' states or implies a FalconBridge eligibility, ranking or "
                        "investment criterion; no approved business rule exists"))
    if ctype is ClaimType.HYPOTHESIS:
        if claim.validation_status is not VS.UNVERIFIED:
            f.append(_r("status_elevated", "a HYPOTHESIS is always UNVERIFIED"))
        if not HEDGE.search(text):
            f.append(_r("hypothesis_stated_as_fact", "a HYPOTHESIS must be worded as a possibility (may, might, could, "
                        "suggests ...)"))
    else:
        # 9: no elevation of validation or methodology status
        if claim.validation_status is not eff_vs:
            higher = VS_RANK[claim.validation_status] > VS_RANK[eff_vs]
            f.append(_r("status_elevated" if higher else "status_mismatch",
                        f"claim states {claim.validation_status.value}; the cited evidence supports at most {eff_vs.value}"))
        if claim.methodology is not eff_me and not (ctype is ClaimType.QUESTION and claim.methodology is ME.NOT_APPLICABLE):
            f.append(_r("methodology_elevated" if claim.methodology is ME.CLIENT_APPROVED else "methodology_mismatch",
                        f"claim states methodology {claim.methodology.value}; the cited evidence is {eff_me.value}"))
    # 6-7: required caveats
    req = required_caveats(pack, cited) if ctype is not ClaimType.QUESTION else set()
    if ctype is ClaimType.HYPOTHESIS:
        req.discard(Caveat.UNVERIFIED)
    have = {Caveat(x) for x in claim.caveats if x in Caveat._value2member_map_ or isinstance(x, Caveat)}
    for cv in sorted(req - have, key=lambda x: x.value):
        f.append(_r("caveat_missing", f"missing required caveat {cv.value}"))
    # 10 (confidentiality): exportable claims
    if claim.exportable:
        if ctype is ClaimType.HYPOTHESIS:
            f.append(_r("export_refused", "hypotheses are never exported"))
        for r in cited:
            if not policy.allows(r):
                f.append(_r("export_refused", f"{r.evidence_id} ({r.classification.value}) is not approved for export"))
        blob = " ".join([claim.text] + [str(x) for x in claim.caveats])
        for marker in EG.FORBIDDEN_MARKERS:
            if marker in blob:
                f.append(_r("confidential_content", f"exportable claim contains forbidden content ({marker!r})"))
        if SHEET_CELL.search(blob):
            f.append(_r("confidential_content", f"exportable claim contains a workbook reference "
                        f"({SHEET_CELL.search(blob).group(0)!r})"))
        low = blob.lower()
        for term in confidential_terms:
            if term and term.lower() in low:
                f.append(_r("confidential_content", "exportable claim contains confidential FalconBridge or internal text"))
                break
    outcome = (Outcome.REJECTED if any(x.severity == "REJECT" for x in f) else
               Outcome.NEEDS_ANALYST_REVIEW if f else Outcome.ACCEPTED)
    return Verification(claim.claim_id, outcome, tuple(f), eff_vs, eff_me, tuple(sorted(req, key=lambda x: x.value)),
                        "; ".join(x.message for x in f) or "all checks passed")


def verify_claims(pack, claims, **kw):
    seen, out = set(), []
    for c in claims:
        v = verify_claim(pack, c, **kw)
        if c.claim_id in seen:
            v = Verification(c.claim_id, Outcome.REJECTED, v.findings + (_r("structure", "duplicate claim identifier"),),
                             v.effective_validation_status, v.effective_methodology, v.required_caveats,
                             v.explanation + "; duplicate claim identifier")
        seen.add(c.claim_id)
        out.append(v)
    return out
