"""Gold-standard tests: PDF and JSON must agree, and nothing abnormal may go missing.

Run:  python tests/test_extraction.py        (no pytest required)

Covers, end to end:
  - the value-anchored row parser on the line shapes real PDFs produce
  - the report-name corpus (must resolve / must not resolve)
  - JSON vs every PDF layout in tests/fixtures/pdf: same parameters, values, flags,
    bases, abnormal findings and conditions
  - hs-CRP: read, recognised, listed as a finding with no rule behind it, one-way
    stand-in for CRP, no leukocytosis from CRP alone, a traceable plan step
  - HbA1c: thresholds, direct routes, no double coverage penalty, IFCC units
  - units the dictionary cannot convert are never compared with canonical thresholds
  - veto logic still holds on a PDF-shaped input; missing is not negative
  - configuration validation catches ambiguous aliases and undocumented rules
  - the Disease Master audit has no unreviewed flags
  - the API, when a local server is running

No expected value in this file comes from one patient's report, and no fix in the
engine keys on a value, a file name or a person.
"""
from __future__ import annotations

import copy
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from engine.config import get_config                          # noqa: E402
from engine.extract import extract_free_text                  # noqa: E402
from engine.layout import Row, parse_rows, drop_repeated, split_side_by_side  # noqa: E402
from engine.pipeline import Pipeline                          # noqa: E402
from lab_panel import PANEL, as_json, expected                # noqa: E402
from name_corpus import POSITIVE, NEGATIVE                    # noqa: E402

CFG = get_config()
PIPE = Pipeline(CFG)
RESOLVE = CFG.resolve_alias
RESULTS = []
PDF_DIR = ROOT / "tests" / "fixtures" / "pdf"


def check(name, condition, detail=""):
    RESULTS.append((bool(condition), name, detail))


def analyse(data, filename="t.json", **kw):
    return PIPE.run(data, filename, **kw)


def param(result, pid):
    for p in result.get("parameters", []):
        if p["parameter_id"] == pid:
            return p
    return None


def line(text):
    obs, _ = extract_free_text(text, resolver=RESOLVE)
    return obs[0] if obs else None


# ================================================================ row parser

def test_single_spaced_result_lines():
    """The exact shape pdfplumber emits: one space between columns, range unbracketed.
    The old parser read the range's upper bound as the result on every one of these."""
    cases = [
        ("HIGHLY SENSITIVE C-REACTIVE PROTEIN (hs-CRP) 31.98 mg/L < 1.0",
         "31.98", "mg/L", "< 1.0", None, "hs_crp"),
        ("HbA1c (Glycosylated Haemoglobin) 7.8 % 4.0 - 5.6", "7.8", "%", "4.0 - 5.6", None, "hba1c"),
        ("Glycated Haemoglobin (HbA1c) 6.9 H % 4.0-5.6", "6.9", "%", "4.0-5.6", "H", "hba1c"),
        ("Haemoglobin 11.2 g/dL 13.0 - 17.0", "11.2", "g/dL", "13.0 - 17.0", None, "hemoglobin"),
        ("TSH 9.4 uIU/mL 0.27 - 4.2", "9.4", "uIU/mL", "0.27 - 4.2", None, "tsh"),
        ("Vitamin D (25-Hydroxy) 11.6 ng/mL 30 - 100 L", "11.6", "ng/mL", "30 - 100", "L", "vitamin_d"),
        ("Vitamin B12 143 pg/mL 197-771", "143", "pg/mL", "197-771", None, "vitamin_b12"),
        ("Total Leucocyte Count (TLC) 11,800 /cumm 4,000 - 10,000 H",
         "11,800", "/cumm", "4,000 - 10,000", "H", "wbc_count"),
        ("Haemoglobin 11.2 g/dL (13.0-17.0)", "11.2", "g/dL", "13.0-17.0", None, "hemoglobin"),
        ("Haemoglobin    11.2    g/dL    13.0 - 17.0", "11.2", "g/dL", "13.0 - 17.0", None, "hemoglobin"),
        ("ESR 38 mm/1st hr 0-15 H", "38", "mm/1st hr", "0-15", "H", "esr"),
        ("Platelet Count 2.4 lakh/cumm 1.5 - 4.1", "2.4", "lakh/cumm", "1.5 - 4.1", None, "platelet_count"),
    ]
    for text, value, unit, rng, flag, pid in cases:
        o = line(text)
        check("parsed: %s" % text[:48], o is not None, "no observation")
        if o is None:
            continue
        check("  value %s (not the range bound)" % value, o.raw_value == value, repr(o.raw_value))
        check("  unit %s" % unit, o.raw_unit == unit, repr(o.raw_unit))
        check("  range %s" % rng, o.raw_range == rng, repr(o.raw_range))
        check("  flag %s" % flag, o.raw_flag == flag, repr(o.raw_flag))
        check("  resolves to %s" % pid, RESOLVE(o.raw_name) == pid, repr(RESOLVE(o.raw_name)))


def test_a_name_with_a_number_in_it():
    """"CA 125 34.5" - both "CA" (calcium) and "CA 125" resolve; the longer, different
    test is the right reading. "CA 19-9" must keep its hyphenated number."""
    for text, value, pid in [("CA 125 34.5 U/mL 0 - 35", "34.5", "ca_125"),
                             ("CA 19-9 12.1 U/mL 0 - 37", "12.1", "ca_19_9"),
                             ("T4 (Thyroxine) 5.1 µg/dL 5.1 - 14.1", "5.1", "total_t4")]:
        o = line(text)
        check("%s -> %s = %s" % (text[:22], pid, value),
              o is not None and o.raw_value == value and RESOLVE(o.raw_name) == pid,
              "got %r" % (((o.raw_name, o.raw_value, RESOLVE(o.raw_name)) if o else None),))


def test_qualitative_results_are_never_split_into_a_false_positive():
    """"HBsAg Non Reactive" read as the result "Reactive" is a false positive."""
    for text, value, pid in [("HBsAg Non Reactive Non Reactive", "Non Reactive", "hbsag"),
                             ("HBsAg Reactive Non Reactive", "Reactive", "hbsag"),
                             ("Anti HCV Non Reactive", "Non Reactive", "anti_hcv"),
                             ("HIV I & II Antibody Not Detected", "Not Detected", "hiv_screen"),
                             ("Urine Glucose Positive Negative", "Positive", "urine_glucose"),
                             ("Urine Protein Negative Negative", "Negative", "urine_protein"),
                             ("Urine Sugar Trace Nil", "Trace", "urine_glucose")]:
        o = line(text)
        check("%s -> %s" % (text, value),
              o is not None and o.raw_value == value and RESOLVE(o.raw_name) == pid,
              "got %r" % (((o.raw_name, o.raw_value) if o else None),))


def test_prose_and_captions_are_not_results():
    for text in ["Payment within 30 days of invoice",
                 "Albumin (3.7) and TSH (5.35)  6 parameters improved with no",
                 "K/μL Increased by 2.5 K/μL",
                 "Low: <3.97 Normal: 3.97 - 4.94",
                 "Range 40-75 (%) 20 - 60 (%) < 10",
                 "Patient Name : Mr Test Age 52",
                 "Your Health Summary Normal Borderline Abnormal"]:
        o = line(text)
        check("not a result: %s" % text[:40],
              o is None or RESOLVE(o.raw_name) is None,
              "read as %r" % (((o.raw_name, o.raw_value) if o else None),))


def test_results_that_span_two_lines():
    rows = [Row(cells=[(40.0, "HIGHLY SENSITIVE C-REACTIVE PROTEIN")], top=100.0, page=1),
            Row(cells=[(40.0, "(hs-CRP)"), (300.0, "18.40 H"), (370.0, "mg/L")], top=111.0, page=1),
            Row(cells=[(430.0, "< 1.0")], top=122.0, page=1)]
    out = parse_rows(rows, RESOLVE)
    check("a wrapped name and a range on the next line make one result", len(out) == 1,
          str([(o.name, o.value, o.range) for o in out]))
    if out:
        check("  the name is joined and recognised", RESOLVE(out[0].name) == "hs_crp", out[0].name)
        check("  the range below is attached", out[0].range == "< 1.0", repr(out[0].range))
        check("  the inline flag is kept", out[0].flag == "H", repr(out[0].flag))

    # A caption far below, or in another column, is not the reference interval.
    rows = [Row(cells=[(40.0, "Haemoglobin (Hb): 14")], top=100.0, page=1),
            Row(cells=[(40.0, "<13")], top=160.0, page=1)]
    out = parse_rows(rows, RESOLVE)
    check("a distant caption is not attached as a range",
          out and out[0].range is None, str([(o.name, o.range) for o in out]))


def test_two_results_side_by_side():
    row = Row(cells=[(40.0, "Haemoglobin (Hb): 15"), (300.0, "RBC Count: 4.5 M/uL")],
              top=100.0, page=1)
    parts = split_side_by_side(row, RESOLVE)
    out = parse_rows([row], RESOLVE)
    check("a row with two results is split", len(parts) == 2, str([p.text for p in parts]))
    got = {RESOLVE(o.name): o.value for o in out}
    check("  both are read", got.get("hemoglobin") == "15" and got.get("rbc_count") == "4.5",
          str(got))
    check("  the first does not take the second's name as its unit",
          all(o.unit != "RBC" for o in out), str([(o.name, o.unit) for o in out]))


def test_running_headers_are_dropped_but_repeated_results_are_kept():
    def page(n, extra):
        return [Row(cells=[(40.0, "CITY DIAGNOSTIC LABORATORY")], top=10.0, page=n),
                Row(cells=[(40.0, "Page %d of 3" % n)], top=800.0, page=n),
                Row(cells=[(40.0, "Haemoglobin (Hb):"), (300.0, extra)], top=100.0, page=n),
                Row(cells=[(430.0, "13.0 - 17.0")], top=150.0, page=n)]
    kept = drop_repeated([page(1, "14"), page(2, "15"), page(3, "15")], RESOLVE)
    texts = [r.text for pg in kept for r in pg]
    check("a running header is dropped", not any("CITY DIAGNOSTIC" in t for t in texts))
    check("a page footer is dropped", not any(t.startswith("Page") for t in texts))
    check("a result repeated on every visit page is kept",
          sum("Haemoglobin" in t for t in texts) == 3, str(texts))
    check("a range printed alone on each page is kept",
          sum(t == "13.0 - 17.0" for t in texts) == 3, str(texts))


# ================================================================ names

def test_report_name_corpus():
    for name, pid in POSITIVE:
        check("resolves: %r -> %s" % (name, pid), RESOLVE(name) == pid, "got %s" % RESOLVE(name))
    for name, wrong in NEGATIVE:
        check("does NOT resolve: %r -> %s" % (name, wrong), RESOLVE(name) != wrong)


def test_short_aliases_are_not_reached_by_trimming_or_splitting():
    for name, wrong in [("K/μL Increased by", "potassium"), ("Total Cholesterol:HDL", "total_cholesterol"),
                        ("Apolipoprotein", "apo_b"), ("Apo", "apo_b"), ("Influenza B", "influenza")]:
        check("%r is not %s" % (name, wrong), RESOLVE(name) != wrong, str(RESOLVE(name)))
    for name, pid in [("K", "potassium"), ("Na", "sodium"), ("PSA (Prostate-Specific Antigen", "psa_total"),
                      ("HbA1c (Glycosylated", "hba1c"), ("SGOT/AST", "sgot_ast")]:
        check("%r still -> %s" % (name, pid), RESOLVE(name) == pid, str(RESOLVE(name)))


