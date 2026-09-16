"""Disease Master audit: can every row actually be inferred the way its own text says?

    python tools/dm_audit.py              # summary + flagged rows
    python tools/dm_audit.py --csv out.csv

For each of the Disease Master's conditions this records, from the row's own wording
and the engine's configuration:

    lab_inferable        not listed in config/unmappable.json as clinical-only
    markers_named        Related Markers/Tests that the parameter dictionary resolves
    markers_unresolved   marker phrases the dictionary cannot resolve
    cohorts              clusters linking to it, with roles
    reachable            a synthesised patient built from a linking cluster's own
                         triggers produces the condition (tools/consistency.py)
    direct_route         a single measured value can establish it (direct_evidence)
    non_lab_requirements Confirmatory text needing imaging, biopsy, clinical criteria...
    demographic_terms    sex / age wording in the row (women, postmenopausal, children)

and raises REVIEW flags - it never changes configuration itself:

    SINGLE_MARKER_CRITERION   Confirmatory/Diagnostic Tests names a single test with an
                              explicit threshold, but there is no direct route. Prediabetes
                              was exactly this ("Fasting glucose 100-125 mg/dL or HbA1c
                              5.7-6.4%") and was being scored as a weak multi-marker pattern.
    COMBINATION_CRITERION     High-Risk Indicators require markers together ("X with Y"),
                              but a primary link fires on one trigger with no support
                              requirement. Autoimmune Hepatitis was exactly this.
    DEMOGRAPHIC_UNMODELLED    the row restricts by sex or age but no linking cluster does.
    UNREACHABLE               lab-inferable, linked, yet no synthesised patient reaches it.
    NOT_LINKED                lab-inferable but no cluster links to it.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.config import get_config            # noqa: E402

THRESHOLD_RE = re.compile(
    r"(?:[≥≤<>]=?\s*\d|\d+(?:\.\d+)?\s*(?:-|–|to)\s*\d+(?:\.\d+)?\s*"
    r"(?:mg/dl|%|mmol|g/dl|ng/ml|pg/ml|u/l|miu|uiu|mg/l|/ul|cells)|above\s+\d|below\s+\d)",
    re.I)
COMBINATION_RE = re.compile(r"\b(?:with|combined|together|plus|along with|and)\b|\+", re.I)
NON_LAB_RE = re.compile(
    r"\b(?:imaging|ultrasound|x-ray|xray|ct\b|mri|scan|biopsy|endoscop\w*|colonoscop\w*|"
    r"ecg|echo\w*|angiograph\w*|dexa|clinical|symptom\w*|examination|history|"
    r"spirometry|audiometr\w*|questionnaire|criteria)\b", re.I)
# A RESTRICTION, not a passing mention: "in postmenopausal women", "an at-risk
# individual (older adult ...)". Plain "male"/"female" appear in ordinary prose.
DEMOGRAPHIC_RE = re.compile(
    r"\b(?:postmenopausal|premenopausal|pregnan\w*|in\s+women|in\s+men\b|"
    r"older\s+adults?|elderly|in\s+children|infants?|newborns?|neonat\w*)\b", re.I)
SYMPTOM_RE = re.compile(
    r"\b(?:symptom\w*|pain|wheeze|breathless\w*|rash|itch\w*|fever|hypotension|"
    r"fatigue|weight\s+loss|jaundice|bleeding|history|exposure|signs?|pattern\s+of|"
    r"clinical|joint|dry\s+(?:eye|mouth)|mass|imaging|biopsy|family|obesity)\b", re.I)


def _marker_phrases(text):
    parts = re.split(r"[,;/]|\band\b|\bor\b|\(|\)", text or "")
    return [p.strip(" .:-") for p in parts if p and len(p.strip(" .:-")) >= 2]


def _lab_arms(cfg, text):
    """Parts of a High-Risk Indicator joined by with/and/+ that are LAB markers.

    "Elevated IgE with recurrent wheeze" has one lab arm - the wheeze is a symptom, so it
    is not a lab combination the engine could enforce. "Low Ferritin with Low Iron and
    elevated TIBC" has three.
    """
    arms = set()
    clauses = re.split(r";", text or "")
    # The row lists ALTERNATIVE routes separated by ';'. If any alternative is a single
    # lab marker, a one-marker route is the row's own wording - not a missing combination.
    for clause in clauses[1:]:
        if len(_lab_arms(cfg, clause)) == 1:
            return set()
    # "especially with X" / "particularly X" marks X as strengthening, not required.
    first_clause = re.split(r"\b(?:especially|particularly|ideally)\b", clauses[0], flags=re.I)[0]
    for part in re.split(r"\bwith\b|\band\b|\+|\bplus\b|\bcombined\b|,", first_clause, flags=re.I):
        if SYMPTOM_RE.search(part):
            continue
        words = re.sub(r"\b(?:elevated|raised|high|low|positive|negative|abnormal|markedly|"
                       r"persistent\w*|both|in|range|above|below|normal|decreased|increased)\b",
                       " ", part, flags=re.I)
        pid = cfg.resolve_alias(words.strip(" .()"))
        if pid:
            arms.add(pid)
    return arms


def _reachability(cfg):
    """condition name -> True if some linking cluster's own triggers produce it."""
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        from consistency import run_mapping          # type: ignore
        from engine.pipeline import Pipeline
        res = run_mapping(Pipeline(cfg), cfg)
        dead = set(res.get("dead_mappings", []))
        return {name: name not in dead for name in cfg.links_by_disease}
    except Exception as exc:                          # pragma: no cover
        print("  (reachability unavailable: %s)" % exc)
        return None


