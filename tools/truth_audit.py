"""Ground-truth audit: every printed test, through BOTH input paths, against a hand
transcription.

    python tools/truth_audit.py REPORT.pdf TRUTH.json [--json OUT.json] [--rows]
    python tools/truth_audit.py --dir private/multi        (report_N.pdf + truth/report_N_truth.json)

TRUTH.json is written by a person from the printed pages - never by this engine:

    {"patient": {"name", "age", "sex"},
     "tests": [{"test_name", "value", "unit", "reference_range", "flag", "section", "page",
                "expected_status",      # normal | high | low | positive | negative | not_graded
                "expected_parameter"}]} # optional canonical id, when the reviewer fixed it

For each test it answers the questions a reviewer asks, separately for the PDF and the
JSON path:
    extracted   did a reading of this row come out of the document at all
    value/unit  is it the printed value and unit
    range       is the report interval the same one the other path used
    parameter   did both paths resolve the same canonical test (and the expected one)
    status      does the verdict match the expected status

"Missed abnormal" and "false positive" are counted against `expected_status`, which is
the reviewer's reading of the report, not the engine's.

Generic: nothing here knows any laboratory. Real reports stay in the git-ignored private/.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.config import norm_unit                     # noqa: E402
from engine.pipeline import analyse                     # noqa: E402

_NUM = re.compile(r"[-+]?\d[\d,]*\.?\d*")


def _num(v):
    if v is None:
        return None
    m = _NUM.search(str(v).replace(",", ""))
    return float(m.group(0)) if m else None


def _same_value(a, b):
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return abs(na - nb) <= 1e-9 * max(1.0, abs(na))
    return re.sub(r"\W+", "", str(a or "")).lower() == re.sub(r"\W+", "", str(b or "")).lower()


def _squash(s):
    return re.sub(r"[\s–—-]+", "", str(s or "")).lower()


def _page(source_path):
    m = re.match(r"\s*page\s+(\d+)", str(source_path or ""))
    return int(m.group(1)) if m else None


def _status(p):
    """The engine's verdict on one parameter, in the vocabulary of expected_status."""
    if p is None:
        return None
    if p.get("kind") in ("qualitative",) or p.get("status"):
        st = p.get("status")
        return {"positive": "positive", "negative": "negative"}.get(st, "not_graded")
    if p.get("kind") == "categorical":
        return "not_graded"
    # An unrecognised unit can still be graded against the interval printed beside it.
    if p.get("value") is None or (not p.get("interpretable", True)
                                  and p.get("reference_source") != "report"):
        return "not_graded"
    if p.get("abnormal"):
        return p.get("direction") or "abnormal"
    if p.get("grade") == "unknown":
        return "not_graded"
    return "normal"


def _index_json_path(result):
    """test index -> the parameter (or unmapped observation) the JSON path produced."""
    by_index = {}
    for p in result.get("parameters") or []:
        m = re.search(r"\.tests\[(\d+)\]", str((p.get("raw") or {}).get("source_path")))
        if m:
            by_index[int(m.group(1))] = ("mapped", p)
    for o in result.get("unmapped_observations") or []:
        m = re.search(r"\.tests\[(\d+)\]", str(o.get("source_path")))
        if m:
            by_index.setdefault(int(m.group(1)), ("unmapped", o))
    # A JSON test dropped as the duplicate of another is still "extracted".
    for d in result.get("duplicates_resolved") or []:
        for x in d.get("dropped") or []:
            m = re.search(r"\.tests\[(\d+)\]", str(x.get("source")))
            if m:
                by_index.setdefault(int(m.group(1)), ("duplicate", {
                    "parameter_id": d["parameter_id"], "raw": {"raw_value": x.get("value")}}))
    return by_index