# ================================================================ PDF == JSON

def _layouts():
    return sorted(PDF_DIR.glob("*.pdf"))


def test_every_pdf_layout_reads_what_the_json_reads():
    check("fixture PDFs are present", len(_layouts()) >= 4, str(_layouts()))
    ref = analyse(as_json(), "panel.json")
    exp = expected()
    ref_abn = {f["parameter_id"] for f in ref["abnormal_findings"]}
    ref_risks = {(d["name"], d["presentation_tier"]) for d in ref["disease_risks"]}
    for pid, (value, abnormal) in exp.items():
        p = param(ref, pid)
        check("JSON reads %s" % pid, p is not None)

    for pdf in _layouts():
        r = analyse(pdf.read_bytes(), pdf.name)
        for pid, (value, abnormal) in exp.items():
            p, q = param(r, pid), param(ref, pid)
            check("[%s] %s is read" % (pdf.stem, pid), p is not None)
            if p is None or q is None:
                continue
            check("[%s] %s value matches JSON" % (pdf.stem, pid),
                  p["value"] == q["value"] and p.get("status") == q.get("status"),
                  "pdf %r vs json %r" % (p["value"], q["value"]))
            check("[%s] %s abnormal flag matches JSON" % (pdf.stem, pid),
                  p["abnormal"] == q["abnormal"], "%s vs %s" % (p["abnormal"], q["abnormal"]))
            check("[%s] %s basis matches JSON" % (pdf.stem, pid),
                  p["finding_basis"] == q["finding_basis"],
                  "%s vs %s" % (p["finding_basis"], q["finding_basis"]))
        abn = {f["parameter_id"] for f in r["abnormal_findings"]}
        check("[%s] abnormal findings match JSON" % pdf.stem, abn == ref_abn,
              "missing %s extra %s" % (sorted(ref_abn - abn), sorted(abn - ref_abn)))
        risks = {(d["name"], d["presentation_tier"]) for d in r["disease_risks"]}
        check("[%s] conditions and tiers match JSON" % pdf.stem, risks == ref_risks,
              "missing %s extra %s" % (sorted(ref_risks - risks), sorted(risks - ref_risks)))


def test_recall_every_abnormal_result_is_shown():
    sys.path.insert(0, str(ROOT / "tools"))
    from recall_audit import run
    _res, summary = run(verbose=False)
    for source, s in summary.items():
        check("[%s] every value read" % source, s["all_read"] == s["total"],
              "%d/%d" % (s["all_read"], s["total"]))
        check("[%s] every abnormal result shown" % source,
              s["abnormal_shown"] == s["abnormal_total"],
              "%d/%d" % (s["abnormal_shown"], s["abnormal_total"]))


# ================================================================ hs-CRP

def test_hs_crp_is_never_silently_dropped():
    text_report = "\n".join([
        "Patient Name : Test Patient   Age / Sex : 45 Y / Male",
        "HIGHLY SENSITIVE C-REACTIVE PROTEIN (hs-CRP) 31.98 mg/L < 1.0",
        "Haemoglobin 14.1 g/dL 13.0 - 17.0",
    ])
    for label, data, fname in [
            ("text", text_report.encode(), "report.txt"),
            ("json", {"gender": "male", "tests": [
                {"test_name": "HIGHLY SENSITIVE C-REACTIVE PROTEIN (hs-CRP)", "value": "31.98",
                 "unit": "mg/L", "reference_range": "< 1.0"},
                {"test_name": "Haemoglobin", "value": "14.1", "unit": "g/dL",
                 "reference_range": "13.0 - 17.0"}]}, "report.json")]:
        r = analyse(data, fname)
        p = param(r, "hs_crp")
        check("[%s] hs-CRP is read" % label, p is not None)
        if not p:
            continue
        check("[%s] value 31.98 mg/L" % label, p["value"] == 31.98 and p["unit"] == "mg/L",
              "%s %s" % (p["value"], p["unit"]))
        check("[%s] flagged against the report's interval" % label,
              p["abnormal"] and p["reference_source"] == "report", str(p["reference_source"]))
        f = [x for x in r["abnormal_findings"] if x["parameter_id"] == "hs_crp"]
        check("[%s] listed as an abnormal laboratory finding" % label, bool(f))
        if f:
            check("[%s] with a neutral statement" % label,
                  "reference interval" in f[0]["statement"], f[0]["statement"])
        citing = [x for x in r["recommendations"]
                  if any(v.get("parameter_id") == "hs_crp" for v in x["values"])]
        check("[%s] at least one plan step quotes it" % label, bool(citing))
        presented = [d["name"] for d in r["disease_risks"]
                     if d["presentation_tier"] in ("direct", "derived", "pattern")]
        check("[%s] no condition is presented as a finding from it" % label,
              not presented, str(presented))

    lone = analyse({"gender": "male", "tests": [
        {"test_name": "hs-CRP", "value": 31.98, "unit": "mg/L", "reference_range": "< 1.0"}]})
    f = [x for x in lone["abnormal_findings"] if x["parameter_id"] == "hs_crp"]
    check("a lone hs-CRP is listed with no rule behind it", f and f[0]["standalone"] is True)
    check("  no condition at all is inferred from it", not lone["disease_risks"],
          str([d["name"] for d in lone["disease_risks"]]))
    mixed = analyse({"gender": "male", "tests": [
        {"test_name": "hs-CRP", "value": 31.98, "unit": "mg/L", "reference_range": "< 1.0"},
        {"test_name": "Haemoglobin", "value": 14.1, "unit": "g/dL", "reference_range": "13 - 17"}]})
    titles = {x["finding"] for x in mixed["recommendations"]
              if any(v.get("parameter_id") == "hs_crp" for v in x["values"])}
    unsupported = {d["name"] for d in mixed["disease_risks"] if d["presentation_tier"] == "insufficient"}
    check("  a step about hs-CRP is never titled with an unsupported condition",
          not (titles & unsupported), str(titles & unsupported))
    check("  and it still has a high-priority step",
          any(x["priority"] == "high" and any(v.get("parameter_id") == "hs_crp" for v in x["values"])
              for x in mixed["recommendations"]))
    steps = [x for x in lone["recommendations"] if x["trace"] == "lab_finding"]
    check("  the plan's step is traceable to the result",
          steps and "hs_crp" in steps[0]["trace_detail"] and steps[0]["priority"] == "high",
          str([(x["trace"], x["trace_detail"], x["priority"]) for x in steps]))


def test_every_abnormal_result_is_cited_by_the_plan():
    for pdf in sorted(PDF_DIR.glob("*.pdf")) + [None]:
        r = analyse(as_json(), "panel.json") if pdf is None else analyse(pdf.read_bytes(), pdf.name)
        cited = {v.get("parameter_id") for x in r["recommendations"] for v in x["values"]}
        missing = [f["parameter_id"] for f in r["abnormal_findings"] if f["parameter_id"] not in cited]
        check("[%s] every abnormal result appears in a plan step" % (pdf.stem if pdf else "json"),
              not missing, str(missing))
        cited_high = {v.get("parameter_id") for x in r["recommendations"] for v in x["values"]
                      if x["priority"] in ("urgent", "high")}
        weak = [f["parameter_id"] for f in r["abnormal_findings"]
                if f["severity_score"] >= 0.75 and f["parameter_id"] not in cited_high]
        check("[%s] every marked abnormality has a high-priority step" % (pdf.stem if pdf else "json"),
              not weak, str(weak))
        for x in r["recommendations"]:
            if x["trace"] != "cohort_action":
                continue
            unsupported = x["because"].startswith("your results partly match")
            if unsupported:
                check("[%s] an unsupported pattern gives only confirmation steps (%s)" % (
                    pdf.stem if pdf else "json", x["category"]),
                      x["category"] in ("Urgent", "Consultation", "Testing"))
        known = {"urgency", "disease_guidance", "cohort_action", "parameter_action",
                 "coverage_gap", "record_context", "baseline", "general", "lab_finding"}
        check("[%s] every step is traceable" % (pdf.stem if pdf else "json"),
              all(x["trace"] in known and x["trace_detail"] for x in r["recommendations"]))


def test_hs_crp_stands_in_for_crp_one_way_only():
    base = [{"test_name": "Total WBC Count", "value": 7200, "unit": "/cumm",
             "reference_range": "4000 - 11000"}]
    r = analyse({"gender": "male", "tests": base + [
        {"test_name": "hs-CRP", "value": 18.0, "unit": "mg/L", "reference_range": "< 1.0"}]})
    names = {c["name"] for c in r["cohorts"]}
    check("hs-CRP 18 reaches the CRP inflammation rule", "Acute Inflammatory / Infective Response" in names,
          str(names))
    hits = [h for c in r["cohorts"] for h in c["hits"] if "crp" in h["parameter_id"]]
    check("  the evidence names the test actually measured",
          hits and all(h["parameter_id"] == "hs_crp" for h in hits), str([h["parameter_id"] for h in hits]))
    check("  a normal WBC does not become Reactive Leukocytosis",
          not any(d["name"] == "Reactive Leukocytosis" for d in r["disease_risks"]))
    check("  nor sepsis from CRP alone", not any(d["name"] == "Sepsis" for d in r["disease_risks"]))

    r2 = analyse({"gender": "male", "tests": [
        {"test_name": "CRP", "value": 4.0, "unit": "mg/L", "reference_range": "0 - 5"},
        {"test_name": "LDL Cholesterol", "value": 150, "unit": "mg/dL"}]})
    check("a standard CRP does not stand in for hs-CRP's cardiovascular band",
          "Vascular Inflammatory Risk" not in {c["name"] for c in r2["cohorts"]})

    r3 = analyse({"gender": "male", "tests": base})
    check("with neither CRP nor hs-CRP nothing is inferred",
          "Acute Inflammatory / Infective Response" not in {c["name"] for c in r3["cohorts"]})

    r4 = analyse({"gender": "male", "tests": [
        {"test_name": "hs-CRP", "value": 18.0, "unit": "mg/L"},
        {"test_name": "Total WBC Count", "value": 15500, "unit": "/cumm", "reference_range": "4000 - 11000"}]})
    check("a raised WBC with it does support Reactive Leukocytosis",
          any(d["name"] == "Reactive Leukocytosis" for d in r4["disease_risks"]))


# ================================================================ HbA1c

def _hba1c(value, unit="%", **extra):
    tests = [{"test_name": "HbA1c", "value": value, "unit": unit}]
    tests += extra.get("more", [])
    return analyse({"gender": "male", "age": 45, "tests": tests})


