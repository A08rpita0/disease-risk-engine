"""Unit tests for the extraction and normalization edge cases the brief calls out:
different names, different units, different reference ranges, missing values, null
values, male/female ranges, duplicate parameters and different JSON structures.

Run:  python tests/test_engine.py       (no pytest required)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.config import get_config              # noqa: E402
from engine.extract import extract, parse_reference_range   # noqa: E402
from engine.normalize import Normalizer, parse_numeric      # noqa: E402
from engine.pipeline import Pipeline               # noqa: E402

CFG = get_config()
PIPE = Pipeline(CFG)
RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((bool(condition), name, detail))


def analyse(payload, filename="test.json", **kw):
    return PIPE.run(payload, filename, **kw)


def param(result, pid):
    for p in result["parameters"]:
        if p["parameter_id"] == pid:
            return p
    return None


# ---------------------------------------------------------------- naming

def test_alias_resolution():
    variants = {
        "hba1c": ["HbA1c", "Glycosylated Haemoglobin (HbA1c)", "HB A1C", "Glycated Hemoglobin"],
        "creatinine": ["Creatinine", "S. Creatinine", "Serum Creatinine", "CREAT"],
        "sgpt_alt": ["SGPT", "ALT", "SGPT (ALT)", "SGOT/ALT" if False else "ALT (SGPT)",
                     "Alanine Aminotransferase"],
        "wbc_count": ["WBC", "Total Leucocyte Count", "TLC", "White Blood Cell Count"],
        "hdl_cholesterol": ["HDL", "HDL Cholesterol", "High Density Lipoprotein", "HDL-C"],
        "vitamin_d": ["Vitamin D", "25-OH Vitamin D", "Vit D 25-Hydroxy", "25(OH)D"],
    }
    for pid, names in variants.items():
        for n in names:
            got = CFG.resolve_alias(n)
            check("alias %-34r -> %s" % (n, pid), got == pid, "got %s" % got)


def test_unrecognised_name_is_not_guessed():
    got = CFG.resolve_alias("Sputnik Index Of Nothing")
    check("an unknown test name resolves to nothing", got is None, "got %s" % got)


# ---------------------------------------------------------------- units

def test_unit_conversion():
    cases = [
        ("Fasting Blood Sugar", 8.4, "mmol/L", "fasting_glucose", 151.3),
        ("Total Cholesterol", 7.1, "mmol/L", "total_cholesterol", 274.6),
        ("Creatinine", 142, "umol/L", "creatinine", 1.60),
        ("Haemoglobin", 108, "g/L", "hemoglobin", 10.8),
        ("Platelet Count", 118, "10^9/L", "platelet_count", 118000),
        ("Platelet Count", 2.4, "lakhs/cumm", "platelet_count", 240000),
        ("Total WBC Count", 6.4, "10^3/uL", "wbc_count", 6400),
        ("Vitamin D", 50, "nmol/L", "vitamin_d", 20.03),
    ]
    for name, value, unit, pid, expected in cases:
        r = analyse({"tests": [{"test_name": name, "value": value, "unit": unit}]})
        p = param(r, pid)
        ok = p is not None and abs(p["value"] - expected) < max(0.05, expected * 0.01)
        check("convert %s %s -> %.4g %s" % (value, unit, expected, pid),
              ok, "got %s" % (p["value"] if p else None))


def test_unknown_unit_is_flagged_not_silently_used():
    r = analyse({"tests": [{"test_name": "Haemoglobin", "value": 13.1, "unit": "furlongs"}]})
    p = param(r, "hemoglobin")
    check("an unrecognised unit is reported in the notes",
          p and any("not recognised" in n for n in p["notes"]),
          "notes=%s" % (p["notes"] if p else None))


# ---------------------------------------------------------------- reference ranges

def test_reference_range_parsing():
    cases = [("70 - 99", (70, 99)), ("< 150", (None, 150)), ("> 40", (40, None)),
             ("0.4-4.0 uIU/mL", (0.4, 4.0)), ("13.0 to 17.0", (13.0, 17.0)),
             ("150000 - 450000", (150000, 450000)), (">= 60", (60, None)),
             ("nonsense", (None, None))]
    for text, expected in cases:
        got = parse_reference_range(text)
        check("parse range %-20r -> %s" % (text, expected), got == expected, "got %s" % (got,))


def test_report_range_overrides_dictionary():
    r = analyse({"tests": [{"test_name": "Serum Creatinine", "value": 1.25, "unit": "mg/dL",
                            "reference_range": "0.6 - 1.1"}]}, sex="male")
    p = param(r, "creatinine")
    check("a range printed on the report takes priority over the dictionary",
          p and p["reference_source"] == "report" and p["abnormal"],
          "source=%s abnormal=%s" % (p["reference_source"], p["abnormal"]))


def test_clinical_bands_survive_a_report_range():
    r = analyse({"tests": [{"test_name": "HbA1c", "value": 7.4, "unit": "%",
                            "reference_range": "4.0 - 6.0"}]})
    p = param(r, "hba1c")
    check("clinical decision bands still grade the value",
          p and p["grade_label"] == "Diabetes range", "label=%s" % (p["grade_label"] if p else None))


def test_sex_specific_ranges():
    for sex, value, expect_abnormal in [("male", 13.5, False), ("female", 13.5, False),
                                        ("male", 12.5, True), ("female", 12.5, False)]:
        r = analyse({"tests": [{"test_name": "Haemoglobin", "value": value, "unit": "g/dL"}]},
                    sex=sex)
        p = param(r, "hemoglobin")
        check("Hb %.1f in a %s is %s" % (value, sex, "abnormal" if expect_abnormal else "normal"),
              p and p["abnormal"] == expect_abnormal, "got abnormal=%s" % (p["abnormal"] if p else None))


def test_unknown_sex_widens_the_range():
    r = analyse({"tests": [{"test_name": "Haemoglobin", "value": 12.5, "unit": "g/dL"}]})
    p = param(r, "hemoglobin")
    check("with sex unknown the widest interval is used and annotated",
          p and not p["abnormal"] and any("sex" in n for n in p["notes"]),
          "abnormal=%s notes=%s" % (p["abnormal"], p["notes"]))


# ---------------------------------------------------------------- values

def test_missing_and_null_values_are_dropped_not_invented():
    r = analyse({"tests": [
        {"test_name": "Serum Calcium", "value": None, "unit": "mg/dL"},
        {"test_name": "Serum Albumin", "value": "", "unit": "g/dL"},
        {"test_name": "Serum Magnesium", "unit": "mg/dL"},
        {"test_name": "Haemoglobin", "value": 13.9, "unit": "g/dL"},
    ]})
    ids = {p["parameter_id"] for p in r["parameters"]}
    check("null, empty and absent values produce no parameter",
          ids == {"hemoglobin"}, "got %s" % sorted(ids))


def test_censored_values():
    for text, expect_note in [("< 0.01", "below"), ("> 1000", "above")]:
        r = analyse({"tests": [{"test_name": "D-Dimer", "value": text, "unit": "ug/mL"}]})
        p = param(r, "d_dimer")
        check("censored value %r keeps a note" % text,
              p and any(expect_note in n for n in p["notes"]),
              "notes=%s" % (p["notes"] if p else None))


def test_numeric_parsing():
    for raw, expected in [("13.5", 13.5), ("13,500", 13500.0), ("< 0.01", 0.01),
                          (" 7.4 ", 7.4), ("abc", None), (None, None), (True, None)]:
        got, _ = parse_numeric(raw)
        check("parse value %-10r -> %s" % (raw, expected), got == expected, "got %s" % got)


def test_qualitative_vocabulary():
    cases = [("Positive", "positive"), ("Reactive", "positive"), ("NEGATIVE", "negative"),
             ("Non-Reactive", "negative"), ("Not Detected", "negative"),
             ("Detected", "positive"), ("Equivocal", "indeterminate")]
    for raw, expected in cases:
        r = analyse({"tests": [{"test_name": "Dengue NS1 Antigen", "value": raw}]})
        p = param(r, "dengue_ns1")
        check("qualitative %-14r -> %s" % (raw, expected),
              p and p["status"] == expected, "got %s" % (p["status"] if p else None))


# ---------------------------------------------------------------- duplicates

def test_duplicate_parameters_are_resolved_and_recorded():
    r = analyse({"tests": [
        {"test_name": "Haemoglobin", "value": 9.8, "unit": "g/dL"},
        {"test_name": "Hb", "value": 9.9, "unit": "g/dL", "reference_range": "13.0 - 17.0"},
    ]}, sex="male")
    dups = r["duplicates_resolved"]
    p = param(r, "hemoglobin")
    check("a duplicated parameter appears only once", p is not None and
          len([x for x in r["parameters"] if x["parameter_id"] == "hemoglobin"]) == 1)
    check("the duplicate resolution is recorded", len(dups) == 1 and dups[0]["occurrences"] == 2,
          "dups=%s" % dups)
    check("the record carrying a report range is the one kept",
          p and p["reference_source"] == "report", "source=%s" % (p["reference_source"] if p else None))
    check("a value conflict is flagged", dups and dups[0]["conflicting_values"] is True)


# ---------------------------------------------------------------- structures

def test_json_shapes():
    shapes = {
        "array of test objects":
            {"tests": [{"test_name": "TSH", "value": 8.2, "unit": "uIU/mL"}]},
        "flat name/value map":
            {"results": {"TSH": "8.2 uIU/mL"}},
        "value object keyed by name":
            {"observations": {"TSH": {"value": 8.2, "unit": "uIU/mL"}}},
        "deeply nested panels":
            {"episode": {"panels": {"endo": {"groups": [{"items": [
                {"analyte": "TSH", "obs_value": 8.2, "uom": "uIU/mL"}]}]}}}},
        "separate low/high keys":
            {"tests": [{"name": "TSH", "result": 8.2, "unit": "uIU/mL",
                        "low": 0.4, "high": 4.0}]},
        "range as a nested object":
            {"tests": [{"name": "TSH", "result": 8.2, "unit": "uIU/mL",
                        "reference_range": {"low": 0.4, "high": 4.0}}]},
    }
    for label, payload in shapes.items():
        r = analyse(payload)
        p = param(r, "tsh")
        check("JSON shape: %s" % label, p is not None and abs(p["value"] - 8.2) < 0.01,
              "got %s" % (p["value"] if p else None))


def test_patient_name_keys():
    """PName and its sibling abbreviations, in any casing, are the patient's name."""
    tests = [{"test_name": "TSH", "value": 6.2, "unit": "uIU/mL"}]
    shapes = {
        "PName at the top level":
            {"PName": "Asha Menon", "Gender": "F", "Age": 41, "tests": tests},
        "PName inside a patient block":
            {"patient": {"PName": "Ravi Iyer", "sex": "male", "age": 50}, "tests": tests},
        "lowercase pname":
            {"pname": "Neha Rao", "gender": "female", "age": 33, "tests": tests},
        "uppercase PNAME":
            {"PNAME": "Vikram S", "gender": "male", "age": 60, "tests": tests},
        "P_Name with an underscore":
            {"P_Name": "Meera K", "sex": "female", "age": 28, "tests": tests},
        "PtName":
            {"PtName": "Arjun D", "sex": "male", "age": 44, "tests": tests},
    }
    for label, payload in shapes.items():
        r = analyse(payload)
        got = r["patient"]["name"]
        check("patient name from %s" % label, got is not None and got.strip() != "",
              "got %r" % got)
        check("%s leaves nothing unmapped" % label,
              not r["unmapped_observations"],
              "unmapped: %s" % [o["raw_name"] for o in r["unmapped_observations"]])


