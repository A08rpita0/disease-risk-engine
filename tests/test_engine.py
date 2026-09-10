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
