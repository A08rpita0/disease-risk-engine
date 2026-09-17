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


def _inside_printed_interval(p):
    """An abnormal verdict reached by a guideline band on a value the report's own
    interval contains. Only a measured number against a report interval qualifies: a
    derived value, a qualitative result, or a dictionary interval says nothing about what
    the laboratory printed.

    Strictly inside only. A stored bound does not say whether the printed one was
    inclusive: "<5.7: Non-diabetes / 5.7 - 6.4: Prediabetes" is kept as an upper limit of
    5.7, and an HbA1c of exactly 5.7 - prediabetes by that very report - must never be
    described as a result the laboratory would call normal."""
    if (not p.abnormal or p.graded_by != "decision_band" or p.derived
            or p.kind != "numeric" or not isinstance(p.value, (int, float))
            or not str(p.reference_source or "").startswith("report")
            or (p.reference_low is None and p.reference_high is None)):
        return False
    return ((p.reference_low is None or p.value > p.reference_low)
            and (p.reference_high is None or p.value < p.reference_high))


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
    # Not taken from the report is not the same as not printed on it. hs-CRP printed as
    # "Low: < 1.0 / Average: 1.0-3.0 / High: > 3.0" gives risk bands rather than one normal
    # interval, so the guideline band decides - and saying the report "gave no reference
    # interval" contradicted the page, which the laboratory had also marked.
    printed = ((p.raw.raw_range if p.raw else None) or "").strip()
    if printed:
        return ("The report printed \"%s\" rather than a single normal interval, so %s was "
                "judged against the guideline band configured here: %s."
                % ("; ".join(s.strip() for s in printed.split("\n") if s.strip()), p.name,
                   p.grade_label or "outside range"))
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
        # Where a result sits against the interval the laboratory PRINTED - a fact about
        # the report, whatever graded it. An LDL of 112 against a printed "100 - 129 :
        # Desirable" band, called near/above optimal by a configured guideline band, was
        # listed under "Results outside their range" while its own statement said the
        # printed interval would not flag it. It belongs with the other guideline-only
        # findings the laboratory would call normal.
        in_lab_range = bool(in_range_bands) or _inside_printed_interval(p)
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
            "in_lab_range": in_lab_range,
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


# Flags a laboratory prints to say a result is outside its range.
_LAB_FLAG_WORDS = {"h", "l", "hh", "ll", "high", "low", "abnormal", "critical", "a", "*", "**",
                   "(h)", "(l)", "[h]", "[l]"}


def _norm_text(s):
    return " ".join(str(s or "").lower().replace(",", " ").split())


def build_lab_noted_findings(patient):
    """What the LABORATORY marked that this analysis does not call abnormal.

    Two cases, both facts printed on the report, neither graded abnormal here:
      - a printed flag ("High", "L", "ABNORMAL") on a result the interval used here calls
        normal. An HDL of 61 printed "High" is cardioprotective by the configured bands,
        but the laboratory's own flag must not vanish into a note in the results table.
      - a descriptive result that differs from the expected description printed beside it
        ("Yellow" where the report expects "Pale Yellow").
    Listed separately: they are not counted as abnormal and no plan step is generated
    from them, since the rules here do not call them abnormal - but nothing the
    laboratory marked is silently dropped.
    """
    out = []
    for pid, p in patient.parameters.items():
        if p.abnormal or p.raw is None or p.derived:
            continue
        flag = _norm_text(p.raw.raw_flag)
        printed = (p.raw.raw_range or "").strip()
        entry = None
        if flag in _LAB_FLAG_WORDS:
            entry = ("lab_flag",
                     "The laboratory printed the flag \"%s\" beside %s. It is not graded "
                     "abnormal here (%s), so both are shown: check the report's own "
                     "interpretation." % (p.raw.raw_flag.strip(), p.name,
                                          p.grade_label or "inside the interval used here"))
        elif p.kind == "categorical" and printed and not any(c.isdigit() for c in printed) \
                and _norm_text(printed) != _norm_text(p.category or p.raw.raw_value):
            entry = ("lab_expected_text",
                     "%s was reported as \"%s\"; the report prints \"%s\" as the expected "
                     "result." % (p.name, str(p.raw.raw_value).strip(), printed))
        if entry:
            out.append({
                "parameter_id": pid, "name": p.name, "profile": p.profile, "kind": p.kind,
                "value": p.value if p.value is not None else (p.status or p.category or p.raw.raw_value),
                "unit": p.unit, "reference_text": printed or _ref_text(p),
                "lab_flag": p.raw.raw_flag, "grade_label": p.grade_label,
                "finding_basis": entry[0], "statement": entry[1], "abnormal": False,
                "severity_score": 0.0, "linked": [], "standalone": True, "in_lab_range": True,
                "derived": False,
            })
    out.sort(key=lambda f: (f["finding_basis"], f["name"]))
    return out
