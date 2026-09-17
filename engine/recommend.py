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

from .findings import NOTED_NEEDS_STEP, build_lab_noted_findings
from .models import Recommendation

PRIORITY_ORDER = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
# Only steps about getting care can carry "urgent".
URGENT_CATEGORIES = {"Urgent", "Consultation", "Testing"}
TRIGGER_CLAUSE = "The result behind this:"
# Steps that confirm or rule a pattern out. The only ones an unsupported pattern gets.
CONFIRMATION_CATEGORIES = {"Urgent", "Consultation", "Testing"}


def _one_lower(priority):
    """urgent stays urgent - a safety escalation is never softened by this rule."""
    return {"high": "medium", "medium": "low"}.get(priority, priority)
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


def _fmt_value(v):
    if isinstance(v, float):
        return ("%g" % round(v, 3)) if abs(v) < 1000 else format(int(round(v)), ",d")
    return str(v)


def _band_clause(f):
    """The configured band label or the lab interval - the evidence, quoted, not advice."""
    if f.get("grade_label") and f.get("finding_basis") == "decision_threshold":
        return "It falls in the configured band \"%s\"." % f["grade_label"]
    if f.get("reference_text"):
        return "The laboratory's reference interval is %s." % f["reference_text"]
    return ""


class RecommendationEngine:
    def __init__(self, config):
        self.cfg = config
        self.lib = config.recommendations

    def build(self, patient, cohort_hits, risks, lab_findings=None):
        recs = []
        seen = set()
        lab_findings = lab_findings or []

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
            # Name the result that set the triage level. "One or more findings need
            # same-day assessment" under a cardiovascular heading did not say that the
            # finding was a troponin of 0.17 ng/mL.
            drivers = [h for c in cohort_hits if c.urgency_override == top_tier
                       for h in c.hits if h.role == "trigger" and h.effective_weight > 0]
            text = spec["text"]
            if drivers:
                text += " " + TRIGGER_CLAUSE + " %s." % "; ".join(
                    "%s %s" % (h.parameter_name, h.observed) for h in drivers[:3])
            add(Recommendation(
                category=spec["category"], priority=spec["priority"], text=text,
                because="based on the most urgent finding in this report",
                trace="urgency", trace_detail=top_tier,
                sources=[c.name for c in cohort_hits if c.urgency_override == top_tier][:2]
                + names or ["overall triage level"]))

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
                    trace="disease_guidance",
                    trace_detail="%s > Prevention/Lifestyle Guidance" % risk.disease_id,
                    sources=[risk.name]))
            if next_step:
                add(Recommendation(
                    category="Consultation", priority=_priority_for(risk),
                    text=next_step,
                    because="the usual next step for %s" % risk.name,
                    trace="disease_guidance",
                    trace_detail="%s > Recommended Next Step" % risk.disease_id,
                    sources=[risk.name]))

        # ---- 3. cohort-specific actions ----
        # A cluster whose conditions are ALL unsupported (insufficient evidence, or none
        # reported) contributes only the steps that would confirm or rule it out -
        # talking to a doctor, and tests - one priority lower. Diet, activity, lifestyle
        # and medication advice presume the pattern is real; for a subclinical TSH with
        # nothing else behind it, "take thyroid medication on an empty stomach" was
        # being printed to someone who has no reason to be on any.
        #
        # The category alone does not make a step a confirmation. "Iron deficiency is a
        # finding, not a diagnosis - the important question is where the iron is going...
        # warrants investigation of the gut" is a Consultation, but it presumes the
        # deficiency is real; it was being given with serum iron and TIBC both normal, and
        # once on a transferrin saturation this engine had calculated itself. Steps written
        # for an established finding carry `presumes_established` in the action library.
        supported = {c.get("id") for r in risks if r.presentation_tier != "insufficient"
                     for c in (r.cohorts or []) if isinstance(c, dict)}
        top_cohorts = sorted(cohort_hits, key=lambda h: -h.confidence)[:MAX_COHORT_SOURCES]
        for hit in top_cohorts:
            confirm_only = hit.cohort_id not in supported
            for spec in self.lib.get("cohort_actions", {}).get(hit.cohort_id, []):
                if confirm_only and (spec["category"] not in CONFIRMATION_CATEGORIES
                                     or spec.get("presumes_established")):
                    continue
                priority = spec["priority"]
                if confirm_only:
                    priority = _one_lower(priority)
                add(Recommendation(
                    category=spec["category"], priority=priority, text=spec["text"],
                    because=("your results partly match %s, which the evidence here does not "
                             "yet support" % hit.name) if confirm_only
                    else "your results match %s" % hit.name,
                    trace="cohort_action", trace_detail=hit.cohort_id,
                    sources=[hit.name]))

        # ---- 4. parameter-level actions for abnormal results ----
        for pid, param in patient.parameters.items():
            if not param.abnormal:
                continue
            for spec in self.lib.get("parameter_actions", {}).get(pid, []):
                # Advice written for one direction ("Anaemia is a finding...") is never
                # given for the other - a haemoglobin of 17.2 is not anaemia.
                if spec.get("direction") and spec["direction"] != param.direction:
                    continue
                add(Recommendation(
                    category=spec["category"], priority=spec["priority"], text=spec["text"],
                    because="your %s was flagged (%s)" % (
                        param.name, (param.grade_label or "abnormal").lower()),
                    trace="parameter_action", trace_detail=pid,
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
                trace="coverage_gap", trace_detail="missing_parameters",
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
                trace="record_context", trace_detail="sex",
                sources=["record completeness"]))

        # ---- 6b. results the report marks that nothing above interprets ----
        # An equivocal screen, an abnormal reading that lost to a duplicate, a test this
        # dictionary does not know but the report flags: none is graded abnormal here, and
        # without this step a report whose only notable result was one of them ended with
        # "No abnormal patterns were detected".
        marked = [f for f in build_lab_noted_findings(patient)
                  if f["finding_basis"] in NOTED_NEEDS_STEP]
        if marked:
            add(Recommendation(
                category="Consultation", priority="medium",
                text="Some results on this report could not be assessed here but need a doctor's "
                     "attention: %s. Take the original report to your doctor."
                     % "; ".join('%s (%s)' % (f["name"], str(f["value"]).strip()) for f in marked[:8]),
                because="the report marks results this analysis cannot grade",
                trace="lab_noted", trace_detail=",".join(f["finding_basis"] for f in marked[:8]),
                sources=[f["name"] for f in marked[:8]]))

        # ---- 7. baseline ----
        if not any(p.abnormal for p in patient.parameters.values()) and not marked:
            for spec in self.lib.get("no_abnormality", []):
                add(Recommendation(category=spec["category"], priority=spec["priority"],
                                   text=spec["text"], because="all your results were in range",
                                   trace="baseline", trace_detail="no_abnormality",
                                   sources=["overall result"]))
        for spec in self.lib.get("general", []):
            add(Recommendation(category=spec["category"], priority=spec["priority"],
                               text=spec["text"], because="applies to every report",
                               trace="general", trace_detail="general",
                               sources=["General advice"]))

        # Resolve each step to the condition it is ultimately about BEFORE collapsing,
        # so advice arriving under three different labels - the cluster "Bone Mineral
        # Deficiency Pattern", the condition "Vitamin D Deficiency" and the parameter
        # "Vitamin D (25-Hydroxy)" - is recognised as one subject and merged.
        for rec in recs:
            self._enrich(rec, patient, risks, cohort_hits)
        recs = self._collapse(recs)

        # ---- every abnormal result is cited somewhere in the plan ----
        # Checked directly, after everything else has been built: which abnormal
        # results does no step quote? Only 10 of the dictionary's parameters carry
        # advice of their own, and a result can feed a cluster whose conditions all
        # sit in insufficient evidence, so "linked to a rule" did not mean "something
        # in the plan mentions it". hs-CRP 31.98 mg/L was reaching the plan with nothing
        # about it. The added step says only what the finding supports - the value is
        # outside its range, and a clinician should decide whether to repeat or
        # investigate it. It names no condition and suggests no treatment.
        cited = {v.get("parameter_id") for r in recs for v in r.values}
        # A MARKED abnormality needs a step at a priority that matches it. Being quoted in
        # a low-priority confirmation step for an unsupported pattern is not enough.
        cited_high = {v.get("parameter_id") for r in recs for v in r.values
                      if r.priority in ("urgent", "high")}
        uncited = [f for f in lab_findings if f["abnormal"] and (
            f["parameter_id"] not in cited or
            (f["severity_score"] >= 0.75 and f["parameter_id"] not in cited_high))]
        extra = []
        for f in uncited:
            if f["severity_score"] < 0.75:
                continue
            extra.append(Recommendation(
                category="Consultation", priority="high",
                text=("Raise your %s result (%s%s) with your doctor. %s Ask whether it "
                      "should be repeated or investigated further."
                      % (f["name"], _fmt_value(f["value"]),
                         (" " + f["unit"]) if f["unit"] else "", _band_clause(f))),
                because="your %s is markedly outside its range" % f["name"],
                trace="lab_finding", trace_detail=f["parameter_id"],
                finding=f["name"], finding_kind="parameter",
                sources=[f["name"]]))
        milder = [f for f in uncited if f["severity_score"] < 0.75]
        if milder:
            extra.append(Recommendation(
                category="Consultation", priority="medium",
                text=("These results are outside their range, or past a guideline threshold, and "
                      "no other step in this plan covers them: %s. Show them to your doctor, who "
                      "can judge whether any needs repeating."
                      % "; ".join("%s %s%s%s" % (f["name"], _fmt_value(f["value"]),
                                                 (" " + f["unit"]) if f["unit"] else "",
                                                 _basis_clause(f))
                                  for f in milder[:10])),
                because="results outside range not covered by another step",
                trace="lab_finding",
                trace_detail=",".join(f["parameter_id"] for f in milder[:10]),
                finding="Other results outside range", finding_kind="general",
                sources=[f["name"] for f in milder[:10]]))
        for rec in extra:
            rec.finding = rec.finding or rec.sources[0]
            self._enrich(rec, patient, risks, cohort_hits)
        recs += extra

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
            # An urgent step's wording is never replaced by a non-urgent one: the longer
            # text used to win and carry the urgency off with it.
            if drop.priority == "urgent" and keep.priority != "urgent":
                keep, drop = drop, keep
            # Two urgent steps: the specific action ("A raised troponin means heart muscle
            # injury ...") is kept over the generic triage wording, and takes the
            # sentence naming the result that set the triage level.
            if keep.priority == drop.priority == "urgent" and keep.trace == "urgency" \
                    and drop.trace != "urgency":
                keep, drop = drop, keep
            if drop.trace == "urgency" and TRIGGER_CLAUSE in drop.text and TRIGGER_CLAUSE not in keep.text:
                keep.text = keep.text.rstrip() + " " + drop.text[drop.text.index(TRIGGER_CLAUSE):]
            for s in drop.sources:
                if s not in keep.sources:
                    keep.sources.append(s)
            if PRIORITY_ORDER[drop.priority] < PRIORITY_ORDER[keep.priority]:
                keep.priority = drop.priority
            best[key] = keep
        # "Urgent" means seek care now. Diet, activity and lifestyle advice for the same
        # condition is not made urgent by it - "reduce alcohol" was printed as urgent
        # beside a raised troponin.
        for rec in best.values():
            if rec.priority == "urgent" and rec.category not in URGENT_CATEGORIES:
                rec.priority = "high"
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

        # Only a SUPPORTED condition may title a step. Advice prompted by hs-CRP was being
        # filed under "Rheumatoid Arthritis" - a condition the evidence did not support -
        # simply because it was the strongest thing that cluster feeds.
        risks = [r for r in risks if r.presentation_tier != "insufficient"]

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
        if rec.trace == "lab_finding":
            # quote exactly the results the step names, nothing inherited from a cluster
            wanted[:] = [pid for pid in rec.trace_detail.split(",") if pid]
        _add([p.parameter_id for p in patient.parameters.values()
              if p.name == rec.finding or p.parameter_id in rec.sources])

        for pid in wanted:
            p = patient.get(pid)
            if p is None:
                continue
            note = notes.get(pid)
            if not p.abnormal and not note:
                continue
            if len(rec.values) >= (10 if rec.trace == "lab_finding" else 4):
                break
            rec.values.append({
                "parameter_id": p.parameter_id,
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


def _basis_clause(f):
    """What judged a listed result - never implied to be the laboratory's interval when it
    was not. hs-CRP 1.28 against printed risk bands ("Low <1.0 / Average 1.0-3.0") is past a
    configured guideline band; it is not outside a laboratory range."""
    if f.get("finding_basis") == "derived":
        return " (calculated here)"
    if f.get("finding_basis") == "decision_threshold":
        if f.get("in_lab_range"):
            return " (within the laboratory's interval; past a guideline threshold)"
        return " (past a guideline threshold, not the laboratory's printed interval)"
    return ""


def _priority_for(risk):
    # A condition's emergency tier makes its triage step and its own urgent actions
    # urgent. Its general Disease Master guidance is not thereby urgent: that is how
    # "ask your doctor to calculate your overall cardiovascular risk" came to be printed
    # as urgent beside a raised troponin.
    if risk.urgency_tier in ("emergency", "specialist") or risk.evidence_level == "High":
        return "high"
    return "medium"
