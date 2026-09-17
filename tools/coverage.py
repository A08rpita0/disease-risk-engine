"""Parameter coverage: are the Disease Master's own markers actually wired into clusters?

Answers the question in both directions, because each catches a different defect:

  MARKER -> PARAMETER   Every marker named in the Disease Master's 'Related Markers/Tests'
                        column must resolve to a parameter in the dictionary, either
                        directly by alias or through a documented umbrella term. A marker
                        that resolves to nothing is either a missing assay (declared in
                        config/marker_map.json) or a genuine gap.

  PARAMETER -> COHORT   Every parameter in the dictionary must be evaluated by at least
                        one cluster, or be declared reported-only. An orphan parameter is
                        extracted, converted and graded but can never influence a result,
                        which is a silent dead end.

  DISEASE -> MARKERS    For each condition, at least one of the markers its own Disease
                        Master row names must be evaluated by a cluster that links to it.
                        This is the check that proves the mapping uses the right markers
                        rather than merely firing by some other route.

Run:  python tools/coverage.py
"""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.config import get_config          # noqa: E402


def resolve_marker(cfg, token):
    """A Disease Master marker phrase -> parameter id. In marker text a slash lists
    alternatives ("HbA1c/fasting glucose"), unlike a report row name where it can denote
    a ratio, so each side is tried in turn when the whole phrase does not resolve."""
    pid = cfg.resolve_alias(token)
    if pid is None and "/" in token:
        for part in token.split("/"):
            part = part.strip()
            pid = cfg.resolve_alias(part) if len(part) >= 3 else None
            if pid:
                break
    return pid


def load_marker_map(cfg):
    path = Path(cfg.dir) / "marker_map.json"
    if not path.exists():
        return {"umbrella_terms": {}, "not_markers": [], "no_assay_available": [],
                "reported_only_parameters": []}
    return json.loads(path.read_text(encoding="utf-8"))


def cohort_parameters(cohort):
    """Parameters this cluster can actually evaluate."""
    out = set()
    for r in cohort.get("triggers", []) + cohort.get("supporting", []):
        out.add(r["parameter"])
    for comp in cohort.get("components", []):
        for r in comp["any_of"]:
            out.add(r["parameter"])
    return out


def run(cfg=None):
    cfg = cfg or get_config()
    mm = load_marker_map(cfg)
    umbrella = {k.lower(): v for k, v in mm.get("umbrella_terms", {}).items()}
    not_markers = {s.lower() for s in mm.get("not_markers", [])}
    no_assay = set()
    for e in mm.get("no_assay_available", []):
        no_assay.add(e["marker"].lower())
        for v in e.get("marker_variants", []):
            no_assay.add(v.lower())
    reported_only = {e["id"] for e in mm.get("reported_only_parameters", [])}

    evaluated = set()
    params_by_disease = collections.defaultdict(set)
    for c in cfg.cohorts:
        ps = cohort_parameters(c)
        evaluated |= ps
        for link in c["diseases"]:
            params_by_disease[link["name"]] |= ps

    # ---------- marker -> parameter ----------
    direct = via_umbrella = fragments = declared_missing = 0
    unresolved = collections.Counter()
    unresolved_where = {}

    for d in cfg.diseases:
        for token in d.get("marker_tokens") or []:
            key = token.lower().strip()
            if key in not_markers:
                fragments += 1
            elif key in no_assay:
                declared_missing += 1
            elif resolve_marker(cfg, token) is not None:
                direct += 1
            elif key in umbrella:
                via_umbrella += 1
            else:
                unresolved[key] += 1
                unresolved_where.setdefault(key, d["name"])

    total_tokens = direct + via_umbrella + fragments + declared_missing + sum(unresolved.values())
    real_markers = total_tokens - fragments
    resolved = direct + via_umbrella

    # ---------- parameter -> cohort ----------
    all_params = {p["id"] for p in cfg.parameters}
    orphans = sorted(all_params - evaluated - reported_only)
    declared_orphans = sorted(reported_only - evaluated)
    mislabelled = sorted(reported_only & evaluated)

    # ---------- disease -> its own markers ----------
    def expand(token):
        key = token.lower().strip()
        if key in not_markers or key in no_assay:
            return set()
        pid = resolve_marker(cfg, token)
        if pid:
            return {pid}
        return set(umbrella.get(key, []))

    uncovered_diseases, covered_diseases, no_marker_rows = [], 0, []
    documented_unmappable = {c["name"] for c in cfg.unmappable.get("conditions", [])}
    for d in cfg.diseases:
        if d["name"] not in params_by_disease:
            continue
        wanted = set()
        for token in d.get("marker_tokens") or []:
            wanted |= expand(token)
        if not wanted:
            no_marker_rows.append(d["name"])
            continue
        if wanted & params_by_disease[d["name"]]:
            covered_diseases += 1
        else:
            uncovered_diseases.append({
                "disease": d["name"],
                "markers": sorted(wanted),
                "cluster_evaluates": sorted(params_by_disease[d["name"]])[:6],
                "documented": d["name"] in documented_unmappable,
            })

    return {
        "marker_tokens_total": total_tokens,
        "marker_prose_fragments": fragments,
        "marker_real": real_markers,
        "marker_direct": direct,
        "marker_via_umbrella": via_umbrella,
        "marker_declared_no_assay": declared_missing,
        "marker_resolved": resolved,
        "marker_coverage_pct": 100.0 * resolved / max(1, real_markers - declared_missing),
        "marker_unresolved": unresolved,
        "marker_unresolved_where": unresolved_where,

        "parameters_total": len(all_params),
        "parameters_evaluated": len(evaluated & all_params),
        "parameters_reported_only": len(declared_orphans),
        "parameters_orphaned": orphans,
        "parameters_mislabelled_reported_only": mislabelled,

        "diseases_with_markers_covered": covered_diseases,
        "diseases_marker_uncovered": uncovered_diseases,
        "diseases_without_parseable_markers": no_marker_rows,
    }


