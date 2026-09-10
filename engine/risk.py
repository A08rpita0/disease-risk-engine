"""Stage 5 - Disease risk scoring against the Disease Master.

Every detected cohort carries links into Disease Master rows. This stage pools those
links per disease and turns them into a transparent evidence score.

Combination rule - noisy-OR:

    score = 1 - PRODUCT over contributions of (1 - contribution)

Each contribution is  link_weight x cohort_confidence x role_factor.

Why noisy-OR rather than a sum:
  - it is bounded in [0, 1] with no arbitrary clipping
  - more independent evidence always raises the score, but with diminishing returns,
    so a disease cannot be pushed to certainty by piling on weak signals
  - each contribution stays individually inspectable, which is what makes the result
    explainable rather than a black box

Data sufficiency is scored separately and can only ever CAP the reported evidence
level, never raise it. A disease whose markers were mostly not measured is reported
as limited evidence with the missing parameters named, which is the honest answer.
"""
from __future__ import annotations

from .models import DiseaseRisk, RiskContribution

ROLE_FACTOR = {
    "primary": 1.0,
    "supporting": 0.7,
    "downstream_risk": 0.6,
    "differential": 0.45,
}

EVIDENCE_BANDS = [
    (0.72, "High"),
    (0.48, "Moderate"),
    (0.28, "Low"),
    (0.12, "Limited"),
]

REPORT_FLOOR = 0.12

# Below this fraction of the disease's marker set, the evidence level is capped.
COVERAGE_CAP_THRESHOLD = 0.34
COVERAGE_CAP_LEVEL = "Limited"
COVERAGE_SOFT_THRESHOLD = 0.6
COVERAGE_SOFT_CAP = "Moderate"

# A finding must clear the lowest reported band before it raises a time-critical
# warning: enough to keep a floor-scraping differential quiet, low enough that a
# single dangerous value still warns. Tied to the band boundary rather than a magic
# number. Gated on the SCORE, not the coverage-capped level - gating on the level is
# what silenced a troponin sixty times the upper limit.
URGENT_SCORE_FLOOR = 0.28

URGENCY_ORDER = {"unknown": 0, "routine": 1, "monitoring": 2, "specialist": 3, "emergency": 4}


def band(score):
    for threshold, label in EVIDENCE_BANDS:
        if score >= threshold:
            return label
    return "Limited"


