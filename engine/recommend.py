"""Stage 6 - Personalized action plan.

Recommendations come from three places, in priority order:
  1. Urgency - anything the clinical reference marks emergency or specialist-level.
  2. The clinical reference's own 'Prevention/Lifestyle Guidance' and 'Recommended Next Step'
     columns for each flagged condition. The master stays the source of truth for
     disease-level guidance; this engine does not paraphrase it.
  3. The action library in config/recommendations.json, keyed by detected cohort and
     by individual abnormal parameter.

Plus a follow-up testing block built from the parameters the risk engine identified as
missing, so the user is told concretely what would sharpen the picture.

Nothing here prescribes treatment or names a drug. Everything routes back to a clinician.
"""
from __future__ import annotations

import re

from .models import Recommendation

PRIORITY_ORDER = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
CATEGORY_ORDER = ["Urgent", "Consultation", "Testing", "Monitoring", "Diet", "Activity", "Lifestyle"]

# Only conditions at or above this evidence level pull their reference guidance,
# so a long tail of low-evidence differentials does not swamp the plan.
GUIDANCE_LEVELS = {"High", "Moderate"}

# An action plan a person will actually read. Guidance is drawn from the strongest
# findings rather than from every condition that cleared the reporting floor.
MAX_GUIDANCE_CONDITIONS = 4
MAX_COHORT_SOURCES = 5

# Lifts "in 4-6 weeks" / "after 8-12 weeks" / "in 3 months" out of the advice text
# so the follow-up schedule can be shown as a schedule rather than buried in prose.
TIMEFRAME_RE = re.compile(
    r"(?:\b(?:in|after|every|within)\s+)?"
    r"\d+\s*(?:-|to|–)?\s*\d*\s*(?:day|week|month|year)s?\b", re.I)


