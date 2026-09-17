"""Pipeline orchestration.

    Input -> Extraction -> Normalization -> Standardized Patient
          -> Cohort/Correlation Engine -> Disease Master -> Disease Risk Engine
          -> Recommendation Engine -> Results

Each stage is independently testable and swappable; this module only wires them
together and assembles the enriched patient record that comes out the far end.
"""
from __future__ import annotations

import datetime

from .cohorts import CohortEngine
from .config import get_config
from .doccheck import assess
from .gatekeeper import Gatekeeper
from .extract import extract_with_text
from .findings import build_lab_findings, build_lab_noted_findings
from .models import PatientContext
from .normalize import Normalizer
from .recommend import RecommendationEngine
from .risk import RiskEngine, URGENT_SCORE_FLOOR

DISCLAIMER = (
    "This is a risk check, not a diagnosis. It compares your results against "
    "established clinical guidelines to flag patterns that may need attention. "
    "Blood tests alone cannot account for your symptoms, examination findings or "
    "medical history, so please go through anything flagged here with your doctor."
)


class Pipeline:
    def __init__(self, config=None):
        self.cfg = config or get_config()
        self.normalizer = Normalizer(self.cfg)
        self.cohort_engine = CohortEngine(self.cfg)
        self.gatekeeper = Gatekeeper(self.cfg)
        self.risk_engine = RiskEngine(self.cfg)
        self.rec_engine = RecommendationEngine(self.cfg)

    def run(self, data, filename="input", sex=None, age=None, patient_id=None):
        # The dictionary's alias lookup helps the row parser choose between ambiguous
        # splits ("CA 125 34.5") and accept a known test printed without unit or range.
        observations, context, warnings, raw_text = extract_with_text(
            data, filename, self.cfg.resolve_alias)

        # Caller-supplied demographics win over anything scraped from the document,
        # because the caller is stating them explicitly.
        if sex:
            context.sex = sex
        if age is not None:
            context.age = age
        if patient_id:
            context.patient_id = patient_id
        if not isinstance(context, PatientContext):
            context = PatientContext(source_file=filename)

        patient = self.normalizer.build(observations, context, warnings)

        # Stage 0: refuse to "analyse" something that is not a laboratory report.
        # Returned rather than raised, so every caller sees the same verdict object.
        document = assess(patient, observations, raw_text, warnings)
        if not document["is_report"]:
            return self._rejected(patient, observations, document, filename)

        # Stage 3.5: hard exclusions. Resolved once, then honoured by every later
        # stage, so a vetoed finding reaches neither scoring nor recommendations.
        vetoes = self.gatekeeper.evaluate(patient)

        cohort_hits, cohorts_skipped, cohorts_suppressed = self.cohort_engine.detect(
            patient, vetoes)
        risks, risks_suppressed = self.risk_engine.score(patient, cohort_hits, vetoes)
        self._classify_bases(patient, cohort_hits)
        # Listed independently of the Disease Master: a result does not have to feed a
        # condition to be worth showing.
        lab_findings = build_lab_findings(patient, cohort_hits, risks)
        recommendations = self.rec_engine.build(patient, cohort_hits, risks, lab_findings)

        return self._assemble(patient, observations, cohort_hits, cohorts_skipped,
                              risks, recommendations, filename, document,
                              cohorts_suppressed + risks_suppressed, lab_findings)

    # ------------------------------------------------------------------

    @staticmethod
    def _classify_bases(patient, cohort_hits):
        """Record WHY each result is being shown, which is not the same as whether it
        is abnormal.

        Three claims were being merged into one "outside the normal range" list:
          - the report's own interval says it is out of range
          - the report gave no interval, so a configured guideline band decided
          - this engine calculated the number; no lab measured it
        and a fourth case had nowhere to go at all: a value INSIDE the lab's interval
        that still met a cluster's configured condition. TSH 5.05 in a 0.54-5.3 range
        drives the hypothyroid pattern while sitting in the normal-results table, so
        the advice that follows from it looked unattached to anything.
        """
        fired = {}
        for hit in cohort_hits:
            for h in hit.hits:
                if h.effective_weight > 0:
                    fired.setdefault(h.parameter_id, []).append(
                        {"cohort": hit.name, "band": h.label or h.parameter_name,
                         "observed": h.observed})

        for pid, q in patient.parameters.items():
            bands = fired.get(pid, [])
            if q.derived:
                q.finding_basis = "derived"
            elif q.abnormal and q.kind in ("qualitative", "categorical"):
                # A reported positive is the laboratory's own result against its expected
                # negative - not a threshold this engine applied.
                q.finding_basis = "lab_range"
            elif q.abnormal:
                # Two things have to line up before this can be called a laboratory
                # abnormality: the lab supplied the interval AND that interval is what
                # the verdict rests on. A vitamin D of 14.2 against a report interval of
                # "up to 20" is called deficient by a configured guideline band, not by
                # the lab's own interval, and saying otherwise put words in the lab's
                # mouth.
                q.finding_basis = ("lab_range"
                                   if (q.reference_source.startswith("report")
                                       and q.graded_by != "decision_band")
                                   else "decision_threshold")
            elif bands:
                q.finding_basis = "decision_threshold"
            else:
                q.finding_basis = "normal"
            # Only worth recording where it explains something the lab range does not.
            if bands and not (q.abnormal and q.finding_basis == "lab_range"):
                q.triggered_bands = bands

    def _rejected(self, patient, observations, document, filename):
        """A minimal, honest response for a document that is not a lab report.

        No parameters, no clusters, no risks - producing an empty dashboard for an
        unrelated file would imply the analysis ran and found nothing wrong.
        """
        return {
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "source_file": filename,
            "analysed": False,
            "document": document,
            "disclaimer": DISCLAIMER,
            "patient": patient.context.to_dict(),
            "warnings": patient.extraction_warnings,
            "summary": {
                "observations_found": len(observations),
                "parameters_recognised": len(patient.parameters),
                "abnormal_count": 0, "cohorts_detected": 0, "conditions_flagged": 0,
            },
        }

    def _assemble(self, patient, observations, cohort_hits, cohorts_skipped,
                  risks, recommendations, filename, document=None, suppressed=None,
                  lab_findings=None):
        lab_findings = lab_findings or []
        params = list(patient.parameters.values())
        abnormal = [p for p in params if p.abnormal]

        # "Unmapped" conflated two very different things: a test this dictionary does
        # not know, and a document field that was never a test. Reporting one number
        # for both told the reader that 241 of 312 results had been thrown away.
        unmapped_tests = [o for o in patient.unmapped if o.shape == "result"]
        document_fields = [o for o in patient.unmapped if o.shape != "result"]

        by_profile = {}
        for p in params:
            by_profile.setdefault(p.profile or "Other", []).append(p.parameter_id)

        # Deliberately NOT gated on evidence level. A lone troponin is the whole point
        # of ordering a troponin; requiring corroborating evidence before warning about
        # it meant the most time-critical result in the system produced no warning.
        # The reporting floor already keeps trivial findings out of `risks`.
        urgent = [r for r in risks if r.urgency_tier == "emergency"
                  and r.score >= URGENT_SCORE_FLOOR]

        coverage = self._coverage_report(patient, risks)

        return {
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "source_file": filename,
            "analysed": True,
            "document": document,
            "disclaimer": DISCLAIMER,
            "provenance": {
                "disease_master_source": self.cfg.dm_meta.get("source_file"),
                "disease_master_rows": self.cfg.dm_meta.get("disease_count"),
                "cohorts_configured": len(self.cfg.cohorts),
                "evidence_citations": len({e["citation"] for c in self.cfg.cohorts
                                           for e in c.get("evidence", [])}),
                "note": self.cfg.dm_meta.get("provenance_note"),
            },
            "patient": patient.context.to_dict(),
            "summary": {
                "observations_found": len(observations),
                "parameters_recognised": len(params),
                "parameters_unmapped": len(unmapped_tests),
                "document_fields_skipped": len(document_fields),
                "values_rejected": len(patient.rejected_values),
                "results_pending": len(patient.pending_results),
                "lab_noted_findings": len(build_lab_noted_findings(patient)),
                "derived_values": sum(1 for p in params if p.derived),
                "duplicates_resolved": len(patient.duplicates_resolved),
                "abnormal_count": len(abnormal),
                "abnormal_findings": sum(1 for f in lab_findings if not f["in_lab_range"]),
                "abnormal_standalone": sum(1 for f in lab_findings
                                           if not f["in_lab_range"] and f["standalone"]),
                "decision_threshold_count": sum(
                    1 for p in params if p.finding_basis == "decision_threshold"),
                "profiles_touched": sorted(by_profile),
                "cohorts_detected": len(cohort_hits),
                "conditions_flagged": len(risks),
                "direct_findings": sum(1 for r in risks if r.presentation_tier == "direct"),
                "derived_findings": sum(1 for r in risks if r.presentation_tier == "derived"),
                "pattern_findings": sum(1 for r in risks if r.presentation_tier == "pattern"),
                "insufficient_findings": sum(
                    1 for r in risks if r.presentation_tier == "insufficient"),
                "suppressed_by_exclusion": len(suppressed or []),
                "high_evidence": sum(1 for r in risks if r.evidence_level == "High"),
                "moderate_evidence": sum(1 for r in risks if r.evidence_level == "Moderate"),
                "limited_evidence": sum(1 for r in risks if r.evidence_level in ("Low", "Limited")),
                "urgent_findings": len(urgent),
                "analysis_confidence": coverage["overall"],
            },
            "parameters": [p.to_dict() for p in sorted(
                params, key=lambda x: (not x.abnormal, x.profile or "", x.name))],
            "abnormal_parameters": [p.to_dict() for p in sorted(
                abnormal, key=lambda x: -x.severity_score)],
            # Every abnormal or threshold-crossing result as a finding in its own right,
            # whether or not any Disease Master rule interprets it.
            "abnormal_findings": [f for f in lab_findings if not f["in_lab_range"]],
            "threshold_findings": [f for f in lab_findings if f["in_lab_range"]],
            # Marked by the laboratory (a printed flag, or a description that differs from
            # the expected one) but not abnormal by the rules here. Shown, never counted.
            "lab_noted_findings": build_lab_noted_findings(patient),
            "parameters_by_profile": by_profile,
            "unmapped_observations": [o.to_dict() for o in unmapped_tests],
            "document_fields_skipped": [o.to_dict() for o in document_fields],
            "rejected_values": patient.rejected_values,
            "pending_results": patient.pending_results,
            "duplicates_resolved": patient.duplicates_resolved,
            "warnings": patient.extraction_warnings,
            "cohorts": [c.to_dict() for c in cohort_hits],
            "cohorts_not_assessable": cohorts_skipped,
            "disease_risks": [r.to_dict() for r in risks],
            # Two presentation tiers. A direct finding is established by one measured
            # value against a configured threshold; a pattern is a multi-marker
            # hypothesis. Mixing them lets a hypothesis read like a measured fact.
            # Kept for anything still reading the two-tier shape.
            "direct_findings": [r.to_dict() for r in risks
                                if r.presentation_tier == "direct"],
            "pattern_findings": [r.to_dict() for r in risks
                                 if r.presentation_tier == "pattern"],
            # A value this engine CALCULATED is not a laboratory abnormality, and a
            # pattern the evidence does not support is not a finding. Both were being
            # rendered in the same list as measured, supported ones.
            "derived_findings": [r.to_dict() for r in risks
                                 if r.presentation_tier == "derived"],
            "insufficient_findings": [r.to_dict() for r in risks
                                      if r.presentation_tier == "insufficient"],
            "suppressed_findings": suppressed or [],
            "urgent_findings": [r.to_dict() for r in urgent],
            "recommendations": [r.to_dict() for r in recommendations],
            "coverage": coverage,
        }

    def _coverage_report(self, patient, risks):
        """How much can be trusted, given what was actually measured.

        Reported explicitly rather than hidden, because a confident-looking answer built
        on four parameters would be misleading.
        """
        n = len(patient.parameters)
        if n == 0:
            level, note = "none", "No laboratory parameters could be recognised in this file."
        elif n < 8:
            level = "very limited"
            note = ("Only %d parameters were recognised. Single-system findings may be valid, "
                    "but cross-system patterns cannot be assessed from this few." % n)
        elif n < 15:
            level = "limited"
            note = ("%d parameters were recognised. Most single-profile patterns can be assessed; "
                    "multi-system clusters are only partly covered." % n)
        elif n < 30:
            level = "moderate"
            note = ("%d parameters were recognised, enough for most cross-profile patterns this "
                    "engine looks for." % n)
        else:
            level = "good"
            note = "%d parameters were recognised, giving broad coverage across profiles." % n

        capped = [r.name for r in risks if r.evidence_capped]
        return {
            "overall": level,
            "note": note,
            "parameters_recognised": n,
            "capped_conditions": capped,
            "capped_note": (
                "%d condition(s) were reported at a reduced evidence level because the relevant "
                "markers were largely absent from this record." % len(capped)) if capped else None,
        }


_PIPELINE = None


def get_pipeline():
    global _PIPELINE
    if _PIPELINE is None:
        _PIPELINE = Pipeline()
    return _PIPELINE


def analyse(data, filename="input", **kwargs):
    return get_pipeline().run(data, filename, **kwargs)
