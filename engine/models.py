"""Data structures passed between pipeline stages.

The pipeline is a straight line:
    RawObservation -> NormalizedParameter -> CohortHit -> DiseaseRisk -> Recommendation
Every stage keeps a trace of what it did so the final result can explain itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class RawObservation:
    """One test result exactly as it came out of the source document, before any mapping."""
    raw_name: str
    raw_value: Any
    raw_unit: Optional[str] = None
    raw_range: Optional[str] = None
    raw_flag: Optional[str] = None
    source_path: Optional[str] = None      # JSON pointer or page/line reference
    source_kind: str = "unknown"           # json | pdf | csv | text | manual
    # How this was found, which decides whether failing to recognise it is worth
    # reporting to the user:
    #   result - a name/value pair in a result-shaped record, or one carrying a unit
    #            or a reference range. If the dictionary does not know it, that is a
    #            genuine gap.
    #   field  - a loose scalar off the document envelope: LabNo, SampleCollDate,
    #            ApprovedByDoctorID, Package_name. Never a test, so counting it as an
    #            "ignored parameter" told the user 241 results were dropped when the
    #            real number was 28.
    shape: str = "result"
    # Where on the page it came from - "table" (a row of a results table whose header
    # was found in that same table), "text" (a row rebuilt from the text layer) or
    # "json". Used to prefer the structurally reliable reading when a report states the
    # same test twice, e.g. a summary page and the laboratory page.
    origin: str = ""
    # The panel / section heading it was printed under ("Urine Routine ..."). Lets a
    # bare "Blood" or "Colour" under a urine heading be read as the urine test.
    section: Optional[str] = None
    # Method line printed under the test name ("HPLC", "Immunoturbidimetric"); kept
    # out of the name so it cannot distort name matching.
    method: Optional[str] = None
    # Where on the page it came from - "table" (a row of a results table whose header
    # was found in that same table), "text" (a row rebuilt from the text layer) or
    # "json". Used to prefer the structurally reliable reading when a report states the
    # same test twice, e.g. a summary page and the laboratory page.
    origin: str = ""
    # The panel / section heading it was printed under ("Urine Routine ..."). Lets a
    # bare "Blood" or "Colour" under a urine heading be read as the urine test.
    section: Optional[str] = None
    # Method line printed under the test name ("HPLC", "Immunoturbidimetric"); kept
    # out of the name so it cannot distort name matching.
    method: Optional[str] = None

    def to_dict(self):
        return asdict(self)


@dataclass
class NormalizedParameter:
    """A raw observation resolved to a canonical parameter, converted and graded."""
    parameter_id: str
    name: str
    profile: Optional[str]
    kind: str                               # numeric | qualitative | categorical
    value: Any = None                       # canonical numeric value
    unit: Optional[str] = None              # canonical unit
    status: Optional[str] = None            # positive | negative | indeterminate (qualitative)
    category: Optional[str] = None          # categorical raw text, lowercased

    reference_low: Optional[float] = None
    reference_high: Optional[float] = None
    reference_source: str = "dictionary"    # dictionary | report | dictionary(sex) | none

    abnormal: bool = False
    direction: Optional[str] = None         # high | low | positive | none
    grade: str = "unknown"                  # normal, mild_high, moderate_low, critical_high...
    grade_label: Optional[str] = None
    severity_score: float = 0.0             # 0..1, used to modulate evidence weight

    # False when the report's unit is not one this dictionary can convert. The number is
    # still shown as reported, and graded against the REPORT's own interval (same unit),
    # but it is never compared with a threshold written in the canonical unit: HbA1c
    # 48 mmol/mol read as 48 % is a diabetic crisis that does not exist.
    interpretable: bool = True
    derived: bool = False                   # computed rather than measured
    derivation: Optional[str] = None
    raw: Optional[RawObservation] = None
    conversion_note: Optional[str] = None
    notes: list = field(default_factory=list)

    # WHY this result is being shown, which is not the same question as whether it is
    # abnormal. "Outside the laboratory's own reference range", "inside that range but
    # past a configured clinical decision threshold" and "a number this engine
    # calculated" are three different claims, and presenting them in one list let the
    # weakest of them borrow the authority of the strongest.
    #   lab_range          - the report's own interval says it is out of range
    #   decision_threshold - a configured guideline band decided it, not the lab
    #   derived            - computed here from other results, never measured
    #   normal             - within range on whichever basis applied
    finding_basis: str = "normal"
    # What decided abnormality: range | decision_band | none. Distinct from
    # reference_source, which says where the interval came from.
    graded_by: str = "range"
    # Set when the value sits inside the lab's interval yet still met a cluster's
    # configured condition - TSH 5.05 in a 0.54-5.3 range is the worked example.
    triggered_bands: list = field(default_factory=list)

    def to_dict(self):
        d = asdict(self)
        d["raw"] = self.raw.to_dict() if self.raw else None
        return d


@dataclass
class PatientContext:
    """Demographics and anything else that changes interpretation. Never invented."""
    patient_id: Optional[str] = None
    name: Optional[str] = None
    sex: Optional[str] = None               # male | female | None
    age: Optional[float] = None
    report_date: Optional[str] = None
    source_file: Optional[str] = None
    extras: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


@dataclass
class StandardizedPatient:
    """The homogenised record every downstream stage reads. Configuration-independent."""
    context: PatientContext
    parameters: dict = field(default_factory=dict)     # parameter_id -> NormalizedParameter
    unmapped: list = field(default_factory=list)       # RawObservation that no alias matched
    duplicates_resolved: list = field(default_factory=list)
    rejected_values: list = field(default_factory=list)   # impossible values, not used
    pending_results: list = field(default_factory=list)   # tests printed as not yet reported
    extraction_warnings: list = field(default_factory=list)
    # requested id -> the measured parameter that may answer for it when the requested
    # one is absent (hs-CRP for CRP). Built from `stands_in_for` in the dictionary, so it
    # is configuration, not code. Rule evaluation goes through get()/present(); the
    # results list (`parameters`) is never altered, so nothing is shown twice.
    substitutes: dict = field(default_factory=dict)

    def get(self, pid):
        p = self.parameters.get(pid)
        if p is None and pid in self.substitutes:
            p = self.parameters.get(self.substitutes[pid])
        return p

    def present(self, pid):
        return self.get(pid) is not None

    def to_dict(self):
        return {
            "context": self.context.to_dict(),
            "parameters": {k: v.to_dict() for k, v in self.parameters.items()},
            "unmapped": [u.to_dict() for u in self.unmapped],
            "duplicates_resolved": self.duplicates_resolved,
            "extraction_warnings": self.extraction_warnings,
            "rejected_values": self.rejected_values,
        }


@dataclass
class TriggerHit:
    """One condition inside a cohort that evaluated true."""
    parameter_id: str
    parameter_name: str
    label: str
    observed: str
    weight: float
    effective_weight: float                 # weight modulated by severity, after redundancy capping
    role: str                               # trigger | supporting | component
    redundancy_group: Optional[str] = None
    suppressed_by: Optional[str] = None     # set when capped as a redundant duplicate

    def to_dict(self):
        return asdict(self)


@dataclass
class CohortHit:
    """A detected cluster, with everything needed to explain why it fired."""
    cohort_id: str
    name: str
    category: str
    description: str
    profiles_touched: list
    cross_profile_rationale: Optional[str]
    mode: str                               # weighted | count_of
    hits: list = field(default_factory=list)              # TriggerHit
    components_met: list = field(default_factory=list)    # count_of mode
    components_unmet: list = field(default_factory=list)
    confidence: float = 0.0                 # 0..1 - strength of the cluster itself
    data_coverage: float = 0.0              # fraction of expected_parameters observed
    parameters_observed: list = field(default_factory=list)
    parameters_missing: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    urgency_override: Optional[str] = None
    explanation: str = ""
    # fired weight, denominator, raw confidence and coverage factor - every number
    # behind `confidence`, so a cluster's strength can be checked by hand.
    confidence_breakdown: dict = field(default_factory=dict)

    def to_dict(self):
        d = asdict(self)
        d["hits"] = [h.to_dict() for h in self.hits]
        return d


@dataclass
class RiskContribution:
    """One cohort's contribution to one disease's risk score."""
    cohort_id: str
    cohort_name: str
    role: str                               # primary | supporting | differential | downstream_risk
    link_weight: float
    cohort_confidence: float
    contribution: float                     # link_weight * confidence * role_factor
    dm_basis: str
    support_penalty: float = 1.0            # 1.0 = untouched; <1 = expected support measured and normal
    support_note: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass
class DiseaseRisk:
    """A disease from the Disease Master, scored and explained."""
    disease_id: str
    name: str
    classification: str
    profiles: list
    score: float                            # 0..1 combined evidence
    evidence_level: str                     # High | Moderate | Low | Limited
    evidence_capped: bool = False           # True when data sufficiency capped the level
    urgency_tier: str = "unknown"
    urgency_raw: Optional[str] = None
    conditional_urgency: Optional[str] = None    # tier reached only if a clinical condition holds
    urgency_escalation: Optional[str] = None     # the clause describing that condition
    contributions: list = field(default_factory=list)     # RiskContribution
    triggering_parameters: list = field(default_factory=list)
    cohorts: list = field(default_factory=list)
    data_coverage: float = 0.0
    missing_parameters: list = field(default_factory=list)
    confirmatory_tests: Optional[str] = None
    explanation: str = ""
    dm_fields: dict = field(default_factory=dict)
    review_status: Optional[str] = None
    icd10: Optional[str] = None

    # --- presentation tier -------------------------------------------------
    # "direct"  a single measured parameter meets a configured threshold that
    #           establishes this finding on its own
    # "pattern" a multi-marker association; a hypothesis to explore, not a finding
    finding_type: str = "pattern"
    direct_evidence: Optional[dict] = None  # parameter, value, unit, reference, statement

    # The section this belongs under. finding_type answers "what KIND of evidence is
    # this"; the tier answers "how should it be presented", which is a different
    # question once evidence strength is folded in:
    #   direct        one measured, lab-reported value establishes it
    #   derived       the establishing value was calculated here, not measured
    #   pattern       a multi-marker association worth exploring
    #   insufficient  considered, but the evidence does not support reporting it as a
    #                 finding. Kept visible so the user can see it was assessed rather
    #                 than silently dropped.
    presentation_tier: str = "pattern"
    # Every number behind the final level, for audit: contributions (link weight,
    # cohort confidence, role, support damping), the raw band, any cap and why.
    score_breakdown: dict = field(default_factory=dict)
    # Measured results that argue AGAINST this, i.e. expected supporting markers that
    # were checked and came back normal. Computed during scoring and previously
    # thrown away, which left a damped pattern looking identical to an undamped one.
    contradicting: list = field(default_factory=list)
    # Expected markers that were measured and normal but carry no configured penalty;
    # shown as context so the picture is not one-sided.
    context_values: list = field(default_factory=list)

    def to_dict(self):
        d = asdict(self)
        d["contributions"] = [c.to_dict() for c in self.contributions]
        return d


@dataclass
class Recommendation:
    category: str                           # Lifestyle | Diet | Activity | Monitoring | Testing | Consultation | Urgent
    priority: str                           # urgent | high | medium | low
    text: str
    because: str                            # what triggered it
    sources: list = field(default_factory=list)   # disease names / parameter ids

    # --- grouping and evidence, so the plan can be read per finding -------------
    finding: str = ""                       # the one thing this step is about
    finding_kind: str = ""                  # condition | pattern | parameter | general
    values: list = field(default_factory=list)    # the measured results behind it
    timeframe: str = ""                     # "4-6 weeks" etc., lifted out of the text
    # Which rule produced this step, so every line can be walked back to its evidence:
    # urgency | disease_guidance | cohort_action | parameter_action | coverage_gap |
    # record_context | baseline | general
    trace: str = ""
    trace_detail: str = ""                  # the cohort id / parameter id / disease name

    def to_dict(self):
        return asdict(self)
