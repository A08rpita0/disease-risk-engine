"""Synthetic reports in the layouts of four real laboratories' PDFs.

Invented patients and values. Each layout reproduces the TRAITS that broke extraction or
interpretation on a real report, so they cannot break silently again. Nothing here is a
real person's data, and nothing in the engine keys on these names or values.

stacked_bands  (a column-placed report with method lines and labelled reference bands)
  - header fields separated only by column gaps: "Name : Mr X   VID No. : 123"
  - "Age / Gender : 41 Year(s)/ Male" and a footer "Page 1 of 3" (read as age 1 before)
  - a band label and its interval in separate cells: "Deficiency | : < 20"
  - bands printed label-last: "< 40 : Low / 40 - 60 : Optimal / > 60 : Desirable"
    (the first band was read as HDL's normal range, calling HDL 52 high)
  - band continuation lines beside a method line in the name column
  - units: "gms%", "mill/cu.mm", "cells/cu.mm", and a superscript "10 3 / µl"
  - "Non Reactive,0.26" - a verdict with the assay index fused to it
  - "Blood group (ABO typing) | O" and "RBC Morphology" / "Remark | Normocytic ..."
  - "Apolipoproteins B/A1" as a heading (was filed as ApoB = 1)
  - urine microscopy continued on the next page with no heading; "Red blood cells
    12 /hpf" (a count, not the dipstick blood test); "CRY - Uric acid"; "Casts -
    Pathological"
  - LH and FSH for a man (the LH/FSH ratio's criterion is for women only)
  - interpretation prose containing "greater than 17 mg/dl"

method_column  (a gridless report whose last column is the METHOD)
  - "Patient Name : Ms. X   Request Date", "Age / Sex : 38 Y / Female", "Patient No : X"
  - method words after the range: "12 - 15.5  Colorimetric", "< 4.5  Calculation"
  - "10^3 /uL" and "10^3 cells/uL" split into tokens (285 read as 285 /uL before)
  - "--" in the unit column of a ratio, "A/G RATIO" with no unit
  - fused band labels "Optimum>60", "Normal:4.6-5.6"; "< or = 0.90"; "Up to 1.2"
  - troponin T with the assay's own upper limit

TESTS: (page, section, name, method, value, unit as printed, range as printed).
"""

STACKED = {
    "patient": {"name": "Mr. Test Rao", "age": "41 Year(s)", "sex": "Male", "patient_id": "TP0000123"},
    "tests": [
        (1, "LIPID PROFILE", "Cholesterol - Total", "(Method: Cholesterol Oxidase)", "175", "mg/dL",
         "<200 - Desirable\n200-239 - Borderline risk\n>240 - High risk"),
        (1, "LIPID PROFILE", "Cholesterol - HDL", "(Method: Enzymatic Colorimetric)", "52", "mg/dL",
         "< 40 : Low\n40 - 60 : Optimal\n> 60 : Desirable"),
        (1, "LIPID PROFILE", "Cholesterol - LDL", "(Method: Calculated)", "112", "mg/dL",
         "< 100 : Normal\n100 - 129 : Desirable\n130 - 159 : Borderline-High"),
        (1, None, "Vitamin D Total (D2+D3 Fractionated)", "(Method: Chemiluminescence)", "25.4", "ng/mL",
         "Deficiency : < 20\nInsufficiency : 20-29\nOptimum Level : 30-80"),
        (1, "HEMATOLOGY", "Hemoglobin", "(Method: Photometric)", "17.2", "gms%", "13.0-17.0"),
        (1, "HEMATOLOGY", "Erythrocyte (RBC) Count", None, "5.50", "mill/cu.mm", "4.4-6.0"),
        (1, "HEMATOLOGY", "Total Leucocytes Count (TLC)", None, "6,700", "cells/cu.mm", "4300-10300"),
        (1, "HEMATOLOGY", "Platelet count", None, "233", "10^3 / µl", "140-440"),
        (1, "HEMATOLOGY", "RBC Morphology", None, "Normocytic Normochromic", None, None),
        (2, None, "HBsAg Screening", "(Serum, ECLIA)", "Non Reactive,0.26", "COI",
         "Non Reactive: < 0.90\nReactive: >= 1.0"),
        (2, None, "Blood group (ABO typing)", None, "O", None, None),
        (2, "Apolipoproteins B/A1", "Apolipoprotein B/A1 Ratio", None, "1.23", None, "0.35-1.0"),
        (2, "HORMONES", "FSH (Follicle Stimulating Hormone)", None, "1.85", "mIU/mL", "1.4-15.4"),
        (2, "HORMONES", "LH (Luteinizing Hormone)", None, "4.56", "mIU/mL", "1.7-8.6"),
        (2, "Routine Examination Profile - Urine > MICROSCOPIC EXAMINATION", "Red blood cells", None,
         "12", "/hpf", "0-2"),
        (2, "Routine Examination Profile - Urine > MICROSCOPIC EXAMINATION", "Casts - Pathological", None,
         "0.0", "/hpf", "0-0.34"),
        (2, "Routine Examination Profile - Urine > MICROSCOPIC EXAMINATION", "CRY - Uric acid", None,
         "0.0", None, "0-1.36"),
        (3, "Routine Examination Profile - Urine > MICROSCOPIC EXAMINATION", "Bacteria", None,
         "2.7", "/hpf", "0-65.00"),
        (3, "Routine Examination Profile - Urine > MICROSCOPIC EXAMINATION", "Yeast cells", None,
         "0.0", "/hpf", "0-0.68"),
    ],
}

