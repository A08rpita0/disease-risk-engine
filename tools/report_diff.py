"""Field-by-field comparison: a PDF through the engine versus its JSON transcription.

    python tools/report_diff.py REPORT.pdf TRUTH.json [--json OUT.json]

TRUTH.json is the same report written out as structured JSON (test_name, value, unit,
reference_range per test, plus patient fields). Whatever the engine makes of the JSON
is the reference; every way the PDF result differs is listed:

    context      patient name / sex / age as read from each
    missing      a test the JSON has and the PDF result does not
    spurious     a parameter the PDF produced that the JSON does not contain
    value        different number or qualitative status
    unit/range   different canonical unit, reference interval or its source
    flag         different abnormal flag, direction, grade or basis
    findings     abnormal findings, conditions and plan steps that differ

Generic: nothing here knows about any particular report. Real reports belong in the
git-ignored private/ folder.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.pipeline import analyse          # noqa: E402

FIELDS = ["value", "status", "unit", "reference_low", "reference_high", "reference_source",
          "abnormal", "direction", "grade_label", "finding_basis", "interpretable"]


def _close(a, b, field=None):
    # "< 45" and "0 - 45" are the same interval for a quantity that cannot be negative;
    # a missing lower bound and a lower bound of 0 are not a discrepancy.
    if field == "reference_low" and {a, b} <= {None, 0, 0.0}:
        return True
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b))
    return a == b


def diff(pdf_path, truth_path):
    truth = json.loads(Path(truth_path).read_text(encoding="utf-8"))
    rj = analyse(truth, Path(truth_path).name)
    rp = analyse(Path(pdf_path).read_bytes(), Path(pdf_path).name)

    out = {"context": [], "missing": [], "spurious": [], "field": [], "abnormal_findings": {},
           "conditions": {}, "plan": {}, "summary": {}}

    def _ctx(v, k):
        if not v:
            return v
        s = str(v).lower().strip()
        # "Mr. X" and "X" are the same patient; the title is kept as printed but not compared
        return re.sub(r"^(?:mr|mrs|ms|miss|master|baby|dr)\.?\s+", "", s) if k == "name" else s

    for k in ("name", "sex", "age", "patient_id"):
        a, b = rj["patient"].get(k), rp["patient"].get(k)
        if _ctx(a, k) != _ctx(b, k):
            out["context"].append({"field": k, "json": a, "pdf": b})

    # A PDF the engine could not read at all (a scan) has no parameters: every JSON test
    # is then missing, which is exactly the discrepancy to report.
    for r in (rj, rp):
        for key in ("parameters", "unmapped_observations", "abnormal_findings", "disease_risks",
                    "recommendations", "duplicates_resolved"):
            r.setdefault(key, [])
    pj = {p["parameter_id"]: p for p in rj["parameters"]}
    pp = {p["parameter_id"]: p for p in rp["parameters"]}
    for pid, a in pj.items():
        b = pp.get(pid)
        if b is None:
            out["missing"].append({"parameter_id": pid, "name": a["name"], "json_value": a["value"],
                                   "raw_name": (a.get("raw") or {}).get("raw_name")})
            continue
        for f in FIELDS:
            if not _close(a.get(f), b.get(f), f):
                out["field"].append({"parameter_id": pid, "field": f, "json": a.get(f),
                                     "pdf": b.get(f),
                                     "pdf_raw": (b.get("raw") or {}).get("raw_name"),
                                     "pdf_source": (b.get("raw") or {}).get("source_path")})
    for pid, b in pp.items():
        if pid not in pj:
            out["spurious"].append({"parameter_id": pid, "value": b["value"],
                                    "raw_name": (b.get("raw") or {}).get("raw_name"),
                                    "source": (b.get("raw") or {}).get("source_path")})

    # JSON tests the dictionary could not resolve - listed so they are not mistaken for
    # PDF misses.
    out["json_unmapped"] = sorted({o["raw_name"] for o in rj["unmapped_observations"]})

    def setdiff(key, a_items, b_items):
        a, b = set(a_items), set(b_items)
        out[key] = {"only_json": sorted(a - b), "only_pdf": sorted(b - a), "both": len(a & b)}

    setdiff("abnormal_findings", [f["parameter_id"] for f in rj["abnormal_findings"]],
            [f["parameter_id"] for f in rp["abnormal_findings"]])
    setdiff("conditions", ["%s | %s | %s" % (d["name"], d["evidence_level"], d["presentation_tier"])
                           for d in rj["disease_risks"]],
            ["%s | %s | %s" % (d["name"], d["evidence_level"], d["presentation_tier"])
             for d in rp["disease_risks"]])
    setdiff("plan", ["%s | %s | %s" % (x["trace"], x["finding"], x["text"][:50]) for x in rj["recommendations"]],
            ["%s | %s | %s" % (x["trace"], x["finding"], x["text"][:50]) for x in rp["recommendations"]])
    for k in ("parameters_recognised", "abnormal_count", "parameters_unmapped",
              "duplicates_resolved", "values_rejected"):
        out["summary"][k] = {"json": rj["summary"].get(k), "pdf": rp["summary"].get(k)}
    out["pdf_duplicate_conflicts"] = [d for d in rp["duplicates_resolved"] if d.get("conflicting_values")]
    return out, rj, rp


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    res, rj, rp = diff(sys.argv[1], sys.argv[2])
    if "--json" in sys.argv:
        Path(sys.argv[sys.argv.index("--json") + 1]).write_text(
            json.dumps(res, indent=1, default=str), encoding="utf-8")
    n = (len(res["context"]) + len(res["missing"]) + len(res["spurious"]) + len(res["field"])
         + len(res["abnormal_findings"]["only_json"]) + len(res["abnormal_findings"]["only_pdf"])
         + len(res["conditions"]["only_json"]) + len(res["conditions"]["only_pdf"]))
    print("SUMMARY  " + "  ".join("%s json=%s pdf=%s" % (k, v["json"], v["pdf"])
                                  for k, v in res["summary"].items()))
    print("CONTEXT  %s" % (res["context"] or "identical"))
    print("MISSING in PDF (%d)" % len(res["missing"]))
    for m in res["missing"]:
        print("   %-26s json=%-10s raw=%r" % (m["parameter_id"], m["json_value"], m["raw_name"]))
    print("SPURIOUS in PDF (%d)" % len(res["spurious"]))
    for s in res["spurious"]:
        print("   %-26s pdf=%-10s raw=%r @ %s" % (s["parameter_id"], s["value"], s["raw_name"], s["source"]))
    print("FIELD DIFFERENCES (%d)" % len(res["field"]))
    for f in res["field"]:
        print("   %-26s %-16s json=%-14r pdf=%-14r raw=%r @ %s" % (
            f["parameter_id"], f["field"], f["json"], f["pdf"], f["pdf_raw"], f["pdf_source"]))
    for key in ("abnormal_findings", "conditions", "plan"):
        r = res[key]
        print("%s: both=%d only_json=%d only_pdf=%d" % (key.upper(), r["both"], len(r["only_json"]), len(r["only_pdf"])))
        for x in r["only_json"]:
            print("   only JSON: %s" % x)
        for x in r["only_pdf"]:
            print("   only PDF : %s" % x)
    print("PDF DUPLICATE CONFLICTS (%d)" % len(res["pdf_duplicate_conflicts"]))
    for d in res["pdf_duplicate_conflicts"]:
        print("   %-22s kept=%s dropped=%s" % (d["parameter"], d["kept"].get("value"),
                                            [x.get("value") for x in d["dropped"]]))
    print("JSON tests the dictionary does not know (%d): %s" % (len(res["json_unmapped"]), res["json_unmapped"]))
    print("\nTOTAL DISCREPANCIES (excluding plan wording): %d" % n)
    return 0 if n == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