def _pdf_candidates(result):
    """Every reading the PDF path produced: mapped, dropped duplicates and unmapped."""
    out = []
    for p in result.get("parameters") or []:
        raw = p.get("raw") or {}
        if raw:
            out.append({"kind": "mapped", "pid": p["parameter_id"], "param": p,
                        "raw_name": raw.get("raw_name"), "value": raw.get("raw_value"),
                        "unit": raw.get("raw_unit"), "range": raw.get("raw_range"),
                        "page": _page(raw.get("source_path"))})
    for d in result.get("duplicates_resolved") or []:
        for x in d.get("dropped") or []:
            out.append({"kind": "duplicate", "pid": d["parameter_id"], "param": None,
                        "raw_name": None, "value": x.get("value"), "unit": x.get("unit"),
                        "range": x.get("range"), "page": _page(x.get("source"))})
    for o in result.get("unmapped_observations") or []:
        out.append({"kind": "unmapped", "pid": None, "param": None, "raw_name": o.get("raw_name"),
                    "value": o.get("raw_value"), "unit": o.get("raw_unit"),
                    "range": o.get("raw_range"), "page": _page(o.get("source_path"))})
    return out


def audit(pdf_path, truth_path, pdf_result=None):
    truth = json.loads(Path(truth_path).read_text(encoding="utf-8"))
    rj = analyse(truth, Path(truth_path).name)
    rp = pdf_result if pdf_result is not None else analyse(Path(pdf_path).read_bytes(), Path(pdf_path).name)
    analysed_pdf = rp.get("analysed", False)
    jidx = _index_json_path(rj)
    cands = _pdf_candidates(rp) if analysed_pdf else []
    pdf_params = {p["parameter_id"]: p for p in rp.get("parameters") or []}
    json_params = {p["parameter_id"]: p for p in rj.get("parameters") or []}

    rows = []
    for i, t in enumerate(truth["tests"]):
        kind, jobj = jidx.get(i, (None, None))
        jpid = jobj.get("parameter_id") if (jobj and kind in ("mapped", "duplicate")) else None
        jp = json_params.get(jpid) if jpid else None
        expected_pid = t.get("expected_parameter", jpid)

        # The PDF reading of the same printed row: same page and value, preferring the
        # same canonical test, then the same printed name.
        same = [c for c in cands if c["page"] in (t.get("page"), None) and _same_value(c["value"], t["value"])]
        pick = ([c for c in same if expected_pid and c["pid"] == expected_pid]
                or [c for c in same if c["kind"] == "unmapped"
                    and _squash(c["raw_name"]).startswith(_squash(t["test_name"])[:8])]
                or [c for c in same if c["kind"] == "mapped" and not expected_pid
                    and _squash(c["raw_name"]) == _squash(t["test_name"])])
        c = pick[0] if pick else None
        pp = pdf_params.get(c["pid"]) if (c and c["pid"]) else None
        # The PDF parameter that path kept for this test, even if it came from another row
        kept = pdf_params.get(expected_pid) if expected_pid else None

        exp = t.get("expected_status")
        row = {
            "index": i, "page": t.get("page"), "test": t["test_name"],
            "truth_value": t["value"], "truth_unit": t.get("unit"), "truth_range": t.get("reference_range"),
            "expected_status": exp, "expected_parameter": expected_pid,
            "json_parameter": jpid, "json_kind": kind,
            "json_value": jp.get("value") if jp else None, "json_status": _status(jp),
            "pdf_extracted": c is not None, "pdf_kind": c["kind"] if c else None,
            "pdf_parameter": c["pid"] if c else None,
            "pdf_value": c["value"] if c else None, "pdf_unit": c["unit"] if c else None,
            "pdf_range": c["range"] if c else None,
            "pdf_kept_value": kept.get("value") if kept else None,
            "pdf_status": _status(kept),
        }
        row["value_match"] = bool(c) and _same_value(c["value"], t["value"])
        row["unit_match"] = bool(c) and norm_unit(c["unit"]) == norm_unit(t.get("unit"))
        row["range_match"] = bool(c) and _squash(c["range"]) == _squash(t.get("reference_range"))
        row["parameter_match"] = (row["pdf_parameter"] == expected_pid) if c else (expected_pid is None and False)
        row["engine_equivalent"] = (
            (jp is None and kept is None) or
            (jp is not None and kept is not None and
             all(_eq(jp.get(f), kept.get(f)) for f in ("value", "unit", "reference_low", "reference_high",
                                                       "abnormal", "direction", "status", "interpretable"))))
        row["pdf_status_ok"] = _status_ok(exp, row["pdf_status"])
        row["json_status_ok"] = _status_ok(exp, row["json_status"])
        rows.append(row)

    abnormal_expected = [r for r in rows if r["expected_status"] in ("high", "low", "positive")]
    summary = {
        "report": Path(pdf_path).name,
        "pdf_analysed": analysed_pdf,
        "ground_truth_tests": len(rows),
        "pdf_extracted": sum(r["pdf_extracted"] for r in rows),
        "pdf_value_correct": sum(r["value_match"] for r in rows),
        "pdf_unit_correct": sum(r["unit_match"] for r in rows),
        "pdf_range_text_correct": sum(r["range_match"] for r in rows),
        "json_mapped": sum(1 for r in rows if r["json_parameter"]),
        "pdf_mapped_same_parameter": sum(1 for r in rows if r["json_parameter"] and r["parameter_match"]),
        "engine_equivalent": sum(r["engine_equivalent"] for r in rows),
        "abnormal_expected": len(abnormal_expected),
        "abnormal_detected_pdf": sum(1 for r in abnormal_expected if r["pdf_status"] == r["expected_status"]),
        "abnormal_detected_json": sum(1 for r in abnormal_expected if r["json_status"] == r["expected_status"]),
        "false_positive_pdf": [r["test"] for r in rows if r["expected_status"] in ("normal", "negative", "not_graded")
                               and r["pdf_status"] in ("high", "low", "positive")],
        "false_positive_json": [r["test"] for r in rows if r["expected_status"] in ("normal", "negative", "not_graded")
                                and r["json_status"] in ("high", "low", "positive")],
        "missed_pdf": [r["test"] for r in abnormal_expected if r["pdf_status"] != r["expected_status"]],
        "missed_json": [r["test"] for r in abnormal_expected if r["json_status"] != r["expected_status"]],
        "status_mismatch_pdf": [(r["test"], r["expected_status"], r["pdf_status"]) for r in rows
                                if r["expected_status"] and not r["pdf_status_ok"]],
        "status_mismatch_json": [(r["test"], r["expected_status"], r["json_status"]) for r in rows
                                 if r["expected_status"] and not r["json_status_ok"]],
        "engine_derived_abnormal_pdf": [p["parameter_id"] for p in rp.get("parameters") or []
                                        if p.get("derived") and p.get("abnormal")],
        "pdf_spurious": [c for c in cands if c["kind"] != "duplicate" and not any(
            r["pdf_extracted"] and r["pdf_value"] == c["value"] and r["page"] == c["page"] for r in rows)],
        "context": {"truth": truth.get("patient"), "pdf": rp.get("patient"), "json": rj.get("patient")},
        "conditions_pdf": ["%s | %s | %s" % (d["name"], d["evidence_level"], d["presentation_tier"])
                           for d in rp.get("disease_risks") or []],
        "conditions_json": ["%s | %s | %s" % (d["name"], d["evidence_level"], d["presentation_tier"])
                            for d in rj.get("disease_risks") or []],
        "warnings_pdf": rp.get("warnings"),
    }
    for c in summary["pdf_spurious"]:
        c.pop("param", None)
    return summary, rows, rj, rp


