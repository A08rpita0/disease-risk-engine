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
