"""Abnormality recall audit: does every result a report contains survive to the UI?

    python tools/recall_audit.py            # fixtures, JSON vs every PDF layout
    python tools/recall_audit.py --json     # machine-readable

For each expected result it records the LAST stage the result reached:

    in report -> extracted -> mapped -> value correct -> abnormal flagged -> shown

"shown" means it appears somewhere a user reads findings: a direct/pattern/derived
finding, OR the abnormal-laboratory-findings list. A result that is abnormal but reaches
only the raw results table counts as NOT shown in the findings.

Exit status is non-zero if any PDF layout recalls fewer abnormal results than the JSON.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from engine.config import get_config          # noqa: E402
from engine.pipeline import analyse            # noqa: E402
from lab_panel import PANEL, as_json, expected  # noqa: E402

STAGES = ["missing", "extracted", "mapped", "value", "abnormal", "shown"]


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return v


def trace(result, cfg):
    """parameter_id -> furthest stage reached, for every expected result."""
    exp = expected()
    raw_names = {}
    for o in (result.get("unmapped_observations") or []):
        raw_names.setdefault(cfg.resolve_alias(o["raw_name"]), o)
    params = {p["parameter_id"]: p for p in result.get("parameters", [])}
    shown = set()
    for key in ("direct_findings", "derived_findings", "pattern_findings",
                "insufficient_findings"):
        for f in result.get(key, []):
            for t in f.get("triggering_parameters", []):
                shown.add(t["parameter_id"])
            ev = f.get("direct_evidence") or {}
            if ev.get("parameter_id"):
                shown.add(ev["parameter_id"])
    # Both lists are rendered: results outside their range, and results inside the
    # laboratory's range that a guideline threshold still flags.
    for key in ("abnormal_findings", "threshold_findings"):
        for f in result.get(key, []):
            shown.add(f["parameter_id"])

    out = {}
    for pid, (value, abnormal) in exp.items():
        p = params.get(pid)
        if p is None:
            out[pid] = "extracted" if pid in raw_names else "missing"
            continue
        got = p["value"] if p["value"] is not None else p.get("status")
        want = value
        if isinstance(want, float):
            unit_scale_ok = isinstance(got, (int, float)) and (
                abs(got - want) < 1e-6 or abs(got - want * 100000) < 1e-3)
        else:
            unit_scale_ok = got is not None
        if not unit_scale_ok:
            out[pid] = "mapped"
        elif abnormal and not p["abnormal"]:
            out[pid] = "value"
        elif abnormal and pid not in shown:
            out[pid] = "abnormal"
        else:
            out[pid] = "shown"
    return out


def run(verbose=True):
    cfg = get_config()
    exp = expected()
    sources = {"json": as_json()}
    for f in sorted((ROOT / "tests" / "fixtures" / "pdf").glob("*.pdf")):
        sources[f.stem] = f.read_bytes()

    results = {}
    for name, data in sources.items():
        fname = name + (".json" if name == "json" else ".pdf")
        r = analyse(data, fname)
        results[name] = trace(r, cfg)

    abnormal_ids = [pid for pid, (_v, abn) in exp.items() if abn]
    summary = {}
    for name, tr in results.items():
        summary[name] = {
            "all_read": sum(1 for pid in exp if STAGES.index(tr[pid]) >= STAGES.index("value")),
            "abnormal_shown": sum(1 for pid in abnormal_ids if tr[pid] == "shown"),
            "total": len(exp), "abnormal_total": len(abnormal_ids),
        }

    if verbose:
        names = list(results)
        print("%-22s" % "parameter" + "".join("%-14s" % n[:13] for n in names))
        for pid in exp:
            mark = "*" if exp[pid][1] else " "
            print("%s %-20s" % (mark, pid[:20]) + "".join("%-14s" % results[n][pid] for n in names))
        print("-" * (22 + 14 * len(names)))
        print("%-22s" % "values read correctly" + "".join(
            "%-14s" % ("%d/%d" % (summary[n]["all_read"], summary[n]["total"])) for n in names))
        print("%-22s" % "abnormal shown" + "".join(
            "%-14s" % ("%d/%d" % (summary[n]["abnormal_shown"], summary[n]["abnormal_total"]))
            for n in names))
        print("\n* = abnormal against the printed range. Stage is the furthest reached.")
    return results, summary


if __name__ == "__main__":
    res, summ = run(verbose="--json" not in sys.argv)
    if "--json" in sys.argv:
        print(json.dumps({"stages": res, "summary": summ}, indent=1))
    ref = summ["json"]["abnormal_shown"]
    worst = min(v["abnormal_shown"] for v in summ.values())
    sys.exit(0 if worst >= ref else 1)