def test_hba1c_thresholds_and_direct_routes():
    rows = {}
    for v in (5.5, 5.9, 6.2, 6.8, 7.9, 9.5):
        r = _hba1c(v)
        g = {d["name"]: d for d in r["disease_risks"] if d["name"] in ("Prediabetes", "Diabetes Mellitus")}
        rows[v] = g
    check("5.5% raises no glycaemic condition", not rows[5.5], str(list(rows[5.5])))
    for v in (5.9, 6.2):
        d = rows[v].get("Prediabetes")
        check("%.1f%% -> Prediabetes" % v, d is not None, str(list(rows[v])))
        if d:
            check("  as a direct finding", d["presentation_tier"] == "direct", d["presentation_tier"])
            check("  not weakened to a weak signal by untested markers",
                  d["evidence_level"] in ("Moderate", "High"), d["evidence_level"])
            check("  no coverage cap applied to a direct finding",
                  d["score_breakdown"]["cap_applied"] is None and
                  d["score_breakdown"]["direct_exempt_from_coverage_cap"] is True)
            check("  the Disease Master criterion is quoted",
                  "5.7-6.4" in (d["direct_evidence"] or {}).get("threshold_source", ""))
    for v in (6.8, 7.9, 9.5):
        d = rows[v].get("Diabetes Mellitus")
        check("%.1f%% -> Diabetes Mellitus, direct" % v,
              d is not None and d["presentation_tier"] == "direct", str(list(rows[v])))
        check("%.1f%% -> not Prediabetes" % v, "Prediabetes" not in rows[v])
    rank = {"Limited": 0, "Low": 1, "Moderate": 2, "High": 3}
    lv = [rank[rows[v]["Diabetes Mellitus"]["evidence_level"]] for v in (6.8, 7.9, 9.5)]
    check("a higher HbA1c never gives a weaker diabetes signal", lv == sorted(lv), str(lv))

    fbg = analyse({"gender": "male", "tests": [{"test_name": "Glucose Fasting", "value": 112, "unit": "mg/dL"}]})
    d = [x for x in fbg["disease_risks"] if x["name"] == "Prediabetes"]
    check("fasting glucose 112 is the other direct route to Prediabetes",
          d and d[0]["presentation_tier"] == "direct")


def test_a_pattern_is_still_capped_for_thin_coverage():
    """The exemption is for DIRECT findings only; patterns keep the coverage cap."""
    r = analyse({"gender": "male", "tests": [
        {"test_name": "SGPT / ALT", "value": 140, "unit": "U/L", "reference_range": "0 - 41"}]})
    patterns = [d for d in r["disease_risks"] if d["presentation_tier"] != "direct"]
    check("thin-coverage patterns are still evaluated for the cap",
          all("direct_exempt_from_coverage_cap" in d["score_breakdown"] and
              d["score_breakdown"]["direct_exempt_from_coverage_cap"] is False for d in patterns))


def test_hba1c_in_ifcc_units_and_units_that_cannot_be_converted():
    r = _hba1c(48, "mmol/mol")
    p = param(r, "hba1c")
    check("48 mmol/mol converts to 6.5%", p and abs(p["value"] - 6.543) < 0.01, str(p and p["value"]))
    check("  and is graded in the diabetes range", p and p["grade_label"] == "Diabetes range")

    r = _hba1c(17.08, "mmol/")
    p = param(r, "hba1c")
    check("a truncated unit is shown but not interpreted",
          p and p["interpretable"] is False and p["abnormal"] is False, str(p and p["grade_label"]))
    check("  and raises no diabetes finding", not r["disease_risks"],
          str([d["name"] for d in r["disease_risks"]]))

    r = analyse({"gender": "male", "tests": [
        {"test_name": "HbA1c", "value": 17.08, "unit": "mmol/", "reference_range": "20 - 42"}]})
    p = param(r, "hba1c")
    check("an unconvertible unit is still graded against the report's own interval",
          p and p["abnormal"] and p["reference_source"] == "report" and p["direction"] == "low")
    check("  but never against a threshold in the canonical unit", not r["disease_risks"])


# ================================================================ veto

def test_veto_holds_on_pdf_shaped_input():
    txt = "\n".join(["SGPT (ALT) 120 U/L 0 - 41 H", "SGOT (AST) 110 U/L 0 - 40 H",
                     "HBsAg Non Reactive Non Reactive"])
    r = analyse(txt.encode(), "liver.txt")
    check("a non-reactive HBsAg read from text still vetoes Hepatitis B",
          "Hepatitis B" in {s.get("disease") for s in r["suppressed_findings"]})
    check("  and no Hepatitis B signal survives",
          not any(d["name"] == "Hepatitis B" for d in r["disease_risks"]))

    r2 = analyse("\n".join(["SGPT (ALT) 120 U/L 0 - 41 H", "SGOT (AST) 110 U/L 0 - 40 H"]).encode(),
                 "liver.txt")
    check("a missing HBsAg is not treated as negative",
          "Hepatitis B" not in {s.get("disease") for s in r2["suppressed_findings"]})


# ================================================================ configuration

def test_validation_rejects_ambiguous_or_undocumented_rules():
    cfg = copy.deepcopy(CFG)
    check("the shipped configuration validates", CFG.validate()["ok"], str(CFG.validate()["errors"][:3]))

    cfg.alias_collisions = {"leukocytes urine": {"a", "b"}}
    check("an alias claimed by two parameters is an error",
          any("claimed by more than one" in e for e in cfg.validate()["errors"]))

    cfg = copy.deepcopy(CFG)
    for p in cfg.parameters:
        if p.get("stands_in_for"):
            p["stands_in_for"] = {"parameter": p["stands_in_for"]["parameter"]}
    check("a stand-in without a documented basis is an error",
          any("stands_in_for has no documented basis" in e for e in cfg.validate()["errors"]))

    cfg = copy.deepcopy(CFG)
    for c in cfg.cohorts:
        for link in c.get("diseases", []):
            specs = link.get("direct_evidence")
            if isinstance(specs, list):
                for s in specs:
                    s.pop("source", None)
    check("a direct route without a recorded source is an error",
          any("direct_evidence has no source" in e for e in cfg.validate()["errors"]))


def test_disease_master_audit_has_no_open_flags():
    sys.path.insert(0, str(ROOT / "tools"))
    from dm_audit import audit
    rows = audit(CFG)
    open_rows = [r["condition"] for r in rows if r["review_status"] == "open"]
    check("every flagged Disease Master row has a recorded decision", not open_rows, str(open_rows))
    check("all 129 rows audited", len(rows) == len(CFG.diseases), str(len(rows)))


# ================================================================ API

