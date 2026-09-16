"""Stage 4 - Cohort / cluster detection.

Evaluates every configured cohort against the standardized patient. A cohort fires
when enough of its conditions are met AND enough of its parameters were actually
measured. Two evaluation modes:

  weighted  - fire when at least `min_triggers` distinct trigger conditions are met.
  count_of  - fire when at least `count_required` of the named components are met.
              Used where the clinical definition is itself a count (metabolic
              syndrome's 3-of-5, the ISTH DIC score, pancytopenia's 3 cell lines).

Confidence, 0..1, answers "how strongly does this patient show this pattern?" and is
built from three things:
  - how much of the cohort's weight actually fired
  - how severe the individual abnormalities are
  - how much of the expected data was available

Double counting is prevented by redundancy groups. LDL, non-HDL and ApoB all measure
atherogenic particle burden, so within one cohort only the strongest signal from each
redundancy group carries full weight; the rest are recorded but heavily discounted.
"""
from __future__ import annotations

from .gatekeeper import Gatekeeper
from .models import CohortHit, TriggerHit

# Weight retained by the 2nd, 3rd... signal from the same redundancy group.
REDUNDANCY_DISCOUNT = 0.25

# A supporting signal is worth less than a defining trigger, by design.
SUPPORTING_FACTOR = 0.6

# Ceiling on how much measured supporting evidence can dilute confidence.
# Only evidence actually present in the record counts towards it.
SUPPORT_ALLOWANCE = 1.2


def evaluate_condition(param, cond):
    """Evaluate one condition against a NormalizedParameter. Returns (bool, description)."""
    if param is None:
        return False, None

    # --- qualitative / categorical ---
    if "status" in cond:
        want = cond["status"]
        if param.kind not in ("qualitative",):
            return False, None
        if want == "any":
            return param.status is not None, "reported as %s" % param.status
        return param.status == want, "reported as %s" % param.status

    if "category_in" in cond:
        if param.category is None:
            return False, None
        wanted = {str(v).strip().lower() for v in cond["category_in"]}
        return param.category in wanted, "reported as %s" % param.category

    # --- abnormality-relative (works with whatever reference range applied) ---
    if "abnormal" in cond:
        want = cond["abnormal"]
        if want == "none":
            if param.kind == "qualitative":
                return param.status == "negative", "reported as %s" % param.status
            if param.value is None:
                return False, None
            return (not param.abnormal), "%s %s (within range)" % (_v(param.value), param.unit or "")
        if not param.abnormal:
            return False, None
        if want == "any":
            return True, _observed(param)
        return param.direction == want, _observed(param)

    if "grade_in" in cond:
        return param.grade in cond["grade_in"], _observed(param)

    # --- numeric comparisons ---
    # Thresholds are written in the canonical unit. A value in a unit the dictionary could
    # not convert is never compared with them.
    if param.value is None or not getattr(param, "interpretable", True):
        return False, None
    v = param.value
    if "between" in cond:
        lo, hi = cond["between"]
        return lo <= v <= hi, _observed(param)
    if "outside" in cond:
        lo, hi = cond["outside"]
        return (v < lo or v > hi), _observed(param)
    for op, test in (("gte", lambda a, b: a >= b), ("gt", lambda a, b: a > b),
                     ("lte", lambda a, b: a <= b), ("lt", lambda a, b: a < b),
                     ("eq", lambda a, b: a == b)):
        if op in cond:
            return test(v, cond[op]), _observed(param)
    return False, None


def _observed(param):
    if param.kind == "qualitative":
        return "reported as %s" % param.status
    unit = (" " + param.unit) if param.unit else ""
    ref = ""
    if param.reference_low is not None or param.reference_high is not None:
        lo = _v(param.reference_low) if param.reference_low is not None else ""
        hi = _v(param.reference_high) if param.reference_high is not None else ""
        ref = " (reference %s-%s)" % (lo, hi) if lo and hi else (
            " (reference under %s)" % hi if hi else " (reference over %s)" % lo)
    return "%s%s%s" % (_v(param.value), unit, ref)


def _v(x):
    """Human-readable number: thousands separators for counts, trimmed decimals otherwise."""
    if x is None:
        return ""
    if isinstance(x, float):
        if abs(x) >= 1000:
            return format(int(round(x)), ",d")
        return "%g" % round(x, 4)
    return str(x)