def test_bare_name_only_taken_from_a_demographics_block():
    tests = [{"test_name": "TSH", "value": 6.2, "unit": "uIU/mL"}]
    r = analyse({"patient": {"name": "Sample Patient A", "gender": "Male", "age": 47},
                 "tests": tests})
    check("a bare 'name' beside sex and age is the patient's name",
          r["patient"]["name"] == "Sample Patient A", "got %r" % r["patient"]["name"])

    r = analyse({"header": {"name": "Metro Diagnostics"}, "tests": tests})
    check("a bare 'name' with no demographics is not taken as the patient",
          r["patient"]["name"] is None, "got %r" % r["patient"]["name"])

    r = analyse({"metadata": {"lab_info": {"name": "Some Lab", "address": "12 Road"}},
                 "patient": {"PName": "Real Person", "sex": "male", "age": 30},
                 "tests": tests})
    check("a laboratory's name never displaces the patient's",
          r["patient"]["name"] == "Real Person", "got %r" % r["patient"]["name"])

    r = analyse({"tests": [{"id": 5, "name": "Total Cholesterol", "value": 190,
                            "unit": "mg/dL"}]})
    check("a test object's 'name' is not mistaken for the patient",
          r["patient"]["name"] is None, "got %r" % r["patient"]["name"])


def test_patient_name_in_report_text():
    from engine.extract import _context_from_text
    cases = [
        ("PName: Kavita Nair   Age: 39  Sex: Female", "Kavita Nair"),
        ("P.Name : Mr. Suresh Babu\nAge/Sex : 55 Y / Male", "Suresh Babu"),
        ("Pt Name: Latha M\nGender: F", "Latha M"),
        ("Patient Name: Sample Patient G UHID: NG-1\nSex: Male", "Sample Patient G"),
    ]
    for text, expected in cases:
        got = _context_from_text(text, "x").name
        check("report header %r -> %s" % (text.split("\n")[0][:34], expected),
              got == expected, "got %r" % got)


def test_metadata_subtrees_are_not_mined_for_results():
    r = analyse({
        "metadata": {"lab_info": {"name": "Some Lab", "address": "12 Road"}},
        "doctor": {"name": "Dr Someone"},
        "tests": [{"test_name": "TSH", "value": 2.0, "unit": "uIU/mL"}],
    })
    check("laboratory and doctor metadata produce no observations",
          r["summary"]["parameters_recognised"] == 1 and r["summary"]["parameters_unmapped"] == 0,
          "recognised=%d unmapped=%d" % (r["summary"]["parameters_recognised"],
                                         r["summary"]["parameters_unmapped"]))


