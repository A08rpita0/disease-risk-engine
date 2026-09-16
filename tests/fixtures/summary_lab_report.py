"""A synthetic two-part report: a designed summary, then the laboratory result pages.

Built to reproduce the STRUCTURE of real multi-part reports, with invented values and a
fictitious patient. Every trait below broke extraction on a real report and is here so
it cannot break silently again:

  summary pages
    - values restated inside prose tiles: "Vitamin D 25 - Hydroxy: 8.7 ng/mL (Normal: ...)"
      (the "25" in the name was read as the result)
    - a two-column tile whose long name is cut at "(hs-" with "CRP):" continuing below
    - "Profile | Abnormal / Total | Key Results" grids and "Mineral Profile 1 / 1" tiles
    - no patient labels - those appear only on the laboratory pages
  laboratory pages
    - the column header is the LAST row of the patient-details box; results sit in the
      next grid, which has no header of its own
    - the method printed on a second line inside the name cell ("HPLC")
    - a name wrapped inside its cell ("... (hs-" / "CRP)" / method)
    - multi-band reference text ("Deficient <20 / Insufficient 21 - 29 / Sufficient 30 - 100")
    - "PCT" meaning plateletcrit (%), and absolute counts "Neutrophils." in 10^3/ul
    - a row that falls across the page break, outside the grid
    - an interpretation table with the SAME column count as the results grid
    - an HPLC peak table ("A1c | 5.8 | --- | 0.495 | 115559")
    - urine sub-headings, bare "Blood" / "Colour", "-" in the unit column, "1-2 /hpf"
    - "Reaction (pH)" with its method on the next line

TESTS lists every result as the laboratory pages state it; `as_json()` is the same report
as structured JSON, the reference the PDF is compared against.
"""

PATIENT = {"name": "Mr Test Kumar", "age": 47, "sex": "Male", "id": "TST0001"}

# section, name, method, value, unit (as printed), reference (as printed)
TESTS = [
    ("Complete Blood Count (CBC)", "Hemoglobin", "Cyanide free spectrophotometry", "12.1", "g/dL", "13.0 - 17.0"),
    ("Complete Blood Count (CBC)", "RDW (CV)", "Calculated", "15.2", "%", "11.6 - 14.0"),
    ("Complete Blood Count (CBC)", "TLC", "Electrical impedance", "7.4", "10^3/µl", "4 - 10"),
    ("Differential Leucocyte Count", "Neutrophils", "Flow-cytometry DHSS", "61.2", "%", "40 - 80"),
    ("Differential Leucocyte Count", "Basophils", "Flow-cytometry DHSS", "0.5", "%", "0 - 2"),
    ("Absolute Leukocyte Counts", "Neutrophils.", "Calculated", "4.53", "10^3/µl", "2 - 7"),
    ("Absolute Leukocyte Counts", "Basophils.", "Calculated", "0.04", "10^3/µl", "0.02-0.1"),
    ("Platelet Parameters", "Platelet Count", "Electrical impedance", "236", "10^3/µl", "150 - 410"),
    ("Platelet Parameters", "PCT", "Calculated", "0.24", "%", "0.17 - 0.32"),
    ("HbA1C (Glycosylated Haemoglobin)", "Glycosylated Hemoglobin (HbA1c)", "HPLC", "6.3", "%", "<5.7"),
    ("Liver Function Test (LFT)", "SGPT/ALT", "Enzymatic", "71.4", "U/L", "< 45"),
    ("Liver Function Test (LFT)", "ESR - Erythrocyte Sedimentation Rate", "MODIFIED WESTERGREN", "34", "mm/hr", "0 - 10"),
    ("Kidney Function Test (KFT)", "eGFR (CKD-EPI)", "", "58.2", "ml/min/1.73 sq m",
     "Normal Or High: >= 90\nMild Or Decrease: 60-89\nMild To Moderate Decrease: 45-59"),
    ("High Sensitivity C-Reactive Protein (Hs-CRP)", "HIGHLY SENSITIVE C-REACTIVE PROTEIN (hs-CRP)",
     "Immunoturbidimetric", "24.60", "mg/L", "<1.00"),
    ("Vitamin D 25 Hydroxy", "Vitamin D 25 - Hydroxy", "CMIA", "8.7", "ng/mL",
     "Deficient <20\nInsufficient 21 - 29\nSufficient 30 - 100"),
    ("Urine Routine and Microscopic Examination > Physical Examination", "Colour", "", "Pale yellow", "-", "Pale yellow"),
    ("Urine Routine and Microscopic Examination > Chemical Examination", "Reaction (pH)", "Double Indicator", "6.0", "-", "4.5 - 8.0"),
    ("Urine Routine and Microscopic Examination > Chemical Examination", "Blood", "Peroxidase Hemoglobin", "Negative", "-", "Negative"),
    ("Urine Routine and Microscopic Examination > Microscopic Examination", "Pus Cells (WBCs)", "", "8-10", "/hpf", "0 - 5"),
    ("Urine Routine and Microscopic Examination > Microscopic Examination", "Epithelial Cells", "", "1-2", "/hpf", "0 - 4"),
]

# the page-break row: drawn below the grid on one page, its method line atop the next
PAGE_BREAK_ROW = "Basophils."

# (test name, value, unit, "Normal: ..." text) restated on the summary page
SUMMARY_TILES = [
    ("Vitamin D 25 - Hydroxy", "8.7", "ng/mL", "30–100 ng/mL"),
    ("SGPT/ALT", "71.4", "U/L", "0–45 U/L"),
    ("Glycosylated Hemoglobin (HbA1c)", "6.3", "%", "0–5.7 %"),
]


def as_json():
    tests = []
    for section, name, method, value, unit, rng in TESTS:
        t = {"section": section, "test_name": name, "value": value, "reference_range": rng}
        if method:
            t["method"] = method
        if unit and unit != "-":
            t["unit"] = unit
        tests.append(t)
    return {"patient_name": PATIENT["name"], "age": str(PATIENT["age"]), "gender": PATIENT["sex"],
            "patient_id": PATIENT["id"], "tests": tests}