def test_api_when_a_server_is_running():
    base = "http://127.0.0.1:8137"
    try:
        urllib.request.urlopen(base + "/api/health", timeout=2).read()
    except Exception:
        check("API test skipped - no local server on :8137", True)
        return

    def post(path, filename, data, ctype):
        boundary = "----gold%d" % len(data)
        body = (("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
                 "Content-Type: %s\r\n\r\n" % (boundary, filename, ctype)).encode()
                + data + ("\r\n--%s--\r\n" % boundary).encode())
        req = urllib.request.Request(base + path, data=body, method="POST",
                                     headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        return json.loads(urllib.request.urlopen(req, timeout=60).read())

    j = post("/api/analyse", "panel.json", json.dumps(as_json()).encode(), "application/json")
    p = post("/api/analyse", "text_columns.pdf", (PDF_DIR / "text_columns.pdf").read_bytes(),
             "application/pdf")
    for key in ("abnormal_findings", "threshold_findings", "direct_findings", "derived_findings",
                "pattern_findings", "insufficient_findings", "recommendations"):
        check("API response carries %s" % key, key in j and key in p)
    check("API: PDF and JSON give the same abnormal findings",
          {f["parameter_id"] for f in j["abnormal_findings"]} ==
          {f["parameter_id"] for f in p["abnormal_findings"]})


# ================================================================ two-part reports
# Every test below reproduces a defect found by running a real two-part laboratory
# report (designed summary pages, then the laboratory pages) through the engine and
# comparing it field by field with the same report transcribed to JSON. The report
# itself is personal health data and is not in the repository; tests/fixtures/
# summary_lab_report.py rebuilds its structure with invented values.

def _diff(pdf_bytes, truth, fname="report.pdf"):
    sys.path.insert(0, str(ROOT / "tools"))
    import tempfile
    from report_diff import diff
    with tempfile.TemporaryDirectory() as d:
        pdf = Path(d) / fname
        pdf.write_bytes(pdf_bytes)
        tj = Path(d) / "truth.json"
        tj.write_text(json.dumps(truth), encoding="utf-8")
        return diff(pdf, tj)[0]


def _count(res):
    return (len(res["context"]) + len(res["missing"]) + len(res["spurious"]) + len(res["field"])
            + len(res["abnormal_findings"]["only_json"]) + len(res["abnormal_findings"]["only_pdf"])
            + len(res["conditions"]["only_json"]) + len(res["conditions"]["only_pdf"]))


def test_two_part_report_pdf_equals_json_field_by_field():
    import summary_lab_report as S
    pdf = (ROOT / "tests" / "fixtures" / "pdf_summary" / "summary_then_lab.pdf").read_bytes()
    res = _diff(pdf, S.as_json())
    check("two-part report: PDF and JSON agree on every field", _count(res) == 0,
          json.dumps({k: res[k] for k in ("context", "missing", "spurious", "field")}, default=str)[:600])
    check("  no reading conflicts between summary and laboratory pages",
          not res["pdf_duplicate_conflicts"], str(res["pdf_duplicate_conflicts"])[:200])
    check("  nothing is left unrecognised on either side",
          res["summary"]["parameters_unmapped"] == {"json": 0, "pdf": 0}, str(res["summary"]))

    # Table detection is not guaranteed on every PDF. The text layer alone must still
    # read the same report without a single wrong value.
    import pdfplumber
    orig = pdfplumber.page.Page.extract_tables
    pdfplumber.page.Page.extract_tables = lambda self, *a, **k: []
    try:
        res_t = _diff(pdf, S.as_json())
    finally:
        pdfplumber.page.Page.extract_tables = orig
    check("two-part report, text layer only: PDF and JSON still agree", _count(res_t) == 0,
          json.dumps({k: res_t[k] for k in ("missing", "spurious", "field")}, default=str)[:600])


def test_patient_details_on_later_pages_are_found():
    from engine.extract import _context_from_text
    text = ("SMART HEALTH SUMMARY\nPatient ID Age\nTST0001 47\n" + "filler text line\n" * 600 +
            "Patient NAME : Mr Test Kumar\nDOB/Age/Gender : 47 Y/Male Report STATUS: Final\n"
            "Patient ID / UHID : TST0001/RCL99 Barcode NO : 1\n")
    ctx = _context_from_text(text, "r.pdf")
    check("name from a labelled line far into the document, title kept",
          ctx.name == "Mr Test Kumar", repr(ctx.name))
    check("age from 'DOB/Age/Gender : 47 Y/Male'", ctx.age == 47.0, repr(ctx.age))
    check("sex from the same line", ctx.sex == "male", repr(ctx.sex))
    check("the summary's stacked 'Patient ID / Age' is not read as an age of 142...",
          ctx.age != 142.0)


def test_multi_band_reference_text_yields_the_normal_band():
    from engine.extract import parse_reference_range
    for text, want in [("Deficient <20\nInsufficient 21 - 29\nSufficient 30 - 100", (30.0, 100.0)),
                       ("Normal Or High: >= 90\nMild Or Decrease: 60-89", (90.0, None)),
                       ("Desirable: < 200\nBorderline: 200-239\nHigh: >= 240", (None, 200.0)),
                       ("Low <1\nAverage 1-3\nHigh 3-10", (None, None)),
                       ("13.0 - 17.0", (13.0, 17.0))]:
        check("range %r -> %s" % (text.replace("\n", " / ")[:40], want),
              parse_reference_range(text) == want, str(parse_reference_range(text)))


def test_a_header_applies_only_to_the_table_right_after_it():
    from engine.extract import extract_table_rows
    header = {"name": 0, "value": 1, "unit": 2, "range": 3}
    results = [["Glycosylated Hemoglobin (HbA1c)\nHPLC", "6.3", "%", "<5.7"]]
    obs, _w, trailing = extract_table_rows(results, "pdf", header=header, resolver=RESOLVE)
    check("a header-less results grid is read with the header before it",
          obs and RESOLVE(obs[0].raw_name) == "hba1c" and obs[0].raw_value == "6.3")
    check("  the method line is split off the name",
          obs and obs[0].method == "HPLC" and obs[0].raw_name == "Glycosylated Hemoglobin (HbA1c)",
          str(obs and (obs[0].raw_name, obs[0].method)))

    box = [["Patient NAME : X", "", "", ""],
           ["Test Description", "Value(s)", "Unit(s)", "Reference Range"]]
    _o, _w, trailing = extract_table_rows(box, "pdf", resolver=RESOLVE)
    check("a table ending in its header hands that header on", trailing is not None)
    box_then_rows = box + [["TSH", "2.1", "uIU/mL", "0.35 - 4.94"]]
    _o, _w, trailing = extract_table_rows(box_then_rows, "pdf", resolver=RESOLVE)
    check("a table with results after its header does not", trailing is None)

    # Without a carried header, an interpretation grid is not read as results.
    interp = [["TSH", "T4", "T3", "Interpretation"], ["High", "Normal", "Normal", "Mild"]]
    obs, _w, _t = extract_table_rows(interp, "pdf", resolver=RESOLVE)
    check("an interpretation grid with no header of its own yields nothing", not obs,
          str([(o.raw_name, o.raw_value) for o in obs]))


def test_name_cells_wrapped_or_with_a_method_line():
    from engine.extract import _split_name_cell
    for cell, name, method in [
            ("HIGHLY SENSITIVE C-REACTIVE PROTEIN (hs-\nCRP)\nImmunoturbidimetric",
             "HIGHLY SENSITIVE C-REACTIVE PROTEIN (hs-CRP)", "Immunoturbidimetric"),
            ("Reaction (pH)\nDouble Indicator", "Reaction (pH)", "Double Indicator"),
            ("HIGHLY SENSITIVE C-REACTIVE\nPROTEIN (hs-CRP)", "HIGHLY SENSITIVE C-REACTIVE PROTEIN (hs-CRP)", None),
            ("Neutrophils.\nCalculated", "Neutrophils.", "Calculated")]:
        got = _split_name_cell(cell, RESOLVE)
        check("name cell %r" % cell.replace("\n", " / ")[:40], got == (name, method), str(got))
    check("'Reaction (pH)' is urine pH, never blood pH", RESOLVE("Reaction (pH)") == "urine_ph")


def test_values_with_a_number_in_the_name_or_a_value_column_swallowed():
    for text, value, pid in [
            ("Vitamin D 25 - Hydroxy: 9.9 ng/mL (Normal: 30–100 ng/mL)", "9.9", "vitamin_d"),
            ("Vitamin D 25 Hydroxy 9.9 ng/mL 30 - 100", "9.9", "vitamin_d"),
            ("SGOT/AST: 39 U/L (Normal: 11–34 U/L)", "39", "sgot_ast")]:
        o = line(text)
        check("%s -> %s" % (text[:36], value), o and o.raw_value == value and RESOLVE(o.raw_name) == pid,
              str(o and (o.raw_name, o.raw_value)))
    rows = [Row(cells=[(29.0, "eGFR (CKD-EPI)"), (283.0, "111.85"), (353.0, "ml/min/1.73 sq m"),
                       (441.0, "Normal Or High: >= 90")], top=273.0, page=19)]
    out = parse_rows(rows, RESOLVE)
    check("a band label column does not become the result",
          out and out[0].value == "111.85" and out[0].unit == "ml/min/1.73 sq m",
          str([(o.name, o.value, o.unit) for o in out]))


def test_a_name_cut_in_one_column_continues_below_in_that_column():
    rows = [Row(cells=[(33.0, "LDL Cholesterol: 108.2 mg/dL"), (261.0, "HIGH"),
                       (312.0, "HIGHLY SENSITIVE C-REACTIVE PROTEIN (hs-")], top=427.0, page=12),
            Row(cells=[(312.0, "CRP): 31.98"), (540.0, "HIGH")], top=442.0, page=12)]
    out = parse_rows(rows, RESOLVE)
    got = {RESOLVE(o.name): o.value for o in out}
    check("the continuation names hs-CRP, not a separate CRP", got.get("hs_crp") == "31.98"
          and "crp" not in got, str(got))


def test_microscopy_count_ranges_use_the_upper_bound():
    r = analyse({"gender": "male", "tests": [
        {"test_name": "Pus Cells (WBCs)", "value": "8-10", "unit": "/hpf", "reference_range": "0 - 5",
         "section": "Urine Routine"}]})
    p = param(r, "urine_pus_cells")
    check("'8-10 /hpf' against 0-5 is flagged", p and p["value"] == 10.0 and p["abnormal"],
          str(p and (p["value"], p["abnormal"])))
    o = line("Pus Cells (WBCs) 8-10 /hpf 0 - 5")
    check("  and read as a value from a text row, not as the range", o and o.raw_value == "8-10",
          str(o and (o.raw_name, o.raw_value, o.raw_range)))


def test_units_that_decide_which_test_a_name_is():
    r = analyse({"gender": "male", "tests": [
        {"test_name": "PCT", "value": "0.61", "unit": "%", "reference_range": "0.17 - 0.32"},
        {"test_name": "Neutrophils", "value": "61", "unit": "%", "reference_range": "40 - 80"},
        {"test_name": "Neutrophils.", "value": "4.53", "unit": "10^3/µl", "reference_range": "2 - 7"}]})
    check("PCT in % is plateletcrit", param(r, "plateletcrit") is not None)
    check("  never procalcitonin, whose sepsis rules fire at 0.5", param(r, "procalcitonin") is None)
    check("  so no infection pattern is raised from it",
          not any("Infective" in c["name"] or "Sepsis" in c["name"] for c in r["cohorts"]))
    check("'Neutrophils.' in 10^3/ul is the absolute count",
          (param(r, "absolute_neutrophil_count") or {}).get("value") == 4530.0)
    check("  and the percentage is kept separately, with no false conflict",
          (param(r, "neutrophil_pct") or {}).get("value") == 61.0 and not r["duplicates_resolved"])
    r2 = analyse({"gender": "male", "tests": [{"test_name": "PCT", "value": "0.8", "unit": "ng/mL"}]})
    check("PCT in ng/mL is still procalcitonin", param(r2, "procalcitonin") is not None)


def test_headings_give_bare_names_their_panel():
    r = analyse({"gender": "male", "tests": [
        {"section": "Urine Routine and Microscopic Examination", "test_name": "Blood", "value": "Negative"},
        {"section": "Urine Routine > Physical Examination", "test_name": "Colour", "value": "Pale yellow"},
        {"section": "Urine Routine > Microscopic Examination", "test_name": "Epithelial Cells",
         "value": "1-2", "unit": "/hpf", "reference_range": "0 - 4"},
        {"test_name": "ESR - Erythrocyte Sedimentation Rate", "value": "34", "unit": "mm/hr",
         "reference_range": "0 - 10"}]})
    check("'Blood' under a urine heading is urine blood", param(r, "urine_blood") is not None)
    check("'Colour' under a urine heading is urine colour", param(r, "urine_colour") is not None)
    check("a sub-heading does not erase the panel", param(r, "urine_epithelial_cells") is not None)
    check("a name stated twice either side of ' - ' resolves", param(r, "esr") is not None)
    r2 = analyse({"gender": "male", "tests": [{"test_name": "Blood", "value": "Negative"}]})
    check("'Blood' with no heading is not guessed", not r2.get("parameters"),
          str([p["parameter_id"] for p in r2.get("parameters", [])]))


def test_specimen_words_never_collapse_a_name():
    for name, wrong in [("Urine Routine and Microscopic Examination", "urine_blood"),
                        ("Urine Osmolality", "urine_blood"), ("Urine Culture", "urine_blood")]:
        check("%r is not %s" % (name, wrong), RESOLVE(name) != wrong, str(RESOLVE(name)))
    check("'Urine Blood' still resolves", RESOLVE("Urine Blood") == "urine_blood")
    check("'Blood Urea' still drops its specimen word", RESOLVE("Blood Urea") == "blood_urea")


def test_band_tables_and_panel_tiles_are_not_unrecognised_tests():
    for text in ["Very High Risk <50 <80", "Non diabetic adults >=18 years <5.7",
                 "Above Optimal 100-129 130 - 159", "Mineral Profile  1 / 1  mg/dL",
                 "Blood Counts  0  All", "LA1c --- 1.7 0.392 42029"]:
        o = line(text)
        check("not an unrecognised test: %r" % text[:30], o is None, str(o and (o.raw_name, o.raw_value)))


def test_no_control_characters_in_source():
    """A shell once rewrote the regex escape \\b as a literal backspace, silently disabling
    a rule. This fails loudly if it happens again."""
    bad = []
    for f in list((ROOT / "engine").glob("*.py")) + list((ROOT / "tools").glob("*.py")) + \
            list((ROOT / "tests").rglob("*.py")) + list((ROOT / "config").rglob("*.json")):
        for n, ln in enumerate(f.read_text(encoding="utf-8").split("\n"), 1):
            if any(ord(ch) < 32 and ch not in "\t\r" for ch in ln):
                bad.append("%s:%d" % (f.name, n))
    check("no control characters in engine, tools, tests or config", not bad, str(bad))


# ---------------------------------------------------------------- five-laboratory validation
#
# Each check below comes from a defect found by running real reports from four laboratories
# (and one scanned report) through both input paths. The PDFs used here are synthetic
# replicas of those layouts - tests/fixtures/format_reports.py lists the traits - with
# invented patients and values.

FORMATS = ROOT / "tests" / "fixtures" / "pdf_formats"


def _p(result, pid):
    return param(result, pid) or {}


def test_format_replicas_pdf_equals_json_field_by_field():
    import format_reports as F
    import pdfplumber
    for key in F.REPORTS:
        pdf = (FORMATS / ("%s.pdf" % key)).read_bytes()
        res = _diff(pdf, F.as_json(key), key + ".pdf")
        check("%s layout: PDF and JSON agree on every field" % key, _count(res) == 0,
              json.dumps({k: res[k] for k in ("context", "missing", "spurious", "field")}, default=str)[:600])
        check("  %s: nothing unrecognised on either side" % key,
              res["summary"]["parameters_unmapped"] == {"json": 0, "pdf": 0}, str(res["summary"]))
        orig = pdfplumber.page.Page.extract_tables
        pdfplumber.page.Page.extract_tables = lambda self, *a, **k: []
        try:
            res_t = _diff(pdf, F.as_json(key), key + ".pdf")
        finally:
            pdfplumber.page.Page.extract_tables = orig
        check("  %s: text layer alone still agrees" % key, _count(res_t) == 0,
              json.dumps({k: res_t[k] for k in ("missing", "spurious", "field")}, default=str)[:400])


def test_stacked_band_layout_reads_what_the_laboratory_printed():
    r = analyse((FORMATS / "stacked_bands.pdf").read_bytes(), "stacked_bands.pdf")
    ctx = r["patient"]
    check("header split only by column gaps: name without the next field's label",
          ctx["name"] == "Test Rao", repr(ctx["name"]))
    check("'Age / Gender : 41 Year(s)/ Male' -> 41, male; 'Page 1 of 3' is not an age",
          ctx["age"] == 41.0 and ctx["sex"] == "male", str(ctx))
    check("'PID No.' is the patient id", ctx["patient_id"] == "TP0000123", repr(ctx["patient_id"]))
    hdl = _p(r, "hdl_cholesterol")
    check("HDL 52 against '< 40 : Low / 40 - 60 : Optimal / > 60 : Desirable' is normal",
          hdl.get("abnormal") is False and hdl.get("reference_low") == 40.0, str(hdl.get("reference_low")))
    ldl = _p(r, "ldl_cholesterol")
    check("LDL 112: inside the lab's Normal+Desirable bands, flagged only by the guideline band",
          ldl.get("reference_high") == 129.0 and ldl.get("graded_by") == "decision_band", str(ldl.get("graded_by")))
    vd = _p(r, "vitamin_d")
    check("'Deficiency | : < 20' split across cells still yields the Optimum band 30-80",
          (vd.get("reference_low"), vd.get("reference_high")) == (30.0, 80.0) and vd.get("direction") == "low",
          str((vd.get("reference_low"), vd.get("reference_high"))))
    hb = _p(r, "hemoglobin")
    check("haemoglobin in 'gms%' is interpreted as g/dL", hb.get("interpretable") and hb.get("abnormal"),
          str(hb.get("unit")))
    for pid, want in (("rbc_count", 5.5), ("wbc_count", 6700.0), ("platelet_count", 233000.0)):
        q = _p(r, pid)
        check("%s: unit spelling folded, value %s" % (pid, want),
              q.get("value") == want and q.get("interpretable"), "%s %s" % (q.get("value"), q.get("unit")))
    check("'Non Reactive,0.26' is a negative HBsAg", _p(r, "hbsag").get("status") == "negative")
    check("'Blood group (ABO typing) | O' is read", _p(r, "blood_group_abo").get("category") == "o")
    check("'Remark' under an 'RBC Morphology' heading is the RBC morphology",
          _p(r, "rbc_morphology").get("category") == "normocytic normochromic")
    check("an 'Apolipoproteins B/A1' heading is not an ApoB of 1", not param(r, "apo_b"))
    check("the ApoB/A1 ratio itself is read", _p(r, "apo_b_apo_a1_ratio").get("value") == 1.23)
    lf = _p(r, "lh_fsh_ratio")
    check("LH/FSH ratio for a man: shown, not graded against the female PCOS criterion",
          lf.get("value") and lf.get("abnormal") is False and lf.get("grade") == "unknown", str(lf.get("grade")))
    urbc = _p(r, "urine_rbc_count")
    check("urine 'Red blood cells 12 /hpf' is the microscopy count, abnormal against 0-2",
          urbc.get("abnormal") and not param(r, "urine_blood"), str(urbc))
    for pid in ("urine_pathological_casts", "urine_uric_acid_crystals", "urine_bacteria", "urine_yeast_cells"):
        check("urine %s read (incl. rows continued on the next page without a heading)" % pid,
              bool(param(r, pid)))
    check("interpretation prose ('greater than 17 mg/dl') is not an unrecognised test",
          not r["unmapped_observations"], str([o["raw_name"] for o in r["unmapped_observations"]]))


def test_method_column_layout_reads_what_the_laboratory_printed():
    r = analyse((FORMATS / "method_column.pdf").read_bytes(), "method_column.pdf")
    ctx = r["patient"]
    check("'Patient Name : Ms. X   Request Date' stops at the column gap",
          ctx["name"] == "Ms. Test Devi" and ctx["age"] == 38.0 and ctx["sex"] == "female", str(ctx))
    check("'Patient No' is the patient id", ctx["patient_id"] == "TST0042", repr(ctx["patient_id"]))
    for pid, want in (("platelet_count", 285000.0), ("wbc_count", 6100.0), ("absolute_neutrophil_count", 3570.0)):
        q = _p(r, pid)
        check("'10^3 /uL' printed apart: %s = %s, not a count per uL" % (pid, want),
              q.get("value") == want and not q.get("abnormal"), "%s %s" % (q.get("value"), q.get("unit")))
    names = {d["name"] for d in r["disease_risks"]}
    check("no thrombocytopenia / leukaemia from a misread multiplier",
          not ({"Immune Thrombocytopenia (ITP)", "Leukemia (Suspected/Screening)"} & names), str(names))
    hct = _p(r, "hematocrit")
    check("'37 - 54  Calculation': the method column does not replace the range",
          (hct.get("reference_low"), hct.get("reference_high")) == (37.0, 54.0) and hct.get("abnormal"))
    ratio = _p(r, "tc_hdl_ratio")
    check("a ratio with '--' in the unit column: 'Calculation' is not its unit",
          ratio.get("unit") == "ratio" and ratio.get("abnormal") and not ratio.get("derived"), str(ratio.get("unit")))
    check("'Normal:4.6-5.6' fused label -> HbA1c 4.6-5.6",
          (_p(r, "hba1c").get("reference_low"), _p(r, "hba1c").get("reference_high")) == (4.6, 5.6))
    check("'Optimum>60' fused label -> HDL low limit 60", _p(r, "hdl_cholesterol").get("reference_low") == 60.0)
    ib = _p(r, "indirect_bilirubin")
    check("'< or = 0.90' parses, so indirect bilirubin 0.08 is not called low",
          ib.get("reference_high") == 0.9 and ib.get("abnormal") is False, str(ib))
    check("'Up to 1.2' parses", _p(r, "total_bilirubin").get("reference_high") == 1.2)
    ag = _p(r, "ag_ratio")
    check("'A/G RATIO' with no unit is read from the report, not recalculated",
          ag.get("derived") is False and ag.get("reference_low") == 0.8, str(ag))
    tt = _p(r, "troponin_t")
    check("troponin T is its own test, graded against the assay's printed limit",
          tt.get("abnormal") and tt.get("reference_high") == 0.014 and not param(r, "troponin_i"), str(tt))
    urgent = [x for x in r["recommendations"] if x["priority"] == "urgent"]
    check("a raised troponin makes the plan urgent and names the result",
          any(x["category"] == "Urgent" and "Troponin T" in x["text"] for x in urgent), str([x["text"][-90:] for x in urgent]))
    check("diet and lifestyle steps are never 'urgent'",
          not any(x["category"] in ("Diet", "Lifestyle", "Exercise") for x in urgent),
          str([(x["category"], x["text"][:40]) for x in urgent]))


def test_scanned_pages_are_reported_not_silently_empty():
    import os
    r = analyse((FORMATS / "scanned.pdf").read_bytes(), "scanned.pdf")
    check("a fully scanned PDF says it needs OCR", any("no extractable text layer" in w for w in r["warnings"]),
          str(r["warnings"]))
    check("  and is refused, with nothing analysed", r.get("analysed") is False and not r.get("parameters"))
    check("  with the message shown to the user",
          r["document"]["title"] == "This PDF appears to be scanned/image-based and could not be reliably read."
          and r["document"]["guidance"].startswith("Please upload a digital/text-based PDF or JSON")
          and "OCR" not in r["document"]["guidance"], str(r["document"]))
    r = analyse((FORMATS / "partly_scanned.pdf").read_bytes(), "partly_scanned.pdf")
    check("a partly scanned PDF says which results are missing",
          any("page(s) of this PDF have no text layer" in w for w in r["warnings"]) and param(r, "hemoglobin"),
          str(r["warnings"]))
    inc = (r.get("document") or {}).get("incomplete") or {}
    check("  and the response marks the analysis INCOMPLETE for the page to show first",
          inc.get("unreadable_pages") == 1 and "scanned/image-based" in inc.get("message", "")
          and "digital/text-based PDF or JSON" in inc.get("message", ""), str(inc))
    whole = analyse((FORMATS / "method_column.pdf").read_bytes(), "method_column.pdf")
    check("  a fully readable PDF is never marked incomplete", "incomplete" not in whole["document"])
    for ui in ("web/app.js", "web_v2/app.js"):
        src = (ROOT / ui).read_text(encoding="utf-8")
        check("  %s shows the incomplete banner and the refusal guidance" % ui,
              "document.incomplete" in src and "Analysis INCOMPLETE" in src and "guidance" in src)
    from engine import ocr
    saved = os.environ.pop("DRE_ENABLE_OCR", None)
    try:
        check("OCR is off unless explicitly enabled (it misread a troponin slip in validation)",
              ocr.available() is False)
    finally:
        if saved is not None:
            os.environ["DRE_ENABLE_OCR"] = saved


def test_barcode_glyphs_do_not_fuse_header_lines():
    from engine.extract import _not_drawing
    chars = [{"object_type": "char", "text": ch, "top": top, "x0": x, "x1": x + 2}
             for top in (108.8, 111.1, 113.5, 115.8, 118.2, 120.5, 122.9, 125.2)
             for x, ch in ((460, "█"), (462, "▐"), (464, " "), (467, "▌"))]
    chars += [{"object_type": "char", "text": "N", "top": 110.5, "x0": 58, "x1": 64},
              {"object_type": "char", "text": " ", "top": 110.5, "x0": 64, "x1": 67}]
    keep = _not_drawing(chars)
    kept = [c["text"] for c in chars if keep(c)]
    check("block-glyph barcode characters and the blanks among them are dropped; text is kept",
          kept == ["N", " "], str(kept))


def test_unit_spellings_fold_to_one_form():
    from engine.config import norm_unit
    for a, b in (("gms%", "g/dL"), ("gm/dL", "g/dL"), ("mill/cu.mm", "10^6/uL"), ("mil/µL", "million/uL"),
                 ("cells/cu.mm", "/uL"), ("10^3 cells/uL", "10^3/uL"), ("thou/µL", "10^3/uL"),
                 ("x10^3/uL", "10^3/uL"), ("lakhs/cumm", "lakh/cumm"), ("mm/Hour", "mm/hr")):
        check("unit %r folds with %r" % (a, b), norm_unit(a) == norm_unit(b), "%s vs %s" % (norm_unit(a), norm_unit(b)))
    for a, b in (("IU/mL", "uIU/mL"), ("mg/dL", "g/dL"), ("10^3/uL", "/uL")):
        check("different units stay different: %r vs %r" % (a, b), norm_unit(a) != norm_unit(b))


def test_reference_band_forms_from_real_reports():
    from engine.extract import parse_reference_range
    cases = [
        ("< 40 : Low\n40 - 60 : Optimal\n> 60 : Desirable", (40, None)),
        ("< 100   : Normal\n100 - 129 : Desirable\n130 - 159 : Borderline-High", (None, 129)),
        ("<200 - Desirable\n200-239 - Borderline risk\n>240 - High risk", (None, 200)),
        ("Deficiency : < 20\nInsufficiency : 20-29\nOptimum Level : 30-80", (30, 80)),
        ("<5.7: Non-diabetes\n5.7 - 6.4: Prediabetes", (None, 5.7)),
        ("Optimum>60\nBorderline : 50-59\nHigh risk : <50", (60, None)),
        ("Optimal: < 100\nNear/Above Optimal: 100 - 129", (None, 100)),
        ("Low: <40\nHigh >/=60", (None, None)),
        ("Optimal <130\nDesirable 130-159\nBorderline High 160-189", (None, 159)),
        ("< /= 30", (None, 30)), ("< or = 0.90", (None, 0.9)), ("Up to 1.2", (None, 1.2)),
        ("0.5 - 3.0 Desirable/Low Risk\n3.1 - 6.0 Borderline/Moderate Risk", (0.5, 3.0)),
        ("0.3-4.5\nPregnant women:\nFirst trimester: 0.25-4.33", (0.3, 4.5)),
        ("Nonsmokers: < 3.0\nSmokers: < 5.0", (None, None)),
        ("0.67-1.17\nKindly note change in Method and\nreference ranges", (0.67, 1.17)),
        ("Low: < 1.0\nAverage: 1.0-3.0\nHigh: > 3.0", (None, None)),
        ("12 - 15.5 Colorimetric", (12, 15.5)), ("0.0-5.0 Ratio", (0.0, 5.0)),
        ("0.4-4.0 uIU/mL", (0.4, 4.0)),
    ]
    norm = lambda t: tuple(None if x is None else float(x) for x in t)
    for text, want in cases:
        got = parse_reference_range(text)
        check("range %r -> %s" % (text.replace("\n", " / ")[:48], want), norm(got) == norm(want), str(got))


def test_names_that_used_to_resolve_to_the_wrong_test():
    for name, want in (("A/G", None), ("A/G RATIO", "ag_ratio"), ("Apolipoproteins B/A", None),
                       ("Apolipoprotein B/A1 Ratio", "apo_b_apo_a1_ratio"),
                       ("Urine Protein (Albumin) Absent", "urine_protein"),
                       ("Vitamin D/B12/Folate", "vitamin_d"), ("Troponin T", "troponin_t"),
                       ("TNI", "troponin_i"), ("HIV-1 ANTIBODIES", "hiv_screen"),
                       ("HEPATITIS C ABS", "anti_hcv"), ("Transferrin", "transferrin"),
                       ("Transferrin saturation", "transferrin_saturation"),
                       ("CRY - Uric acid", "urine_uric_acid_crystals"), ("Uric Acid", "uric_acid")):
        check("name %r -> %s" % (name, want), RESOLVE(name) == want, str(RESOLVE(name)))


def test_a_reference_line_is_not_a_count_result():
    from engine.layout import parse_row
    row = Row(cells=[(0.0, "TNI"), (100.0, "0.00-0.02"), (200.0, ">0.02")], source="t")
    p = parse_row(row, RESOLVE)
    check("'TNI | 0.00-0.02 | >0.02' is not a troponin result of 0.00-0.02",
          p is None or p.value != "0.00-0.02", str(p and p.value))
    row = Row(cells=[(0.0, "Pus Cells"), (100.0, "8-10"), (160.0, "/hpf"), (220.0, "0 - 5")], source="t")
    p = parse_row(row, RESOLVE)
    check("'Pus Cells | 8-10 | /hpf' is still a count result", p is not None and p.value == "8-10",
          str(p and p.value))


def test_units_and_bands_of_a_scanned_style_json_report():
    tests = [
        {"test_name": "RED BLOOD CELL (RBC) COUNT", "value": "4.47", "unit": "mil/µL", "reference_range": "4.5 - 5.5"},
        {"test_name": "ABSOLUTE LYMPHOCYTE COUNT", "value": "1.06", "unit": "thou/µL", "reference_range": "1.0 - 3.0"},
        {"test_name": "HDL CHOLESTEROL", "value": "61", "unit": "mg/dL", "reference_range": "Low: <40\nHigh >/=60", "flag": "High"},
        {"test_name": "VERY LOW DENSITY LIPOPROTEIN", "value": "52.4", "unit": "mg/dL", "reference_range": "< /= 30"},
        {"test_name": "CHOL/HDL RATIO", "value": "3.7", "reference_range": "Optimal < 3.5\nDesirable 3.5 - 5.0\nHigh Risk > 5.0"},
        {"test_name": "TSH (ULTRASENSITIVE)", "value": "1.400", "unit": "IU/mL",
         "reference_range": "0.3-4.5\nPregnant women:\nFirst trimester: 0.25-4.33"},
        {"test_name": "HIV-1 ANTIBODIES", "value": "NON REACTIVE"},
        {"test_name": "TNI", "value": "0.17", "unit": "ng/mL", "reference_range": "0.00-0.02"},
    ]
    r = analyse({"patient": {"name": "Test Agil", "age": "54 Years", "sex": "Male"}, "tests": tests})
    check("RBC 'mil/µL' interpreted and low", _p(r, "rbc_count").get("interpretable") and _p(r, "rbc_count").get("abnormal"))
    check("'thou/µL' converts to /uL", _p(r, "absolute_lymphocyte_count").get("value") == 1060.0)
    check("HDL 61 flagged 'High' by the lab is protective, not abnormal", _p(r, "hdl_cholesterol").get("abnormal") is False)
    check("'< /= 30' makes VLDL 52.4 high", _p(r, "vldl_cholesterol").get("abnormal") is True)
    check("Chol/HDL 3.7 inside Optimal+Desirable is not abnormal", _p(r, "tc_hdl_ratio").get("abnormal") is False)
    tsh = _p(r, "tsh")
    check("TSH in an unrecognised unit is graded only against its own printed interval",
          tsh.get("reference_source") == "report" and tsh.get("abnormal") is False, str(tsh))
    # HIV-1 alone is not the whole screen: HIV-2 was not reported, so it is incomplete, never negative
    check("'HIV-1 ANTIBODIES: NON REACTIVE' alone is an incomplete HIV screen, not a negative one",
          _p(r, "hiv_screen").get("status") == "incomplete"
          and _p(r, "hiv_screen").get("components") == {"HIV-1": "negative", "HIV-2": "not reported"},
          str(_p(r, "hiv_screen")))
    check("'TNI 0.17' is a raised troponin I", _p(r, "troponin_i").get("abnormal") is True)
    check("the urgent step names the troponin result",
          any(x["priority"] == "urgent" and "Troponin I 0.17" in x["text"] for x in r["recommendations"]))


def test_a_band_label_is_not_a_printed_flag():
    from engine.layout import parse_row
    for cells, want in ((["Cholesterol - HDL", "52", "mg/dL", "< 40", ": Low"], None),
                        (["Cholesterol - HDL", "52", "mg/dL", "< 40 : Low"], None),
                        (["RED BLOOD CELL (RBC) COUNT", "4.47 Low", "4.5 - 5.5", "mil/uL"], "Low"),
                        (["HAEMOGLOBIN", "18.4", "H", "g/dL", "13.0 - 17.0"], "H"),
                        (["HDL CHOLESTEROL", "61 High", "Low: <40", "mg/dL"], "High")):
        p = parse_row(Row(cells=[(float(k * 90), c) for k, c in enumerate(cells)], source="t"), RESOLVE)
        check("flag of %r is %r" % (" | ".join(cells), want), p is not None and p.flag == want,
              str(p and p.flag))
    r = analyse((FORMATS / "stacked_bands.pdf").read_bytes(), "stacked_bands.pdf")
    check("  a PDF printing '< 40 : Low' bands shows no laboratory flag on HDL",
          not any(f["parameter_id"] == "hdl_cholesterol" for f in r["lab_noted_findings"]),
          str(r["lab_noted_findings"]))


def test_what_the_laboratory_marked_is_shown_even_when_not_graded_abnormal():
    tests = [
        {"test_name": "HDL CHOLESTEROL", "value": "61", "unit": "mg/dL", "reference_range": "Low: <40\nHigh >/=60", "flag": "High"},
        {"section": "Routine Examination - Urine", "test_name": "Colour", "value": "Yellow", "reference_range": "Pale Yellow"},
        {"section": "Routine Examination - Urine", "test_name": "Transparency", "value": "Clear", "reference_range": "Clear"},
        {"test_name": "Haemoglobin", "value": "14.0", "unit": "g/dL", "reference_range": "13 - 17", "flag": "N"},
    ]
    r = analyse({"patient": {"sex": "Male", "age": "54"}, "tests": tests})
    noted = {f["parameter_id"]: f for f in r["lab_noted_findings"]}
    check("a printed 'High' flag on a result graded protective here is listed, flag quoted",
          "hdl_cholesterol" in noted and '"High"' in noted["hdl_cholesterol"]["statement"], str(noted))
    check("  a description differing from the printed expected one is listed",
          "urine_colour" in noted and "Pale Yellow" in noted["urine_colour"]["statement"], str(noted))
    check("  a description matching its printed reference is not", "urine_transparency" not in noted)
    check("  a normal flag ('N') is not", "hemoglobin" not in noted)
    check("  none of them is counted as abnormal or given a plan step",
          not any(f["parameter_id"] in noted for f in r["abnormal_findings"] + r["threshold_findings"])
          and not any(v.get("parameter_id") in noted for x in r["recommendations"] for v in x.get("values", [])))
    check("  the summary counts them separately", r["summary"]["lab_noted_findings"] == 2, str(r["summary"]))


def test_a_date_field_is_never_read_as_a_result():
    r = analyse({"patient": {"sex": "Male", "age": "54"},
                 "report": {"troponin_slip_date": "20/08/2025", "hba1c_collection_time": "10:30",
                            "creatinine_reported": "21-08-2025"},
                 "tests": [{"test_name": "Hemoglobin", "value": "14", "unit": "g/dL", "reference_range": "13-17"}]})
    ids = [p["parameter_id"] for p in r["parameters"]]
    check("a '..._date' field naming troponin gives no troponin result", "troponin_i" not in ids, str(ids))
    check("  a date-shaped value under a test-like key gives no result", "creatinine" not in ids, str(ids))
    check("  so no urgent step comes from a date", not any(x["priority"] == "urgent" for x in r["recommendations"]))
    r = analyse({"patient": {"sex": "Male", "age": "54"}, "report": {"troponin_slip_date": "20/08/2025"},
                 "tests": [{"test_name": "TNI", "value": "0.17", "unit": "ng/mL", "reference_range": "0.00-0.02"}]})
    check("  the real troponin row is still read, with no duplicate from the date",
          [p["value"] for p in r["parameters"] if p["parameter_id"] == "troponin_i"] == [0.17]
          and not any(d["parameter_id"] == "troponin_i" for d in r["duplicates_resolved"]), str(r["duplicates_resolved"]))


def test_a_reactive_component_is_never_hidden_by_a_non_reactive_one():
    for order in ((("HIV-1 ANTIBODIES", "NON REACTIVE"), ("HIV-2 ANTIBODIES", "REACTIVE")),
                  (("HIV-1 ANTIBODIES", "REACTIVE"), ("HIV-2 ANTIBODIES", "NON REACTIVE"))):
        tests = [{"test_name": n, "value": v, "reference_range": "NON REACTIVE"} for n, v in order]
        r = analyse({"patient": {"sex": "Male", "age": "54"}, "tests": tests})
        hiv = [p for p in r["parameters"] if p["parameter_id"] == "hiv_screen"]
        check("HIV screen with %s reactive is positive" % [n for n, v in order if v == "REACTIVE"][0],
              hiv and hiv[0]["status"] == "positive" and hiv[0]["abnormal"], str(hiv))
    tests = [{"test_name": n, "value": "NON REACTIVE", "reference_range": "NON REACTIVE"}
             for n in ("HIV-1 ANTIBODIES", "HIV-2 ANTIBODIES")]
    r = analyse({"patient": {"sex": "Male", "age": "54"}, "tests": tests})
    check("  both non-reactive stays negative",
          [p["status"] for p in r["parameters"] if p["parameter_id"] == "hiv_screen"] == ["negative"])
    tests = [{"test_name": "HBsAg", "value": "Reactive"}, {"test_name": "HBsAg", "value": "Non Reactive"}]
    r = analyse({"patient": {"sex": "Male", "age": "54"}, "tests": tests})
    d = [x for x in r["duplicates_resolved"] if x["parameter_id"] == "hbsag"]
    check("  the same test printed twice with conflicting answers is still reported as a conflict",
          d and d[0]["conflicting_values"], str(d))


def test_guideline_and_calculated_findings_are_never_called_outside_the_laboratory_range():
    from engine.recommend import _basis_clause
    check("a guideline finding inside the printed interval says so",
          "past a guideline threshold" in _basis_clause({"finding_basis": "decision_threshold", "in_lab_range": True}))
    check("  a guideline finding with only printed risk bands is not called outside the lab interval",
          "not the laboratory's printed interval" in _basis_clause({"finding_basis": "decision_threshold"}))
    check("  a calculated value is marked as calculated", "calculated" in _basis_clause({"finding_basis": "derived"}))
    check("  a laboratory-range finding needs no qualifier", _basis_clause({"finding_basis": "lab_range"}) == "")
    tests = [
        {"test_name": "hs-CRP", "value": "1.28", "unit": "mg/L",
         "reference_range": "Low: < 1.0\nAverage: 1.0-3.0\nHigh: > 3.0"},
        {"test_name": "SGPT (ALT)", "value": "49", "unit": "U/L", "reference_range": "0 - 41"},
        {"test_name": "HbA1c", "value": "5.7", "unit": "%",
         "reference_range": "Non-diabetic <5.7\nPrediabetes 5.7-6.4\nDiabetes >=6.5"},
    ]
    r = analyse({"patient": {"sex": "Male", "age": "45"}, "tests": tests})
    by = {f["parameter_id"]: f for f in r["abnormal_findings"] + r["threshold_findings"]}
    check("  hs-CRP 1.28 against printed risk bands is a guideline finding, not a lab-range one",
          by.get("hs_crp", {}).get("finding_basis") == "decision_threshold", str(by.get("hs_crp")))
    check("  ALT 49 over a printed 0-41 is a lab-range finding",
          by.get("sgpt_alt", {}).get("finding_basis") == "lab_range", str(by.get("sgpt_alt")))
    for x in r["recommendations"]:
        for f in by.values():
            label = "%s %s%s" % (f["name"], f["value"], (" " + f["unit"]) if f["unit"] else "")
            if f["finding_basis"] != "lab_range" and "outside their range" in x["text"] and label in x["text"]:
                check("  %s in a grouped step carries its basis" % f["name"],
                      (label + _basis_clause(f)) in x["text"], x["text"])


def test_urgent_wording_is_the_specific_action_and_nothing_routine_inherits_it():
    tests = [
        {"test_name": "TNI", "value": "0.17", "unit": "ng/mL", "reference_range": "0.00-0.02"},
        {"test_name": "TRIGLYCERIDES", "value": "262", "unit": "mg/dL", "reference_range": "Normal <150"},
        {"test_name": "HDL CHOLESTEROL", "value": "38", "unit": "mg/dL", "reference_range": "Low: <40\nHigh >/=60"},
        {"test_name": "NON HDL CHOLESTEROL", "value": "163", "unit": "mg/dL", "reference_range": "Optimal <130"},
        {"test_name": "CHOLESTEROL, TOTAL", "value": "201", "unit": "mg/dL", "reference_range": "Desirable <200"},
    ]
    r = analyse({"patient": {"age": "54", "sex": "Male"}, "tests": tests})
    urgent = [x for x in r["recommendations"] if x["priority"] == "urgent"]
    check("a raised troponin gives exactly one urgent step", len(urgent) == 1,
          str([(x["category"], x["text"][:60]) for x in urgent]))
    check("  it is the troponin-specific action, not generic triage wording",
          urgent and "heart muscle injury" in urgent[0]["text"], str([x["text"] for x in urgent]))
    check("  and it names the result behind it",
          urgent and "Troponin I 0.17" in urgent[0]["text"], str([x["text"] for x in urgent]))
    check("  cardiovascular-risk and lifestyle advice for the same condition is not urgent",
          not any("calculate your overall cardiovascular risk" in x["text"] for x in urgent))


def _cond(tests, name, sex="male"):
    r = analyse({"patient": {"sex": sex, "age": "40"}, "tests": tests})
    return next(((d["evidence_level"], d["presentation_tier"]) for d in r["disease_risks"] if d["name"] == name), None)


def test_measured_normal_markers_argue_against_a_single_marker_pattern():
    T = lambda n, v, u, rg: {"test_name": n, "value": v, "unit": u, "reference_range": rg}
    got = _cond([T("Hemoglobin", "17.2", "g/dL", "13-17"), T("Hematocrit", "46", "%", "40-50"),
                 T("RBC Count", "5.1", "mill/cumm", "4.5-5.5")], "Polycythemia")
    check("Hb just high with haematocrit and RBC normal: polycythaemia not presented as a pattern",
          got is None or got[1] == "insufficient", str(got))
    got = _cond([T("Hemoglobin", "18.5", "g/dL", "13-17"), T("Hematocrit", "55", "%", "40-50"),
                 T("RBC Count", "6.3", "mill/cumm", "4.5-5.5")], "Polycythemia")
    check("Hb, haematocrit and RBC all raised: still a supported pattern", got and got[1] == "pattern", str(got))
    got = _cond([T("Transferrin saturation", "17.7", "%", "20-50"), T("Iron", "58.8", "ug/dL", "33-193"),
                 T("TIBC", "332", "ug/dL", "240-450")], "Iron Deficiency Anemia")
    check("low TSAT with iron and TIBC normal: iron deficiency anaemia not a supported pattern",
          got is None or got[1] == "insufficient", str(got))
    got = _cond([T("Transferrin saturation", "12", "%", "20-50"), T("Ferritin", "8", "ng/mL", "30-400"),
                 T("Iron", "30", "ug/dL", "33-193"), T("TIBC", "470", "ug/dL", "240-450")], "Iron Deficiency Anemia")
    check("low ferritin, iron and TSAT with high TIBC: supported", got and got[1] == "pattern", str(got))
    got = _cond([T("TSH", "5.05", "uIU/mL", "0.54-5.3"), T("FT4", "1.39", "ng/dL", "0.93-1.7"),
                 T("FT3", "2.9", "pg/mL", "2.0-4.4")], "Hypothyroidism")
    check("TSH 5.05 with free T4 and T3 normal: hypothyroidism not a supported pattern",
          got is None or got[1] == "insufficient", str(got))
    got = _cond([T("TSH", "12", "uIU/mL", "0.54-5.3"), T("FT4", "0.6", "ng/dL", "0.93-1.7")], "Hypothyroidism")
    check("TSH 12 with free T4 low: supported", got and got[1] == "pattern", str(got))


def test_advice_is_never_given_for_the_opposite_direction():
    T = lambda n, v, u, rg: {"test_name": n, "value": v, "unit": u, "reference_range": rg}
    high = analyse({"patient": {"sex": "male"}, "tests": [T("Hemoglobin", "17.2", "g/dL", "13-17")]})
    low = analyse({"patient": {"sex": "female"}, "tests": [T("Hemoglobin", "9.0", "g/dL", "12-15")]})
    check("a HIGH haemoglobin gets no anaemia advice",
          not any("Anaemia" in x["text"] for x in high["recommendations"]))
    check("a LOW haemoglobin still does", any("Anaemia" in x["text"] for x in low["recommendations"]))
    lf = analyse({"patient": {"sex": "female"}, "tests": [T("LH", "9.0", "mIU/mL", "1.7-8.6"),
                                                         T("FSH", "3.0", "mIU/mL", "1.4-15.4")]})
    check("the LH/FSH ratio is still graded for a woman", _p(lf, "lh_fsh_ratio").get("grade") not in (None, "unknown"))


def test_a_generic_row_label_takes_the_test_name_of_its_heading():
    # A JSON export of a "RBC Morphology" heading with its result on a "Remark" row: the
    # PDF path already read this as RBC morphology; the JSON path left it unrecognised.
    r = analyse({"patient": {"sex": "Male"}, "tests": [
        {"section": "CBC > Erythrocytes > RBC Morphology", "test_name": "Remark",
         "value": "Normocytic Normochromic"}]})
    check("'Remark' under an RBC Morphology heading is RBC morphology",
          _p(r, "rbc_morphology").get("category") == "normocytic normochromic",
          str([(o["raw_name"], o["raw_value"]) for o in r["unmapped_observations"]]))
    r = analyse({"patient": {"sex": "Male"}, "tests": [
        {"test_name": "AST", "value": "30", "unit": "U/L", "reference_range": "0-40"},
        {"section": "Liver Panel > ALT", "test_name": "Findings", "value": "54", "unit": "U/L"}]})
    check("  a NUMBER under a 'Findings' label is not assigned to the heading's test",
          r.get("analysed") and not _p(r, "sgpt_alt"), str([p["parameter_id"] for p in r.get("parameters", [])]))


def test_a_guideline_band_inside_the_printed_interval_is_not_outside_its_range():
    bands = "< 100 : Normal\n100 - 129 : Desirable\n130 – 159 : Borderline-High\n160 – 189 : High"
    inside = analyse({"patient": {"sex": "Male", "age": "40"}, "tests": [
        {"test_name": "Cholesterol - LDL", "value": "118", "unit": "mg/dL", "reference_range": bands}]})
    thr = [f for f in inside["threshold_findings"] if f["parameter_id"] == "ldl_cholesterol"]
    abn = [f for f in inside["abnormal_findings"] if f["parameter_id"] == "ldl_cholesterol"]
    check("LDL inside the printed healthy bands but past a guideline band: listed as a threshold finding",
          thr and not abn and thr[0]["in_lab_range"],
          str([(f["parameter_id"], f["in_lab_range"]) for f in inside["abnormal_findings"] + inside["threshold_findings"]]))
    check("  it is still shown, with the guideline band named",
          thr and "guideline" in thr[0]["statement"], str(thr))
    check("  and the summary does not count it as outside its range",
          inside["summary"]["abnormal_findings"] == 0, str(inside["summary"]["abnormal_findings"]))
    outside = analyse({"patient": {"sex": "Male", "age": "40"}, "tests": [
        {"test_name": "Cholesterol - LDL", "value": "171", "unit": "mg/dL", "reference_range": bands}]})
    check("  an LDL beyond the printed healthy bands is still outside its range",
          any(f["parameter_id"] == "ldl_cholesterol" and not f["in_lab_range"] for f in outside["abnormal_findings"]))
    edge = analyse({"patient": {"sex": "Male", "age": "35"}, "tests": [
        {"test_name": "Glycated Haemoglobin (A1c)", "value": "5.7", "unit": "%",
         "reference_range": "<5.7: Non-diabetes\n5.7 – 6.4: Prediabetes\n= 6.5- Diabetes"}]})
    check("  a value ON the printed boundary ('<5.7' normal, '5.7 - 6.4' prediabetes) is never "
          "described as one the laboratory would call normal",
          any(f["parameter_id"] == "hba1c" for f in edge["abnormal_findings"])
          and not any(f["parameter_id"] == "hba1c" for f in edge["threshold_findings"]),
          str([(f["parameter_id"], f["in_lab_range"]) for f in edge["abnormal_findings"] + edge["threshold_findings"]]))
    derived = analyse({"patient": {"sex": "Female"}, "tests": [
        {"test_name": "Iron", "value": "40", "unit": "ug/dL", "reference_range": "37-145"},
        {"test_name": "TIBC", "value": "300", "unit": "ug/dL", "reference_range": "250-450"}]})
    check("  a value calculated here is never placed inside a laboratory range",
          all(not f["in_lab_range"] for f in derived["abnormal_findings"] + derived["threshold_findings"]
              if f["derived"]))


def test_a_pending_result_is_said_to_be_pending_not_dropped_or_normal():
    alone = analyse({"patient": {"sex": "Male", "age": "50"}, "tests": [
        {"test_name": "TSH 3RD GENERATION ULTRASENSITIVE, SERUM", "value": "RESULT PENDING"},
        {"test_name": "Hemoglobin", "value": "14", "unit": "g/dL", "reference_range": "13-17"}]})
    check("a pending test is recorded as pending", alone["summary"]["results_pending"] == 1,
          str(alone.get("pending_results")))
    check("  and the reader is told no result was analysed",
          any("RESULT PENDING" in w and "not treated as normal" in w for w in alone["warnings"]), str(alone["warnings"]))
    check("  it is not listed as an unrecognised test",
          not any("TSH" in o["raw_name"] for o in alone["unmapped_observations"]))
    check("  and no TSH value is invented", not _p(alone, "tsh"))
    final = analyse({"patient": {"sex": "Male", "age": "50"}, "tests": [
        {"test_name": "TSH 3RD GENERATION ULTRASENSITIVE, SERUM", "value": "RESULT PENDING"},
        {"test_name": "TSH (ULTRASENSITIVE)", "value": "2.1", "unit": "uIU/mL", "reference_range": "0.3-4.5"}]})
    check("  a pending row with a final result elsewhere: the final result is used",
          _p(final, "tsh").get("value") == 2.1)
    check("  and the pending row is not silently discarded",
          any("RESULT PENDING" in w and "was used" in w for w in final["warnings"]), str(final["warnings"]))
    cat = analyse({"patient": {"sex": "Male"}, "tests": [{"test_name": "Blood Group", "value": "Pending"}]})
    check("  a categorical test is never given 'pending' as its answer",
          not _p(cat, "blood_group"), str(_p(cat, "blood_group")))
    note = analyse({"patient": {"sex": "Male"}, "tests": [
        {"test_name": "RBC Morphology", "value": "Normocytic; smear review pending confirmation"}]})
    check("  a result that merely mentions 'pending' is still the result",
          note["summary"]["results_pending"] == 0 and _p(note, "rbc_morphology"))


def test_a_finding_never_says_the_report_gave_no_interval_when_it_printed_one():
    banded = analyse({"patient": {"sex": "Male", "age": "36"}, "tests": [
        {"test_name": "HsCRP (High Sensitivity CRP)", "value": "1.4", "unit": "mg/L",
         "reference_range": "Low: < 1.0\nAverage: 1.0-3.0\nHigh: > 3.0"}]})
    f = [x for x in banded["abnormal_findings"] + banded["threshold_findings"] if x["parameter_id"] == "hs_crp"]
    check("hs-CRP with printed risk bands: still listed as a finding", bool(f))
    check("  and its statement does not claim the report gave no interval",
          f and "gave no reference interval" not in f[0]["statement"], str(f and f[0]["statement"]))
    check("  it quotes what the report printed", f and "Average: 1.0-3.0" in f[0]["statement"],
          str(f and f[0]["statement"]))
    bare = analyse({"patient": {"sex": "Male", "age": "36"}, "tests": [
        {"test_name": "HsCRP (High Sensitivity CRP)", "value": "1.4", "unit": "mg/L"}]})
    g = [x for x in bare["abnormal_findings"] + bare["threshold_findings"] if x["parameter_id"] == "hs_crp"]
    check("  with truly no interval printed, it still says so",
          g and "gave no reference interval" in g[0]["statement"], str(g and g[0]["statement"]))


def test_an_unsupported_pattern_gets_no_advice_that_presumes_it():
    T = lambda n, v, u, rg: {"test_name": n, "value": v, "unit": u, "reference_range": rg}
    GUT = "where the iron is going"
    measured = analyse({"patient": {"sex": "Male", "age": "40"}, "tests": [
        T("Transferrin saturation", "18.2", "%", "20-50"), T("Iron", "61", "ug/dL", "33-193"),
        T("TIBC", "330", "ug/dL", "240-450")]})
    check("low TSAT with iron and TIBC normal: no 'where the iron is going' gut-investigation advice",
          not any(GUT in x["text"] for x in measured["recommendations"]),
          str([x["text"][:60] for x in measured["recommendations"]]))
    check("  and no coeliac screening 'if the deficiency is unexplained'",
          not any("coeliac" in x["text"] for x in measured["recommendations"]))
    check("  the low TSAT is still in the plan",
          any(v.get("parameter_id") == "transferrin_saturation" for x in measured["recommendations"]
              for v in x["values"]))
    calculated = analyse({"patient": {"sex": "Female", "age": "40"}, "tests": [
        T("Iron", "47", "ug/dL", "43.6-180"), T("TIBC", "262", "ug/dL", "250-450")]})
    check("  a TSAT this engine calculated, iron and TIBC normal: no gut-investigation advice",
          not any(GUT in x["text"] for x in calculated["recommendations"]),
          str([x["text"][:60] for x in calculated["recommendations"]]))
    real = analyse({"patient": {"sex": "Male", "age": "40"}, "tests": [
        T("Transferrin saturation", "9", "%", "20-50"), T("Ferritin", "6", "ng/mL", "30-400"),
        T("Iron", "25", "ug/dL", "33-193"), T("TIBC", "480", "ug/dL", "240-450"),
        T("Hemoglobin", "10.8", "g/dL", "13-17"), T("MCV", "72", "fL", "80-100")]})
    check("  an iron deficiency the evidence supports still gets it",
          any(GUT in x["text"] for x in real["recommendations"]),
          str([x["text"][:60] for x in real["recommendations"]]))
    from engine.config import get_config
    tagged = [s for specs in get_config().recommendations["cohort_actions"].values() for s in specs
              if s.get("presumes_established")]
    check("  every step tagged as presuming the finding is a confirmation-category step (else the tag is moot)",
          tagged and all(s["category"] in ("Urgent", "Consultation", "Testing") for s in tagged), str(len(tagged)))


def test_a_test_objects_page_or_id_is_not_patient_context():
    from engine.extract import extract_json
    _o, ctx, _w = extract_json({"tests": [{"test_name": "Hb", "value": "13.1", "unit": "g/dL", "page": 3,
                                           "id": 17}],
                                "patient": {"name": "A B", "sex": "Female", "age": "38 Y",
                                            "patient_id": "P-9"}})
    check("results listed before the demographics: age is the patient's, not a page number",
          ctx.age == 38, str(ctx.age))
    check("  and the patient id is not a test's id", ctx.patient_id == "P-9", str(ctx.patient_id))
    _o, ctx, _w = extract_json({"patient": {"sex": "Female"},
                                "tests": [{"test_name": "Hb", "value": "13.1", "page": 2}]})
    check("  demographics without an age: no age is invented from a page number", ctx.age is None, str(ctx.age))
    _o, ctx, _w = extract_json({"PName": "A B", "PAge": "52", "Gender": "F",
                                "results": [{"test_name": "Hb", "value": "12"}]})
    check("  a LIS 'PAge' field is still the patient's age", ctx.age == 52, str(ctx.age))
    _o, ctx, _w = extract_json([{"patient_id": "X1", "age": 45, "sex": "M", "test": "Hb", "value": 13}])
    check("  one-row-per-result exports keep their demographics",
          (ctx.patient_id, ctx.age, ctx.sex) == ("X1", 45, "male"), str((ctx.patient_id, ctx.age, ctx.sex)))


def test_private_real_reports_when_present():
    """Real reports live in the git-ignored private/ folder beside a *_truth.json
    transcription. When present they must diff to zero; elsewhere this is skipped."""
    pairs = [(p, p.with_name(p.stem + "_truth.json")) for p in sorted((ROOT / "private").glob("*.pdf"))]
    pairs += [(p, p.parent / "truth" / (p.stem + "_truth.json"))
              for p in sorted((ROOT / "private" / "multi").glob("report_*.pdf"))]
    pairs = [(p, t) for p, t in pairs if t.exists()]
    # A scanned report cannot be read without OCR; it is checked for its warning instead.
    scanned = []
    for p, t in list(pairs):
        import pdfplumber
        with pdfplumber.open(str(p)) as doc:
            if not any(pg.chars for pg in doc.pages):
                scanned.append(p)
                pairs.remove((p, t))
    for p in scanned:
        r = analyse(p.read_bytes(), p.name)
        check("real scanned report %s says it needs OCR" % p.stem,
              any("no extractable text layer" in w for w in r.get("warnings", [])))
    if not pairs:
        check("private real-report check skipped - none present", True)
        return
    for pdf, truth in pairs:
        res = _diff(pdf.read_bytes(), json.loads(truth.read_text(encoding="utf-8")), pdf.name)
        check("real report %s: PDF and JSON agree on every field" % pdf.stem, _count(res) == 0,
              json.dumps({k: res[k] for k in ("context", "missing", "spurious", "field")}, default=str)[:400])


# ---------------------------------------------------------------- run

def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        try:
            t()
        except Exception as exc:
            import traceback
            check("%s raised %s" % (t.__name__, type(exc).__name__), False,
                  traceback.format_exc(limit=3))
    failed = [r for r in RESULTS if not r[0]]
    for ok, name, detail in RESULTS:
        if not ok:
            print("FAIL  %s   %s" % (name, detail))
    print("-" * 70)
    print("%d checks, %d passed, %d failed" % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