class CohortEngine:
    def __init__(self, config):
        self.cfg = config

    def detect(self, patient, vetoes=None):
        """Detect clusters. `vetoes` are hard exclusions already resolved for this
        patient; a vetoed cluster is removed outright rather than scored down, so it
        reaches neither the risk engine nor the recommendation engine."""
        vetoes = vetoes or []
        hits, skipped, suppressed = [], [], []
        for cohort in self.cfg.cohorts:
            veto = Gatekeeper.cohort_vetoed(cohort["id"], vetoes)
            if veto is not None:
                # Evaluate anyway, purely so the audit records what WOULD have fired.
                would, _ = self._evaluate(cohort, patient)
                if would:
                    suppressed.append({
                        "cohort_id": cohort["id"], "name": cohort["name"],
                        "would_have_confidence": round(would.confidence, 4),
                        **veto.audit()})
                continue
            hit, why = self._evaluate(cohort, patient)
            if hit:
                hits.append(hit)
            elif why:
                skipped.append({"cohort_id": cohort["id"], "name": cohort["name"], "reason": why})
        hits.sort(key=lambda h: h.confidence, reverse=True)
        return hits, skipped, suppressed

    # ------------------------------------------------------------------

    def _evaluate(self, cohort, patient):
        sex_restriction = cohort.get("sex_restriction")
        if sex_restriction:
            if patient.context.sex is None:
                return None, "needs the patient's sex, which was not supplied"
            if patient.context.sex != sex_restriction:
                return None, None       # simply not applicable; not worth reporting

        expected = cohort.get("expected_parameters", [])
        observed = [p for p in expected if patient.present(p)]
        missing = [p for p in expected if not patient.present(p)]
        coverage = (len(observed) / len(expected)) if expected else 0.0

        min_present = cohort.get("min_parameters_present", 1)
        n_present_any = sum(
            1 for p in self._all_referenced(cohort) if patient.present(p))
        if n_present_any < min_present:
            # Only worth reporting when the record was part-way there. A cardiac cluster
            # in a stool-analysis-only report is simply irrelevant, not "not assessable".
            if n_present_any == 0:
                return None, None
            return None, "only %d of the %d parameters this cluster needs were available" % (
                n_present_any, min_present)

        mode = cohort.get("mode", "weighted")
        if mode == "count_of":
            return self._eval_count_of(cohort, patient, observed, missing, coverage)
        return self._eval_weighted(cohort, patient, observed, missing, coverage)

    def _all_referenced(self, cohort):
        out = set(cohort.get("expected_parameters", []))
        for r in cohort.get("triggers", []) + cohort.get("supporting", []):
            out.add(r["parameter"])
        for comp in cohort.get("components", []):
            for r in comp["any_of"]:
                out.add(r["parameter"])
        return out

    # ------------------------------------------------------------------

    def _eval_weighted(self, cohort, patient, observed, missing, coverage):
        trigger_hits = self._collect(cohort.get("triggers", []), patient, "trigger")
        distinct_trigger_params = {h.parameter_id for h in trigger_hits}
        min_triggers = cohort.get("min_triggers", 1)
        if len(distinct_trigger_params) < min_triggers:
            return None, None

        support_hits = self._collect(cohort.get("supporting", []), patient, "supporting")
        all_hits = self._apply_redundancy(trigger_hits + support_hits)

        fired = sum(h.effective_weight for h in all_hits)
        # Denominator: the trigger weight required to fire, plus an allowance for
        # supporting evidence that was ACTUALLY MEASURED.
        #
        # Charging for supporting tests nobody ordered scores a complete criterion as
        # though evidence were missing. That is how a troponin sixty times the upper
        # limit came out labelled "not enough data": its three supporting markers were
        # absent from the record, so two thirds of the denominator was for tests that
        # were never going to fire. Absent supporting evidence is handled by the
        # coverage factor below, which is the right place for it.
        top_triggers = sorted((r.get("weight", 1.0) for r in cohort.get("triggers", [])),
                              reverse=True)[:min_triggers]
        available_support = sum(
            r.get("weight", 1.0) * SUPPORTING_FACTOR
            for r in cohort.get("supporting", [])
            if patient.present(r["parameter"]))
        denom = sum(top_triggers) + min(SUPPORT_ALLOWANCE, available_support)
        raw = min(1.0, fired / denom) if denom else 0.0
        confidence = self._apply_coverage(raw, coverage, cohort)

        hit = self._build_hit(cohort, patient, all_hits, [], [], confidence,
                              coverage, observed, missing)
        hit.confidence_breakdown = {
            "mode": "weighted",
            "signals": [{
                "parameter": h.parameter_name, "role": h.role, "base_weight": h.weight,
                "effective_weight": h.effective_weight,
                "severity_factor": round(h.effective_weight / h.weight, 4)
                if h.weight and not h.suppressed_by else None,
                "redundancy": h.suppressed_by,
            } for h in all_hits],
            "fired_weight": round(fired, 4),
            "denominator": {"required_trigger_weight": round(sum(top_triggers), 4),
                            "measured_support_allowance": round(
                                min(SUPPORT_ALLOWANCE, available_support), 4)},
            "raw_confidence": round(raw, 4),
            "coverage": round(coverage, 4),
            "coverage_factor": self._coverage_factor(coverage, cohort),
            "confidence": confidence,
            "constants": {"SUPPORTING_FACTOR": SUPPORTING_FACTOR,
                          "REDUNDANCY_DISCOUNT": REDUNDANCY_DISCOUNT,
                          "SUPPORT_ALLOWANCE": SUPPORT_ALLOWANCE},
        }
        return hit, None

    def _eval_count_of(self, cohort, patient, observed, missing, coverage):
        met, unmet, comp_hits = [], [], []
        for comp in cohort.get("components", []):
            best = None
            for ref in comp["any_of"]:
                p = patient.get(ref["parameter"])
                ok, desc = evaluate_condition(p, ref["condition"])
                if ok:
                    cand = TriggerHit(
                        parameter_id=ref["parameter"], parameter_name=p.name,
                        label=ref.get("label", comp["label"]), observed=desc,
                        weight=1.0, effective_weight=1.0, role="component",
                        redundancy_group=self.cfg.param_by_id[ref["parameter"]].get("redundancy_group"))
                    if best is None or p.severity_score > 0:
                        best = cand
            if best is not None:
                met.append({"id": comp["id"], "label": comp["label"], "evidence": best.label})
                comp_hits.append(best)
            else:
                measurable = any(patient.present(r["parameter"]) for r in comp["any_of"])
                unmet.append({
                    "id": comp["id"], "label": comp["label"],
                    "status": "not met" if measurable else "not assessable - parameter not measured",
                    "parameters": [r["parameter"] for r in comp["any_of"]],
                })

        required = cohort["count_required"]
        if len(met) < required:
            return None, None

        support_hits = self._collect(cohort.get("supporting", []), patient, "supporting")
        all_hits = self._apply_redundancy(comp_hits + support_hits)

        # Confidence scales with how far past the threshold the patient is.
        total_components = len(cohort.get("components", []))
        extra = (len(met) - required) / max(1, total_components - required)
        confidence = 0.7 + 0.25 * min(1.0, extra)
        confidence += 0.05 * min(1.0, sum(h.effective_weight for h in support_hits) / 2.0)
        confidence = min(1.0, confidence)
        raw = confidence
        confidence = self._apply_coverage(confidence, coverage, cohort)

        hit = self._build_hit(cohort, patient, all_hits, met, unmet, confidence,
                              coverage, observed, missing)
        hit.confidence_breakdown = {
            "mode": "count_of",
            "components_met": len(met), "components_required": required,
            "components_total": total_components,
            "raw_confidence": round(raw, 4),
            "coverage": round(coverage, 4),
            "coverage_factor": self._coverage_factor(coverage, cohort),
            "confidence": confidence,
        }
        return hit, None

    # ------------------------------------------------------------------

    def _collect(self, refs, patient, role):
        out = []
        for ref in refs:
            p = patient.get(ref["parameter"])
            ok, desc = evaluate_condition(p, ref["condition"])
            if not ok:
                continue
            base = ref.get("weight", 1.0)
            if role == "supporting":
                base *= SUPPORTING_FACTOR
            # Severity modulates: a value just over the line counts for less than a
            # markedly deranged one. Floor at 0.6 so a genuine threshold crossing
            # always carries real weight.
            sev = 0.6 + 0.4 * min(1.0, p.severity_score)
            label = ref.get("label", "")
            if p.parameter_id != ref["parameter"]:
                label = "%s (measured as %s)" % (label or ref["parameter"], p.name)
            out.append(TriggerHit(
                parameter_id=p.parameter_id, parameter_name=p.name,
                label=label, observed=desc,
                weight=round(base, 4), effective_weight=round(base * sev, 4), role=role,
                redundancy_group=self.cfg.param_by_id[ref["parameter"]].get("redundancy_group")))
        return out

    def _apply_redundancy(self, hits):
        """Keep full weight for the strongest signal in each redundancy group; discount
        the rest so several views of the same biology cannot inflate the score."""
        by_group = {}
        for h in hits:
            key = h.redundancy_group or ("__unique__" + h.parameter_id)
            by_group.setdefault(key, []).append(h)

        out = []
        for key, group in by_group.items():
            if len(group) == 1 or key.startswith("__unique__"):
                out += group
                continue
            group.sort(key=lambda h: h.effective_weight, reverse=True)
            keeper = group[0]
            out.append(keeper)
            for h in group[1:]:
                h.effective_weight = round(h.effective_weight * REDUNDANCY_DISCOUNT, 4)
                h.suppressed_by = keeper.parameter_name
                out.append(h)
        # Deduplicate the same parameter matching two conditions in one cohort:
        # keep only its strongest contribution.
        best_per_param = {}
        for h in out:
            prev = best_per_param.get(h.parameter_id)
            if prev is None or h.effective_weight > prev.effective_weight:
                if prev is not None:
                    prev.effective_weight = 0.0
                    prev.suppressed_by = "same parameter, stronger condition matched"
                best_per_param[h.parameter_id] = h
            else:
                h.effective_weight = 0.0
                h.suppressed_by = "same parameter, stronger condition matched"
        return sorted(out, key=lambda h: h.effective_weight, reverse=True)

    @staticmethod
    def _coverage_factor(coverage, cohort):
        if not cohort.get("expected_parameters"):
            return 1.0
        return round(0.72 + 0.28 * min(1.0, coverage / 0.6), 4)

    @staticmethod
    def _apply_coverage(confidence, coverage, cohort):
        """Thin data lowers confidence but never zeroes a genuine finding.

        A cohort that fired on 2 of its 8 expected parameters is real, but the engine
        should say so with less certainty than one that fired on 7 of 8.
        """
        if not cohort.get("expected_parameters"):
            return round(confidence, 4)
        factor = 0.72 + 0.28 * min(1.0, coverage / 0.6)
        return round(min(1.0, confidence * factor), 4)

    # ------------------------------------------------------------------

    def _build_hit(self, cohort, patient, hits, met, unmet, confidence,
                   coverage, observed, missing):
        urgency = cohort.get("urgency_override")
        cond_override = cohort.get("urgency_override_when")
        if cond_override:
            p = patient.get(cond_override["parameter"])
            ok, _ = evaluate_condition(p, cond_override["condition"])
            if ok:
                urgency = cond_override["tier"]

        hit = CohortHit(
            cohort_id=cohort["id"], name=cohort["name"],
            category=cohort.get("category", cohort.get("domain", "General")),
            description=cohort.get("description", ""),
            profiles_touched=cohort.get("profiles_touched", []),
            cross_profile_rationale=cohort.get("cross_profile_rationale"),
            mode=cohort.get("mode", "weighted"),
            hits=hits, components_met=met, components_unmet=unmet,
            confidence=confidence, data_coverage=round(coverage, 4),
            parameters_observed=observed, parameters_missing=missing,
            evidence=cohort.get("evidence", []), urgency_override=urgency)
        hit.explanation = self._explain(cohort, hit)
        return hit

    def _pname(self, pid):
        p = self.cfg.param_by_id.get(pid)
        return p["name"] if p else pid

    def _explain(self, cohort, hit):
        parts = []
        active = [h for h in hit.hits if h.effective_weight > 0 and not h.suppressed_by]
        if hit.mode == "count_of":
            parts.append("%d of the %d defining components are present (%d required): %s." % (
                len(hit.components_met), len(cohort.get("components", [])),
                cohort["count_required"],
                "; ".join(c["evidence"] for c in hit.components_met)))
        else:
            named = [h for h in active if h.role == "trigger"]
            if named:
                parts.append("Triggered by " + "; ".join(
                    "%s (%s)" % (h.label or h.parameter_name, h.observed) for h in named) + ".")
        support = [h for h in active if h.role == "supporting"]
        if support:
            parts.append("Supporting findings: " + "; ".join(
                "%s (%s)" % (h.label or h.parameter_name, h.observed) for h in support[:6]) + ".")
        discounted = [h for h in hit.hits if h.suppressed_by and h.suppressed_by != "same parameter, stronger condition matched"]
        if discounted:
            parts.append("Counted at reduced weight to avoid double-counting the same biology: "
                         + ", ".join(sorted({h.parameter_name for h in discounted})) + ".")
        if hit.parameters_missing:
            parts.append("Not measured in this record: "
                         + ", ".join(self._pname(p) for p in hit.parameters_missing) + ".")
        return " ".join(parts)