def _status_ok(expected, got):
    """A result with no printed interval may be graded against the dictionary or left
    ungraded; either is acceptable so long as it is not called abnormal."""
    if expected is None:
        return True
    if expected == "not_graded":
        return got in ("not_graded", "normal", "negative")
    return got == expected


def _eq(a, b):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b))
    return a == b


def _print(summary, rows, show_rows):
    s = summary
    print("=" * 100)
    print("%s   (PDF analysed: %s)" % (s["report"], s["pdf_analysed"]))
    print("  context truth=%s" % s["context"]["truth"])
    print("          pdf=%s" % {k: s["context"]["pdf"].get(k) for k in ("name", "age", "sex")})
    print("         json=%s" % {k: s["context"]["json"].get(k) for k in ("name", "age", "sex")})
    for k in ("ground_truth_tests", "pdf_extracted", "pdf_value_correct", "pdf_unit_correct",
              "pdf_range_text_correct", "json_mapped", "pdf_mapped_same_parameter", "engine_equivalent",
              "abnormal_expected", "abnormal_detected_pdf", "abnormal_detected_json"):
        print("  %-28s %s" % (k, s[k]))
    for k in ("missed_pdf", "missed_json", "false_positive_pdf", "false_positive_json",
              "status_mismatch_pdf", "status_mismatch_json", "engine_derived_abnormal_pdf"):
        print("  %-28s %s" % (k, s[k]))
    print("  pdf_spurious (%d):" % len(s["pdf_spurious"]))
    for c in s["pdf_spurious"]:
        print("     p%s %-10s %-26s %r = %r %r [%r]" % (c["page"], c["kind"], c["pid"], c["raw_name"],
                                                     c["value"], c["unit"], c["range"]))
    print("  conditions pdf : %s" % s["conditions_pdf"])
    print("  conditions json: %s" % s["conditions_json"])
    if show_rows:
        print("  %-3s %-4s %-44s %-12s %-10s | %-26s %-9s %-9s | %-26s %-9s %-9s | %s" % (
            "#", "pg", "test", "truth", "expect", "json parameter", "json val", "json st",
            "pdf parameter", "pdf val", "pdf st", "problems"))
        for r in rows:
            probs = []
            if not r["pdf_extracted"]:
                probs.append("NOT EXTRACTED")
            else:
                if not r["unit_match"]:
                    probs.append("unit %r" % r["pdf_unit"])
                if not r["range_match"]:
                    probs.append("range %r" % (r["pdf_range"] or "")[:30])
                if r["json_parameter"] and not r["parameter_match"]:
                    probs.append("param")
            if not r["engine_equivalent"]:
                probs.append("PDF!=JSON")
            if not r["pdf_status_ok"]:
                probs.append("PDF STATUS")
            if not r["json_status_ok"]:
                probs.append("JSON STATUS")
            print("  %-3d %-4s %-44.44s %-12.12s %-10s | %-26.26s %-9.9s %-9s | %-26.26s %-9.9s %-9s | %s" % (
                r["index"], r["page"], r["test"], r["truth_value"], r["expected_status"],
                r["json_parameter"] or "-", r["json_value"], r["json_status"],
                r["pdf_parameter"] or "-", r["pdf_kept_value"], r["pdf_status"], "; ".join(probs)))


def main():
    args = sys.argv[1:]
    show_rows = "--rows" in args
    out_json = args[args.index("--json") + 1] if "--json" in args else None
    pairs = []
    if "--dir" in args:
        d = Path(args[args.index("--dir") + 1])
        for pdf in sorted(d.glob("report_*.pdf")):
            truth = d / "truth" / (pdf.stem + "_truth.json")
            if truth.exists():
                pairs.append((pdf, truth))
    else:
        pairs.append((Path(args[0]), Path(args[1])))
    everything = {}
    for pdf, truth in pairs:
        summary, rows, _rj, _rp = audit(pdf, truth)
        _print(summary, rows, show_rows)
        everything[pdf.name] = {"summary": summary, "rows": rows}
    if out_json:
        Path(out_json).write_text(json.dumps(everything, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
