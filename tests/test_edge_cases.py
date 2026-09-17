"""Independent edge cases - generic behaviour, no real report involved.

    python tests/test_edge_cases.py

Every input here is invented. Each test states the behaviour it guards; the ones marked
REGRESSION failed before the discrepancy audit that added them.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.config import get_config          # noqa: E402
from engine.normalize import parse_numeric     # noqa: E402
from engine.pipeline import Pipeline           # noqa: E402

CFG = get_config()
PIPE = Pipeline(CFG)
FORMATS = ROOT / "tests" / "fixtures" / "pdf_formats"
RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((bool(condition), name, detail))


def T(name, value, unit=None, rng=None, flag=None):
    d = {"test_name": name, "value": value}
    if unit is not None:
        d["unit"] = unit
    if rng is not None:
        d["reference_range"] = rng
    if flag is not None:
        d["flag"] = flag
    return d


def run(tests, sex="Male", age="45", **extra):
    patient = {"age": age}
    if sex:
        patient["sex"] = sex
    payload = {"patient": patient, "tests": tests}
    payload.update(extra)
    return PIPE.run(payload, "edge.json")


def P(r, pid):
    return next((p for p in r.get("parameters", []) if p["parameter_id"] == pid), None)


def ids(r, key):
    return [f["parameter_id"] for f in r.get(key, [])]


def noted(r, basis=None):
    return [f for f in r.get("lab_noted_findings", []) if basis is None or f["finding_basis"] == basis]


def urgent(r):
    return [x for x in r.get("recommendations", []) if x["priority"] == "urgent"]


def no_patterns_message(r):
    return any("No abnormal patterns were detected" in x["text"] for x in r.get("recommendations", []))


# ================================================================ the 20 generic cases

def test_01_empty_report():
    r = run([])
    check("01 empty report is refused as not a report", r["document"]["status"] != "ok", r["document"])
    check("01   and shows no parameters or conditions",
          not r.get("parameters") and not r.get("disease_risks"))


def test_02_one_normal_test():
    r = run([T("Hemoglobin", "14.2", "g/dL", "13 - 17")])
    check("02 one normal test: nothing abnormal", not r["abnormal_findings"] and not r["disease_risks"])
    check("02   the no-abnormality baseline step is given", no_patterns_message(r))


def test_03_one_abnormal_test():
    r = run([T("SGPT (ALT)", "95", "U/L", "0 - 41")])
    check("03 one abnormal test is an abnormal finding against the lab range",
          ids(r, "abnormal_findings") == ["sgpt_alt"]
          and r["abnormal_findings"][0]["finding_basis"] == "lab_range", r["abnormal_findings"])
    check("03   and is named in a plan step",
          any("ALT" in x["text"] or "liver" in x["text"].lower() for x in r["recommendations"]))
    check("03   and the no-abnormality message is not given", not no_patterns_message(r))


def test_04_same_test_twice():
    r = run([T("Hemoglobin", "14", "g/dL", "13-17"), T("Haemoglobin", "14.0", "g/dL", "13-17")])
    d = [x for x in r["duplicates_resolved"] if x["parameter_id"] == "hemoglobin"]
    check("04 same result twice: one parameter, no conflict", d and not d[0]["conflicting_values"], d)
    r = run([T("Hemoglobin", "14", "g/dL", "13-17"), T("Hemoglobin", "9", "g/dL", "13-17")])
    d = [x for x in r["duplicates_resolved"] if x["parameter_id"] == "hemoglobin"]
    check("04 same test, different results: the conflict is recorded", d and d[0]["conflicting_values"], d)
    # REGRESSION: the abnormal 9 g/dL used to survive only as a note in the results table
    c = noted(r, "conflicting_reading")
    check("04   REGRESSION the abnormal reading that was not kept is listed", c and "9" in c[0]["statement"], c)
    check("04   REGRESSION and a plan step points at it",
          any(x["trace"] == "lab_noted" for x in r["recommendations"]) and not no_patterns_message(r))


def test_05_missing_unit():
    r = run([T("Hemoglobin", "14", None, "13-17")])
    p = P(r, "hemoglobin")
    check("05 missing unit on the canonical scale: graded against the report range",
          p and p["reference_source"] == "report" and not p["abnormal"], p)
    # REGRESSION: "150" with "150-450" was taken as 150 /uL - a thrombocytopenia pattern
    r = run([T("Platelet Count", "150", None, "150 - 450")])
    p = P(r, "platelet_count")
    check("05   REGRESSION missing unit on another scale: compared only with the report's interval",
          p and not p["abnormal"] and not r["threshold_findings"] and p["reference_source"] == "report"
          and any("different scale" in n for n in p["notes"]), p)
    check("05   REGRESSION and no thrombocytopenia condition is suggested",
          not any("Thrombocytopenia" in d["name"] for d in r["disease_risks"]), r["disease_risks"])


def test_06_missing_range():
    r = run([T("Hemoglobin", "14.5", "g/dL")])
    p = P(r, "hemoglobin")
    check("06 missing range: the configured interval is used and says so",
          p and p["reference_source"].startswith("dictionary"), p)


def test_07_missing_sex():
    r = run([T("Creatinine", "1.25", "mg/dL")], sex=None)
    p = P(r, "creatinine")
    check("07 missing sex: the widened interval is used and labelled",
          p and "sex unknown" in p["reference_source"], p)
    check("07   and the plan says sex was not recorded",
          any("Sex was not recorded" in x["text"] for x in r["recommendations"]))


def test_08_invalid_numeric_value():
    for raw in ("1O5", "14..5"):
        r = run([T("Hemoglobin", "14", "g/dL", "13-17"), T("Glucose Fasting", raw, "mg/dL", "70-99")])
        check("08 REGRESSION garbled value %r is not read as a smaller number" % raw,
              P(r, "fasting_glucose") is None, P(r, "fasting_glucose"))
        check("08   REGRESSION and is reported as unreadable",
              any(v["reported"] == raw for v in r["rejected_values"]), r["rejected_values"])
    check("08 scientific notation is still a number", parse_numeric("1e3") == (1000.0, None))
    check("08 a multiplier after the number is not garbling", parse_numeric("3.2x10^3")[0] == 3.2)


def test_09_date_looking_value():
    for raw in ("20/08/2025", "2025-08-20", "12:30", "12:30 Hrs"):
        check("09 REGRESSION %r is not a number" % raw, parse_numeric(raw) == (None, None), parse_numeric(raw))
    r = run([T("Hemoglobin", "14", "g/dL", "13-17"), T("TSH", "20/08/2025", "uIU/mL", "0.4-4")])
    check("09 REGRESSION a date in a TSH result cell gives no TSH", P(r, "tsh") is None, P(r, "tsh"))


def test_10_very_large_value():
    r = run([T("Glucose Fasting", "1e9", "mg/dL")])
    p = P(r, "fasting_glucose")
    check("10 an implausibly large value is kept but carries a unit warning",
          p and any("more than" in n and "check the unit" in n for n in p["notes"]), p)


def test_11_negative_where_impossible():
    r = run([T("Hemoglobin", "-14", "g/dL", "13-17"), T("Sodium", "140", "mmol/L", "136-145")])
    check("11 a negative haemoglobin is rejected, not graded", P(r, "hemoglobin") is None, P(r, "hemoglobin"))
    check("11   and the rejection is reported",
          any(v["parameter"].lower().startswith("h") and "negative" in v["reason"] for v in r["rejected_values"]),
          r["rejected_values"])


def test_12_text_results():
    r = run([T("HBsAg", "Non-Reactive"), T("Anti HCV", "REACTIVE"), T("Hemoglobin", "14", "g/dL", "13-17")])
    check("12 Non-Reactive is negative", (P(r, "hbsag") or {}).get("status") == "negative")
    check("12 REACTIVE is positive and abnormal",
          (P(r, "anti_hcv") or {}).get("status") == "positive" and "anti_hcv" in ids(r, "abnormal_findings"))
    r = run([T("Sodium", "--", "mmol/L", "136-145"), T("Hemoglobin", "14", "g/dL", "13-17")])
    check("12 REGRESSION a dash placeholder is not a negative sodium", P(r, "sodium") is None, P(r, "sodium"))


def test_13_multiple_hiv_components():
    base = [T("Hemoglobin", "14", "g/dL", "13-17")]
    cases = (("NON REACTIVE", "NON REACTIVE", "negative"), ("NON REACTIVE", "REACTIVE", "positive"),
             ("REACTIVE", "NON REACTIVE", "positive"), ("NON REACTIVE", "Equivocal", "indeterminate"),
             ("Equivocal", "NON REACTIVE", "indeterminate"))
    for one, two, want in cases:
        r = run(base + [T("HIV-1 ANTIBODIES", one), T("HIV-2 ANTIBODIES", two)])
        got = (P(r, "hiv_screen") or {}).get("status")
        check("13 HIV-1 %s + HIV-2 %s -> screen %s" % (one, two, want), got == want, got)
        vetoed = any("HIV" in str(s) for s in r.get("suppressed_findings", []))
        if want != "negative":
            check("13   REGRESSION no HIV exclusion from a screen that is not wholly negative", not vetoed)
    r = run(base + [T("HIV-1 ANTIBODIES", "NON REACTIVE"), T("HIV-2 ANTIBODIES", "Equivocal")])
    check("13 REGRESSION the equivocal screen is listed", noted(r, "equivocal"), r["lab_noted_findings"])
    r = run(base + [T("HIV-1 ANTIBODIES", "NON REACTIVE"), T("HIV-2 ANTIBODIES", "Sample hemolysed")])
    check("13 REGRESSION an unreadable component is reported, not silently dropped",
          any(v["reported"] == "Sample hemolysed" for v in r["rejected_values"]), r["rejected_values"])


def test_14_scan_only_pdf():
    r = PIPE.run((FORMATS / "scanned.pdf").read_bytes(), "scanned.pdf")
    check("14 a scan-only PDF is refused as unreadable", r["document"]["status"] == "unreadable", r["document"])
    check("14   with no guessed result", not r.get("parameters"))


def _adversarial():
    return PIPE.run((FORMATS / "adversarial_layout.pdf").read_bytes(), "adversarial_layout.pdf")


def test_15_pdf_with_repeated_headers():
    r = _adversarial()
    check("15 repeated header: age read as the patient's age", r["patient"]["age"] == 36.0, r["patient"])
    got = {p["parameter_id"] for p in r["parameters"]}
    for leaked in ("troponin_i", "potassium", "creatinine"):
        check("15 no %s result from headers, footers, dates or IDs" % leaked, leaked not in got, sorted(got))
    d = [x for x in r["duplicates_resolved"] if x["parameter_id"] == "hemoglobin"]
    check("15 a test repeated on the next page is one result with no conflict",
          d and not d[0]["conflicting_values"], d)
    check("15 the Indian-grouped count and lakh unit read correctly",
          (P(r, "wbc_count") or {}).get("value") == 7800.0 and (P(r, "platelet_count") or {}).get("value") == 120000.0)


def test_16_result_inside_interpretation_text():
    r = _adversarial()
    hba1c = P(r, "hba1c")
    check("16 the HbA1c is the table's 5.9, not the 6.5 in the interpretation paragraph",
          hba1c and hba1c["value"] == 5.9, hba1c)
    fg = P(r, "fasting_glucose")
    check("16 the fasting glucose is 92, not the 126 in the paragraph", fg and fg["value"] == 92.0, fg)
    tsh = P(r, "tsh")
    check("16 REGRESSION TSH printed in mIU/mL beside 0.40-4.00 is not converted to 2100",
          tsh and tsh["value"] == 2.1 and not tsh["abnormal"], tsh)
    check("16 REGRESSION the PDF keeps the equivocal HIV-2 row: screen not negative",
          (P(r, "hiv_screen") or {}).get("status") == "indeterminate", P(r, "hiv_screen"))
    check("16 REGRESSION 'LDL/HDL' 3.9 is not an LDL of 3.9", (P(r, "ldl_cholesterol") or {}).get("value") != 3.9,
          P(r, "ldl_cholesterol"))
    check("16 REGRESSION 'Albumin/Globulin' 1.6 is not an albumin of 1.6", P(r, "albumin") is None, P(r, "albumin"))


def test_17_direct_abnormality_without_disease_master_mapping():
    r = run([T("MCHC", "37", "g/dL", "31.5 - 34.5")])
    f = [x for x in r["abnormal_findings"] if x["parameter_id"] == "mchc"]
    check("17 an abnormality no condition uses is still an abnormal finding", f and f[0]["standalone"], f)
    check("17   and still gets a plan step", any("MCHC" in x["text"] for x in r["recommendations"]))
    r = run([T("Hemoglobin", "14", "g/dL", "13-17"), T("Zonulin", "95", "ng/mL", "0 - 40", "H")])
    check("17 REGRESSION a test not in the dictionary that the report flags is listed",
          noted(r, "not_in_dictionary"), r["lab_noted_findings"])
    check("17 REGRESSION and the plan does not say no abnormality was found", not no_patterns_message(r))
    r = run([T("Hemoglobin", "14", "g/dL", "13-17"), T("HIV-1 p24 antigen", "Reactive")])
    check("17 REGRESSION an unrecognised test reported Reactive is listed",
          noted(r, "not_in_dictionary") and not no_patterns_message(r), r["lab_noted_findings"])
    r = run([T("Hemoglobin", "14", "g/dL", "13-17"), T("Widal", "Non Reactive")])
    check("17 an unrecognised negative result is not listed as marked", not noted(r, "not_in_dictionary"))


def _risk(r, name):
    return next((d for d in r["disease_risks"] if d["name"] == name), None)


def test_18_pattern_with_missing_supporting_markers():
    missing = _risk(run([T("TSH", "7.5", "uIU/mL", "0.4-4.0")], sex="Female"), "Hypothyroidism")
    low = _risk(run([T("TSH", "7.5", "uIU/mL", "0.4-4.0"), T("Free T4", "0.6", "ng/dL", "0.9-1.7")],
                    sex="Female"), "Hypothyroidism")
    check("18 missing free T4 is not evidence against: no damping applied",
          missing and all(c["support_penalty"] == 1.0 for c in missing["contributions"]), missing)
    check("18   but coverage is lower than with free T4 measured",
          missing and low and missing["data_coverage"] < low["data_coverage"])
    check("18   and low free T4 raises the score", missing and low and low["score"] > missing["score"])


def test_19_pattern_with_normal_supporting_markers():
    normal = _risk(run([T("TSH", "7.5", "uIU/mL", "0.4-4.0"), T("Free T4", "1.2", "ng/dL", "0.9-1.7")],
                       sex="Female"), "Hypothyroidism")
    low = _risk(run([T("TSH", "7.5", "uIU/mL", "0.4-4.0"), T("Free T4", "0.6", "ng/dL", "0.9-1.7")],
                    sex="Female"), "Hypothyroidism")
    check("19 a measured normal free T4 damps the pattern and says why",
          normal and any(c["support_penalty"] < 1.0 and c["support_note"] for c in normal["contributions"]), normal)
    check("19   the normal result scores below the abnormal one", normal and low and normal["score"] < low["score"])
    r = run([T("TSH", "7.5", "uIU/mL", "0.4-4.0"), T("Free T4", "1.2", "ng/dL", "0.9-1.7")], sex="Female")
    check("19   and the normal measurement itself is not deleted", (P(r, "free_t4") or {}).get("value") == 1.2)


def test_20_conflicting_lab_flag_vs_interpretation():
    r = run([T("Hemoglobin", "15", "g/dL", "13-17", "L"), T("TSH", "6.0", "uIU/mL", "0.4-4.0", "N")])
    check("20 a printed 'L' on an in-range value is listed as a laboratory flag",
          any(f["parameter_id"] == "hemoglobin" for f in noted(r, "lab_flag")), r["lab_noted_findings"])
    tsh = P(r, "tsh")
    check("20 a printed 'N' on an out-of-range value stays abnormal, with the disagreement noted",
          tsh and tsh["abnormal"] and any("flags this as normal" in n for n in tsh["notes"]), tsh)


# ================================================================ audit regressions

def test_ratio_names_are_never_one_of_their_analytes():
    for name, value in (("Cholesterol/HDL", "6.2"), ("Total Cholesterol / HDL", "5.6"), ("LDL/HDL", "4.1"),
                        ("Albumin/Globulin", "1.5"), ("BUN/Creatinine", "20"), ("Iron/TIBC", "0.3"),
                        ("Calcium/Phosphorus", "2.1")):
        r = run([T("Hemoglobin", "14", "g/dL", "13-17"), T(name, value)])
        wrong = [p["parameter_id"] for p in r["parameters"]
                 if p["parameter_id"] != "hemoglobin" and not p["derived"] and "ratio" not in p["parameter_id"]]
        check("REGRESSION '%s' %s is not filed as a single analyte" % (name, value), not wrong, wrong)
    for name, pid in (("SGOT/AST", "sgot_ast"), ("PCV/HCT", "hematocrit"), ("Glucose Fasting / FBS", "fasting_glucose"),
                      ("Vitamin D/B12/Folate", "vitamin_d"), ("TG/HDL", "tg_hdl_ratio")):
        check("  dual naming '%s' still resolves" % name, CFG.resolve_alias(name) == pid, CFG.resolve_alias(name))


def test_identifier_and_date_fields_are_never_results():
    extra = {"report": {"troponin_id": "20", "potassium_phone": "9.1", "hba1c_barcode": "5.5",
                        "glucoseReportNumber": "300", "creatinine_lab_code": "4.5", "tsh_accession": "8.2",
                        "ldl_sample_no": "150", "troponin_slip_date": "20/08/2025"},
             "collected": "20/08/2025", "received_at": "2026-02-20 12:32:16"}
    r = run([T("Hemoglobin", "14", "g/dL", "13-17")], **extra)
    got = sorted(p["parameter_id"] for p in r["parameters"])
    check("REGRESSION identifier and date fields naming analytes give no results", got == ["hemoglobin"], got)
    check("REGRESSION so no urgent step and no hyperkalaemia", not urgent(r)
          and not any("Potassium" in d["name"] for d in r["disease_risks"]))
    listed = {f.get("raw_name") for f in r["document_fields_skipped"]}
    check("  they are still listed as document fields", {"troponin_id", "potassium_phone"} <= listed, listed)
    r = run([T("Hemoglobin", "14", "g/dL", "13-17")], report={"hemoglobin": "9.5"})
    check("  a loose field that IS a result name is still read", (P(r, "hemoglobin") or {}).get("value") in (14.0, 9.5))


def test_unit_conversion_is_applied_only_where_it_fits():
    r = run([T("Glucose Fasting", "5.2", "mmol/L", "3.9-5.5"), T("Creatinine", "88", "umol/L", "62-106"),
             T("Hemoglobin", "140", "g/L", "130-170"), T("Vitamin D", "50", "nmol/L", "75-250"),
             T("Troponin I", "17", "ng/L"), T("CRP", "1.2", "mg/dL")])
    exp = {"fasting_glucose": 93.68, "creatinine": 0.99, "hemoglobin": 14.0, "vitamin_d": 20.03,
           "troponin_i": 0.017, "crp": 12.0}
    for pid, want in exp.items():
        p = P(r, pid)
        check("conversion %s -> %s" % (pid, want), p and abs(p["value"] - want) < 0.02 * max(1, want), p)
    r = run([T("TSH", "2.5", "mIU/mL", "0.4-4.0")])
    tsh = P(r, "tsh")
    check("REGRESSION TSH 2.5 mIU/mL with interval 0.4-4.0 is not converted to 2500",
          tsh and tsh["value"] == 2.5 and not tsh["abnormal"] and not r["threshold_findings"], tsh)
    r = run([T("TSH", "2.5", "mIU/mL")])
    check("REGRESSION and without an interval it is not graded at all rather than called markedly high",
          not r["abnormal_findings"] and not r["disease_risks"], r["abnormal_findings"])


def test_report_range_formats_are_used():
    r = run([T("Platelet Count", "2,50,000", "/cumm", "1,50,000-4,10,000")])
    p = P(r, "platelet_count")
    check("REGRESSION Indian-grouped interval is the report's, not the dictionary's",
          p and p["reference_source"] == "report" and p["reference_high"] == 410000.0, p)
    r = run([T("Hemoglobin", "13,5", "g/dL", "13-17")])
    check("REGRESSION a decimal comma is a decimal", (P(r, "hemoglobin") or {}).get("value") == 13.5)
    r = run([T("Ferritin", "10", "ng/mL", ">=20"), T("HDL", "35", "mg/dL", ">40"), T("Triglycerides", "180", "mg/dL", "<150")])
    for pid in ("ferritin", "hdl_cholesterol", "triglycerides"):
        p = P(r, pid)
        check("open-ended printed range used for %s" % pid, p and p["reference_source"] == "report" and p["abnormal"], p)


def test_a_value_at_the_printed_limit_is_not_called_normal_by_the_report():
    r = run([T("HbA1c", "5.7", "%", "<5.7: Non-diabetes\n5.7-6.4: Prediabetes")])
    p = P(r, "hba1c")
    check("REGRESSION HbA1c 5.7 at '<5.7' is not described as normal by the report",
          p and not any("would call this normal" in n for n in p["notes"]), p and p["notes"])
    f = [x for x in r["abnormal_findings"] if x["parameter_id"] == "hba1c"]
    check("REGRESSION the finding says it is at the printed limit",
          f and "exactly at a limit" in f[0]["statement"], f and f[0]["statement"])


def test_exclusion_needs_a_consistent_negative():
    liver = [T("ALT", "180", "U/L", "0-40"), T("AST", "150", "U/L", "0-40"), T("Total Bilirubin", "2.5", "mg/dL", "0.2-1.2")]

    def vetoed(r):
        return any("Hepatitis B" in str(s) for s in r.get("suppressed_findings", []))
    check("negative HBsAg vetoes hepatitis B", vetoed(run(liver + [T("HBsAg", "Non Reactive")])))
    check("positive HBsAg does not", not vetoed(run(liver + [T("HBsAg", "Reactive")])))
    check("equivocal HBsAg does not", not vetoed(run(liver + [T("HBsAg", "Equivocal")])))
    check("missing HBsAg does not", not vetoed(run(liver)))
    check("unreadable HBsAg does not", not vetoed(run(liver + [T("HBsAg", "Insufficient sample")])))
    r = run(liver + [T("HBsAg", "Non Reactive"), T("HBsAg", "Reactive")])
    check("REGRESSION conflicting HBsAg results do not veto", not vetoed(r), r.get("suppressed_findings"))
    r = run(liver + [T("HBsAg", "Non Reactive")])
    check("a veto does not hide the liver results themselves",
          {"sgpt_alt", "sgot_ast", "total_bilirubin"} <= set(ids(r, "abnormal_findings")))


# ---------------------------------------------------------------- run

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
            print("FAIL  %s   %s" % (name, str(detail)[:600]))
    print("-" * 70)
    print("%d checks, %d passed, %d failed" % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
