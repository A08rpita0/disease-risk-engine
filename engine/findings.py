"""Direct laboratory findings - every result worth a reader's attention, rule or no rule.

Why this exists. A result used to reach "What we found" only by feeding a cluster that
mapped to a Disease Master condition. An hs-CRP of 31.98 mg/L feeds none - the only
rule written for hs-CRP is the 3-10 mg/L cardiovascular band, and a lone CRP is too
little to call an inflammatory PATTERN - so it was graded, flagged abnormal, and then
shown nowhere except the raw results table. The Disease Master had become a filter on
what counts as a finding.

It is an enrichment instead. This module lists results in their own right:

    abnormal against the laboratory's interval        basis = lab_range
    abnormal against a configured guideline band       basis = decision_threshold
    inside the lab interval but meeting a cluster's    basis = decision_threshold
      configured condition (TSH 5.05 in 0.54-5.3)        in_lab_range = True
    calculated here, and out of range                  basis = derived
    a positive qualitative result                      basis = lab_range / qualitative

and, separately, records which conditions or patterns - if any - draw on each one. A
result with no link is still listed, with a neutral statement and no diagnosis.
"""
from __future__ import annotations

from .cohorts import _v


BASIS_ORDER = {"lab_range": 0, "decision_threshold": 1, "derived": 2}


def _ref_text(p):
    lo, hi = p.reference_low, p.reference_high
    if lo is not None and hi is not None:
        return "%s-%s" % (_v(lo), _v(hi))
    if hi is not None:
        return "up to %s" % _v(hi)
    if lo is not None:
        return "%s or above" % _v(lo)
    return None


def _statement(p, in_range_bands):
    unit = (" " + p.unit) if p.unit else ""
    ref = _ref_text(p)
    if p.kind == "qualitative" or (p.value is None and p.status):
        return "%s was reported as %s." % (p.name, p.status or p.category)
    if p.derived:
        return ("%s was calculated here from other results (%s). No laboratory measured "
                "or flagged it, so check it against the results it was derived from."
                % (p.name, p.derivation or "derived value"))
    if in_range_bands:
        band = in_range_bands[0]
        return ("%s is inside the laboratory's reference interval (%s%s), but meets the "
                "configured condition \"%s\" used by the %s rule."
                % (p.name, ref or "not stated", unit, band["band"], band["cohort"]))
    word = {"high": "above", "low": "below"}.get(p.direction, "outside")
    if p.finding_basis == "lab_range":
        return "%s is %s the laboratory's reference interval (%s%s)." % (
            p.name, word, ref, unit)
    if p.graded_by == "decision_band":
        return ("%s is in the \"%s\" band of the guideline thresholds configured here. The "
                "interval printed on the report would not flag it." % (p.name, p.grade_label))
    return ("The report gave no reference interval, so %s was judged against the guideline "
            "band configured here: %s." % (p.name, p.grade_label or "outside range"))


def build_lab_findings(patient, cohort_hits, risks):
    """Every abnormal or threshold-crossing result, with what (if anything) it feeds."""
    links = {}

    def link(pid, entry):
        lst = links.setdefault(pid, [])
        if not any(e["name"] == entry["name"] for e in lst):
            lst.append(entry)

    for r in risks:
        ev = r.direct_evidence or {}
        if ev.get("parameter_id"):
            link(ev["parameter_id"], {"kind": "condition", "name": r.name,
                                      "tier": r.presentation_tier,
                                      "evidence_level": r.evidence_level})
        for t in r.triggering_parameters:
            link(t["parameter_id"], {"kind": "condition", "name": r.name,
                                     "tier": r.presentation_tier,
                                     "evidence_level": r.evidence_level})
    reported = {r.name for r in risks}
    for h in cohort_hits:
        for t in h.hits:
            if t.effective_weight > 0:
                # A cluster that fired but whose conditions all fell below the reporting
                # floor is still worth naming - it is why the result mattered.
                link(t.parameter_id, {"kind": "pattern", "name": h.name, "tier": "pattern",
                                      "evidence_level": None})

    out = []
    for pid, p in patient.parameters.items():
        in_range_bands = p.triggered_bands if (not p.abnormal and p.triggered_bands) else []
        if not p.abnormal and not in_range_bands:
            continue
        linked = links.get(pid, [])
        conditions = [e for e in linked if e["kind"] == "condition" and e["name"] in reported]
        out.append({
            "parameter_id": pid,
            "name": p.name,
            "profile": p.profile,
            "kind": p.kind,
            "value": p.value if p.value is not None else (p.status or p.category),
            "unit": p.unit,
            "reference_low": p.reference_low,
            "reference_high": p.reference_high,
            "reference_text": _ref_text(p),
            "reference_source": p.reference_source,
            "finding_basis": p.finding_basis if p.finding_basis != "normal" else "decision_threshold",
            "graded_by": p.graded_by,
            "in_lab_range": bool(in_range_bands),
            "abnormal": p.abnormal,
            "direction": p.direction,
            "grade": p.grade,
            "grade_label": p.grade_label,
            "severity_score": p.severity_score,
            "lab_flag": p.raw.raw_flag if p.raw else None,
            "derived": p.derived,
            "statement": _statement(p, in_range_bands),
            "linked": linked,
            # True when nothing in the rule set interprets this result. It is listed
            # anyway - that is the point - but without any suggestion of a diagnosis.
            "standalone": not conditions and not any(e["kind"] == "pattern" for e in linked),
        })

    out.sort(key=lambda f: (f["in_lab_range"], -f["severity_score"],
                            BASIS_ORDER.get(f["finding_basis"], 9), f["name"]))
    return out