def test_results_nested_under_a_metadata_named_key_are_kept():
    r = analyse({"laboratory": {"tests": [{"test_name": "TSH", "value": 2.0, "unit": "uIU/mL"}]}})
    check("a results block named 'laboratory' is not skipped",
          param(r, "tsh") is not None)


# ---------------------------------------------------------------- derived

def test_derived_parameters():
    r = analyse({"tests": [
        {"test_name": "Total Cholesterol", "value": 220, "unit": "mg/dL"},
        {"test_name": "HDL Cholesterol", "value": 40, "unit": "mg/dL"},
        {"test_name": "Triglycerides", "value": 200, "unit": "mg/dL"},
    ]}, sex="male")
    nonhdl, ratio = param(r, "non_hdl_cholesterol"), param(r, "tg_hdl_ratio")
    check("non-HDL is derived", nonhdl and nonhdl["value"] == 180 and nonhdl["derived"])
    check("TG/HDL ratio is derived", ratio and abs(ratio["value"] - 5.0) < 0.01)


def test_derived_needs_all_inputs():
    r = analyse({"tests": [{"test_name": "Total Cholesterol", "value": 220, "unit": "mg/dL"}]})
    check("a derived value is not computed from a missing input",
          param(r, "non_hdl_cholesterol") is None)


# ---------------------------------------------------------------- engine behaviour

def test_negative_result_never_raises_its_disease():
    r = analyse({"patient": {"sex": "male"}, "tests": [
        {"test_name": "Dengue NS1 Antigen", "value": "Positive"},
        {"test_name": "Malaria Antigen", "value": "Negative"},
        {"test_name": "Platelet Count", "value": 70000, "unit": "/uL"},
    ]}, sex="male")
    names = {x["name"] for x in r["disease_risks"]}
    check("a positive dengue test raises dengue", "Dengue Fever" in names)
    check("a negative malaria test never raises malaria", "Malaria" not in names,
          "flagged: %s" % sorted(names))


def test_every_risk_is_explained():
    r = PIPE.run(Path(__file__).resolve().parents[1].joinpath("samples/p1_metabolic.json").read_bytes(),
                 "p1_metabolic.json")
    ok = all(x["explanation"] and x["contributions"] and x["triggering_parameters"]
             for x in r["disease_risks"])
    check("every reported risk carries an explanation, contributions and triggers", ok)
    ok2 = all(c["dm_basis"] for x in r["disease_risks"] for c in x["contributions"])
    check("every contribution quotes the Disease Master field behind it", ok2)


def test_no_abnormality_means_no_risk():
    r = analyse({"tests": [
        {"test_name": "Haemoglobin", "value": 14.0, "unit": "g/dL"},
        {"test_name": "TSH", "value": 2.0, "unit": "uIU/mL"},
        {"test_name": "Fasting Blood Sugar", "value": 88, "unit": "mg/dL"},
        {"test_name": "Total Cholesterol", "value": 170, "unit": "mg/dL"},
        {"test_name": "Serum Creatinine", "value": 0.9, "unit": "mg/dL"},
    ]}, sex="male")
    check("a normal panel raises nothing", not r["disease_risks"],
          "flagged %s" % [x["name"] for x in r["disease_risks"]])


def test_redundant_parameters_do_not_inflate_a_cluster():
    """LDL, non-HDL and ApoB all measure atherogenic particle burden; the cluster must
    not treat them as three independent pieces of evidence."""
    one = analyse({"tests": [{"test_name": "LDL Cholesterol", "value": 165, "unit": "mg/dL"}]},
                  sex="male")
    many = analyse({"tests": [
        {"test_name": "LDL Cholesterol", "value": 165, "unit": "mg/dL"},
        {"test_name": "Total Cholesterol", "value": 250, "unit": "mg/dL"},
        {"test_name": "HDL Cholesterol", "value": 45, "unit": "mg/dL"},
        {"test_name": "Apolipoprotein B", "value": 135, "unit": "mg/dL"},
    ]}, sex="male")

    def conf(res):
        for c in res["cohorts"]:
            if c["cohort_id"] == "hypercholesterolaemia":
                return c["confidence"]
        return 0.0

    c1, c2 = conf(one), conf(many)
    check("more redundant lipid markers raise confidence but not without limit",
          c2 > c1 and c2 <= 1.0, "one=%.2f many=%.2f" % (c1, c2))
    discounted = any(h["suppressed_by"] for c in many["cohorts"]
                     if c["cohort_id"] == "hypercholesterolaemia" for h in c["hits"])
    check("redundant markers are explicitly discounted in the trace", discounted)


def test_thin_data_caps_the_evidence_level():
    r = analyse({"tests": [{"test_name": "SGPT", "value": 88, "unit": "U/L"}]}, sex="male")
    capped = [x for x in r["disease_risks"] if x["evidence_capped"]]
    check("a single abnormal marker cannot produce high-confidence disease claims",
          all(x["evidence_level"] in ("Low", "Limited", "Moderate") for x in r["disease_risks"]),
          "levels=%s" % [(x["name"], x["evidence_level"]) for x in r["disease_risks"]])
    check("thin coverage is reported as capped", capped or not r["disease_risks"])


def test_file_formats():
    root = Path(__file__).resolve().parents[1] / "samples"
    for f in ["p1_metabolic.json", "p7_report.pdf", "p8_thyroid.csv"]:
        path = root / f
        if not path.exists():
            check("sample %s exists" % f, False)
            continue
        r = PIPE.run(path.read_bytes(), f)
        check("%s yields parameters" % f, r["summary"]["parameters_recognised"] >= 8,
              "got %d" % r["summary"]["parameters_recognised"])


def test_single_marker_findings_are_not_understated():
    """A condition whose whole stated criterion is one marker must not read as
    'not enough data' when that marker is present and clearly abnormal.

    Regression: supporting signals that were never ordered used to sit in the
    confidence denominator, so a complete criterion scored as if two thirds of the
    evidence were missing."""
    cases = [
        ("G6PD Deficiency", {"G6PD": (1.8, "U/g Hb")},
         "reference criterion is 'Low quantitative G6PD enzyme level' - nothing else"),
        ("Hyperprolactinemia", {"Prolactin": (180, "ng/mL")},
         "criterion is 'Significantly elevated prolactin'"),
    ]
    for target, tests, why in cases:
        r = analyse({"Gender": "female", "tests": [
            {"test_name": k, "value": v[0], "unit": v[1]} for k, v in tests.items()]})
        hit = [x for x in r["disease_risks"] if x["name"] == target]
        check("%s is raised at all" % target, bool(hit), why)
        if hit:
            check("%s scores above the weak band (%s)" % (target, why),
                  hit[0]["score"] >= 0.45, "score %.2f" % hit[0]["score"])


def test_time_critical_finding_raises_the_urgent_banner():
    """Regression: the urgent list was gated on evidence level, which the coverage cap
    had already pushed to 'Limited', so a lone troponin produced no warning at all."""
    r = analyse({"Gender": "male", "tests": [
        {"test_name": "Troponin I", "value": 2.4, "unit": "ng/mL"}]})
    names = [x["name"] for x in r["urgent_findings"]]
    check("a troponin 60x the upper limit raises a time-critical warning",
          any("Cardiovascular" in n for n in names), "urgent_findings=%s" % names)

    r = analyse({"Gender": "male", "tests": [
        {"test_name": "Potassium", "value": 6.6, "unit": "mmol/L"}]})
    check("severe hyperkalaemia raises a time-critical warning",
          bool(r["urgent_findings"]),
          "urgent_findings=%s" % [x["name"] for x in r["urgent_findings"]])