class RiskEngine:
    def __init__(self, config):
        self.cfg = config

    def score(self, patient, cohort_hits):
        by_cohort = {h.cohort_id: h for h in cohort_hits}
        pooled = {}

        for hit in cohort_hits:
            cohort = self.cfg.cohort_by_id[hit.cohort_id]
            for link in cohort.get("diseases", []):
                pooled.setdefault(link["name"], []).append((hit, link))

        risks = []
        for disease_name, entries in pooled.items():
            risk = self._score_one(disease_name, entries, patient, by_cohort)
            if risk is not None:
                risks.append(risk)

        risks.sort(key=lambda r: (-r.score, -URGENCY_ORDER.get(r.urgency_tier, 0), r.name))
        return risks

    # ------------------------------------------------------------------

    @staticmethod
    def _fired_parameters(hit):
        """Parameters that actually contributed evidence inside this cohort."""
        return {h.parameter_id for h in hit.hits if h.effective_weight > 0}

    def _link_applies(self, hit, link):
        """A link gated with `requires_any` is only credited when one of those specific
        markers actually fired.

        Without this, a panel cohort would credit every disease it lists. A patient with
        a positive dengue test and a NEGATIVE malaria test would be reported as at risk
        of malaria, which is plainly wrong.
        """
        required = link.get("requires_any")
        if not required:
            return True
        return bool(set(required) & self._fired_parameters(hit))

    def _score_one(self, disease_name, entries, patient, by_cohort):
        disease = self.cfg.disease_by_name[disease_name]
        contributions = []

        entries = [(hit, link) for hit, link in entries if self._link_applies(hit, link)]
        if not entries:
            return None

        # Keep only the strongest contribution per cohort; a cohort should not be able
        # to score a disease twice through two links.
        best_by_cohort = {}
        for hit, link in entries:
            role = link.get("role", "supporting")
            factor = ROLE_FACTOR.get(role, 0.5)
            value = link["weight"] * hit.confidence * factor
            prev = best_by_cohort.get(hit.cohort_id)
            if prev is None or value > prev[0]:
                best_by_cohort[hit.cohort_id] = (value, hit, link, role)

        for value, hit, link, role in best_by_cohort.values():
            contributions.append(RiskContribution(
                cohort_id=hit.cohort_id, cohort_name=hit.name, role=role,
                link_weight=link["weight"], cohort_confidence=hit.confidence,
                contribution=round(value, 4), dm_basis=link.get("dm_basis", "")))

        contributions.sort(key=lambda c: c.contribution, reverse=True)

        product = 1.0
        for c in contributions:
            product *= (1.0 - min(0.97, c.contribution))
        score = round(1.0 - product, 4)

        if score < REPORT_FLOOR:
            return None

        triggering, cohort_names = self._collect_triggers(contributions, by_cohort)
        coverage, observed, missing = self._coverage(disease, contributions, by_cohort, patient)

        level = band(score)
        # `capped` means "this level is constrained by how little was measured", which is
        # true whether or not the band actually moved - a Limited finding built on 20% of
        # the relevant markers still needs that caveat shown.
        # Thin data steps the level DOWN one band; it does not slam it to the bottom.
        # Coverage is already folded into the score itself, so collapsing a 0.79 straight
        # to "Limited" both double-counts it and prints a label that contradicts the
        # number beside it.
        capped = coverage < COVERAGE_CAP_THRESHOLD
        if coverage < COVERAGE_CAP_THRESHOLD:
            level = _step_down(level)
        elif coverage < COVERAGE_SOFT_THRESHOLD and _rank(level) > _rank(COVERAGE_SOFT_CAP):
            level, capped = COVERAGE_SOFT_CAP, True

        # A differential-only case is a "consider and exclude", not a positive finding.
        if all(c.role == "differential" for c in contributions) and _rank(level) > _rank("Low"):
            level, capped = "Low", True

        urgency = disease.get("urgency", {})
        tier = urgency.get("tier", "unknown")
        # A cohort's urgency override belongs to the pattern, so it only escalates the
        # condition that pattern PRIMARILY indicates. A critical potassium makes the
        # potassium imbalance urgent; it does not make the patient's chronic kidney
        # disease an emergency.
        for c in contributions:
            if c.role != "primary":
                continue
            override = by_cohort[c.cohort_id].urgency_override
            if override and URGENCY_ORDER.get(override, 0) > URGENCY_ORDER.get(tier, 0):
                tier = override

        risk = DiseaseRisk(
            disease_id=disease["id"], name=disease["name"],
            classification=disease.get("classification", "Disease"),
            profiles=disease.get("profiles", []),
            score=score, evidence_level=level, evidence_capped=capped,
            urgency_tier=tier, urgency_raw=urgency.get("raw"),
            conditional_urgency=urgency.get("conditional_tier"),
            urgency_escalation=urgency.get("escalation"),
            contributions=contributions,
            triggering_parameters=triggering, cohorts=cohort_names,
            data_coverage=round(coverage, 4), missing_parameters=missing,
            confirmatory_tests=disease["fields"].get("Confirmatory/Diagnostic Tests"),
            dm_fields=disease["fields"], review_status=disease.get("review_status"),
            icd10=disease.get("icd10"))
        risk.explanation = self._explain(risk, disease, coverage, observed)
        return risk

    # ------------------------------------------------------------------

    def _collect_triggers(self, contributions, by_cohort):
        seen, triggering, cohort_names = set(), [], []
        for c in contributions:
            hit = by_cohort[c.cohort_id]
            cohort_names.append({"id": hit.cohort_id, "name": hit.name,
                                 "confidence": hit.confidence, "role": c.role})
            for h in hit.hits:
                if h.effective_weight <= 0:
                    continue
                if h.parameter_id in seen:
                    continue
                seen.add(h.parameter_id)
                p = self.cfg.param_by_id.get(h.parameter_id, {})
                triggering.append({
                    "parameter_id": h.parameter_id,
                    "name": h.parameter_name,
                    "profile": p.get("profile"),
                    "observed": h.observed,
                    "finding": h.label,
                    "via_cohort": hit.name,
                    "discounted": bool(h.suppressed_by),
                })
        return triggering, cohort_names

    def _coverage(self, disease, contributions, by_cohort, patient):
        """How much of this disease's relevant marker set was actually measured.

        The marker set is the union of expected_parameters across the cohorts that link
        to this disease - which is how the Disease Master's free-text Related Markers/Tests
        field becomes a concrete, checkable list.

        Inside a panel cohort, markers that gate a DIFFERENT disease are excluded. A
        dengue assessment should not be marked down for a scrub typhus test that was
        never relevant to it.
        """
        per_cohort, expected_union = [], set()
        for c in contributions:
            cohort = self.cfg.cohort_by_id[c.cohort_id]
            cohort_expected = set(cohort.get("expected_parameters", []))
            mine, others = set(), set()
            for link in cohort.get("diseases", []):
                if link["name"] == disease["name"]:
                    mine |= set(link.get("requires_any", []))
                else:
                    others |= set(link.get("requires_any", []))
            relevant = cohort_expected - (others - mine)
            if not relevant:
                continue
            present = sum(1 for p in relevant if patient.present(p))
            per_cohort.append((c.contribution, present / len(relevant)))
            expected_union |= relevant

        if not per_cohort:
            return 1.0, [], []

        # Weight each cohort's coverage by how much it actually contributed. A supporting
        # cluster that happens to expect a wide marker set should not drag down the
        # coverage of a disease that its primary cluster covered fully.
        total_weight = sum(w for w, _ in per_cohort)
        coverage = (sum(w * cov for w, cov in per_cohort) / total_weight) if total_weight else 0.0

        observed = sorted(p for p in expected_union if patient.present(p))
        missing = sorted(p for p in expected_union if not patient.present(p))
        return coverage, observed, missing

    def _pname(self, pid):
        p = self.cfg.param_by_id.get(pid)
        return p["name"] if p else pid

    def _explain(self, risk, disease, coverage, observed):
        parts = []
        cls = disease.get("classification", "Disease")
        lead = "risk condition" if cls == "Risk Condition/Syndrome" else "condition"

        drivers = [c for c in risk.contributions if c.role in ("primary", "supporting")]
        if drivers:
            parts.append("Flagged because %s matched this %s in the clinical reference: %s." % (
                "a detected pattern" if len(drivers) == 1 else "detected patterns",
                lead,
                "; ".join("%s (cluster confidence %.0f%%, link weight %.2f, %s link)"
                          % (c.cohort_name, c.cohort_confidence * 100, c.link_weight, c.role)
                          for c in drivers[:3])))
        diffs = [c for c in risk.contributions if c.role == "differential"]
        if diffs:
            parts.append("Raised as a differential to consider and exclude, from %s."
                         % ", ".join(c.cohort_name for c in diffs[:3]))
        downstream = [c for c in risk.contributions if c.role == "downstream_risk"]
        if downstream:
            parts.append("Also carried as a downstream risk of %s."
                         % ", ".join(c.cohort_name for c in downstream[:3]))

        named = [t["name"] for t in risk.triggering_parameters if not t["discounted"]][:8]
        if named:
            parts.append("Driven by: " + ", ".join(named) + ".")

        parts.append("Evidence combined across %d independent cluster%s gives a score of %.2f (%s)."
                     % (len(risk.contributions), "" if len(risk.contributions) == 1 else "s",
                        risk.score, risk.evidence_level))

        if risk.evidence_capped:
            parts.append("Only %.0f%% of the markers relevant to this condition were present in "
                         "this record, so the evidence level is held down deliberately and this "
                         "finding should be read as a prompt to test further, not as a conclusion."
                         % (coverage * 100))
        if risk.missing_parameters:
            parts.append("Testing these would most improve confidence: "
                         + ", ".join(self._pname(p) for p in risk.missing_parameters[:8]) + ".")
        if risk.conditional_urgency and risk.conditional_urgency != risk.urgency_tier:
            escalation = (risk.urgency_escalation or risk.urgency_raw or "").rstrip(" .")
            parts.append("Note that this can become %s-level: %s." % (
                risk.conditional_urgency, escalation))
        if risk.confirmatory_tests:
            parts.append("Confirming it would require: %s."
                         % risk.confirmatory_tests.rstrip(" ."))
        return " ".join(parts)


def _rank(level):
    order = {"Limited": 0, "Low": 1, "Moderate": 2, "High": 3}
    return order.get(level, 0)


_LADDER = ["Limited", "Low", "Moderate", "High"]


def _step_down(level):
    """One band lower, floored at Limited."""
    return _LADDER[max(0, _rank(level) - 1)]
