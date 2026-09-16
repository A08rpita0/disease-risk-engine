"""Stage 2 and 3 - Normalization and abnormality detection.

Takes raw observations and produces the StandardizedPatient every later stage reads:
  - resolves each test name onto a canonical parameter via the alias index
  - converts the value into the canonical unit
  - picks the effective reference interval (the report's own range wins over the dictionary)
  - resolves duplicates and conflicts, recording what was dropped and why
  - computes derived parameters (ratios, eGFR-adjacent indices) only from present inputs
  - flags abnormality and assigns a severity grade

Rules that matter clinically:
  - A value is never invented. Missing stays missing.
  - A range printed on the patient's own report takes priority over the dictionary,
    because it reflects the assay and population the lab actually used.
  - Sex-specific dictionary ranges are used only when sex is known; otherwise the
    widest defensible interval is used and the parameter is annotated.
"""
from __future__ import annotations

import re

from .config import norm_key, norm_unit
from .extract import parse_reference_range
from .models import NormalizedParameter, StandardizedPatient

GRADE_SEVERITY = {
    "normal": 0.0, "protective": 0.0, "unknown": 0.0,
    "mild_low": 0.25, "mild_high": 0.25,
    "moderate_low": 0.55, "moderate_high": 0.55,
    "severe_low": 0.8, "severe_high": 0.8,
    "critical_low": 1.0, "critical_high": 1.0,
}

VALUE_NUM = re.compile(r"[-+]?\d[\d,]*\.?\d*(?:[eE][-+]?\d+)?")


def parse_numeric(value):
    """Extract a number from '13.5', '13,500', '< 0.01', '1.2 mg/dL'. Returns (num, qualifier)."""
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, None
    if isinstance(value, (int, float)):
        return float(value), None
    s = str(value).strip()
    if not s:
        return None, None
    qual = None
    if s.startswith(("<", "≤")):
        qual = "less_than"
    elif s.startswith((">", "≥")):
        qual = "greater_than"
    m = VALUE_NUM.search(s.replace(",", ""))
    if not m:
        return None, qual
    try:
        return float(m.group(0)), qual
    except ValueError:
        return None, qual