METHOD_COLUMN = {
    "patient": {"name": "Ms. Test Devi", "age": "38 Y", "sex": "Female", "patient_id": "TST0042"},
    "tests": [
        (1, "HAEMATOLOGY > COMPLETE BLOOD COUNT (CBC)", "HEMOGLOBIN", "Colorimetric", "12.2", "g/dL", "12 - 15.5"),
        (1, "HAEMATOLOGY > COMPLETE BLOOD COUNT (CBC)", "HEMATOCRIT", "Calculation", "36.1", "%", "37 - 54"),
        (1, "HAEMATOLOGY > COMPLETE BLOOD COUNT (CBC)", "PLATELET COUNT", "EI", "285", "10^3 /uL", "140 - 450"),
        (1, "HAEMATOLOGY > COMPLETE BLOOD COUNT (CBC)", "WBC COUNT", "EI", "6.1", "10^3 /uL", "4 - 10"),
        (1, "HAEMATOLOGY > DIFFERENTIAL COUNT (DC)", "ABSOLUTE NEUTROPHIL COUNT", None, "3.57", "10^3 cells/uL", "2 - 7"),
        (1, "CLINICAL BIOCHEMISTRY", "HBA1C", "HPLC", "5.6", "%",
         "Normal:4.6-5.6\nPrediabetes: 5.7- 6.4\nDiabetes: >6.5"),
        (1, "CLINICAL BIOCHEMISTRY > LIPID PROFILE TEST", "HDL CHOLESTEROL", "Direct", "47.6", "mg/dL",
         "Optimum>60\nBorderline : 50-59\nHigh risk : <50"),
        (1, "CLINICAL BIOCHEMISTRY > LIPID PROFILE TEST", "LDL CHOLESTEROL", "Direct", "185.3", "mg/dl",
         "Optimal: < 100\nNear/Above Optimal: 100 - 129"),
        (1, "CLINICAL BIOCHEMISTRY > LIPID PROFILE TEST", "TOTAL CHOLESTEROL / HDL RATIO", "Calculation",
         "5.1", "--", "< 4.5"),
        (2, "CLINICAL BIOCHEMISTRY > LIVER FUNCTION TEST", "ALT / SGPT", "IFCC", "6.4", "U/L", "7 - 40"),
        (2, "CLINICAL BIOCHEMISTRY > LIVER FUNCTION TEST", "BILIRUBIN (TOTAL)", "DSA", "0.15", "mg/dL", "Up to 1.2"),
        (2, "CLINICAL BIOCHEMISTRY > LIVER FUNCTION TEST", "INDIRECT BILIRUBIN", "Calculation", "0.08", "mg/dL",
         "< or = 0.90"),
        (2, "CLINICAL BIOCHEMISTRY > LIVER FUNCTION TEST", "A/G RATIO", "Calculation", "1.8", None, "0.8 - 2.0"),
        (2, "CARDIAC", "TROPONIN T", "ECLIA", "0.050", "ng/mL", "0 - 0.014"),
    ],
}

REPORTS = {"stacked_bands": STACKED, "method_column": METHOD_COLUMN}


def as_json(key):
    rep = REPORTS[key]
    tests = []
    for page, section, name, method, value, unit, rng in rep["tests"]:
        t = {"test_name": name, "value": value, "page": page}
        if unit and unit != "--":
            t["unit"] = unit
        if rng:
            t["reference_range"] = rng
        if section:
            t["section"] = section
        if method:
            t["method"] = method
        tests.append(t)
    return {"patient": dict(rep["patient"]), "tests": tests}