def main():
    cfg = get_config()
    r = run(cfg)

    print("MARKER -> PARAMETER  (Disease Master 'Related Markers/Tests')")
    print("   %-46s %d" % ("marker tokens in the column", r["marker_tokens_total"]))
    print("   %-46s %d" % ("prose fragments, not markers", r["marker_prose_fragments"]))
    print("   %-46s %d" % ("actual markers named", r["marker_real"]))
    print("     %-44s %d" % ("resolve directly by alias", r["marker_direct"]))
    print("     %-44s %d" % ("resolve via a documented umbrella term", r["marker_via_umbrella"]))
    print("     %-44s %d" % ("declared as having no assay available", r["marker_declared_no_assay"]))
    print("     %-44s %d" % ("UNRESOLVED", sum(r["marker_unresolved"].values())))
    print("   %-46s %.1f%%" % ("coverage of markers with an assay", r["marker_coverage_pct"]))
    for tok, n in r["marker_unresolved"].most_common():
        print("     GAP  x%d  %-52s [%s]" % (n, tok[:52], r["marker_unresolved_where"][tok][:30]))

    print()
    print("PARAMETER -> COHORT")
    print("   %-46s %d" % ("parameters in the dictionary", r["parameters_total"]))
    print("   %-46s %d" % ("evaluated by at least one cluster", r["parameters_evaluated"]))
    print("   %-46s %d" % ("declared reported-only", r["parameters_reported_only"]))
    print("   %-46s %d" % ("ORPHANED (neither of the above)", len(r["parameters_orphaned"])))
    for pid in r["parameters_orphaned"]:
        print("     ORPHAN  %s (%s)" % (cfg.param_by_id[pid]["name"],
                                        cfg.param_by_id[pid].get("profile")))
    for pid in r["parameters_mislabelled_reported_only"]:
        print("     MISLABELLED  %s is declared reported-only but a cluster evaluates it" % pid)

    print()
    print("DISEASE -> ITS OWN MARKERS")
    print("   %-46s %d" % ("conditions whose own markers are evaluated",
                           r["diseases_with_markers_covered"]))
    print("   %-46s %d" % ("conditions with no parseable marker text",
                           len(r["diseases_without_parseable_markers"])))
    print("   %-46s %d" % ("conditions NOT using their own markers",
                           len(r["diseases_marker_uncovered"])))
    for u in r["diseases_marker_uncovered"]:
        tag = "documented" if u["documented"] else "GAP"
        print("     %-11s %-44s wants %s" % (tag, u["disease"][:44], ", ".join(u["markers"])[:44]))

    undocumented = [u for u in r["diseases_marker_uncovered"] if not u["documented"]]
    ok = (not r["marker_unresolved"] and not r["parameters_orphaned"]
          and not r["parameters_mislabelled_reported_only"] and not undocumented)
    print()
    print("PARAMETER COVERAGE: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