def test_urgent_banner_ignores_floor_scraping_findings():
    """The banner must stay quiet for a barely-reported finding, or it cries wolf."""
    r = analyse({"Gender": "male", "tests": [
        {"test_name": "Platelet Count", "value": 11000, "unit": "/uL"}]})
    names = [x["name"] for x in r["urgent_findings"]]
    check("an isolated platelet count does not raise DIC as an emergency",
          not any("Disseminated" in n for n in names), "urgent_findings=%s" % names)


def test_polycythemia_does_not_discount_its_own_trigger():
    """Regression: haemoglobin, haematocrit and RBC count share one redundancy group,
    so requiring two triggers guaranteed the second was discounted as a duplicate."""
    r = analyse({"Gender": "male", "tests": [
        {"test_name": "Haemoglobin", "value": 19.4, "unit": "g/dL"},
        {"test_name": "Haematocrit", "value": 58, "unit": "%"},
        {"test_name": "RBC Count", "value": 6.9, "unit": "million/uL"}]})
    hit = [x for x in r["disease_risks"] if x["name"] == "Polycythemia"]
    check("raised haemoglobin and haematocrit raise Polycythemia", bool(hit))
    if hit:
        check("Polycythemia is not scored as weak evidence",
              hit[0]["score"] >= 0.6, "score %.2f" % hit[0]["score"])


def test_evidence_level_never_contradicts_the_score():
    """A finding must never print a strong score beside a 'not enough data' label."""
    payloads = [
        {"tests": [{"test_name": "Troponin I", "value": 2.4, "unit": "ng/mL"}]},
        {"tests": [{"test_name": "G6PD", "value": 1.8, "unit": "U/g Hb"}]},
        {"tests": [{"test_name": "TSH", "value": 14.2, "unit": "uIU/mL"},
                   {"test_name": "Free T4", "value": 0.5, "unit": "ng/dL"}]},
    ]
    floor = {"High": 0.48, "Moderate": 0.28, "Low": 0.0, "Limited": 0.0}
    for p in payloads:
        r = analyse(dict(p, Gender="male"))
        for x in r["disease_risks"]:
            # A high score may be stepped DOWN by thin data, but never more than one
            # band - "Limited" beside 0.79 is a contradiction, not a caveat.
            check("%s: level %s is compatible with score %.2f"
                  % (x["name"][:28], x["evidence_level"], x["score"]),
                  not (x["score"] >= 0.72 and x["evidence_level"] == "Limited"),
                  "score %.2f labelled %s" % (x["score"], x["evidence_level"]))


# ================= hard exclusion gates (veto logic) =================

def _panel(sex="male", **tests):
    return {"Gender": sex, "tests": [
        {"test_name": k.replace("_", " "), "value": v[0],
         **({"unit": v[1]} if len(v) > 1 and v[1] else {})}
        for k, v in tests.items()]}


def _names(r, key="disease_risks"):
    return [x["name"] for x in r[key]]


def test_A_hepatitis_b_vetoed_by_non_reactive_hbsag():
    """A definitive negative outranks any number of non-specific secondary signals."""
    r = analyse({"Gender": "male", "tests": [
        {"test_name": "HBsAg", "value": "Non-Reactive"},
        {"test_name": "SGPT", "value": 96, "unit": "U/L"},
        {"test_name": "SGOT", "value": 88, "unit": "U/L"},
        {"test_name": "Lymphocytes", "value": 52, "unit": "%"}]})
    check("A: Hepatitis B is not raised when HBsAg is non-reactive",
          "Hepatitis B" not in _names(r), "got %s" % _names(r))
    sup = [s for s in r["suppressed_findings"] if s.get("disease") == "Hepatitis B"]
    check("A: the suppression is recorded with a reason", bool(sup))
    if sup:
        check("A: the reason names the hard exclusion",
              "hard exclusion" in sup[0]["reason"].lower(), sup[0]["reason"])
    leak = [x for x in r["recommendations"]
            if "hepatitis b" in (x["text"] + x["because"] + " ".join(x["sources"])).lower()]
    check("A: no recommendation is generated from the vetoed finding", not leak)


def test_B_hepatitis_b_vetoed_by_numeric_coi_below_cutoff():
    """0.80 COI is a NEGATIVE result. Before this fix it read as positive and raised
    Hepatitis B - the titre fallback treated any number > 0 as reactive."""
    r = analyse({"Gender": "male", "tests": [
        {"test_name": "HBsAg", "value": 0.80, "unit": "COI"},
        {"test_name": "SGPT", "value": 96, "unit": "U/L"},
        {"test_name": "SGOT", "value": 88, "unit": "U/L"}]})
    p = param(r, "hbsag")
    check("B: HBsAg 0.80 COI is read as negative, not positive",
          p and p["status"] == "negative", "status=%s" % (p["status"] if p else None))
    check("B: Hepatitis B is suppressed below the COI cut-off",
          "Hepatitis B" not in _names(r), "got %s" % _names(r))


def test_C_reactive_hbsag_is_not_vetoed():
    r = analyse({"Gender": "male", "tests": [
        {"test_name": "HBsAg", "value": "Reactive"},
        {"test_name": "SGPT", "value": 96, "unit": "U/L"},
        {"test_name": "SGOT", "value": 88, "unit": "U/L"}]})
    check("C: a reactive HBsAg is evaluated normally, not vetoed",
          "Hepatitis B" in _names(r), "got %s" % _names(r))
    check("C: nothing was suppressed", not r["suppressed_findings"])

    # A value above the assay cut-off must behave the same way.
    r2 = analyse({"Gender": "male", "tests": [
        {"test_name": "HBsAg", "value": 5.2, "unit": "COI"},
        {"test_name": "SGPT", "value": 96, "unit": "U/L"}]})
    check("C: HBsAg 5.2 COI is read as positive",
          "Hepatitis B" in _names(r2), "got %s" % _names(r2))


def test_D_missing_hbsag_is_not_treated_as_negative():
    """Missing must stay missing. Treating an absent test as negative would silently
    suppress real findings in sparse reports."""
    r = analyse({"Gender": "male", "tests": [
        {"test_name": "SGPT", "value": 96, "unit": "U/L"},
        {"test_name": "SGOT", "value": 88, "unit": "U/L"},
        {"test_name": "Lymphocytes", "value": 52, "unit": "%"}]})
    check("D: a missing HBsAg triggers no veto", not r["suppressed_findings"],
          "suppressed=%s" % [s.get("disease") for s in r["suppressed_findings"]])