class RecommendationEngine:
    def __init__(self, config):
        self.cfg = config
        self.lib = config.recommendations

    def build(self, patient, cohort_hits, risks):
        recs = []
        seen = set()

        def add(rec):
            key = (rec.category, rec.text.strip().lower()[:120])
            if key in seen:
                for existing in recs:
                    if (existing.category, existing.text.strip().lower()[:120]) == key:
                        for s in rec.sources:
                            if s not in existing.sources:
                                existing.sources.append(s)
                        if PRIORITY_ORDER[rec.priority] < PRIORITY_ORDER[existing.priority]:
                            existing.priority = rec.priority
                        break
                return
            seen.add(key)
            recs.append(rec)

        # ---- 1. urgency ----
        top_tier = self._top_urgency(risks, cohort_hits)
        if top_tier in self.lib.get("urgency_actions", {}):
            spec = self.lib["urgency_actions"][top_tier]
            names = [r.name for r in risks
                     if r.urgency_tier == top_tier and r.evidence_level in GUIDANCE_LEVELS][:4]
            add(Recommendation(
                category=spec["category"], priority=spec["priority"], text=spec["text"],
                because="based on the most urgent finding in this report",
                sources=names or ["overall triage level"]))

        # ---- 2. Disease Master guidance for the conditions actually flagged ----
        guidance_risks = [r for r in risks if r.evidence_level in GUIDANCE_LEVELS]
        guidance_risks.sort(key=lambda r: (-_urgency_rank(r), -r.score))
        for risk in guidance_risks[:MAX_GUIDANCE_CONDITIONS]:
            guidance = risk.dm_fields.get("Prevention/Lifestyle Guidance")
            next_step = risk.dm_fields.get("Recommended Next Step")
            if guidance:
                add(Recommendation(
                    category="Lifestyle", priority=_priority_for(risk),
                    text=guidance,
                    because="advised for %s" % risk.name,
                    sources=[risk.name]))
            if next_step:
                add(Recommendation(
                    category="Consultation", priority=_priority_for(risk),
                    text=next_step,
                    because="the usual next step for %s" % risk.name,
                    sources=[risk.name]))

        # ---- 3. cohort-specific actions ----
        top_cohorts = sorted(cohort_hits, key=lambda h: -h.confidence)[:MAX_COHORT_SOURCES]
        for hit in top_cohorts:
            for spec in self.lib.get("cohort_actions", {}).get(hit.cohort_id, []):
                add(Recommendation(
                    category=spec["category"], priority=spec["priority"], text=spec["text"],
                    because="your results match %s" % hit.name,
                    sources=[hit.name]))

        # ---- 4. parameter-level actions for abnormal results ----
        for pid, param in patient.parameters.items():
            if not param.abnormal:
                continue
            for spec in self.lib.get("parameter_actions", {}).get(pid, []):
                add(Recommendation(
                    category=spec["category"], priority=spec["priority"], text=spec["text"],
                    because="your %s was flagged (%s)" % (
                        param.name, (param.grade_label or "abnormal").lower()),
                    sources=[param.name]))

        # ---- 5. follow-up testing that would raise confidence ----
        follow_up = self._follow_up_tests(risks)
        if follow_up:
            add(Recommendation(
                category="Testing", priority="high" if follow_up[0][1] >= 2 else "medium",
                text=("The following tests were not in this report and would most improve the "
                      "confidence of this assessment: %s. Discuss with your doctor which are "
                      "worth adding." % ", ".join(name for name, _ in follow_up[:8])),
                because="these tests were not in your report",
                finding="Tests not in this report", finding_kind="general",
                sources=[name for name, _ in follow_up[:8]]))

        # ---- 6. context gaps that change interpretation ----
        if patient.context.sex is None:
            add(Recommendation(
                category="Monitoring", priority="medium",
                text="Sex was not recorded in this report. Several reference ranges used here "
                     "(haemoglobin, ferritin, creatinine, HDL, uric acid and others) differ "
                     "between men and women, so some results may change once sex is supplied.",
                because="sex was not recorded in this report",
                sources=["record completeness"]))

        # ---- 7. baseline ----
        if not any(p.abnormal for p in patient.parameters.values()):
            for spec in self.lib.get("no_abnormality", []):
                add(Recommendation(category=spec["category"], priority=spec["priority"],
                                   text=spec["text"], because="all your results were in range",
                                   sources=["overall result"]))
        for spec in self.lib.get("general", []):
            add(Recommendation(category=spec["category"], priority=spec["priority"],
                               text=spec["text"], because="applies to every report",
                               sources=["General advice"]))

        # Resolve each step to the condition it is ultimately about BEFORE collapsing,
        # so advice arriving under three different labels - the cluster "Bone Mineral
        # Deficiency Pattern", the condition "Vitamin D Deficiency" and the parameter
        # "Vitamin D (25-Hydroxy)" - is recognised as one subject and merged.
        for rec in recs:
            self._enrich(rec, patient, risks, cohort_hits)
        recs = self._collapse(recs)

        recs.sort(key=lambda r: (PRIORITY_ORDER[r.priority],
                                 CATEGORY_ORDER.index(r.category) if r.category in CATEGORY_ORDER else 99))
        return recs

    # ------------------------------------------------------------------

    @staticmethod
    def _collapse(recs):
        """One step per (finding, category).

        Vitamin D was producing five separate items across three categories, three of
        which said the same thing - take a supplement and get some sun. The richest
        wording wins: the action library is written for a patient to read, while the
        Disease Master's own columns are terse clinical shorthand ("Vitamin D
        supplementation per clinician guidance"), so preferring the longer text
        reliably keeps the readable one and drops the stub.
        """
        best = {}
        order = []
        for rec in recs:
            key = (rec.finding or (rec.sources[0] if rec.sources else rec.text[:40]),
                   rec.category)
            prev = best.get(key)
            if prev is None:
                best[key] = rec
                order.append(key)
                continue
            keep, drop = (rec, prev) if len(rec.text) > len(prev.text) else (prev, rec)
            for s in drop.sources:
                if s not in keep.sources:
                    keep.sources.append(s)
            if PRIORITY_ORDER[drop.priority] < PRIORITY_ORDER[keep.priority]:
                keep.priority = drop.priority
            best[key] = keep
        return [best[k] for k in order]

    @staticmethod
    def _canonical(label, patient, risks, cohort_names):
        """Resolve a cluster or parameter name to the condition it actually concerns.

        A cluster maps to the strongest reported condition it feeds; a parameter to the
        strongest condition it triggers. That is what lets three differently-labelled
        vitamin D steps collapse into one subject instead of three.
        """
        by_name = {r.name: r for r in risks}
        if label in by_name:
            return label, ("direct" if by_name[label].finding_type == "direct" else "condition")

        if label in cohort_names:
            # risk.cohorts holds {id, name, confidence, role} records, not bare names.
            owners = [r for r in risks
                      if any((c.get("name") if isinstance(c, dict) else c) == label
                             for c in (r.cohorts or []))]
            if owners:
                best = max(owners, key=lambda r: r.score)
                return best.name, ("direct" if best.finding_type == "direct" else "condition")
            return label, "pattern"

        for p in patient.parameters.values():
            if p.name != label:
                continue
            owners = [r for r in risks
                      if any(t.get("parameter_id") == p.parameter_id
                             for t in r.triggering_parameters)]
            if owners:
                best = max(owners, key=lambda r: r.score)
                return best.name, ("direct" if best.finding_type == "direct" else "condition")
            return label, "parameter"

        return label, "general"

    def _enrich(self, rec, patient, risks, cohort_hits):
        """Attach what this step is about, the results behind it, and any timing.

        Without this the plan reads as generic advice: not one item in the original
        output quoted the value that prompted it, so a reader could not connect
        'discuss the ApoB result' to their own 142 mg/dL.
        """
        by_name = {r.name: r for r in risks}
        cohort_names = {h.name for h in cohort_hits}

        if not rec.finding:
            label = rec.sources[0] if rec.sources else "General"
            rec.finding, rec.finding_kind = self._canonical(
                label, patient, risks, cohort_names)

        # The measured results that justify this step. Order matters: the cohort this
        # step actually came from is quoted first, so "ask for a repeat TSH" shows the
        # TSH rather than whichever marker happens to lead the condition's trigger
        # list, and the cap applies after filtering - slicing first was dropping
        # ApoB 142 behind the ApoB/ApoA1 ratio.
        #
        # A firing trigger is shown even when the lab's own range calls it normal.
        # TSH 5.05 sits inside a 0.54-5.3 lab range but in the 4-10 subclinical band
        # that fired the pattern; hiding it would leave the advice unexplained. The
        # band label travels with it so the reading is not read as "out of range".
        wanted, notes = [], {}

        def _add(ids, note=None):
            for pid in ids:
                if pid not in wanted:
                    wanted.append(pid)
                if note and pid not in notes:
                    notes[pid] = note

        for h in cohort_hits:
            if h.name in rec.sources or h.name == rec.finding:
                for x in sorted(h.hits, key=lambda x: -x.effective_weight):
                    if x.effective_weight > 0:
                        _add([x.parameter_id], x.label)

        risk = by_name.get(rec.finding)
        if risk:
            _add([t["parameter_id"] for t in risk.triggering_parameters
                  if not t.get("discounted")])
        _add([p.parameter_id for p in patient.parameters.values()
              if p.name == rec.finding or p.parameter_id in rec.sources])

        for pid in wanted:
            p = patient.get(pid)
            if p is None:
                continue
            note = notes.get(pid)
            if not p.abnormal and not note:
                continue
            if len(rec.values) >= 4:
                break
            rec.values.append({
                "name": p.name,
                "value": p.value if p.value is not None else p.status,
                "unit": p.unit,
                "reference_low": p.reference_low,
                "reference_high": p.reference_high,
                "reading": (p.grade_label or p.grade) if p.abnormal else note,
                "in_range": not p.abnormal,
            })

        m = TIMEFRAME_RE.search(rec.text)
        if m:
            rec.timeframe = m.group(0).strip()

    # ------------------------------------------------------------------

    @staticmethod
    def _top_urgency(risks, cohort_hits):
        order = {"routine": 1, "monitoring": 2, "specialist": 3, "emergency": 4}
        best, best_rank = None, 0
        for r in risks:
            if r.evidence_level not in GUIDANCE_LEVELS:
                continue
            rank = order.get(r.urgency_tier, 0)
            if rank > best_rank:
                best, best_rank = r.urgency_tier, rank
        for h in cohort_hits:
            rank = order.get(h.urgency_override or "", 0)
            if rank > best_rank:
                best, best_rank = h.urgency_override, rank
        return best

    def _follow_up_tests(self, risks):
        """Rank missing parameters by how many flagged conditions would benefit, weighted
        by the evidence level of those conditions."""
        score = {}
        for risk in risks:
            if risk.evidence_level == "Limited" and not risk.evidence_capped:
                continue
            weight = 2 if risk.evidence_level in GUIDANCE_LEVELS else 1
            for pid in risk.missing_parameters:
                score[pid] = score.get(pid, 0) + weight
        ranked = sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))
        out = []
        for pid, n in ranked:
            p = self.cfg.param_by_id.get(pid)
            if p:
                out.append((p["name"], n))
        return out


def _urgency_rank(risk):
    return {"routine": 1, "monitoring": 2, "specialist": 3, "emergency": 4}.get(
        risk.urgency_tier, 0)


def _priority_for(risk):
    if risk.urgency_tier == "emergency":
        return "urgent"
    if risk.urgency_tier == "specialist" or risk.evidence_level == "High":
        return "high"
    return "medium"
