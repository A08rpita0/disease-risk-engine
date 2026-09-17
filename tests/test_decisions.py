"""Regression tests for the approved clinical/product decisions (#19 Rule A, #20, #21, #22,
#23, #24, #25, #26, #27, #28, #29).

    python tests/test_decisions.py

Every input is invented; nothing here refers to a real report.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.config import get_config                       # noqa: E402
from engine.extract import extract_with_text                # noqa: E402
from engine.normalize import Normalizer, clean_number       # noqa: E402
from engine.pipeline import Pipeline                        # noqa: E402

CFG = get_config()
PIPE = Pipeline(CFG)
RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((bool(condition), name, detail))


def T(name, value, unit=None, rng=None, flag=None, **extra):
    d = {"test_name": name, "value": value}
    if unit is not None:
        d["unit"] = unit
    if rng is not None:
        d["reference_range"] = rng
    if flag is not None:
        d["flag"] = flag
    d.update(extra)
    return d


def run(tests, sex="Male", age="50", **patient_extra):
    patient = {"sex": sex, "age": age} if sex else {"age": age}
    patient.update(patient_extra)
    return PIPE.run({"patient": patient, "tests": tests}, "decisions.json")


def P(r, pid):
    return next((p for p in r.get("parameters", []) if p["parameter_id"] == pid), None)


def risk(r, name):
    return next((d for d in r.get("disease_risks", []) if d["name"] == name), None)


def normalized(tests, sex="male"):
    obs, ctx, warnings, _ = extract_with_text({"patient": {"sex": sex}, "tests": tests}, "t.json",
                                              CFG.resolve_alias)
    return PIPE.normalizer.build(obs, ctx, warnings)


CAD = "Cardiovascular / Coronary Artery Disease"
AD = "Atherogenic Dyslipidemia"


# ================================================================ #19 Rule A

def test_rule_a_calculated_value_is_not_an_independent_trigger():
    r = run([T("Triglycerides", "262", "mg/dL", "<150"), T("HDL Cholesterol", "61", "mg/dL", ">40")])
    check("#19 high TG with NORMAL HDL: the TG/HDL ratio does not make a second trigger",
          risk(r, AD) is None, [d["name"] for d in r["disease_risks"]])
    check("#19   triglycerides stay an abnormal finding",
          any(f["parameter_id"] == "triglycerides" for f in r["abnormal_findings"]))
    r = run([T("Triglycerides", "262", "mg/dL", "<150"), T("HDL Cholesterol", "35", "mg/dL", ">40")])
    check("#19 high TG with LOW HDL: two independent triggers, the pattern is reported",
          risk(r, AD) is not None, [d["name"] for d in r["disease_risks"]])
    r = run([T("Triglycerides", "140", "mg/dL", "<150"), T("HDL Cholesterol", "38", "mg/dL", ">40")])
    check("#19 normal TG with low HDL: the ratio, raised only by the low HDL, is not a second trigger",
          risk(r, AD) is None, [d["name"] for d in r["disease_risks"]])
    r = run([T("Iron", "45", "ug/dL", "33-193"), T("TIBC", "300", "ug/dL", "250-450")])
    hit = next((c for c in r["cohorts"] if c["cohort_id"] == "iron_deficiency"), None)
    sigs = (hit or {}).get("confidence_breakdown", {}).get("signals", [])
    check("#19 a calculated value whose inputs did NOT fire still counts at full weight (TSAT 15%)",
          hit and any(s["parameter"] == "Transferrin Saturation" and not s["redundancy"] for s in sigs), sigs)


def test_rule_a_discounts_a_calculated_supporting_value_and_says_why():
    r = run([T("Total Cholesterol", "260", "mg/dL", "<200"), T("HDL Cholesterol", "45", "mg/dL", ">40"),
             T("Cholesterol/HDL Ratio", "5.8", None, "<4.5")])
    hit = next((c for c in r["cohorts"] if c["cohort_id"] == "hypercholesterolaemia"), None)
    sig = [s for s in (hit or {}).get("confidence_breakdown", {}).get("signals", [])
           if s["parameter"] == "Total Cholesterol / HDL Ratio"]
    check("#19 a ratio whose input already counts is discounted, with the reason recorded",
          sig and "calculated from Total Cholesterol" in (sig[0]["redundancy"] or ""), sig)


def test_rule_a_leaves_a_shared_redundancy_group_as_it_was():
    r = run([T("Apolipoprotein B", "142", "mg/dL", "66-133"), T("Apolipoprotein A1", "115", "mg/dL", "104-202"),
             T("Apo B/Apo A1 Ratio", "1.23", None, "0.35-1.0")])
    hit = next((c for c in r["cohorts"] if c["cohort_id"] == "apolipoprotein_imbalance"), None)
    sigs = (hit or {}).get("confidence_breakdown", {}).get("signals", [])
    check("#19 ApoB and ApoB/ApoA1 (same group) keep the existing group discount only",
          sigs and not any("calculated from" in (s["redundancy"] or "") for s in sigs), sigs)
    check("#19   and the ratio remains the full-weight signal",
          any(s["parameter"] == "ApoB / ApoA1 Ratio" and not s["redundancy"] for s in sigs), sigs)


# ================================================================ #20 HIV components

HIV_CASES = [
    ("neg + neg", [("HIV-1 ANTIBODIES", "NON REACTIVE"), ("HIV-2 ANTIBODIES", "NON REACTIVE")], "negative"),
    ("neg + pos", [("HIV-1 ANTIBODIES", "NON REACTIVE"), ("HIV-2 ANTIBODIES", "REACTIVE")], "positive"),
    ("neg + equivocal", [("HIV-1 ANTIBODIES", "NON REACTIVE"), ("HIV-2 ANTIBODIES", "Equivocal")], "indeterminate"),
    ("neg + missing", [("HIV-1 ANTIBODIES", "NON REACTIVE")], "incomplete"),
    ("neg + unreadable", [("HIV-1 ANTIBODIES", "NON REACTIVE"), ("HIV-2 ANTIBODIES", "Sample hemolysed")], "incomplete"),
    ("neg + pending", [("HIV-1 ANTIBODIES", "NON REACTIVE"), ("HIV-2 ANTIBODIES", "Result Pending")], "incomplete"),
    ("combined assay negative", [("HIV 1 & 2 Antibodies", "Non Reactive")], "negative"),
    ("unnumbered screen negative", [("HIV Antibody", "Non Reactive")], "negative"),
    ("HIV-2 only negative", [("HIV-2 ANTIBODY", "NON REACTIVE")], "incomplete"),
]


def _hiv_vetoed(tests):
    patient = normalized(tests)
    return any(v.rule_id.startswith("hiv") for v in PIPE.gatekeeper.evaluate(patient))


def test_hiv_component_state_model():
    base = [T("Hemoglobin", "14", "g/dL", "13-17")]
    for label, rows, want in HIV_CASES:
        tests = base + [T(n, v) for n, v in rows]
        r = run(tests)
        p = P(r, "hiv_screen")
        check("#20 %s -> %s" % (label, want), p and p["status"] == want, p and (p["status"], p["components"]))
        vetoed = _hiv_vetoed(tests)
        check("#20   %s: HIV exclusion only on a complete negative" % label,
              vetoed == (want == "negative"), "vetoed=%s" % vetoed)
        if want == "incomplete":
            check("#20   %s: never shown as negative, listed with a plan step" % label,
                  "Negative" not in (p["grade_label"] or "")
                  and any(f["finding_basis"] == "incomplete_screen" for f in r["lab_noted_findings"])
                  and any(x["trace"] == "lab_noted" for x in r["recommendations"]),
                  (p["grade_label"], r["lab_noted_findings"]))
    r = run(base)
    check("#20 both missing: no screen result and no exclusion",
          P(r, "hiv_screen") is None and not _hiv_vetoed(base))
    p = P(run(base + [T("HIV-1 ANTIBODIES", "NON REACTIVE"), T("HIV-2 ANTIBODIES", "Sample hemolysed")]), "hiv_screen")
    check("#20 unreadable component is named in the components", p and p["components"].get("HIV-2") == "unreadable", p)


# ================================================================ #21 presentation

def test_lipid_only_evidence_is_a_risk_signal_not_a_disease_name():
    r = run([T("LDL Cholesterol", "185", "mg/dL", "<100"), T("Total Cholesterol", "245", "mg/dL", "<200"),
             T("HDL Cholesterol", "47", "mg/dL", ">40")])
    d = risk(r, CAD)
    check("#21 lipid-only CAD link is presented as a cardiovascular risk signal",
          d and d["display_name"] == "Cardiovascular risk signal" and d["evidence_type"] == "risk_factor", d and d["display_name"])
    check("#21   the note says it is not evidence of the disease",
          d and "not evidence that the disease is present" in d["display_note"])
    check("#21   the Disease Master name is kept for audit", d and d["name"] == CAD)
    check("#21   plan steps are headed by the display name, never the disease name",
          any(x["finding"] == CAD for x in r["recommendations"])
          and all(x["finding_display"] != CAD for x in r["recommendations"]),
          sorted({(x["finding"], x["finding_display"]) for x in r["recommendations"]}))


def test_troponin_is_presented_as_possible_myocardial_injury_and_stays_urgent():
    r = run([T("Troponin I", "0.17", "ng/mL", "0.00-0.02")])
    d = risk(r, CAD)
    check("#21 troponin-led CAD link is 'Possible acute myocardial injury'",
          d and d["display_name"].startswith("Possible acute myocardial injury")
          and d["evidence_type"] == "process_marker", d and d["display_name"])
    check("#21   the urgent step is unchanged", any(x["priority"] == "urgent" for x in r["recommendations"]))
    r = run([T("Vitamin D", "8", "ng/mL", "30-100")])
    d = risk(r, "Vitamin D Deficiency")
    check("#21 a condition with no presentation tag keeps its Disease Master name",
          d and d["display_name"] == d["name"] and d["evidence_type"] is None, d and d["display_name"])


def test_presentation_changes_no_score():
    presentation = CFG.presentation
    try:
        CFG.presentation = {"presentations": {}}
        before = run([T("LDL Cholesterol", "185", "mg/dL", "<100"), T("Troponin I", "0.17", "ng/mL", "0.00-0.02")])
    finally:
        CFG.presentation = presentation
    after = run([T("LDL Cholesterol", "185", "mg/dL", "<100"), T("Troponin I", "0.17", "ng/mL", "0.00-0.02")])
    check("#21 presentation wording changes no score, level or tier",
          [(d["name"], d["score"], d["evidence_level"], d["presentation_tier"]) for d in before["disease_risks"]]
          == [(d["name"], d["score"], d["evidence_level"], d["presentation_tier"]) for d in after["disease_risks"]])


# ================================================================ #22 eGFR

def _egfr(unit, method=None, rng=None, creat=True):
    tests = [T("eGFR", "55", unit, rng, method=method)] if method else [T("eGFR", "55", unit, rng)]
    if creat:
        tests.append(T("Creatinine", "1.0", "mg/dL", "0.7-1.3"))
    return run(tests, age="70")


def test_egfr_is_staged_only_when_indexed():
    r = _egfr("mL/min/1.73m2", "CKD-EPI", ">=90")
    p = P(r, "egfr")
    check("#22 indexed CKD-EPI eGFR is staged", p and p["interpretable"] and p["grade_label"].startswith("G3a"), p)
    for label, unit, method, rng in (("Cockcroft-Gault mL/min", "mL/min", "Cockcroft-Gault", "60 - 300"),
                                     ("mL/min without a method", "mL/min", None, "60 - 300"),
                                     ("CKD-EPI printed in mL/min", "mL/min", "CKD-EPI", ">=90"),
                                     ("Cockcroft-Gault printed per 1.73m2", "mL/min/1.73m2", "Cockcroft Gault", None),
                                     ("creatinine clearance", "mL/min", "Creatinine clearance", None)):
        p = P(_egfr(unit, method, rng), "egfr")
        check("#22 %s: not staged, not used by clusters" % label,
              p and not p["interpretable"] and not (p["grade_label"] or "").startswith("G"), p)
        check("#22   %s: the reason is stated" % label,
              p and any("staging" in n for n in p["notes"]), p and p["notes"])
    p = P(_egfr("mL/min", "Cockcroft-Gault", "60 - 300"), "egfr")
    check("#22 an unindexed value is still graded against the report's own interval",
          p and p["reference_source"] == "report" and p["abnormal"], p)
    r = run([T("eGFR", "45", "mL/min", "60 - 300", method="Cockcroft-Gault")], age="70")
    check("#22 an unindexed eGFR below 60 fires no renal cluster",
          not any("renal" in c["cohort_id"] for c in r["cohorts"]), [c["cohort_id"] for c in r["cohorts"]])
    r = run([T("eGFR", "45", "mL/min/1.73m2", ">=90")], age="70")
    check("#22 an indexed eGFR below 60 still does", any("renal" in c["cohort_id"] for c in r["cohorts"]),
          [c["cohort_id"] for c in r["cohorts"]])


# ================================================================ #26 HDL printed band vs interpretation

def test_printed_band_and_flag_are_kept_apart_from_interpretation():
    r = run([T("HDL CHOLESTEROL", "61", "mg/dL", "Low: <40\nHigh >/=60", "High")])
    p = P(r, "hdl_cholesterol")
    check("#26 HDL 61 is not abnormal", p and not p["abnormal"])
    check("#26   the printed band it falls in is recorded", p and p["printed_band"] == "High >=60", p and p["printed_band"])
    check("#26   the lab-range status is not determinable (no normal band printed)",
          p and p["lab_range_status"] == "not_determinable", p and p["lab_range_status"])
    check("#26   the flag is described as the band's name, not as an abnormality",
          p and any("band name" in n for n in p["notes"]) and not any("flags this as abnormal" in n for n in p["notes"]),
          p and p["notes"])
    noted = [f for f in r["lab_noted_findings"] if f["parameter_id"] == "hdl_cholesterol"]
    check("#26   and it is still listed as marked by the laboratory",
          noted and "name of its printed band" in noted[0]["statement"], noted)
    p = P(run([T("HDL", "38", "mg/dL", "Low: <40\nHigh >/=60")]), "hdl_cholesterol")
    check("#26 HDL 38: guideline finding, printed band 'Low' recorded",
          p and p["abnormal"] and p["finding_basis"] == "decision_threshold" and p["printed_band"] == "Low: <40", p)
    p = P(run([T("HDL", "40", "mg/dL", "Low: <40\nHigh >/=60")]), "hdl_cholesterol")
    check("#26 HDL 40 is not in the strict 'Low: <40' band", p and p["printed_band"] is None, p and p["printed_band"])
    p = P(run([T("Hemoglobin", "15", "g/dL", "13-17", "L")]), "hemoglobin")
    check("#26 a flag with no band is described as a printed flag, not an abnormality",
          p and any("prints the flag 'L'" in n for n in p["notes"]) and p["lab_range_status"] == "within", p)


# ================================================================ #24 / #27 vocabulary states

def test_result_states():
    n = Normalizer(CFG)
    for raw, status, state in (("Normal", "normal", "normal_text"), ("Negative", "negative", "negative"),
                               ("Non Reactive, 0.26", "negative", "negative"), ("Reactive", "positive", "positive"),
                               ("Weakly Reactive", "indeterminate", "weak_positive"),
                               ("Weak positive", "indeterminate", "weak_positive"),
                               ("Trace", "indeterminate", "trace"), ("Equivocal", "indeterminate", "equivocal"),
                               ("Borderline Reactive", "indeterminate", "equivocal"),
                               ("Not Detected", "negative", "negative"), ("Detected", "positive", "positive")):
        got = n._qual_state(raw)
        check("#24 %r -> %s / %s" % (raw, status, state), got == (status, state), got)
    check("#24 per-test grading is only applied where a parameter declares it",
          n._qual_state("Trace", pdef={"state_grading": {"trace": "positive"}}) == ("positive", "trace"))
    declared = [p["id"] for p in CFG.parameters if p.get("state_grading")]
    check("#24 no parameter has an invented state grading", not declared, declared)


def test_urobilinogen_normal_is_normal_not_negative():
    r = run([T("Urobilinogen", "Normal"), T("Hemoglobin", "14", "g/dL", "13-17")])
    p = P(r, "urine_urobilinogen")
    check("#27 'Normal' urobilinogen is status normal, not negative", p and p["status"] == "normal", p)
    check("#27   its label keeps the printed word", p and '"Normal"' in p["grade_label"] and "not detected" not in p["grade_label"], p)
    tests = [T("HBsAg", "Normal"), T("ALT", "180", "U/L", "0-40"), T("AST", "150", "U/L", "0-40")]
    vetoed = any(v.rule_id.startswith("hepatitis_b") for v in PIPE.gatekeeper.evaluate(normalized(tests)))
    check("#27 a result written 'Normal' never excludes a condition", not vetoed)


def test_weak_positive_and_trace_are_listed_not_negative():
    r = run([T("VDRL", "Weakly Reactive"), T("Urine Glucose", "Trace"), T("HBsAg", "Equivocal"),
             T("Hemoglobin", "14", "g/dL", "13-17")])
    kinds = {f["parameter_id"]: f["finding_basis"] for f in r["lab_noted_findings"]}
    check("#24 weak positive, trace and equivocal are listed as their own states",
          kinds.get("vdrl_rpr") == "weak_positive" and kinds.get("urine_glucose") == "trace"
          and kinds.get("hbsag") == "equivocal", kinds)
    check("#24   none of them is negative", all(P(r, pid)["status"] != "negative" for pid in ("vdrl_rpr", "urine_glucose", "hbsag")))


# ================================================================ #25 MinValue / MaxValue

def test_limit_fields():
    def one(t):
        r = PIPE.run({"patient": {"sex": "Male"}, "tests": [t]}, "fields.json")
        return r["parameters"][0], r["warnings"]
    p, _ = one({"TestName": "ALT", "Result": "45", "Unit": "U/L", "MinValue": "0", "MaxValue": "40"})
    check("#25 a valid MinValue/MaxValue pair is the report's interval",
          p["reference_source"] == "report" and p["reference_high"] == 40.0 and p["abnormal"], p)
    p, _ = one({"TestName": "Platelet Count", "Result": "90", "Unit": "10^3/uL", "MinValue": "150", "MaxValue": "450"})
    check("#25 a valid pair in a converted unit is still used", p["reference_source"] == "report"
          and p["reference_low"] == 150000.0, p)
    for label, lo, hi in (("placeholder 999", "0", "999"), ("both zero", "0", "0"), ("low above high", "145", "135"),
                          ("equal limits", "5", "5")):
        p, w = one({"TestName": "Sodium", "Result": "150", "Unit": "mmol/L", "MinValue": lo, "MaxValue": hi})
        check("#25 %s is not used as a reference interval" % label,
              p["reference_source"].startswith("dictionary") and any("not used as a reference interval" in x for x in w),
              (p["reference_source"], w))
    p, _ = one({"TestName": "Sodium", "Result": "150", "Unit": "mmol/L", "MinValue": "0", "MaxValue": "50000"})
    check("#25 limit fields on another scale are not used", p["reference_source"].startswith("dictionary")
          and any("different scale" in n for n in p["notes"]), p)
    p, _ = one({"test_name": "Sodium", "value": "150", "unit": "mmol/L", "reference_range": "0 - 999"})
    check("#25 printed reference TEXT is never second-guessed by the field checks", p["reference_source"] == "report", p)


# ================================================================ #23 data quality

def test_suspicious_values():
    r = run([T("Hemoglobin", "1400", "g/dL", "13-17"), T("Hematocrit", "45", "%", "40-50")])
    p = P(r, "hemoglobin")
    check("#23 an extreme value is kept, visible and marked suspicious",
          p and p["data_quality"] == "suspicious" and any(f["parameter_id"] == "hemoglobin" for f in r["abnormal_findings"]), p)
    supported = [d for d in r["disease_risks"] if d["presentation_tier"] != "insufficient"]
    check("#23   no supported pattern rests on it alone", not supported, [(d["name"], d["presentation_tier"]) for d in supported])
    check("#23   conditions resting on it carry the reason",
          all(d["unconfirmed_reason"] for d in r["disease_risks"]), [d["name"] for d in r["disease_risks"]])
    step = [x for x in r["recommendations"] if x["trace"] == "data_quality"]
    check("#23   the plan asks the laboratory to confirm it without delaying urgent steps",
          step and "do not delay any urgent step" in step[0]["text"], step)

    r = run([T("Troponin I", "20", "ng/mL", "0-0.04")])
    d = risk(r, CAD)
    check("#23 an extreme troponin keeps its level and its urgent step",
          d and d["evidence_level"] != "Limited" and any(x["priority"] == "urgent" for x in r["recommendations"]), d)
    r = run([T("Hemoglobin", "14", "g/dL", "13-17"), T("Hemoglobin", "9", "g/dL", "13-17")])
    check("#23 conflicting duplicate results are marked suspicious", P(r, "hemoglobin")["data_quality"] == "suspicious")
    r = run([T("Hemoglobin", "14", "g/dL", "13-17")])
    check("#23 an ordinary result is valid", P(r, "hemoglobin")["data_quality"] == "valid")


def test_uninterpretable_values_and_limit_hook():
    r = run([T("Hemoglobin", "-14", "g/dL", "13-17"), T("Sodium", "140", "mmol/L", "136-145")])
    check("#23 an impossible value is listed as could-not-be-interpreted with a plan step",
          any(f["finding_basis"] == "uninterpretable" for f in r["lab_noted_findings"])
          and any(x["trace"] == "lab_noted" for x in r["recommendations"]), r["lab_noted_findings"])
    configured = [p["id"] for p in CFG.parameters if p.get("plausible_limits")]
    check("#23 no physiological limit has been configured", not configured, configured)
    n = Normalizer(CFG)
    pdef = dict(CFG.param_by_id["hemoglobin"])
    pdef["plausible_limits"] = {"max": 30, "source": "test citation"}
    check("#23 the limit hook rejects a value only with a cited source",
          n._implausible(pdef, 1400.0, "male") and "test citation" in n._implausible(pdef, 1400.0, "male"))
    pdef["plausible_limits"] = {"max": 30}
    check("#23   and ignores an uncited limit", n._implausible(pdef, 1400.0, "male") is None)


# ================================================================ #28 conditional ranges

def test_smoking_dependent_ranges():
    rng = "Nonsmokers: < 3.0\nSmokers: < 5.0"
    grid = {("2.35", None): (False, None), ("2.35", "yes"): (False, None), ("2.35", "no"): (False, None),
            ("4.0", None): (False, "conditional"), ("4.0", "yes"): (False, None), ("4.0", "no"): (True, None),
            ("6.0", None): (True, None), ("6.0", "yes"): (True, None), ("6.0", "no"): (True, None),
            ("3.0", None): (False, "conditional"), ("5.0", None): (True, None)}
    for (value, smoking), (abnormal, grade) in grid.items():
        extra = {"smoking_status": smoking} if smoking else {}
        r = run([T("CEA", value, "ng/mL", rng)], **extra)
        p = P(r, "cea")
        ok = p and p["abnormal"] == abnormal and (grade is None or p["grade"] == grade)
        check("#28 CEA %s, smoking %s -> abnormal=%s%s" % (value, smoking, abnormal, " (conditional)" if grade else ""),
              ok, p and (p["abnormal"], p["grade"], p["reference_high"]))
        if grade == "conditional":
            check("#28   conditional result is listed with a plan step and no interval",
                  any(f["finding_basis"] == "conditional_range" for f in r["lab_noted_findings"])
                  and p["reference_low"] is None and p["reference_high"] is None, r["lab_noted_findings"])
    obs, ctx, _, _ = extract_with_text({"patient": {"smoking_status": "former"}, "tests": [T("CEA", "4", "ng/mL", rng)]},
                                       "t.json", CFG.resolve_alias)
    check("#28 an unclear smoking status stays unknown", ctx.smoking is None)
    p = P(run([T("TSH", "2.0", "uIU/mL", "0.3-4.5\nPregnant women: First trimester: 0.25-4.33")]), "tsh")
    check("#28 bands beside a general interval do not make it conditional",
          p and p["grade"] != "conditional" and p["reference_high"] == 4.5, p)


# ================================================================ #29 formatting

def test_no_floating_point_noise_in_computed_numbers():
    r = run([T("Platelet Count", "2.16", "lakh/cumm", "1.50 - 4.10")])
    p = P(r, "platelet_count")
    check("#29 converted interval limits carry no binary noise",
          p and p["reference_low"] == 150000.0 and p["reference_high"] == 410000.0
          and repr(p["reference_high"]) == "410000.0", p and (p["reference_low"], p["reference_high"]))
    check("#29 clean_number keeps real precision", clean_number(0.1 + 0.2) == 0.3 and clean_number(1.23456789) == 1.23456789)
    check("#29 a conversion note shows a plain number, not an exponent",
          p and p["conversion_note"] and "e+" not in p["conversion_note"] and "216,000" in p["conversion_note"],
          p and p["conversion_note"])
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    check("#29 the UI number format does not depend on the browser's locale", 'var NUMBER_LOCALE = "en-US"' in js)
    check("#29 the UI drops a label that repeats the value or badge", "function repeats(" in js)


# ---------------------------------------------------------------- guard rails

def test_no_disease_master_or_threshold_file_is_touched_by_these_decisions():
    import subprocess
    try:
        out = subprocess.run(["git", "diff", "--name-only", "HEAD~0", "--", "config/disease_master.json"],
                             capture_output=True, text=True, cwd=str(ROOT)).stdout.strip()
    except Exception:
        out = ""
    check("Disease Master unchanged in the working tree", out == "", out)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        try:
            t()
        except Exception as exc:
            import traceback
            check("%s raised %s" % (t.__name__, type(exc).__name__), False, traceback.format_exc(limit=4))
    failed = [r for r in RESULTS if not r[0]]
    for ok, name, detail in RESULTS:
        if not ok:
            print("FAIL  %s   %s" % (name, str(detail)[:700]))
    print("-" * 70)
    print("%d checks, %d passed, %d failed" % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
