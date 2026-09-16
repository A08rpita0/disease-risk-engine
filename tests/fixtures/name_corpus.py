"""Test names as laboratory reports actually print them.

POSITIVE: printed name -> the canonical parameter it must resolve to.
NEGATIVE: printed name -> a parameter it must NOT resolve to. These guard against the
          alias system becoming greedy: a different assay that merely shares words with
          a known test must stay unmapped rather than be silently filed as that test.

Spellings are drawn from the formats Indian reference laboratories use (upper case,
bracketed abbreviations, British spellings, "Serum"/"S." prefixes, trailing method
names). No clinical thresholds live here - only names.
"""

POSITIVE = [
    # --- hs-CRP / CRP ---
    ("hs-CRP", "hs_crp"),
    ("HsCRP", "hs_crp"),
    ("HS CRP", "hs_crp"),
    ("High Sensitivity CRP", "hs_crp"),
    ("High Sensitive C-Reactive Protein", "hs_crp"),
    ("HIGHLY SENSITIVE C-REACTIVE PROTEIN", "hs_crp"),
    ("HIGHLY SENSITIVE C-REACTIVE PROTEIN (hs-CRP)", "hs_crp"),
    ("High Sensitivity C Reactive Protein", "hs_crp"),
    ("C-Reactive Protein, High Sensitivity", "hs_crp"),
    ("hs-CRP (Cardiac)", "hs_crp"),
    ("C-Reactive Protein (CRP)", "crp"),
    ("CRP - Quantitative", "crp"),
    ("C Reactive Protein", "crp"),

    # --- HbA1c ---
    ("HbA1c", "hba1c"),
    ("Hb A1c", "hba1c"),
    ("HBA1C", "hba1c"),
    ("HbA1C (Glycosylated Haemoglobin)", "hba1c"),
    ("Glycated Haemoglobin", "hba1c"),
    ("Glycosylated Haemoglobin", "hba1c"),
    ("Glycosylated Hemoglobin (HbA1c)", "hba1c"),
    ("GLYCATED HAEMOGLOBIN (HbA1c), EDTA WHOLE BLOOD", "hba1c"),
    ("A1c", "hba1c"),
    ("Haemoglobin A1c", "hba1c"),

    # --- glucose ---
    ("Glucose - Fasting", "fasting_glucose"),
    ("Fasting Blood Sugar (FBS)", "fasting_glucose"),
    ("Blood Sugar Fasting", "fasting_glucose"),
    ("Plasma Glucose - Fasting", "fasting_glucose"),

    # --- vitamins ---
    ("Vitamin D (25-Hydroxy)", "vitamin_d"),
    ("25-Hydroxy Vitamin D", "vitamin_d"),
    ("25 OH Vitamin D", "vitamin_d"),
    ("Vitamin D Total - 25 Hydroxy (OH)", "vitamin_d"),
    ("25(OH) Vitamin D", "vitamin_d"),
    ("Vitamin D3", "vitamin_d"),
    ("Vitamin B12", "vitamin_b12"),
    ("Vit. B12", "vitamin_b12"),
    ("Cyanocobalamin (Vitamin B12)", "vitamin_b12"),

    # --- lipids / apolipoproteins ---
    ("Apolipoprotein B (Apo B)", "apo_b"),
    ("Apo B", "apo_b"),
    ("APO-B", "apo_b"),
    ("Apolipoproteins B", "apo_b"),
    ("Apolipoprotein A1 (Apo A1)", "apo_a1"),
    ("Apo A1", "apo_a1"),
    ("Apo B / Apo A1 Ratio", "apo_b_apo_a1_ratio"),
    ("Apolipoprotein B/A1 Ratio", "apo_b_apo_a1_ratio"),
    ("APO B/APO A1 RATIO", "apo_b_apo_a1_ratio"),
    ("LDL Cholesterol - Direct", "ldl_cholesterol"),
    ("HDL Cholesterol", "hdl_cholesterol"),
    ("Cholesterol - Total", "total_cholesterol"),
    ("Triglycerides", "triglycerides"),

    # --- liver ---
    ("SGPT (ALT)", "sgpt_alt"),
    ("Alanine Aminotransferase (ALT/SGPT)", "sgpt_alt"),
    ("ALT (SGPT)", "sgpt_alt"),
    ("SGOT (AST)", "sgot_ast"),
    ("Aspartate Aminotransferase (AST/SGOT)", "sgot_ast"),
    ("Bilirubin - Total", "total_bilirubin"),
    ("Gamma GT (GGT)", "ggt"),
    ("Alkaline Phosphatase (ALP)", "alkaline_phosphatase"),

    # --- kidney ---
    ("Creatinine, Serum", "creatinine"),
    ("S. Creatinine", "creatinine"),
    ("Blood Urea Nitrogen (BUN)", "bun"),
    ("Uric Acid", "uric_acid"),

    # --- thyroid ---
    ("TSH", "tsh"),
    ("TSH (Thyroid Stimulating Hormone)", "tsh"),
    ("Thyroid Stimulating Hormone - Ultrasensitive", "tsh"),
    ("Free T4 (FT4)", "free_t4"),
    ("FT4", "free_t4"),
    ("Free Thyroxine (FT4)", "free_t4"),
    ("Free T3", "free_t3"),

    # --- blood counts ---
    ("Haemoglobin (Hb)", "hemoglobin"),
    ("Hemoglobin", "hemoglobin"),
    ("Total Leucocyte Count (TLC)", "wbc_count"),
    ("Total Leucocytes Count (TLC)", "wbc_count"),
    ("WBC Count", "wbc_count"),
    ("Platelet Count", "platelet_count"),
    ("MCV (Mean Corpuscular Volume)", "mcv"),
    ("Absolute Lymphocyte Count", "absolute_lymphocyte_count"),
    ("Erythrocyte Sedimentation Rate (ESR)", "esr"),

    # --- hormones ---
    ("Prolactin", "prolactin"),
    ("Testosterone, Total", "testosterone_total"),

    # --- urine / serology ---
    ("Urine Glucose", "urine_glucose"),
    ("Urine Protein", "urine_protein"),
    ("HBsAg", "hbsag"),
    ("Hepatitis B Surface Antigen (HBsAg)", "hbsag"),
]

NEGATIVE = [
    # a different vitamin D assay - must never be filed as 25-OH vitamin D
    ("1,25-Dihydroxy Vitamin D", "vitamin_d"),
    ("1,25 (OH)2 Vitamin D", "vitamin_d"),
    # a free, not total, hormone
    ("Free Testosterone", "testosterone_total"),
    # the ratio is its own quantity, never one of its analytes
    ("Apo B / Apo A1 Ratio", "apo_b"),
    ("Albumin/Globulin Ratio", "albumin"),
    # urine and serum analytes are different parameters
    ("Urine Creatinine", "creatinine"),
    ("Urine Glucose", "fasting_glucose"),
    # standard CRP is not hs-CRP
    ("C-Reactive Protein (CRP)", "hs_crp"),
    # HbA1c is not haemoglobin
    ("HbA1c", "hemoglobin"),
    ("Glycated Haemoglobin", "hemoglobin"),
    # a tumour marker is not calcium
    ("CA 125", "calcium"),
    ("CA 19-9", "calcium"),
]
