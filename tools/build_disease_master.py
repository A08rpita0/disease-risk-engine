"""
Excel -> config/disease_master.json

Converts the stakeholder-supplied DiseaseMaster_Updated.xlsx into the machine-readable
Disease Master the engine consumes. Every column of the source sheet is preserved
verbatim under `fields`; derived helper structures are added under separate keys so the
original content stays auditable and re-generatable.

Nothing here invents clinical content. The only additions are mechanical:
  - splitting `Related Profile(s)` on commas into a list
  - parsing the leading triage stem out of `Severity/Urgency Level`
  - tokenising `Related Markers/Tests` for search/UI (not used for scoring)
  - a stable slug id per disease row

Usage:  python tools/build_disease_master.py <path-to-xlsx>
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "config" / "disease_master.json"

SHEET_MASTER = "Disease Master"
SHEET_PROFILES = "Profile Coverage Reference"
SHEET_UNCLEAR = "Unclear Mappings (Resolved)"
SHEET_LEGEND = "Field Legend"
SHEET_DQ = "Data Quality Notes"

# Triage stems as used in the Severity/Urgency Level column. Ordered most -> least urgent.
# The column is free text ("Needs monitoring; emergency if severe acute attack"), so we
# parse a structured base tier plus an escalation clause.
URGENCY_TIERS = [
    ("emergency", 4, ("emergency", "life-threatening", "respiratory failure")),
    ("specialist", 3, ("needs specialist management", "needs urgent workup")),
    ("monitoring", 2, ("needs monitoring",)),
    ("routine", 1, ("routine",)),
]


def slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return re.sub(r"_+", "_", text)


def clean(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in {"N/A", "NA", "NONE"}:
        return None
    return re.sub(r"\s+", " ", text)


def _tier_of(text):
    """Highest tier named anywhere in `text`, or None."""
    low = (text or "").lower()
    for name, rank, needles in URGENCY_TIERS:
        if any(n in low for n in needles):
            return name, rank
    return None


# 'Emergency if acute chest pain...', 'urgent if thyroid storm suspected' - the high tier
# applies only when a clinical condition holds, which lab data alone cannot establish.
CONDITIONAL = re.compile(
    r"\b(emergency(?:-level)?(?:\s+urgency)?|urgent|needs? urgent \w+|seek care promptly)\b"
    r"[^.;]{0,40}?\bif\b", re.I)
OTHERWISE = re.compile(r"\botherwise\b[,\s]*(?P<rest>[^;.]+)", re.I)


def parse_urgency(raw):
    """Turn the free-text Severity/Urgency Level column into a structured triage object.

    The column mixes unconditional statements ('Emergency - life-threatening condition')
    with conditional ones ('Emergency if acute chest pain; otherwise needs monitoring').
    Treating the second kind as an unconditional emergency would make the engine cry wolf
    on every raised cholesterol, so conditional clauses are recorded separately and the
    baseline tier is taken from the unconditional part.
    """
    if not raw:
        return {"tier": "unknown", "rank": 0, "base": None, "escalation": None,
                "can_escalate": False, "conditional_tier": None, "raw": None}

    base, _, escalation = raw.partition(";")
    escalation = escalation.strip() or None

    conditional_tier = None
    base_for_tier = base

    if CONDITIONAL.search(base):
        found = _tier_of(base)
        conditional_tier = found[0] if found else None
        # Prefer an explicit 'otherwise ...' clause for the baseline tier.
        m = OTHERWISE.search(raw)
        base_for_tier = m.group("rest") if m else ""

    found = _tier_of(base_for_tier)
    if found is None:
        # No unconditional tier in the base: fall back to the rest of the string, but
        # never inherit a tier that was itself stated conditionally.
        rest = escalation or ""
        found = _tier_of(rest) if rest and not CONDITIONAL.search(rest) else None
    if found is None:
        found = ("monitoring", 2) if conditional_tier else ("routine", 1)
    tier, rank = found

    if escalation and CONDITIONAL.search(escalation):
        esc_tier = _tier_of(escalation)
        if esc_tier and (conditional_tier is None
                         or esc_tier[1] > (_tier_of(conditional_tier) or (None, 0))[1]):
            conditional_tier = esc_tier[0]

    escalates = bool(conditional_tier) or (
        bool(escalation) and any(w in escalation.lower()
                                 for w in ("emergency", "urgent", "seek care", "promptly")))

    return {
        "tier": tier,
        "rank": rank,
        "base": base.strip() or None,
        "escalation": escalation,
        "can_escalate": escalates,
        "conditional_tier": conditional_tier,
        "raw": raw,
    }


def split_profiles(raw):
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


MARKER_SPLIT = re.compile(r"[;,]| and (?=[A-Z])")


def tokenise_markers(raw):
    """Best-effort split of the free-text marker list. Display/search only."""
    if not raw:
        return []
    parts = []
    for chunk in MARKER_SPLIT.split(raw):
        chunk = chunk.strip(" .")
        chunk = re.sub(r"\s*\([^)]*\)\s*$", "", chunk).strip()
        if chunk and len(chunk) < 90 and not chunk.lower().startswith(("used only", "no specific")):
            parts.append(chunk)
    return parts


def read_kv_sheet(wb, name, header_row_marker):
    """Reads the small reference sheets that have a title banner above the real header."""
    if name not in wb.sheetnames:
        return []
    ws = wb[name]
    rows = [r for r in ws.iter_rows(values_only=True) if any(c is not None for c in r)]
    header_idx = None
    for i, r in enumerate(rows):
        if r and r[0] is not None and str(r[0]).strip() == header_row_marker:
            header_idx = i
            break
    if header_idx is None:
        return []
    header = [clean(c) or ("col%d" % i) for i, c in enumerate(rows[header_idx])]
    out = []
    for r in rows[header_idx + 1:]:
        rec = {header[i]: clean(r[i]) for i in range(min(len(header), len(r)))}
        if any(rec.values()):
            out.append(rec)
    return out


def main(xlsx_path):
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb[SHEET_MASTER]

    header = [clean(c) for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
    header = [h for h in header if h]

    diseases, skipped = [], []
    seen_ids = {}

    for row in ws.iter_rows(min_row=2, values_only=True):
        name = clean(row[0])
        if not name:
            continue
        fields = {header[i]: clean(row[i]) for i in range(len(header))}

        classification = fields.get("Classification")
        markers = fields.get("Related Markers/Tests")

        # 'Note: Tumour Marker Test profile overlaps with Cancer Profile' is a data note
        # carried inside the sheet, not a condition. No markers, no ICD code.
        if name.lower().startswith("note:") or (classification == "Other" and not markers):
            skipped.append({"name": name, "reason": "sheet annotation row, not a condition"})
            continue

        did = slugify(name)
        if did in seen_ids:
            seen_ids[did] += 1
            did = "%s_%d" % (did, seen_ids[did])
        else:
            seen_ids[did] = 1

        diseases.append({
            "id": did,
            "name": name,
            "classification": classification,
            "profiles": split_profiles(fields.get("Related Profile(s)")),
            "synonyms": [s.strip() for s in (fields.get("Synonyms / AKA") or "").split(",")
                         if s.strip()],
            "urgency": parse_urgency(fields.get("Severity/Urgency Level")),
            "marker_tokens": tokenise_markers(markers),
            "icd10": fields.get("ICD-10 Code (approx.)"),
            "review_status": fields.get("Review Status"),
            "fields": fields,
        })

    payload = {
        "meta": {
            "source_file": Path(xlsx_path).name,
            "source_sheet": SHEET_MASTER,
            "columns": header,
            "disease_count": len(diseases),
            "skipped_rows": skipped,
            "provenance_note": (
                "Disease definitions, markers and high-risk indicators are taken verbatim "
                "from this workbook and are never paraphrased. The cohort rules that map "
                "laboratory patterns onto these rows are defined separately in "
                "config/cohorts/, each citing the clinical reference that establishes it, "
                "and each disease link quotes the Disease Master field that justifies it. "
                "Correctness is established by the technical validation suites in "
                "tools/validate.py: configuration integrity, evidence coverage, clinical "
                "test cases, consistency properties, end-to-end mapping reachability and "
                "parameter-count coverage. Output is disease-risk signalling, not diagnosis."
            ),
        },
        "profiles": read_kv_sheet(wb, SHEET_PROFILES, "NG Profile (canonical)"),
        "field_legend": read_kv_sheet(wb, SHEET_LEGEND, "Field"),
        "unclear_mappings_resolved": read_kv_sheet(wb, SHEET_UNCLEAR, "Condition"),
        "data_quality_notes": read_kv_sheet(wb, SHEET_DQ, "Observation"),
        "diseases": diseases,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print("wrote %s" % OUT)
    print("  diseases      : %d" % len(diseases))
    print("  skipped rows  : %d -> %s" % (len(skipped), [s["name"] for s in skipped]))
    print("  profiles ref  : %d" % len(payload["profiles"]))
    print("  legend rows   : %d" % len(payload["field_legend"]))
    print("  dq notes      : %d" % len(payload["data_quality_notes"]))


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else str(
        Path.home() / "Downloads" / "DiseaseMaster_Updated.xlsx"
    )
    main(src)