class Normalizer:
    def __init__(self, config):
        self.cfg = config

    # ---------- qualitative ----------

    def _qual_status(self, raw_value, raw_flag=None, interpretation=None):
        for candidate in (raw_value, raw_flag):
            if candidate is None:
                continue
            s = str(candidate).strip().lower()
            if not s:
                continue
            if s in self.cfg.qual_positive_raw:
                return "positive"
            if s in self.cfg.qual_negative_raw:
                return "negative"
            k = norm_key(s)
            if k in self.cfg.qual_positive:
                return "positive"
            if k in self.cfg.qual_negative:
                return "negative"
            if k in self.cfg.qual_indeterminate:
                return "indeterminate"
            # 'reactive (1:8)', 'positive for IgM', 'growth of E. coli'
            for word in self.cfg.qual_positive:
                if word and re.search(r"\b%s\b" % re.escape(word), k):
                    return "positive"
            for word in self.cfg.qual_negative:
                if word and re.search(r"\b%s\b" % re.escape(word), k):
                    return "negative"
            # A numeric result on a qualitative test.
            num, _ = parse_numeric(candidate)
            if num is not None:
                if interpretation:
                    return self._numeric_status(num, interpretation)
                # Legacy fallback for titres ('1:64'), where any reactive dilution is
                # positive. NOT safe for a signal-to-cutoff index such as COI, where
                # 0.80 is negative - such parameters must declare
                # `numeric_interpretation` in config/parameters.json.
                return "positive" if num > 0 else "negative"
        return None

    @staticmethod
    def _numeric_status(num, spec):
        """Read a numeric serology result using the parameter's declared convention."""
        neg = spec.get("negative_below")
        pos = spec.get("positive_at_or_above")
        if neg is not None and num < neg:
            return "negative"
        if pos is not None and num >= pos:
            return "positive"
        if neg is not None and pos is not None and neg <= num < pos:
            return "indeterminate"      # the assay's grey zone
        if pos is not None:
            return "negative"
        return "positive" if num > 0 else "negative"

    # ---------- units ----------

    def _convert(self, pdef, value, raw_unit):
        """Convert into the parameter's canonical unit. Returns (value, note)."""
        units = pdef.get("units") or {}
        canonical = pdef.get("unit")
        if value is None:
            return None, None
        u = norm_unit(raw_unit)
        if u is None:
            return value, None
        if norm_unit(canonical) == u:
            return value, None
        factor = None
        for known, f in units.items():
            if norm_unit(known) == u:
                factor = f
                break
        if factor is None:
            return value, ("unit '%s' is not recognised for %s; value used as reported "
                           "against the %s reference range" % (raw_unit, pdef["name"], canonical))
        if factor == 1:
            return value, None
        return value * factor, "converted %s %s to %.4g %s" % (
            _fmt(value), raw_unit, value * factor, canonical)

    # ---------- reference range ----------

    def _reference(self, pdef, sex, raw_range):
        """Effective (low, high, source). Report-supplied range wins."""
        if raw_range:
            low, high = parse_reference_range(raw_range)
            if low is not None or high is not None:
                return low, high, "report"
        ref = pdef.get("ref") or {}
        if sex and sex in ref:
            return ref[sex][0], ref[sex][1], "dictionary(%s)" % sex

        sex_specific = [k for k in ("male", "female") if k in ref]
        if sex_specific and not sex:
            # Sex matters for this parameter but was not supplied. Use the widest
            # defensible interval so nothing is over-flagged, and say so - a borderline
            # result here could change once sex is known.
            lows = [ref[k][0] for k in sex_specific]
            highs = [ref[k][1] for k in sex_specific]
            if "default" in ref:
                lows.append(ref["default"][0])
                highs.append(ref["default"][1])
            return min(lows), max(highs), "dictionary(sex unknown - widened)"

        if "default" in ref:
            return ref["default"][0], ref["default"][1], "dictionary"
        return None, None, "none"

    # ---------- grading ----------

    def _bands_for(self, pdef, sex):
        if "bands_by_sex" in pdef and sex in pdef["bands_by_sex"]:
            return pdef["bands_by_sex"][sex]
        if "bands_by_sex" in pdef and not sex:
            return None      # cannot pick a sex-specific band set without sex
        return pdef.get("bands")

    def _grade(self, pdef, value, low, high, sex, ref_source):
        """Return (abnormal, direction, grade, label, note).

        Where a parameter has clinical decision bands (ADA glucose thresholds, KDIGO
        eGFR stages, NCEP lipid bands) those win, because they are absolute standards
        rather than assay-specific intervals - 'HbA1c 7.4% = diabetes range' is more
        useful than 'above reference range'. The reference interval still governs
        parameters that have no bands, and a disagreement between the two is reported
        rather than hidden.
        """
        if value is None:
            return False, None, "unknown", None, None

        bands = self._bands_for(pdef, sex)
        if bands:
            for band in bands:
                if ("lt" in band and value < band["lt"]) or ("gte" in band and value >= band["gte"]):
                    abnormal, direction, grade, label = self._from_band(band)
                    note = None
                    range_abnormal = ((high is not None and value > high)
                                      or (low is not None and value < low))
                    if range_abnormal and not abnormal:
                        note = ("this value sits outside the reference interval on the report "
                                "but inside the clinical decision band used for grading")
                    elif abnormal and not range_abnormal and ref_source == "report":
                        note = ("the reference interval on the report would call this normal; it is "
                                "graded here against the standard clinical decision band")
                    return abnormal, direction, grade, label, note

        if low is None and high is None:
            return False, None, "unknown", None, None

        width = None
        if low is not None and high is not None and high > low:
            width = high - low
        elif high not in (None, 0):
            width = abs(high) * 0.5
        elif low not in (None, 0):
            width = abs(low) * 0.5

        if high is not None and value > high:
            dev = (value - high) / width if width else 1.0
            grade = "mild_high" if dev < 0.25 else ("moderate_high" if dev < 0.75 else "severe_high")
            return True, "high", grade, "Above reference range", None
        if low is not None and value < low:
            dev = (low - value) / width if width else 1.0
            grade = "mild_low" if dev < 0.25 else ("moderate_low" if dev < 0.75 else "severe_low")
            return True, "low", grade, "Below reference range", None
        return False, None, "normal", "Within reference range", None

    @staticmethod
    def _from_band(band):
        grade = band["grade"]
        if grade in ("normal", "protective"):
            return False, None, grade, band.get("label")
        direction = "high" if grade.endswith("_high") else "low"
        return True, direction, grade, band.get("label")

    # ---------- main ----------

    def build(self, observations, context, warnings=None):
        patient = StandardizedPatient(context=context)
        patient.extraction_warnings = list(warnings or [])
        sex = context.sex

        candidates = {}
        for obs in observations:
            pid = self.cfg.resolve_alias(obs.raw_name)
            if pid is None:
                patient.unmapped.append(obs)
                continue
            candidates.setdefault(pid, []).append(obs)

        for pid, group in candidates.items():
            pdef = self.cfg.param_by_id[pid]
            built = [self._build_one(pdef, o, sex) for o in group]
            built = [b for b in built if b is not None]
            if not built:
                for o in group:
                    patient.unmapped.append(o)
                continue
            chosen = self._resolve_duplicates(pdef, built, patient)
            patient.parameters[pid] = chosen

        self._compute_derived(patient, sex)
        return patient

    def _build_one(self, pdef, obs, sex):
        kind = pdef["type"]
        np_ = NormalizedParameter(
            parameter_id=pdef["id"], name=pdef["name"], profile=pdef.get("profile"),
            kind=kind, raw=obs)

        if kind == "qualitative":
            status = self._qual_status(obs.raw_value, obs.raw_flag,
                                       pdef.get('numeric_interpretation'))
            if status is None:
                return None
            np_.status = status
            abnormal_when = pdef.get("abnormal_when", "positive")
            np_.abnormal = (status == abnormal_when)
            np_.direction = status
            np_.grade = "positive" if status == "positive" else (
                "indeterminate" if status == "indeterminate" else "normal")
            np_.grade_label = {"positive": "Positive / detected",
                               "negative": "Negative / not detected",
                               "indeterminate": "Equivocal"}[status]
            np_.severity_score = 1.0 if np_.abnormal else (0.3 if status == "indeterminate" else 0.0)
            return np_

        if kind == "categorical":
            text = str(obs.raw_value).strip()
            if not text:
                return None
            np_.category = text.lower()
            np_.grade = "reported"
            np_.grade_label = text
            neg = {v.lower() for v in pdef.get("negative_values", [])}
            np_.abnormal = np_.category in neg if neg else False
            # A categorical state is either present or it is not - there is no partial
            # Rh-negativity. When it is the noteworthy state it carries full weight,
            # the same as a positive qualitative result.
            np_.severity_score = 1.0 if np_.abnormal else 0.0
            return np_

        value, qualifier = parse_numeric(obs.raw_value)
        if value is None:
            # a numeric parameter reported qualitatively, e.g. Urine Protein 'Trace'
            status = self._qual_status(obs.raw_value, obs.raw_flag,
                                       pdef.get('numeric_interpretation'))
            if status is None:
                return None
            np_.kind = "qualitative"
            np_.status = status
            np_.abnormal = status == "positive"
            np_.grade = "positive" if status == "positive" else "normal"
            np_.grade_label = "Reported qualitatively as '%s'" % obs.raw_value
            np_.severity_score = 0.7 if np_.abnormal else 0.0
            np_.notes.append("numeric parameter reported without a number")
            return np_

        value, conv_note = self._convert(pdef, value, obs.raw_unit)
        np_.value = round(value, 6)
        np_.unit = pdef.get("unit")
        np_.conversion_note = conv_note
        if conv_note and "not recognised" in conv_note:
            np_.notes.append(conv_note)

        low, high, src = self._reference(pdef, sex, obs.raw_range)
        np_.reference_low, np_.reference_high, np_.reference_source = low, high, src
        if src == "none":
            np_.notes.append("no reference interval available for this parameter")
        if "sex unknown" in src:
            np_.notes.append("sex was not supplied, so the widest reference interval was used; "
                             "a sex-specific range may change this result")

        abnormal, direction, grade, label, gnote = self._grade(pdef, value, low, high, sex, src)
        np_.abnormal, np_.direction, np_.grade, np_.grade_label = abnormal, direction, grade, label
        np_.severity_score = GRADE_SEVERITY.get(grade, 0.0)
        if gnote:
            np_.notes.append(gnote)

        if qualifier == "less_than":
            np_.notes.append("reported as below the assay's measuring limit")
        elif qualifier == "greater_than":
            np_.notes.append("reported as above the assay's measuring limit")

        # A report flag that disagrees with our grading is worth surfacing, not silently
        # overriding - the lab may be using a different range than the one we resolved.
        flag = (obs.raw_flag or "").strip().lower()
        if flag:
            flagged_abnormal = flag in ("h", "l", "high", "low", "abnormal", "a", "critical", "*")
            if flagged_abnormal and not abnormal:
                np_.notes.append("the report flags this as abnormal ('%s') but it falls inside the "
                                 "reference interval used here" % obs.raw_flag)
            elif not flagged_abnormal and abnormal and flag in ("n", "normal"):
                np_.notes.append("the report flags this as normal but it falls outside the "
                                 "reference interval used here")
        return np_

    def _resolve_duplicates(self, pdef, built, patient):
        """Same parameter reported more than once. Prefer the most informative record."""
        if len(built) == 1:
            return built[0]

        def rank(b):
            return (
                1 if b.reference_source == "report" else 0,
                1 if b.value is not None or b.status is not None else 0,
                1 if b.raw and b.raw.raw_unit else 0,
                b.severity_score,
            )

        ordered = sorted(built, key=rank, reverse=True)
        chosen, dropped = ordered[0], ordered[1:]

        values = {b.value for b in built if b.value is not None}
        conflict = len(values) > 1
        statuses = {b.status for b in built if b.status is not None}
        conflict = conflict or len(statuses) > 1

        patient.duplicates_resolved.append({
            "parameter_id": pdef["id"],
            "parameter": pdef["name"],
            "occurrences": len(built),
            "kept": _describe(chosen),
            "dropped": [_describe(b) for b in dropped],
            "conflicting_values": conflict,
            "reason": ("kept the record with a report-supplied reference range and units"
                       if chosen.reference_source == "report"
                       else "kept the first fully-parsed record"),
        })
        if conflict:
            chosen.notes.append("this parameter appeared %d times with differing values; "
                                "the most complete record was used" % len(built))
        return chosen

    # ---------- derived ----------

    def _compute_derived(self, patient, sex):
        """Fill in ratios the engine can compute from what is already present.

        Only ever computed from measured inputs. Never chained off another derived
        value, so a single bad input cannot cascade.
        """
        for pdef in self.cfg.parameters:
            spec = pdef.get("derived_from")
            if not spec or pdef["id"] in patient.parameters:
                continue
            inputs = spec["inputs"]
            vals = {}
            ok = True
            for src in inputs:
                p = patient.parameters.get(src)
                if p is None or p.value is None or p.derived:
                    ok = False
                    break
                vals[src] = p.value
            if not ok:
                continue
            try:
                value = _eval_formula(spec["formula"], vals)
            except (ZeroDivisionError, ValueError, KeyError, SyntaxError):
                continue
            if value is None:
                continue

            np_ = NormalizedParameter(
                parameter_id=pdef["id"], name=pdef["name"], profile=pdef.get("profile"),
                kind="numeric", value=round(value, 6), unit=pdef.get("unit"),
                derived=True,
                derivation="computed as %s from %s" % (
                    spec["formula"], ", ".join(self.cfg.param_by_id[i]["name"] for i in inputs)))
            low, high, src = self._reference(pdef, sex, None)
            np_.reference_low, np_.reference_high, np_.reference_source = low, high, src
            abnormal, direction, grade, label, gnote = self._grade(pdef, value, low, high, sex, src)
            np_.abnormal, np_.direction, np_.grade, np_.grade_label = abnormal, direction, grade, label
            np_.severity_score = GRADE_SEVERITY.get(grade, 0.0)
            if gnote:
                np_.notes.append(gnote)
            patient.parameters[pdef["id"]] = np_


_ALLOWED_FORMULA = re.compile(r"^[a-z0-9_ ().*/+\-]+$")


def _eval_formula(formula, values):
    """Evaluate a whitelisted arithmetic formula over named inputs."""
    if not _ALLOWED_FORMULA.match(formula):
        raise ValueError("formula contains unsupported characters: %r" % formula)
    return eval(formula, {"__builtins__": {}}, dict(values))   # noqa: S307 - inputs are config-controlled


def _describe(b):
    raw = b.raw
    return {
        "value": b.value if b.value is not None else b.status,
        "unit": (raw.raw_unit if raw else None),
        "range": (raw.raw_range if raw else None),
        "source": (raw.source_path if raw else None),
    }


def _fmt(v):
    return ("%g" % v) if isinstance(v, float) else str(v)
