"""Ground truth for the PDF-versus-JSON equivalence tests.

One broad panel, written ONCE, rendered into several PDF layouts that real Indian
laboratory reports actually use and also emitted as JSON. Whatever the engine reads from
the JSON is the reference: a PDF of the same results should produce the same canonical
parameters, the same values and the same abnormal flags, unless the PDF genuinely does
not carry the information.

The names are deliberately written the way reports print them - upper case, bracketed
abbreviations, British spellings - rather than the way the dictionary stores them, since
that gap is exactly what was going unrecognised.

Fields: printed name, value (as printed), unit, reference range (as printed), flag,
expected canonical parameter id, and whether it is abnormal against the printed range.
"""

PATIENT = {"name": "Mr Test Patient", "age": 52, "sex": "Male", "uhid": "NG-000123"}

# (section, printed name, value, unit, printed range, flag, parameter_id, abnormal)
PANEL = [
    # --- inflammation ---
    ("Inflammation", "HIGHLY SENSITIVE C-REACTIVE PROTEIN (hs-CRP)", "18.40", "mg/L",
     "< 1.0", "H", "hs_crp", True),
    ("Inflammation", "Erythrocyte Sedimentation Rate (ESR)", "38", "mm/hr",
     "0 - 15", "H", "esr", True),

    # --- diabetes ---
    ("Diabetes", "HbA1c (Glycosylated Haemoglobin)", "7.9", "%",
     "4.0 - 5.6", "H", "hba1c", True),
    ("Diabetes", "Glucose - Fasting", "142", "mg/dL", "70 - 100", "H", "fasting_glucose", True),

    # --- lipids ---
    ("Lipid Profile", "Total Cholesterol", "236", "mg/dL", "< 200", "H", "total_cholesterol", True),
    ("Lipid Profile", "HDL Cholesterol", "38", "mg/dL", "> 40", "L", "hdl_cholesterol", True),
    ("Lipid Profile", "Triglycerides", "212", "mg/dL", "< 150", "H", "triglycerides", True),
    ("Lipid Profile", "Apolipoprotein B (Apo B)", "142", "mg/dL", "66 - 133", "H", "apo_b", True),
    ("Lipid Profile", "Apolipoprotein A1 (Apo A1)", "115", "mg/dL", "104 - 202", "", "apo_a1", False),
    ("Lipid Profile", "Apo B / Apo A1 Ratio", "1.23", "", "0.35 - 1.0", "H",
     "apo_b_apo_a1_ratio", True),

    # --- liver ---
    ("Liver Function Test", "SGPT (ALT)", "68", "U/L", "0 - 41", "H", "sgpt_alt", True),
    ("Liver Function Test", "SGOT (AST)", "29", "U/L", "0 - 40", "", "sgot_ast", False),
    ("Liver Function Test", "Bilirubin - Total", "0.8", "mg/dL", "0.3 - 1.2", "", "total_bilirubin", False),

    # --- kidney ---
    ("Kidney Function Test", "Creatinine, Serum", "1.62", "mg/dL", "0.7 - 1.3", "H", "creatinine", True),
    ("Kidney Function Test", "Blood Urea Nitrogen (BUN)", "14", "mg/dL", "7 - 20", "", "bun", False),

    # --- thyroid ---
    ("Thyroid Profile", "TSH (Thyroid Stimulating Hormone)", "8.9", "uIU/mL", "0.27 - 4.2", "H", "tsh", True),
    ("Thyroid Profile", "Free T4 (FT4)", "0.71", "ng/dL", "0.93 - 1.7", "L", "free_t4", True),

    # --- vitamins ---
    ("Vitamins", "Vitamin D (25-Hydroxy)", "11.6", "ng/mL", "30 - 100", "L", "vitamin_d", True),
    ("Vitamins", "Vitamin B12", "164", "pg/mL", "197 - 771", "L", "vitamin_b12", True),

    # --- blood counts ---
    ("Complete Blood Count", "Haemoglobin (Hb)", "10.9", "g/dL", "13.0 - 17.0", "L", "hemoglobin", True),
    ("Complete Blood Count", "Total Leucocyte Count (TLC)", "11,800", "/cumm", "4,000 - 10,000", "H",
     "wbc_count", True),
    ("Complete Blood Count", "Platelet Count", "2.4", "lakh/cumm", "1.5 - 4.1", "", "platelet_count", False),
    ("Complete Blood Count", "MCV (Mean Corpuscular Volume)", "71.2", "fL", "83 - 101", "L", "mcv", True),

    # --- hormones ---
    ("Hormones", "Prolactin", "21.3", "ng/mL", "4.04 - 15.2", "H", "prolactin", True),
    ("Hormones", "Testosterone, Total", "397", "ng/dL", "249 - 836", "", "testosterone_total", False),

    # --- urine / categorical ---
    ("Urine Routine", "Urine Glucose", "Positive", "", "Negative", "", "urine_glucose", True),
    ("Urine Routine", "Urine Protein", "Negative", "", "Negative", "", "urine_protein", False),
    ("Serology", "HBsAg", "Non Reactive", "", "Non Reactive", "", "hbsag", False),
]


def as_json():
    """The same panel as a JSON report: the equivalence reference."""
    tests = []
    for section, name, value, unit, rng, flag, _pid, _abn in PANEL:
        t = {"test_name": name, "value": value, "section": section}
        if unit:
            t["unit"] = unit
        if rng:
            t["reference_range"] = rng
        if flag:
            t["flag"] = flag
        tests.append(t)
    return {"patient_name": PATIENT["name"], "age": PATIENT["age"],
            "gender": PATIENT["sex"], "uhid": PATIENT["uhid"], "tests": tests}


def expected():
    """parameter_id -> (value as number or text, abnormal) that every layout must yield."""
    out = {}
    for _s, _n, value, _u, _r, _f, pid, abn in PANEL:
        v = value.replace(",", "")
        try:
            v = float(v)
        except ValueError:
            pass
        out[pid] = (v, abn)
    return out
