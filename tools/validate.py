"""Technical validation harness.

Seven suites, each of which either passes or prints exactly why it failed:

  1. CONFIG       - every cross-reference resolves, every cohort cites its clinical
                    reference, every disease link quotes the Disease Master field behind
                    it, and no cluster can fire on an entirely normal panel.
  2. EVIDENCE     - the literature backing is measured, not assumed: citation coverage
                    across cohorts and the distinct reference count.
  3. CLINICAL     - hand-built cases with expectations stated up front, including
                    negative controls (a healthy panel must stay silent; a negative test
                    must never raise its disease).
  4. CONSISTENCY  - properties that must hold on every input: determinism, bounded
                    scores, monotonicity, mutually exclusive patterns never co-firing,
                    and a complete audit trail on every finding.
  5. MAPPING      - every cohort -> Disease Master link is exercised end to end through
                    the real pipeline. A link that resolves by name but can never fire
                    is a dead mapping and fails the suite.
  6. PARAM COVER  - every marker named in the Disease Master's own Related Markers/Tests
                    column resolves to a parameter, every parameter is evaluated by some
                    cluster or declared reported-only, and every condition uses the markers
                    its own row names.
  7. COVERAGE     - synthetic panels of 10, 15, 20 and 30 parameters drawn from real
                    panel compositions, measuring the fraction of records that produce a
                    usable, explained result. Target is ~80% across those sizes.

Run:  python tools/validate.py
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.config import get_config          # noqa: E402
from engine.pipeline import Pipeline          # noqa: E402
from tools.consistency import run_consistency, run_mapping   # noqa: E402
from tools.coverage import run as run_param_coverage          # noqa: E402

PASS, FAIL = "PASS", "FAIL"


# --------------------------------------------------------------------------
# 2. Clinical cases. Each states what MUST appear and what MUST NOT.
# --------------------------------------------------------------------------

CASES = [
    {
        "name": "Metabolic syndrome with diabetes",
        "sex": "male",
        "values": {
            "Fasting Blood Sugar": (132, "mg/dL"), "HbA1c": (7.8, "%"),
            "Triglycerides": (240, "mg/dL"), "HDL Cholesterol": (32, "mg/dL"),
            "LDL Cholesterol": (150, "mg/dL"), "Total Cholesterol": (232, "mg/dL"),
            "Systolic BP": (144, "mmHg"), "Diastolic BP": (94, "mmHg"),
            "Waist Circumference": (108, "cm"),
        },
        "expect": ["Metabolic Syndrome", "Diabetes Mellitus", "Atherogenic Dyslipidemia"],
        "expect_level": {"Metabolic Syndrome": ["High", "Moderate"],
                         "Diabetes Mellitus": ["High", "Moderate"]},
        "forbid": [],
    },
    {
        "name": "Iron deficiency anaemia, female",
        "sex": "female",
        "values": {
            "Haemoglobin": (8.9, "g/dL"), "MCV": (69, "fL"), "MCH": (21, "pg"),
            "Serum Ferritin": (4, "ng/mL"), "Serum Iron": (18, "ug/dL"),
            "TIBC": (510, "ug/dL"), "RDW-CV": (18.2, "%"),
        },
        "expect": ["Iron Deficiency Anemia"],
        "expect_level": {"Iron Deficiency Anemia": ["High"]},
        "forbid": ["Thalassemia (Trait/Major)"],
        "forbid_reason": "ferritin is depleted, so this is iron deficiency and not a "
                         "thalassaemia trait - the microcytic cohorts must not both fire",
    },
    {
        "name": "Thalassaemia trait pattern",
        "sex": "male",
        "values": {
            "Haemoglobin": (11.8, "g/dL"), "MCV": (66, "fL"), "RBC Count": (6.1, "million/uL"),
            "Serum Ferritin": (145, "ng/mL"), "HbA2": (5.2, "%"), "RDW-CV": (13.4, "%"),
        },
        "expect": ["Thalassemia (Trait/Major)"],
        "forbid": ["Iron Deficiency Anemia"],
        "forbid_reason": "iron stores are preserved, so iron deficiency must not be raised",
    },
    {
        "name": "Primary hypothyroidism, autoimmune",
        "sex": "female",
        "values": {
            "TSH": (14.2, "uIU/mL"), "Free T4": (0.55, "ng/dL"),
            "Anti-TPO Antibodies": (620, "IU/mL"),
        },
        "expect": ["Hypothyroidism", "Autoimmune thyroiditis (Hashimoto's)"],
        "expect_level": {"Hypothyroidism": ["High"]},
        "forbid": ["Hyperthyroidism"],
    },
    {
        "name": "Negative control - healthy panel",
        "sex": "female",
        "values": {
            "Haemoglobin": (13.2, "g/dL"), "Total WBC Count": (6900, "/uL"),
            "Platelet Count": (240000, "/uL"), "Fasting Blood Sugar": (86, "mg/dL"),
            "HbA1c": (5.1, "%"), "Total Cholesterol": (168, "mg/dL"),
            "HDL Cholesterol": (64, "mg/dL"), "LDL Cholesterol": (88, "mg/dL"),
            "Triglycerides": (88, "mg/dL"), "Serum Creatinine": (0.72, "mg/dL"),
            "SGPT": (18, "U/L"), "TSH": (1.9, "uIU/mL"),
        },
        "expect": [],
        "forbid_any_risk": True,
        "forbid_reason": "a normal panel must produce no disease risk at all",
    },
    {
        "name": "Negative control - negative infection panel",
        "sex": "male",
        "values": {
            "Dengue NS1 Antigen": ("Negative", None),
            "Malaria Antigen": ("Negative", None),
            "Widal Test": ("Negative", None),
            "HBsAg": ("Non-Reactive", None),
            "Anti HCV": ("Negative", None),
            "HIV": ("Non-Reactive", None),
            "VDRL": ("Non-Reactive", None),
            "Haemoglobin": (14.1, "g/dL"),
            "Platelet Count": (233000, "/uL"),
        },
        "expect": [],
        "forbid": ["Dengue Fever", "Malaria", "Typhoid Fever", "Hepatitis B",
                   "Hepatitis C", "HIV", "Syphilis"],
        "forbid_reason": "every one of these tests was NEGATIVE; none of these diseases "
                         "may be raised",
    },
    {
        "name": "Adrenal insufficiency electrolyte + hormone pattern",
        "sex": "male",
        "values": {
            "Sodium": (124, "mmol/L"), "Potassium": (6.1, "mmol/L"),
            "Cortisol": (2.1, "ug/dL"), "ACTH": (180, "pg/mL"),
        },
        "expect": ["Addison's Disease"],
        "expect_level": {"Addison's Disease": ["High", "Moderate"]},
        "forbid": [],
    },
    {
        "name": "Cross-profile: B12 deficiency driving neuropathy risk",
        "sex": "male",
        "values": {
            "Vitamin B12": (118, "pg/mL"), "MCV": (108, "fL"),
            "Haemoglobin": (10.9, "g/dL"), "HbA1c": (8.4, "%"),
            "Fasting Blood Sugar": (168, "mg/dL"), "Homocysteine": (28, "umol/L"),
        },
        "expect": ["Vitamin B12 Deficiency", "Peripheral Neuropathy", "Diabetes Mellitus"],
        "forbid": [],
        "note": "exercises the requirement that parameters from different profiles "
                "(Vitamin Profile + Diabetes Monitoring) feed one cohort",
    },
    {
        "name": "Unit conversion: SI units throughout",
        "sex": "male",
        "values": {
            "Glucose Fasting": (8.4, "mmol/L"),
            "Total Cholesterol": (7.1, "mmol/L"),
            "Triglycerides": (3.4, "mmol/L"),
            "HDL Cholesterol": (0.78, "mmol/L"),
            "Creatinine": (142, "umol/L"),
        },
        "expect": ["Atherogenic Dyslipidemia", "Diabetes Mellitus"],
        "forbid": [],
        "note": "all values are in SI units and must be converted before thresholds apply",
    },
]


def build_payload(case):
    tests = []
    for name, (value, unit) in case["values"].items():
        entry = {"test_name": name, "value": value}
        if unit:
            entry["unit"] = unit
        tests.append(entry)
    return {"patient": {"sex": case["sex"]}, "tests": tests}


def run_clinical(pipeline):
    rows, failures = [], []
    for case in CASES:
        result = pipeline.run(build_payload(case), case["name"] + ".json", sex=case["sex"])
        names = {r["name"] for r in result["disease_risks"]}
        levels = {r["name"]: r["evidence_level"] for r in result["disease_risks"]}
        problems = []

        for want in case.get("expect", []):
            if want not in names:
                problems.append("expected '%s' but it was not flagged" % want)
        for want, allowed in case.get("expect_level", {}).items():
            if want in levels and levels[want] not in allowed:
                problems.append("'%s' came out as %s, expected one of %s"
                                % (want, levels[want], allowed))
        for banned in case.get("forbid", []):
            if banned in names:
                problems.append("'%s' was flagged but must not be (%s)"
                                % (banned, case.get("forbid_reason", "")))
        if case.get("forbid_any_risk") and names:
            problems.append("expected no risks at all, got: %s" % ", ".join(sorted(names)))

        status = PASS if not problems else FAIL
        rows.append((status, case["name"], len(result["parameters"]),
                     len(result["cohorts"]), len(result["disease_risks"])))
        if problems:
            failures.append((case["name"], problems))
    return rows, failures


# --------------------------------------------------------------------------
# 3. Coverage at realistic panel sizes
# --------------------------------------------------------------------------

# Parameters that commonly appear together, grouped as real panels are ordered.
PANEL_POOLS = {
    "cbc": ["hemoglobin", "rbc_count", "hematocrit", "mcv", "mch", "mchc", "rdw_cv",
            "wbc_count", "neutrophil_pct", "lymphocyte_pct", "eosinophil_pct",
            "monocyte_pct", "platelet_count"],
    "lipid": ["total_cholesterol", "ldl_cholesterol", "hdl_cholesterol", "triglycerides",
              "vldl_cholesterol"],
    "glycemic": ["fasting_glucose", "postprandial_glucose", "hba1c", "fasting_insulin"],
    "renal": ["creatinine", "blood_urea", "egfr", "uric_acid", "urine_acr"],
    "liver": ["sgpt_alt", "sgot_ast", "alkaline_phosphatase", "total_bilirubin",
              "albumin", "total_protein", "globulin", "ggt"],
    "thyroid": ["tsh", "free_t4", "free_t3", "anti_tpo"],
    "iron": ["ferritin", "serum_iron", "tibc", "transferrin_saturation"],
    "vitamin": ["vitamin_d", "vitamin_b12", "folate"],
    "electrolyte": ["sodium", "potassium", "chloride", "calcium", "phosphorus", "bicarbonate"],
    "inflammation": ["crp", "esr"],
    "vitals": ["systolic_bp", "diastolic_bp", "bmi", "waist_circumference"],
    "coag": ["pt", "inr", "aptt", "d_dimer"],
    "hormone": ["lh", "fsh", "prolactin", "testosterone_total", "estradiol", "cortisol"],
}


def _abnormal_value(pdef, rng):
    """Produce a value clearly outside the reference interval, in canonical units."""
    ref = pdef.get("ref", {})
    interval = ref.get("default") or ref.get("male") or ref.get("female")
    if not interval:
        return None
    low, high = interval
    direction = pdef.get("direction_of_concern", "high")
    if direction == "low" or (direction == "both" and rng.random() < 0.5):
        return round(max(0.01, low * rng.uniform(0.35, 0.75)), 3)
    return round(high * rng.uniform(1.3, 2.2), 3)


def _normal_value(pdef, rng):
    ref = pdef.get("ref", {})
    interval = ref.get("default") or ref.get("male") or ref.get("female")
    if not interval:
        return None
    low, high = interval
    return round(rng.uniform(low + (high - low) * 0.2, high - (high - low) * 0.2), 3)


def run_coverage(pipeline, cfg, sizes=(10, 15, 20, 30), trials=60, seed=20260908):
    """Generate synthetic patients of each size and measure useful output.

    A record counts as USEFUL when the engine either flags at least one condition with
    a real explanation and triggering parameters, or correctly reports that nothing
    abnormal was found. A record counts as a MISS when abnormalities were present but
    produced no mapped condition at all.
    """
    rng = random.Random(seed)
    numeric = [p for p in cfg.parameters
               if p["type"] == "numeric" and (p.get("ref") or {}) and not p.get("derived_from")]
    by_id = {p["id"]: p for p in numeric}
    pools = {k: [pid for pid in v if pid in by_id] for k, v in PANEL_POOLS.items()}

    results = {}
    for size in sizes:
        useful = abnormal_records = flagged = explained = 0
        for _ in range(trials):
            chosen, keys = [], list(pools)
            rng.shuffle(keys)
            for k in keys:
                if len(chosen) >= size:
                    break
                take = pools[k][:]
                rng.shuffle(take)
                chosen += take[: max(1, min(len(take), size - len(chosen)))]
            chosen = chosen[:size]

            sex = rng.choice(["male", "female"])
            n_abnormal = max(1, int(len(chosen) * rng.uniform(0.2, 0.5)))
            abnormal_ids = set(rng.sample(chosen, min(n_abnormal, len(chosen))))

            tests = []
            for pid in chosen:
                pdef = by_id[pid]
                value = (_abnormal_value(pdef, rng) if pid in abnormal_ids
                         else _normal_value(pdef, rng))
                if value is None:
                    continue
                tests.append({"test_name": pdef["name"], "value": value,
                              "unit": pdef.get("unit")})
            if not tests:
                continue

            result = pipeline.run({"patient": {"sex": sex}, "tests": tests},
                                  "synthetic.json", sex=sex)
            risks = result["disease_risks"]
            had_abnormal = result["summary"]["abnormal_count"] > 0
            if had_abnormal:
                abnormal_records += 1
            if risks:
                flagged += 1
                if all(r["explanation"] and r["triggering_parameters"] for r in risks):
                    explained += 1
            if (risks and all(r["explanation"] for r in risks)) or (not had_abnormal and not risks):
                useful += 1

        results[size] = {
            "trials": trials,
            "useful_pct": 100.0 * useful / trials,
            "records_with_abnormality": abnormal_records,
            "records_flagging_a_condition": flagged,
            "flag_rate_when_abnormal": (100.0 * flagged / abnormal_records) if abnormal_records else 0.0,
            "all_findings_explained": explained == flagged,
        }
    return results


# --------------------------------------------------------------------------

def main():
    cfg = get_config()
    pipeline = Pipeline(cfg)
    overall_ok = True

    print("=" * 78)
    print("1. CONFIGURATION")
    print("=" * 78)
    v = cfg.validation
    for k, val in v["counts"].items():
        print("   %-34s %s" % (k, val))
    print("   %-34s %s" % ("errors", len(v["errors"])))
    print("   %-34s %s" % ("warnings", len(v["warnings"])))
    for e in v["errors"]:
        print("     ERROR   %s" % e)
    for w in v["warnings"][:15]:
        print("     WARNING %s" % w)
    print("   -> %s" % (PASS if v["ok"] else FAIL))
    overall_ok &= v["ok"]

    print()
    print("=" * 78)
    print("2. EVIDENCE BASIS")
    print("=" * 78)
    cited = [c for c in cfg.cohorts if c.get("evidence")]
    citations = {e["citation"] for c in cfg.cohorts for e in c.get("evidence", [])}
    links = [l for c in cfg.cohorts for l in c["diseases"]]
    with_basis = [l for l in links if l.get("dm_basis")]
    sourced_params = [p for p in cfg.parameters if p.get("source")]
    print("   %-46s %d / %d" % ("cohorts citing a clinical reference", len(cited), len(cfg.cohorts)))
    print("   %-46s %d" % ("distinct references cited", len(citations)))
    print("   %-46s %.1f" % ("average citations per cohort",
                             sum(len(c.get("evidence", [])) for c in cfg.cohorts) / len(cfg.cohorts)))
    print("   %-46s %d / %d" % ("disease links quoting a Disease Master field",
                                len(with_basis), len(links)))
    print("   %-46s %d" % ("parameters with a named threshold source", len(sourced_params)))
    evidence_ok = (len(cited) == len(cfg.cohorts) and len(with_basis) == len(links))
    print("   -> %s" % (PASS if evidence_ok else FAIL))
    overall_ok &= evidence_ok

    print()
    print("=" * 78)
    print("3. CLINICAL CASES")
    print("=" * 78)
    rows, failures = run_clinical(pipeline)
    print("   %-6s %-46s %5s %6s %6s" % ("", "case", "prms", "cohts", "risks"))
    for status, name, nparams, ncohorts, nrisks in rows:
        print("   %-6s %-46s %5d %6d %6d" % (status, name[:46], nparams, ncohorts, nrisks))
    for name, problems in failures:
        print("\n   FAILURE in '%s':" % name)
        for p in problems:
            print("     - %s" % p)
    print("   -> %s (%d/%d)" % (PASS if not failures else FAIL,
                                len(rows) - len(failures), len(rows)))
    overall_ok &= not failures

    print()
    print("=" * 78)
    print("4. CONSISTENCY")
    print("=" * 78)
    checks = run_consistency(pipeline, cfg, Path(__file__).resolve().parents[1] / "samples")
    for ok, name, detail in checks:
        print("   %-6s %s" % (PASS if ok else FAIL, name))
        if not ok and detail:
            print("          -> %s" % detail)
    cons_failed = [c for c in checks if not c[0]]
    print("   -> %s (%d/%d)" % (PASS if not cons_failed else FAIL,
                                len(checks) - len(cons_failed), len(checks)))
    overall_ok &= not cons_failed

    print()
    print("=" * 78)
    print("5. DISEASE MASTER MAPPING")
    print("=" * 78)
    m = run_mapping(pipeline, cfg)
    print("   %-46s %d" % ("cohort -> disease links exercised", m["links_total"]))
    print("   %-46s %d (%.1f%%)" % ("links that fire end to end", m["links_reachable"],
                                    100.0 * m["links_reachable"] / max(1, m["links_total"])))
    print("   %-46s %d / %d" % ("mappable conditions reachable",
                                m["diseases_reachable"], m["mappable_total"]))
    for role in ("primary", "supporting", "downstream_risk", "differential"):
        b = m["by_role"].get(role)
        if b:
            print("     %-44s %d / %d" % (role, b["reachable"], b["total"]))
    print("   %-46s %d" % ("documented as not lab-mappable", len(m["documented_unmappable"])))
    print("   %-46s %d" % ("combination-only links (by design)", len(m["combination_only"])))
    print("   %-46s %d" % ("dead mappings", len(m["dead_mappings"])))
    for d in m["dead_mappings"]:
        print("     DEAD  %s" % d)
    for u in m["cluster_never_fired"]:
        print("     UNSATISFIABLE CLUSTER  %s -> %s" % (u["cohort"], u["disease"]))
    mapping_ok = (not m["dead_mappings"] and not m["cluster_never_fired"]
                  and m["diseases_reachable"] == m["mappable_total"]
                  and m["by_role"].get("primary", {}).get("reachable")
                  == m["by_role"].get("primary", {}).get("total"))
    print("   target: every mappable condition reachable, every primary link fires, "
          "no dead mappings")
    print("   -> %s" % (PASS if mapping_ok else FAIL))
    overall_ok &= mapping_ok

    print()
    print("=" * 78)
    print("6. PARAMETER COVERAGE")
    print("=" * 78)
    pc = run_param_coverage(cfg)
    print("   %-52s %d" % ("markers named in the Disease Master", pc["marker_real"]))
    print("     %-50s %d" % ("resolve directly to a parameter", pc["marker_direct"]))
    print("     %-50s %d" % ("resolve via a documented umbrella term", pc["marker_via_umbrella"]))
    print("     %-50s %d" % ("declared as having no assay available", pc["marker_declared_no_assay"]))
    print("     %-50s %d" % ("unresolved", sum(pc["marker_unresolved"].values())))
    print("   %-52s %.1f%%" % ("marker coverage (of those with an assay)", pc["marker_coverage_pct"]))
    print("   %-52s %d / %d" % ("parameters evaluated by a cluster",
                                pc["parameters_evaluated"], pc["parameters_total"]))
    print("   %-52s %d" % ("parameters declared reported-only", pc["parameters_reported_only"]))
    print("   %-52s %d" % ("orphaned parameters", len(pc["parameters_orphaned"])))
    print("   %-52s %d" % ("conditions using their own Disease Master markers",
                           pc["diseases_with_markers_covered"]))
    for tok, n in pc["marker_unresolved"].most_common(10):
        print("     UNRESOLVED MARKER  %s" % tok)
    for pid in pc["parameters_orphaned"]:
        print("     ORPHAN PARAMETER   %s" % pid)
    undoc = [u for u in pc["diseases_marker_uncovered"] if not u["documented"]]
    for u in undoc:
        print("     NOT USING OWN MARKERS  %s" % u["disease"])
    pcov_ok = (not pc["marker_unresolved"] and not pc["parameters_orphaned"]
               and not pc["parameters_mislabelled_reported_only"] and not undoc)
    print("   target: every marker resolves or is declared, no orphan parameters, "
          "every condition uses its own markers")
    print("   -> %s" % (PASS if pcov_ok else FAIL))
    overall_ok &= pcov_ok

    print()
    print("=" * 78)
    print("7. COVERAGE AT REALISTIC PANEL SIZES")
    print("=" * 78)
    cov = run_coverage(pipeline, cfg)
    print("   %8s %9s %11s %13s %11s" % ("params", "useful %", "w/abnormal", "flagged", "explained"))
    coverage_ok = True
    for size, r in cov.items():
        print("   %8d %8.1f%% %11d %13d %11s"
              % (size, r["useful_pct"], r["records_with_abnormality"],
                 r["records_flagging_a_condition"],
                 "yes" if r["all_findings_explained"] else "NO"))
        if r["useful_pct"] < 80.0 or not r["all_findings_explained"]:
            coverage_ok = False
    print("   target: >= 80%% useful at every size, every finding explained")
    print("   -> %s" % (PASS if coverage_ok else FAIL))
    overall_ok &= coverage_ok

    print()
    print("=" * 78)
    print("OVERALL: %s" % (PASS if overall_ok else FAIL))
    print("=" * 78)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
