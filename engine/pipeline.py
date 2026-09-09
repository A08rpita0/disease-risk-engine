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
from .extract import extract
from .models import PatientContext
from .normalize import Normalizer
from .recommend import RecommendationEngine
from .risk import RiskEngine

DISCLAIMER = (
    "This is a disease-risk signalling tool, not a diagnostic system. It identifies "
    "clinically established patterns in laboratory data and maps them to the supplied "
    "Disease Master. Laboratory data alone cannot account for symptoms, examination "
    "findings or history, so every finding here is a risk signal to be taken to a "
    "doctor - not a diagnosis."
)


class Pipeline:
    def __init__(self, config=None):
        self.cfg = config or get_config()
        self.normalizer = Normalizer(self.cfg)
        self.cohort_engine = CohortEngine(self.cfg)
        self.risk_engine = RiskEngine(self.cfg)
        self.rec_engine = RecommendationEngine(self.cfg)

    def run(self, data, filename="input", sex=None, age=None, patient_id=None):
        observations, context, warnings = extract(data, filename)

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
        cohort_hits, cohorts_skipped = self.cohort_engine.detect(patient)
        risks = self.risk_engine.score(patient, cohort_hits)
        recommendations = self.rec_engine.build(patient, cohort_hits, risks)

        return self._assemble(patient, observations, cohort_hits, cohorts_skipped,
                              risks, recommendations, filename)

    # ------------------------------------------------------------------

    def _assemble(self, patient, observations, cohort_hits, cohorts_skipped,
                  risks, recommendations, filename):
        params = list(patient.parameters.values())
        abnormal = [p for p in params if p.abnormal]

        by_profile = {}
        for p in params:
            by_profile.setdefault(p.profile or "Other", []).append(p.parameter_id)

        urgent = [r for r in risks if r.urgency_tier == "emergency"
                  and r.evidence_level in ("High", "Moderate")]

        coverage = self._coverage_report(patient, risks)

        return {
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "source_file": filename,
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
