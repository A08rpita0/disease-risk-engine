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
from .extract import extract_with_text
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
        self.risk_engine = RiskEngine(self.cfg)
        self.rec_engine = RecommendationEngine(self.cfg)

    def run(self, data, filename="input", sex=None, age=None, patient_id=None):
        observations, context, warnings, raw_text = extract_with_text(data, filename)

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

        cohort_hits, cohorts_skipped = self.cohort_engine.detect(patient)
        risks = self.risk_engine.score(patient, cohort_hits)
        recommendations = self.rec_engine.build(patient, cohort_hits, risks)

        return self._assemble(patient, observations, cohort_hits, cohorts_skipped,
                              risks, recommendations, filename, document)

    # ------------------------------------------------------------------

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
                  risks, recommendations, filename, document=None):
        params = list(patient.parameters.values())
        abnormal = [p for p in params if p.abnormal]

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
                "parameters_unmapped": len(patient.unmapped),
                "abnormal_count": len(abnormal),
                "profiles_touched": sorted(by_profile),
                "cohorts_detected": len(cohort_hits),
                "conditions_flagged": len(risks),
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
            "parameters_by_profile": by_profile,
            "unmapped_observations": [o.to_dict() for o in patient.unmapped],
            "duplicates_resolved": patient.duplicates_resolved,
            "warnings": patient.extraction_warnings,
            "cohorts": [c.to_dict() for c in cohort_hits],
            "cohorts_not_assessable": cohorts_skipped,
            "disease_risks": [r.to_dict() for r in risks],
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