def test_veto_does_not_fire_on_equivocal_result():
    r = analyse({"Gender": "male", "tests": [
        {"test_name": "HBsAg", "value": 0.95, "unit": "COI"},
        {"test_name": "SGPT", "value": 96, "unit": "U/L"}]})
    p = param(r, "hbsag")
    check("an in-between COI is equivocal, not negative",
          p and p["status"] == "indeterminate", "status=%s" % (p["status"] if p else None))
    check("an equivocal result does not veto", not r["suppressed_findings"])


def test_veto_needs_every_companion_marker_negative():
    """dengue's gate lists NS1 AND IgM. A negative NS1 with IgM missing must NOT veto."""
    r = analyse({"Gender": "male", "tests": [
        {"test_name": "Dengue NS1 Antigen", "value": "Negative"},
        {"test_name": "Platelet Count", "value": 46000, "unit": "/uL"}]})
    check("a negative NS1 with IgM missing does not rule out dengue",
          not [s for s in r["suppressed_findings"] if s.get("disease") == "Dengue Fever"])

    r2 = analyse({"Gender": "male", "tests": [
        {"test_name": "Dengue NS1 Antigen", "value": "Negative"},
        {"test_name": "Dengue IgM", "value": "Negative"},
        {"test_name": "Platelet Count", "value": 46000, "unit": "/uL"}]})
    check("both serology arms negative does rule out dengue",
          "Dengue Fever" not in _names(r2), "got %s" % _names(r2))


# ================= direct findings vs pattern exploration =================

def test_E_vitamin_d_is_a_direct_finding():
    r = analyse(_panel(Vitamin_D=(14, "ng/mL")))
    direct = _names(r, "direct_findings")
    check("E: Vitamin D Deficiency is a DIRECT finding",
          "Vitamin D Deficiency" in direct, "direct=%s" % direct)
    check("E: it is not listed as a pattern",
          "Vitamin D Deficiency" not in _names(r, "pattern_findings"))
    hit = [x for x in r["direct_findings"] if x["name"] == "Vitamin D Deficiency"][0]
    ev = hit["direct_evidence"]
    check("E: the measured value is carried for display", ev and ev["value"] == 14,
          "evidence=%s" % ev)
    check("E: the reference range is carried for display",
          ev.get("reference_high") is not None or ev.get("reference_low") is not None)


def test_multi_marker_conditions_stay_patterns():
    r = analyse(_panel(Triglycerides=(260, "mg/dL"), HDL_Cholesterol=(32, "mg/dL"),
                       Fasting_Blood_Sugar=(118, "mg/dL"), HbA1c=(6.1, "%"),
                       Systolic_BP=(146, "mmHg"), Diastolic_BP=(94, "mmHg")))
    patterns = _names(r, "pattern_findings")
    for name in ("Metabolic Syndrome", "Atherogenic Dyslipidemia"):
        check("%s is a pattern, not a direct finding" % name,
              name in patterns, "patterns=%s" % patterns[:6])


# ================= confounder penalty =================

def _osteo(r):
    hit = [x for x in r["disease_risks"] if x["name"] == "Osteomalacia/Rickets"]
    return hit[0]["score"] if hit else 0.0


def test_F_osteomalacia_damped_when_support_measured_and_normal():
    r = analyse(_panel(Vitamin_D=(14, "ng/mL"), Serum_Calcium=(9.4, "mg/dL"),
                       Serum_Phosphorus=(3.5, "mg/dL"), Alkaline_Phosphatase=(80, "U/L")))
    check("F: osteomalacia is damped when calcium, phosphate and ALP are all normal",
          _osteo(r) < 0.2, "score %.2f" % _osteo(r))
    check("F: vitamin D deficiency still appears as a direct finding",
          "Vitamin D Deficiency" in _names(r, "direct_findings"))


def test_G_osteomalacia_stronger_with_real_support():
    r = analyse(_panel(Vitamin_D=(9, "ng/mL"), Serum_Calcium=(7.9, "mg/dL"),
                       Serum_Phosphorus=(2.1, "mg/dL"), Alkaline_Phosphatase=(190, "U/L")))
    supported = _osteo(r)
    r_flat = analyse(_panel(Vitamin_D=(14, "ng/mL"), Serum_Calcium=(9.4, "mg/dL"),
                            Serum_Phosphorus=(3.5, "mg/dL"), Alkaline_Phosphatase=(80, "U/L")))
    check("G: supporting abnormalities give substantially stronger evidence",
          supported > _osteo(r_flat) + 0.25,
          "supported %.2f vs damped %.2f" % (supported, _osteo(r_flat)))


def test_H_missing_support_is_not_treated_as_normal():
    """The core distinction: absent evidence is not evidence of absence."""
    r_missing = analyse(_panel(Vitamin_D=(14, "ng/mL")))
    r_normal = analyse(_panel(Vitamin_D=(14, "ng/mL"), Serum_Calcium=(9.4, "mg/dL"),
                              Serum_Phosphorus=(3.5, "mg/dL"),
                              Alkaline_Phosphatase=(80, "U/L")))
    check("H: missing support scores higher than support measured and normal",
          _osteo(r_missing) > _osteo(r_normal),
          "missing %.2f vs normal %.2f" % (_osteo(r_missing), _osteo(r_normal)))
    check("H: with support missing the pattern is still reported, at low certainty",
          _osteo(r_missing) > 0, "score %.2f" % _osteo(r_missing))


def test_I_healthy_report_has_no_pattern_explosion():
    r = analyse(_panel("female",
                       Haemoglobin=(13.4, "g/dL"), TSH=(2.1, "uIU/mL"),
                       Fasting_Blood_Sugar=(88, "mg/dL"), HbA1c=(5.2, "%"),
                       Serum_Creatinine=(0.8, "mg/dL"), Triglycerides=(96, "mg/dL"),
                       HDL_Cholesterol=(62, "mg/dL"), Serum_Calcium=(9.4, "mg/dL"),
                       Vitamin_D=(42, "ng/mL"), Serum_Ferritin=(85, "ng/mL")))
    check("I: a healthy panel raises nothing at all",
          not r["disease_risks"], "got %s" % _names(r))
    check("I: neither tier has entries",
          not r["direct_findings"] and not r["pattern_findings"])


# ================= audit fixes: numeric results on qualitative tests =================

def test_bare_number_on_a_qualitative_test_is_equivocal_not_positive():
    """44 of 45 qualitative parameters used to read any number > 0 as positive, so an
    index result of 0.2 fabricated a finding. Without a declared cut-off the honest
    answer is equivocal: it fires neither a positive trigger nor a negative veto."""
    for test_name, pid in [("Dengue NS1 Antigen", "dengue_ns1"), ("Anti HCV", "anti_hcv"),
                           ("HIV", "hiv_screen"), ("Malaria Antigen", "malaria_antigen")]:
        r = analyse({"Gender": "male", "tests": [{"test_name": test_name, "value": 0.2}]})
        p = param(r, pid)
        check("%s 0.2 is equivocal, not positive" % test_name,
              p and p["status"] == "indeterminate",
              "status=%s" % (p["status"] if p else None))
        check("%s 0.2 is not flagged abnormal" % test_name, p and not p["abnormal"])