REVIEWS = {}
_review_path = ROOT / "config" / "dm_review.json"
if _review_path.exists():
    REVIEWS = {d["condition"]: d for d in
               json.loads(_review_path.read_text(encoding="utf-8")).get("decisions", [])}


def audit(cfg=None):
    cfg = cfg or get_config()
    unmappable = {u["name"] for u in cfg.unmappable.get("conditions", [])}
    reach = _reachability(cfg)
    rows = []
    for d in cfg.diseases:
        name = d["name"]
        f = d["fields"]
        links = cfg.links_by_disease.get(name, [])
        markers = _marker_phrases(f.get("Related Markers/Tests"))
        resolved = sorted({cfg.resolve_alias(m) for m in markers if cfg.resolve_alias(m)})
        unresolved = [m for m in markers if not cfg.resolve_alias(m)]
        direct = any(link.get("direct_evidence") for _c, link in links)
        confirm = f.get("Confirmatory/Diagnostic Tests") or ""
        hri = f.get("High-Risk Indicators") or ""
        primary_single = [
            c["id"] for c, link in links
            if link.get("role") == "primary" and c.get("mode", "weighted") == "weighted"
            and c.get("min_triggers", 1) == 1 and not link.get("requires_support")
            and not link.get("requires_any")]
        demo = sorted({m.group(0).lower() for m in DEMOGRAPHIC_RE.finditer(
            " ".join([f.get("Definition") or "", hri, f.get("Other Important Factors") or ""]))})
        cohort_restricted = any(c.get("sex_restriction") for c, _l in links)

        flags = []
        inferable = name not in unmappable
        if inferable and not links:
            flags.append("NOT_LINKED")
        if inferable and links and reach is not None and not reach.get(name, True):
            flags.append("UNREACHABLE")
        if inferable and links and not direct:
            # one test, one threshold, stated as sufficient ("X ... or Y ..."), in the row's
            # own confirmatory text - and not a test needing several markers together
            first = confirm.split(";")[0]
            alternatives = re.split(r"\bor\b", first, flags=re.I)
            singles = [a for a in alternatives
                       if THRESHOLD_RE.search(a) and len(_lab_arms(cfg, a)) == 1
                       and not re.search(r"\bwith\b|\band\b", a, re.I)]
            if singles and not NON_LAB_RE.search(first):
                flags.append("SINGLE_MARKER_CRITERION")
        arms = _lab_arms(cfg, hri)
        if primary_single and len(arms) >= 2 and not direct:
            flags.append("COMBINATION_CRITERION")
        # Informational only: the engine has no route to "postmenopausal" or "older adult"
        # from laboratory values, so this is recorded, not flagged for change.
        demographic_note = bool(demo and links and not cohort_restricted)

        review = REVIEWS.get(name)
        status = ("reviewed: %s" % review["decision"]) if review else ("open" if flags else "")

        rows.append({
            "condition": name,
            "classification": d.get("classification"),
            "lab_inferable": inferable,
            "cohorts": "; ".join("%s(%s)" % (c["id"], l.get("role")) for c, l in links),
            "n_cohorts": len(links),
            "markers_resolved": ", ".join(resolved),
            "markers_unresolved": ", ".join(unresolved[:6]),
            "direct_route": direct,
            "non_lab_requirements": bool(NON_LAB_RE.search(confirm)),
            "demographic_terms": ", ".join(demo),
            "high_risk_indicators": hri,
            "confirmatory": confirm,
            "lab_arms_in_high_risk": ", ".join(sorted(arms)),
            "primary_single_trigger_links": ", ".join(primary_single),
            "flags": ", ".join(flags),
            "review_status": status,
            "review_reason": review["reason"] if review else "",
            "demographic_restriction_unmodelled": demographic_note,
        })
    return rows


