"""Consistency checks and Disease Master mapping verification.

These are the two validation pillars that go beyond "does it run":

CONSISTENCY - properties the engine must satisfy on every input, not just the ones in
the test suite. Determinism, bounded scores, monotonicity (a worse value can never
lower a risk), mutually exclusive patterns never co-firing, negative results never
raising their disease, and every reported finding carrying a complete audit trail.

MAPPING - proof that every cohort -> Disease Master link actually works end to end.
For each link, a patient is synthesised from that cohort's own trigger conditions and
pushed through the real pipeline; the linked condition must come out the other side.
A link that resolves by name but can never fire is a dead mapping, and this finds it.

Imported by tools/validate.py; runnable on its own for detail:
    python tools/consistency.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.config import get_config          # noqa: E402
from engine.pipeline import Pipeline          # noqa: E402
from engine.risk import REPORT_FLOOR          # noqa: E402


# --------------------------------------------------------------------------
# Synthesising a patient that satisfies a cohort's own conditions
# --------------------------------------------------------------------------

def _ref(pdef, sex):
    ref = pdef.get("ref") or {}
    if sex and sex in ref:
        return ref[sex]
    if "default" in ref:
        return ref["default"]
    for k in ("male", "female"):
        if k in ref:
            return ref[k]
    return None


def value_satisfying(pdef, cond, sex):
    """A value that makes `cond` true for `pdef`, or None if it cannot be synthesised."""
    if pdef["type"] == "qualitative":
        want = cond.get("status")
        if want in ("positive", "any"):
            return "Positive"
        if want == "negative":
            return "Negative"
        if cond.get("abnormal") in ("any", "high", "positive"):
            return "Positive"
        if cond.get("abnormal") == "none":
            return "Negative"
        return "Positive"

    if pdef["type"] == "categorical":
        vals = cond.get("category_in")
        return vals[0] if vals else None

    interval = _ref(pdef, sex)

    if "between" in cond:
        lo, hi = cond["between"]
        return round((lo + hi) / 2.0, 4)
    if "outside" in cond:
        lo, hi = cond["outside"]
        return round(hi * 1.25 + 1, 4)
    for op, factor in (("gte", 1.15), ("gt", 1.15)):
        if op in cond:
            base = cond[op]
            return round(base * factor if base else 1.0, 4)
    for op, factor in (("lte", 0.85), ("lt", 0.85)):
        if op in cond:
            base = cond[op]
            return round(base * factor if base else -1.0, 4)
    if "eq" in cond:
        return cond["eq"]

    if "abnormal" in cond:
        if interval is None:
            return None
        low, high = interval
        want = cond["abnormal"]
        if want == "low":
            return round(low * 0.5, 4) if low else -1.0
        if want == "none":
            return round((low + high) / 2.0, 4)
        return round(high * 1.6, 4) if high else 1.0        # high / any

    if "grade_in" in cond:
        wants_low = any(g.endswith("_low") for g in cond["grade_in"])
        if interval is None:
            return None
        low, high = interval
        return round(low * 0.4, 4) if wants_low else round(high * 1.8, 4)
    return None


def synthesise(cfg, cohort, sex, extra_params=()):
    """Build the minimum set of tests that should make this cohort fire."""
    tests, used = [], set()

    def add(ref):
        pid = ref["parameter"]
        if pid in used:
            return False
        pdef = cfg.param_by_id[pid]
        value = value_satisfying(pdef, ref["condition"], sex)
        if value is None:
            return False
        used.add(pid)
        entry = {"test_name": pdef["name"], "value": value}
        if pdef.get("unit"):
            entry["unit"] = pdef["unit"]
        tests.append(entry)
        return True

    if cohort.get("mode") == "count_of":
        need = cohort["count_required"]
        got = 0
        for comp in cohort.get("components", []):
            if got >= need:
                break
            for ref in comp["any_of"]:
                if add(ref):
                    got += 1
                    break
    else:
        need = cohort.get("min_triggers", 1)
        got = 0
        for ref in cohort.get("triggers", []):
            if got >= need:
                break
            if add(ref):
                got += 1

    # Supporting signals help the cohort clear its data-coverage floor.
    for ref in cohort.get("supporting", []):
        add(ref)

    # Gating markers for the specific disease under test.
    for pid in extra_params:
        if pid in used:
            continue
        for ref in (cohort.get("triggers", []) + cohort.get("supporting", [])):
            if ref["parameter"] == pid:
                add(ref)
                break

    # Top up with in-range values for the rest of the expected set, so data coverage
    # reflects a realistically ordered panel rather than a single isolated test.
    for pid in cohort.get("expected_parameters", []):
        if pid in used:
            continue
        pdef = cfg.param_by_id[pid]
        if pdef["type"] != "numeric":
            continue
        interval = _ref(pdef, sex)
        if not interval:
            continue
        used.add(pid)
        tests.append({"test_name": pdef["name"],
                      "value": round((interval[0] + interval[1]) / 2.0, 4),
                      "unit": pdef.get("unit")})
    return tests


# --------------------------------------------------------------------------
# Suite: Disease Master mapping
# --------------------------------------------------------------------------

def run_mapping(pipeline, cfg):
    """Every cohort -> disease link is exercised through the real pipeline."""
    results = {"links_total": 0, "links_reachable": 0, "unreachable": [],
               "diseases_reachable": set(), "by_role": {}}

    for cohort in cfg.cohorts:
        sexes = ([cohort["sex_restriction"]] if cohort.get("sex_restriction")
                 else ["male", "female"])
        for link in cohort.get("diseases", []):
            results["links_total"] += 1
            role = link.get("role", "supporting")
            bucket = results["by_role"].setdefault(role, {"total": 0, "reachable": 0})
            bucket["total"] += 1

            reached, cohort_fired, best_conf = False, False, 0.0
            for sex in sexes:
                tests = synthesise(cfg, cohort, sex, link.get("requires_any", []))
                if not tests:
                    continue
                out = pipeline.run({"patient": {"sex": sex}, "tests": tests},
                                   "mapping.json", sex=sex)
                for c in out["cohorts"]:
                    if c["cohort_id"] == cohort["id"]:
                        cohort_fired = True
                        best_conf = max(best_conf, c["confidence"])
                if any(r["name"] == link["name"] for r in out["disease_risks"]):
                    reached = True
                    break
            if reached:
                results["links_reachable"] += 1
                bucket["reachable"] += 1
                results["diseases_reachable"].add(link["name"])
            else:
                results["unreachable"].append({
                    "cohort": cohort["id"], "disease": link["name"],
                    "role": role, "weight": link["weight"],
                    "ceiling": round(link["weight"] * _role_factor(role), 4),
                    "cohort_fired": cohort_fired,
                    "achieved": round(link["weight"] * _role_factor(role) * best_conf, 4),
                })

    linked = set(cfg.links_by_disease)
    documented = {c["name"] for c in cfg.unmappable.get("conditions", [])}
    unreached = linked - results["diseases_reachable"]

    results["diseases_linked"] = len(linked)
    results["documented_unmappable"] = sorted(unreached & documented)
    # A condition that no cluster can raise AND that is not documented as unmappable is
    # a dead mapping - the link resolves by name but can never produce a result.
    results["dead_mappings"] = sorted(unreached - documented)
    results["diseases_reachable"] = len(results["diseases_reachable"])
    results["mappable_total"] = len(linked) - len(documented & linked)

    # A weak link whose cluster DID fire is combination-only by design: it cannot lift a
    # condition over the reporting floor alone, but it still adds evidence alongside
    # others. A link whose cluster did NOT fire is a genuine defect - the cluster cannot
    # be satisfied even by values built from its own conditions.
    for u in results["unreachable"]:
        u["combination_only"] = u["cohort_fired"] and u["achieved"] < REPORT_FLOOR
    results["combination_only"] = [u for u in results["unreachable"] if u["combination_only"]]
    results["cluster_never_fired"] = [u for u in results["unreachable"] if not u["cohort_fired"]]
    results["unexpected_unreachable"] = [
        u for u in results["unreachable"]
        if not u["combination_only"] and u["disease"] not in documented]
    return results


def _role_factor(role):
    from engine.risk import ROLE_FACTOR
    return ROLE_FACTOR.get(role, 0.5)


# --------------------------------------------------------------------------
# Suite: consistency
# --------------------------------------------------------------------------

MUTUALLY_EXCLUSIVE = [
    ("primary_hypothyroid_pattern", "hyperthyroid_pattern",
     "a patient cannot be biochemically hypo- and hyperthyroid at once"),
    ("iron_deficiency", "microcytic_non_iron_deficient",
     "the second cohort exists precisely to exclude the first"),
    ("hypogonadotropic_pattern", "hypergonadotropic_pattern",
     "gonadotropins cannot be both suppressed and raised"),
    ("polycythemia", "pancytopenia",
     "raised red cell mass and pancytopenia are opposites"),
]

MONOTONIC = [
    ("Cardiovascular / Coronary Artery Disease", "LDL Cholesterol", "mg/dL",
     [135, 165, 200], "male"),
    ("Hypothyroidism", "TSH", "uIU/mL", [5.0, 12.0, 40.0], "female"),
    ("Iron Deficiency Anemia", "Serum Ferritin", "ng/mL", [28, 12, 3], "female"),
    ("Chronic Kidney Disease (CKD)", "eGFR", "mL/min/1.73m2", [58, 40, 18], "male"),
    ("Diabetes Mellitus", "HbA1c", "%", [6.6, 8.0, 11.0], "male"),
]

NEGATIVE_SUPPRESSION = [
    ("Dengue NS1 Antigen", "Dengue Fever"),
    ("Malaria Antigen", "Malaria"),
    ("HBsAg", "Hepatitis B"),
    ("Anti HCV", "Hepatitis C"),
    ("HIV", "HIV"),
    ("VDRL", "Syphilis"),
    ("Chlamydia PCR", "Chlamydia"),
    ("ANA", "Systemic Lupus Erythematosus"),
    ("AChR Antibody", "Myasthenia Gravis"),
    ("Sickling Test", "Sickle Cell Disease"),
]


def run_consistency(pipeline, cfg, samples_dir):
    checks = []

    def check(name, ok, detail=""):
        checks.append((bool(ok), name, detail))

    # --- determinism -----------------------------------------------------
    payload = {"patient": {"sex": "male"}, "tests": [
        {"test_name": "HbA1c", "value": 7.6, "unit": "%"},
        {"test_name": "Triglycerides", "value": 260, "unit": "mg/dL"},
        {"test_name": "HDL Cholesterol", "value": 33, "unit": "mg/dL"},
        {"test_name": "Serum Creatinine", "value": 1.5, "unit": "mg/dL"},
    ]}
    a = pipeline.run(dict(payload), "d.json", sex="male")
    b = pipeline.run(dict(payload), "d.json", sex="male")
    for k in ("generated_at",):
        a.pop(k, None)
        b.pop(k, None)
    check("identical input produces byte-identical output",
          json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str))

    # --- bounds and internal agreement across every sample ---------------
    bound_errors, trace_errors, grade_errors = [], [], []
    for f in sorted(Path(samples_dir).iterdir()):
        if f.suffix.lower() not in (".json", ".csv", ".pdf", ".txt"):
            continue
        out = pipeline.run(f.read_bytes(), f.name)
        for c in out["cohorts"]:
            if not 0.0 <= c["confidence"] <= 1.0:
                bound_errors.append("%s cohort %s conf=%s" % (f.name, c["cohort_id"], c["confidence"]))
            if not 0.0 <= c["data_coverage"] <= 1.0:
                bound_errors.append("%s cohort %s coverage=%s" % (f.name, c["cohort_id"], c["data_coverage"]))
        for r in out["disease_risks"]:
            if not REPORT_FLOOR <= r["score"] <= 1.0:
                bound_errors.append("%s risk %s score=%s" % (f.name, r["name"], r["score"]))
            if not r["explanation"] or not r["contributions"] or not r["triggering_parameters"]:
                trace_errors.append("%s: %s has an incomplete audit trail" % (f.name, r["name"]))
            if any(not c["dm_basis"] for c in r["contributions"]):
                trace_errors.append("%s: %s has a contribution with no Disease Master basis"
                                    % (f.name, r["name"]))
            if not 0.0 <= r["data_coverage"] <= 1.0:
                bound_errors.append("%s risk %s coverage=%s" % (f.name, r["name"], r["data_coverage"]))
        for p in out["parameters"]:
            if p["abnormal"] and p["grade"] in ("normal", "protective"):
                grade_errors.append("%s: %s flagged abnormal but graded %s"
                                    % (f.name, p["name"], p["grade"]))
            if not p["abnormal"] and p["grade"].startswith(("severe", "critical", "moderate")):
                grade_errors.append("%s: %s graded %s but not flagged abnormal"
                                    % (f.name, p["name"], p["grade"]))
    check("all cohort confidences and disease scores stay within bounds",
          not bound_errors, "; ".join(bound_errors[:3]))
    check("every reported finding carries a complete audit trail",
          not trace_errors, "; ".join(trace_errors[:3]))
    check("abnormality flags and severity grades never contradict each other",
          not grade_errors, "; ".join(grade_errors[:3]))

    # --- mutually exclusive patterns -------------------------------------
    for a_id, b_id, why in MUTUALLY_EXCLUSIVE:
        ca, cb = cfg.cohort_by_id.get(a_id), cfg.cohort_by_id.get(b_id)
        if not ca or not cb:
            check("mutually exclusive pair %s / %s exists" % (a_id, b_id), False)
            continue
        clash = None
        for cohort, other in ((ca, b_id), (cb, a_id)):
            sex = cohort.get("sex_restriction") or "female"
            tests = synthesise(cfg, cohort, sex)
            out = pipeline.run({"patient": {"sex": sex}, "tests": tests}, "mx.json", sex=sex)
            fired = {c["cohort_id"] for c in out["cohorts"]}
            if cohort["id"] in fired and other in fired:
                clash = "%s and %s both fired" % (cohort["id"], other)
        check("%s and %s never co-fire (%s)" % (a_id, b_id, why), clash is None, clash or "")

    # --- monotonicity ----------------------------------------------------
    for disease, test_name, unit, series, sex in MONOTONIC:
        scores = []
        for value in series:
            out = pipeline.run(
                {"patient": {"sex": sex},
                 "tests": [{"test_name": test_name, "value": value, "unit": unit}]},
                "mono.json", sex=sex)
            hit = [r for r in out["disease_risks"] if r["name"] == disease]
            scores.append(hit[0]["score"] if hit else 0.0)
        ok = all(scores[i] <= scores[i + 1] + 1e-9 for i in range(len(scores) - 1))
        check("worsening %s never lowers the %s score" % (test_name, disease),
              ok, "scores=%s" % scores)

    # --- negative results never raise their disease ----------------------
    neg_failures = []
    for test_name, disease in NEGATIVE_SUPPRESSION:
        out = pipeline.run(
            {"patient": {"sex": "male"},
             "tests": [{"test_name": test_name, "value": "Negative"}]},
            "neg.json", sex="male")
        if any(r["name"] == disease for r in out["disease_risks"]):
            neg_failures.append("%s negative still raised %s" % (test_name, disease))
    check("a negative result never raises the disease it tests for",
          not neg_failures, "; ".join(neg_failures))

    # --- a normal panel stays silent no matter how wide ------------------
    wide = []
    for p in cfg.parameters:
        if p["type"] != "numeric":
            continue
        interval = _ref(p, "male")
        if not interval:
            continue
        wide.append({"test_name": p["name"],
                     "value": round((interval[0] + interval[1]) / 2.0, 4),
                     "unit": p.get("unit")})
    out = pipeline.run({"patient": {"sex": "male"}, "tests": wide}, "wide.json", sex="male")
    check("a full panel of mid-range values raises nothing (%d parameters)" % len(wide),
          not out["disease_risks"],
          "flagged: %s" % [r["name"] for r in out["disease_risks"]][:5])

    # --- adding normal results never invents risk ------------------------
    base = {"patient": {"sex": "male"}, "tests": [
        {"test_name": "Serum Ferritin", "value": 6, "unit": "ng/mL"},
        {"test_name": "Haemoglobin", "value": 10.5, "unit": "g/dL"},
        {"test_name": "MCV", "value": 72, "unit": "fL"}]}
    padded = {"patient": {"sex": "male"}, "tests": base["tests"] + [
        {"test_name": "TSH", "value": 2.1, "unit": "uIU/mL"},
        {"test_name": "Sodium", "value": 140, "unit": "mmol/L"},
        {"test_name": "Total Cholesterol", "value": 165, "unit": "mg/dL"},
        {"test_name": "SGPT", "value": 20, "unit": "U/L"}]}
    r1 = {r["name"] for r in pipeline.run(base, "b.json", sex="male")["disease_risks"]}
    r2 = {r["name"] for r in pipeline.run(padded, "p.json", sex="male")["disease_risks"]}
    check("adding in-range results does not invent new conditions",
          r2.issubset(r1) or not (r2 - r1),
          "new: %s" % sorted(r2 - r1))

    return checks


# --------------------------------------------------------------------------

def main():
    cfg = get_config()
    pipeline = Pipeline(cfg)
    samples = Path(__file__).resolve().parents[1] / "samples"

    print("CONSISTENCY")
    checks = run_consistency(pipeline, cfg, samples)
    for ok, name, detail in checks:
        print("   %-5s %s%s" % ("PASS" if ok else "FAIL", name,
                                ("   -> " + detail) if detail and not ok else ""))

    print()
    print("DISEASE MASTER MAPPING")
    m = run_mapping(pipeline, cfg)
    print("   links exercised      : %d" % m["links_total"])
    print("   links reachable      : %d (%.1f%%)"
          % (m["links_reachable"], 100.0 * m["links_reachable"] / max(1, m["links_total"])))
    print("   conditions reachable : %d of %d mappable"
          % (m["diseases_reachable"], m["mappable_total"]))
    for role, b in sorted(m["by_role"].items()):
        print("     %-16s %d/%d" % (role, b["reachable"], b["total"]))
    print("   documented as not lab-mappable: %d (%s)"
          % (len(m["documented_unmappable"]), ", ".join(m["documented_unmappable"]) or "none"))
    print("   combination-only links        : %d (cluster fires, but this link alone stays "
          "under the %.2f reporting floor - by design)" % (len(m["combination_only"]), REPORT_FLOOR))
    if m["dead_mappings"]:
        print("   DEAD MAPPINGS - no cluster can ever raise these:")
        for d in m["dead_mappings"]:
            print("     -", d)
    if m["cluster_never_fired"]:
        print("   CLUSTERS THAT COULD NOT BE SATISFIED BY THEIR OWN CONDITIONS:")
        for u in m["cluster_never_fired"]:
            print("     %-34s -> %s" % (u["cohort"], u["disease"]))
    if m["unexpected_unreachable"]:
        print("   OTHER LINKS THAT DID NOT FIRE:")
        for u in m["unexpected_unreachable"]:
            print("     %-34s -> %-40s %s w=%.2f achieved=%.3f"
                  % (u["cohort"], u["disease"][:40], u["role"], u["weight"], u["achieved"]))
    failed = [c for c in checks if not c[0]]
    return 1 if (failed or m["dead_mappings"] or m["unexpected_unreachable"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