def test_equivocal_result_neither_triggers_nor_vetoes():
    r = analyse({"Gender": "male", "tests": [
        {"test_name": "Dengue NS1 Antigen", "value": 0.4},
        {"test_name": "Dengue IgM", "value": 0.4},
        {"test_name": "Platelet Count", "value": 46000, "unit": "/uL"}]})
    check("an equivocal serology does not raise the disease",
          "Dengue Fever" not in [x["name"] for x in r["disease_risks"]])
    check("an equivocal serology does not veto the disease either",
          not [s for s in r["suppressed_findings"] if s.get("disease") == "Dengue Fever"],
          "suppressed=%s" % [s.get("disease") for s in r["suppressed_findings"]])


def test_dipstick_and_titre_notation_still_read_as_positive():
    """Notation must keep working - these are facts about how a result is written,
    not clinical thresholds."""
    for value, pid, name in [("3+", "urine_protein", "Urine Protein"),
                             ("2+", "urine_blood", "Urine Blood"),
                             ("1:160", "widal_test", "Widal Test"),
                             ("1:8", "vdrl_rpr", "VDRL")]:
        r = analyse({"Gender": "male", "tests": [{"test_name": name, "value": value}]})
        p = param(r, pid)
        check("%s %r still reads as positive" % (name, value),
              p and p["status"] == "positive", "status=%s" % (p["status"] if p else None))


def test_trace_result_is_recorded_not_dropped():
    """'Trace' returned None, which silently discarded the parameter entirely."""
    r = analyse({"Gender": "male", "tests": [{"test_name": "Urine Protein", "value": "Trace"}]})
    p = param(r, "urine_protein")
    check("a Trace result is recorded rather than dropped", p is not None)
    if p:
        check("a Trace result is equivocal, not a positive finding",
              p["status"] == "indeterminate", "status=%s" % p["status"])


def test_declared_cutoff_honours_less_than_and_greater_than():
    for value, expected in [("<0.90", "negative"), (">8.0", "positive"),
                            (0.80, "negative"), (5.2, "positive"), (0.95, "indeterminate")]:
        r = analyse({"Gender": "male", "tests": [
            {"test_name": "HBsAg", "value": value, "unit": "COI"}]})
        p = param(r, "hbsag")
        check("HBsAg %r -> %s" % (value, expected),
              p and p["status"] == expected, "got %s" % (p["status"] if p else None))


# ================= audit fixes: impossible values =================

def test_impossible_values_are_rejected_not_reported_as_critical():
    """A haemoglobin of -5 is a typo, not a critical finding. Reporting it as
    'critical low' turns a data fault into a clinical alarm."""
    r = analyse({"Gender": "male", "tests": [
        {"test_name": "Haemoglobin", "value": -5, "unit": "g/dL"},
        {"test_name": "Neutrophils", "value": 140, "unit": "%"},
        {"test_name": "HbA1c", "value": 6.1, "unit": "%"}]})
    check("a negative haemoglobin is not used", param(r, "hemoglobin") is None)
    check("a percentage above 100 is not used", param(r, "neutrophil_pct") is None)
    check("valid parameters in the same report are still read",
          param(r, "hba1c") is not None)
    check("the rejected values are reported, not silently dropped",
          len(r["warnings"]) >= 2, "warnings=%s" % r["warnings"])


def test_extreme_but_possible_values_are_kept_with_a_warning():
    """Genuinely extreme results occur; dropping one would be far worse than flagging."""
    r = analyse({"Gender": "male", "tests": [
        {"test_name": "Serum Ferritin", "value": 4000, "ature": None, "unit": "ng/mL"}]})
    p = param(r, "ferritin")
    check("an extreme but possible ferritin is still used", p is not None)


# ================= audit fixes: reference range parsing =================

def test_reference_range_with_thousands_separators():
    """'4,000 - 15,000' failed to parse, so the lab's own range was silently discarded."""
    from engine.extract import parse_reference_range
    check("'13,500 - 17,000' parses", parse_reference_range("13,500 - 17,000") == (13500.0, 17000.0),
          "got %s" % (parse_reference_range("13,500 - 17,000"),))
    check("'150,000 - 450,000' parses",
          parse_reference_range("150,000 - 450,000") == (150000.0, 450000.0))


def test_titre_in_a_reference_range_field_is_not_read_as_an_interval():
    from engine.extract import parse_reference_range
    check("'1:8' is not read as the interval 1 to 8",
          parse_reference_range("1:8") == (None, None),
          "got %s" % (parse_reference_range("1:8"),))


def test_lab_reference_range_wins_over_a_non_guideline_band():
    """A WBC of 12,000 was flagged abnormal against a lab range of 4,000-15,000, because
    a hardcoded band overrode the laboratory that ran the assay."""
    r = analyse({"Gender": "male", "tests": [
        {"test_name": "Total WBC Count", "value": 12000, "unit": "/uL",
         "reference_range": "4,000 - 15,000"}]})
    p = param(r, "wbc_count")
    check("a value inside the lab's own range is not flagged abnormal",
          p and not p["abnormal"], "abnormal=%s" % (p["abnormal"] if p else None))

    r2 = analyse({"Gender": "male", "tests": [
        {"test_name": "Total WBC Count", "value": 18000, "unit": "/uL",
         "reference_range": "4,000 - 15,000"}]})
    check("a value outside the lab's own range is still flagged",
          param(r2, "wbc_count")["abnormal"])


def test_band_severity_survives_when_the_lab_range_decides_abnormality():
    """The lab's range decides WHETHER a value is abnormal; the band still decides HOW
    abnormal. Dropping the band outright graded a B12 of 143 as 'mild'."""
    r = analyse({"Gender": "female", "tests": [
        {"test_name": "Vitamin B12", "value": 143, "unit": "pg/mL",
         "reference_range": "200 - 900"}]})
    p = param(r, "vitamin_b12")
    check("B12 143 is abnormal by the lab's range", p and p["abnormal"])
    check("and keeps the band's severity rather than a deviation ratio",
          p and p["grade"] in ("severe_low", "critical_low", "moderate_low"),
          "grade=%s" % (p["grade"] if p else None))


def test_guideline_thresholds_still_outrank_a_permissive_lab_range():
    """ADA and KDIGO thresholds hold whatever a lab prints."""
    r = analyse({"Gender": "male", "tests": [
        {"test_name": "HbA1c", "value": 6.6, "unit": "%", "reference_range": "4.0 - 7.0"}]})
    check("HbA1c 6.6 is still flagged despite a lab range up to 7.0",
          param(r, "hba1c")["abnormal"])
    r2 = analyse({"Gender": "male", "tests": [
        {"test_name": "eGFR", "value": 40, "unit": "mL/min/1.73m2",
         "reference_range": "30 - 120"}]})
    check("eGFR 40 is still flagged despite a lab range down to 30",
          param(r2, "egfr")["abnormal"])


# ================= audit fixes: duplicate resolution =================

def test_conflicting_duplicates_do_not_silently_pick_the_worse_value():
    """severity_score was the tiebreaker, so the engine quietly preferred the more
    alarming reading while the audit line claimed it kept the first record."""
    r = analyse({"Gender": "male", "tests": [
        {"test_name": "Haemoglobin", "value": 14.0, "unit": "g/dL"},
        {"test_name": "Hemoglobin", "value": 9.0, "unit": "g/dL"}]})
    p = param(r, "hemoglobin")
    check("the first record is kept, as the audit line states",
          p and p["value"] == 14.0, "kept %s" % (p["value"] if p else None))
    dup = r["duplicates_resolved"][0]
    check("the conflict is flagged", dup["conflicting_values"])
    check("the note names both values so the reader can check the original",
          any("14" in n and "9" in n for n in p["notes"]), "notes=%s" % p["notes"])