def main():
    rows = audit()
    out_csv = None
    if "--csv" in sys.argv:
        out_csv = sys.argv[sys.argv.index("--csv") + 1]
    total = len(rows)
    inferable = sum(r["lab_inferable"] for r in rows)
    linked = sum(1 for r in rows if r["n_cohorts"])
    direct = sum(r["direct_route"] for r in rows)
    print("DISEASE MASTER AUDIT")
    print("  conditions                     %d" % total)
    print("  lab-inferable                  %d" % inferable)
    print("  linked to at least one cluster %d" % linked)
    print("  with a direct single-value route %d" % direct)
    print("  needing non-lab confirmation   %d" % sum(r["non_lab_requirements"] for r in rows))
    counts = {}
    for r in rows:
        for fl in filter(None, r["flags"].split(", ")):
            counts[fl] = counts.get(fl, 0) + 1
    print("  review flags                   %s" % (counts or "none"))
    reviewed = sum(1 for r in rows if r["review_status"].startswith("reviewed"))
    open_rows = [r for r in rows if r["review_status"] == "open"]
    changed = sum(1 for r in rows if r["review_status"] == "reviewed: changed")
    print("  decisions recorded             %d (%d changed the engine, %d retained)" % (
        reviewed, changed, reviewed - changed))
    print("  OPEN (flagged, not reviewed)   %d" % len(open_rows))
    print("  demographic wording unmodelled %d (informational)" % sum(
        r["demographic_restriction_unmodelled"] for r in rows))
    for r in rows:
        if r["flags"] or r["review_status"]:
            print("\n  [%s] %s  -- %s" % (r["flags"] or "no flag", r["condition"],
                                        r["review_status"] or "open"))
            print("      high-risk : %s" % r["high_risk_indicators"][:150])
            print("      confirm   : %s" % r["confirmatory"][:150])
            print("      clusters  : %s" % r["cohorts"][:150])
            if r["lab_arms_in_high_risk"]:
                print("      lab arms  : %s | single-trigger primaries: %s" % (
                    r["lab_arms_in_high_risk"], r["primary_single_trigger_links"]))
    if out_csv:
        with open(out_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print("\nwrote", out_csv)


if __name__ == "__main__":
    main()