# ========== audit round 2: found on a real Metropolis report ==========

def test_negated_status_beats_the_word_it_negates():
    """'Non Reactive, 0.26' was read as POSITIVE: 'reactive' matched the positive
    vocabulary and positives were scanned first. Longest match now wins, because the
    phrase that negates a word is always longer than the word it negates."""
    from engine.normalize import Normalizer
    from engine.config import get_config
    n = Normalizer(get_config())
    for raw, expected in [("Non Reactive, 0.26", "negative"), ("Non Reactive", "negative"),
                          ("Non-Reactive", "negative"), ("Not Detected", "negative"),
                          ("No Growth", "negative"), ("Reactive", "positive"),
                          ("Reactive, 5.2", "positive"), ("positive for IgM", "positive")]:
        check("%r reads as %s" % (raw, expected), n._qual_status(raw) == expected,
              "got %s" % n._qual_status(raw))


def test_hepatitis_b_vetoed_on_combined_status_and_value():
    """The real-world format: a status and a COI value in one field."""
    r = analyse({"Gender": "male", "tests": [
        {"test_name": "HBsAg Screening", "value": "Non Reactive, 0.26", "unit": "COI"},
        {"test_name": "SGPT (ALT)", "value": 49, "unit": "U/L"},
        {"test_name": "Lymphocytes", "value": 48.3, "unit": "%"}]})
    check("HBsAg 'Non Reactive, 0.26' is negative",
          param(r, "hbsag") and param(r, "hbsag")["status"] == "negative")
    check("Hepatitis B is not reported",
          "Hepatitis B" not in [x["name"] for x in r["disease_risks"]],
          "got %s" % [x["name"] for x in r["disease_risks"]])
    check("and the suppression is recorded",
          any(s.get("disease") == "Hepatitis B" for s in r["suppressed_findings"]))


def test_camelcase_reference_range_keys_are_read():
    """MinValue/MaxValue lowercased to 'minvalue' and never matched 'min_value', so the
    laboratory's own ranges were discarded for every CamelCase report format."""
    r = analyse({"Gender": "male", "tests": [
        {"test_name": "TSH", "value": 5.05, "unit": "uIU/mL",
         "MinValue": "0.54", "MaxValue": "5.3"}]})
    p = param(r, "tsh")
    check("the report's own range is used", p and p["reference_source"] == "report",
          "source=%s" % (p["reference_source"] if p else None))
    check("TSH 5.05 inside the lab's 0.54-5.3 is not flagged abnormal",
          p and not p["abnormal"], "abnormal=%s" % (p["abnormal"] if p else None))


def test_report_reference_range_is_unit_converted():
    """A platelet count of 233 10^3/uL became 233,000 /uL and was then compared against
    the report's own '140 - 440', flagging a normal count as high."""
    r = analyse({"Gender": "male", "tests": [
        {"test_name": "Platelet count", "value": 233, "unit": "10^3/ul",
         "MinValue": "140", "MaxValue": "440"}]})
    p = param(r, "platelet_count")
    check("the value is converted", p and p["value"] == 233000)
    check("and so is the report's range", p and p["reference_high"] == 440000,
          "ref %s-%s" % (p["reference_low"], p["reference_high"]))
    check("a normal platelet count is not flagged", p and not p["abnormal"])


def test_a_ratio_never_resolves_to_one_of_its_own_analytes():
    """'Apolipoprotein B/A1 Ratio' split on the slash and matched 'Apolipoprotein B', so
    the ratio 1.23 was stored as an ApoB of 1.23 and the real ApoB of 142 was lost."""
    cfg = get_config()
    for name, expected in [("Apolipoprotein B/A1 Ratio", "apo_b_apo_a1_ratio"),
                           ("Albumin/Globulin Ratio", "ag_ratio"),
                           ("Apolipoproteins B", "apo_b"),
                           ("Apolipoproteins A1", "apo_a1")]:
        check("%r -> %s" % (name, expected), cfg.resolve_alias(name) == expected,
              "got %s" % cfg.resolve_alias(name))

    r = analyse({"Gender": "male", "tests": [
        {"test_name": "Apolipoproteins A1", "value": 115, "unit": "mg/dL"},
        {"test_name": "Apolipoproteins B", "value": 142, "unit": "mg/dL"},
        {"test_name": "Apolipoprotein B/A1 Ratio", "value": 1.23}]})
    check("ApoB keeps its real value, not the ratio",
          param(r, "apo_b") and param(r, "apo_b")["value"] == 142,
          "apo_b=%s" % (param(r, "apo_b") or {}).get("value"))
    check("the ratio lands on its own parameter",
          param(r, "apo_b_apo_a1_ratio") is not None)


def test_real_report_matches_the_laboratorys_own_flags():
    """End to end on the shape of a real Metropolis report: every parameter the lab
    flagged, and nothing it called normal."""
    tests = [
        {"test_name": "HBsAg Screening", "value": "Non Reactive, 0.26", "unit": "COI"},
        {"test_name": "TSH", "value": 5.05, "unit": "uIU/mL", "MinValue": "0.54", "MaxValue": "5.3"},
        {"test_name": "SGPT (ALT)", "value": 49, "unit": "U/L", "MinValue": "0", "MaxValue": "41"},
        {"test_name": "SGOT (AST)", "value": 29, "unit": "U/L", "MinValue": "0", "MaxValue": "40"},
        {"test_name": "Platelet count", "value": 233, "unit": "10^3/ul",
         "MinValue": "140", "MaxValue": "440"},
        {"test_name": "Calcium, Serum", "value": 10.0, "unit": "mg/dL",
         "MinValue": "8.6", "MaxValue": "10.0"},
        {"test_name": "Prolactin", "value": 21.3, "unit": "ng/mL",
         "MinValue": "4.04", "MaxValue": "15.2"},
    ]
    r = analyse({"Gender": "male", "tests": tests})
    flagged = {p["parameter_id"] for p in r["abnormal_parameters"]}
    for pid in ("tsh", "sgot_ast", "platelet_count", "calcium"):
        check("%s is NOT flagged (the lab called it normal)" % pid, pid not in flagged)
    for pid in ("sgpt_alt", "prolactin"):
        check("%s IS flagged (the lab called it high)" % pid, pid in flagged)
    check("Hepatitis B is not reported on a non-reactive HBsAg",
          "Hepatitis B" not in [x["name"] for x in r["disease_risks"]])


def test_empty_and_garbage_input():
    r = analyse({"nothing": "here"})
    check("an empty payload is refused rather than analysed",
          r["analysed"] is False and r["summary"]["parameters_recognised"] == 0)
    check("an empty payload carries no risk findings",
          "disease_risks" not in r)
    obs, ctx, warn = extract(b"this is not a lab report at all", "junk.txt")
    check("unparseable text does not raise", isinstance(obs, list))


# ------------------------------------------------- is this a lab report at all?

NOT_REPORTS = [
    ("an invoice",
     b"INVOICE #4471\nAcme Consulting Pvt Ltd\nConsulting services August 2026\n"
     b"Subtotal 45000\nTax 8100\nTotal Due 53100\nPayment within 30 days."),
    ("a CV mentioning iron",
     b"CURRICULUM VITAE\nLuv Arora\nSoftware Engineer\nSkills: Python, Iron-clad testing\n"
     b"Experience: 6 years\nEducation: B.Tech 2019"),
    ("a recipe mentioning sugar and iron",
     b"Gajar Halwa\nIngredients: 1 kg carrots, 200 g sugar, 1 litre milk.\n"
     b"Carrots are rich in iron and vitamin A.\nSimmer 40 minutes, add sugar."),
    ("a rental agreement",
     b"RENTAL AGREEMENT between Lessor and Lessee for 14 MG Road. Monthly rent Rs 35000 "
     b"payable on the 5th. Security deposit Rs 210000. Term 11 months."),
    ("a bank statement",
     b"ACCOUNT STATEMENT\n01 Aug Opening balance 128400.50\n04 Aug UPI transfer 2300.00\n"
     b"11 Aug Salary credit 95000.00\n28 Aug Closing balance 221100.50"),
]


def test_generic_documents_are_refused():
    for label, data in NOT_REPORTS:
        r = analyse(data, "doc.txt")
        check("%s is not analysed" % label, r["analysed"] is False,
              "status=%s params=%d" % (r.get("document", {}).get("status"),
                                       r["summary"]["parameters_recognised"]))
        check("%s is told why" % label,
              bool(r["document"]["title"]) and bool(r["document"]["guidance"]))


def test_blank_file_is_reported_as_unreadable():
    r = analyse(b"   \n  \n", "blank.txt")
    check("a blank file is flagged unreadable, not 'not a report'",
          r["document"]["status"] == "unreadable", r["document"]["status"])
    r = analyse(b'{"note":"hello"}', "note.json")
    check("a readable file with no tests is 'not a report', not 'unreadable'",
          r["document"]["status"] == "not_a_report", r["document"]["status"])


def test_real_reports_are_still_analysed():
    cases = [
        ("a sparse single-spaced text report",
         b"LABORATORY REPORT\nPatient: Mr Luv Arora\nHaemoglobin 11.2 g/dL (13.0-17.0)\n"
         b"Serum Creatinine 1.6 mg/dL (0.7-1.3)\nTSH 9.4 uIU/mL", "sparse.txt"),
        ("a two-test JSON payload",
         {"PatientName": "A", "tests": [
             {"test_name": "TSH", "value": 9.4, "unit": "uIU/mL"},
             {"test_name": "Free T4", "value": 0.6, "unit": "ng/dL"}]}, "r.json"),
        ("a single qualitative result",
         {"tests": [{"test_name": "Dengue NS1 Antigen", "value": "Positive"}]}, "d.json"),
    ]
    for label, data, name in cases:
        r = analyse(data, name)
        check("%s is analysed" % label, r["analysed"] is True,
              "status=%s" % r.get("document", {}).get("status"))


def test_the_action_plan_is_grouped_and_evidenced():
    """Every step used to arrive under its own category heading with no value attached:
    vitamin D alone produced five items across three headings and not one of them
    quoted the 14.2 that prompted them."""
    r = analyse({"Gender": "male", "Age": 34, "tests": [
        {"test_name": "Vitamin D (25-OH)", "value": 14.2, "unit": "ng/mL",
         "reference_range": "30 - 100"},
        {"test_name": "Apolipoproteins A1", "value": 115, "unit": "mg/dL"},
        {"test_name": "Apolipoproteins B", "value": 142, "unit": "mg/dL",
         "reference_range": "66 - 133"}]})
    recs = r["recommendations"]
    check("the plan still has steps", len(recs) > 0)

    for rec in recs:
        check("every step names the finding it is about (%s)" % rec["category"],
              bool(rec["finding"]), rec["text"][:40])

    vd = [x for x in recs if x["finding"] == "Vitamin D Deficiency"]
    check("vitamin D advice lands under one finding", len(vd) >= 2,
          "%d items" % len(vd))
    check("no two vitamin D steps share a category",
          len({x["category"] for x in vd}) == len(vd),
          str([x["category"] for x in vd]))
    check("vitamin D steps quote the measured 14.2",
          all(any(v["value"] == 14.2 for v in x["values"]) for x in vd))

    apo = [x for x in recs if x["values"] and
           any(v["name"].startswith("Apolipoprotein B") for v in x["values"])]
    check("ApoB steps quote 142, not the ratio",
          apo and all(any(v["value"] == 142 for v in x["values"]) for x in apo),
          str([[(v["name"], v["value"]) for v in x["values"]] for x in apo]))


def test_a_step_quotes_a_trigger_that_the_lab_called_normal():
    """TSH 5.05 falls inside a 0.54-5.3 lab range but in the 4-10 subclinical band that
    fires the pattern. Filtering the plan's evidence on the lab flag alone hid the one
    number the advice is about."""
    r = analyse({"Gender": "male", "Age": 40, "tests": [
        {"test_name": "TSH", "value": 5.05, "unit": "uIU/mL",
         "reference_range": "0.54 - 5.3"}]})
    vals = [v for rec in r["recommendations"] for v in rec["values"]
            if v["name"] == "TSH"]
    check("the subclinical TSH is shown", bool(vals), str(r["recommendations"])[:120])
    if vals:
        check("it is marked as inside the lab range", vals[0]["in_range"] is True)
        check("and carries the band that flagged it",
              "subclinical" in (vals[0]["reading"] or "").lower(),
              str(vals[0]["reading"]))


def test_follow_up_timing_is_lifted_out_of_the_prose():
    from engine.recommend import TIMEFRAME_RE
    for text, expected in [("a repeat liver panel in 4-6 weeks to see", "in 4-6 weeks"),
                           ("recheck PTH after 8-12 weeks of", "after 8-12 weeks"),
                           ("a repeat sample 6-8 weeks apart", "6-8 weeks"),
                           ("review every 6 months with", "every 6 months")]:
        m = TIMEFRAME_RE.search(text)
        check("%r -> %r" % (text[:24], expected), m and m.group(0) == expected,
              "got %s" % (m and m.group(0)))
    for text in ["15-30 minutes of sunlight on arms",
                 "separate it by at least 4 hours from iron"]:
        check("no false schedule in %r" % text[:26],
              TIMEFRAME_RE.search(text) is None,
              "got %s" % (TIMEFRAME_RE.search(text) or [None]))


# ---------------------------------------------------------------- run

def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        try:
            t()
        except Exception as exc:                      # a crash is a failure, not a stop
            check("%s raised %s" % (t.__name__, type(exc).__name__), False, str(exc))

    failed = [r for r in RESULTS if not r[0]]
    for ok, name, detail in RESULTS:
        if not ok:
            print("FAIL  %s   %s" % (name, detail))
    print("-" * 70)
    print("%d checks, %d passed, %d failed" % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
