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
from .layout import GENERIC_RESULT_LABEL
from .models import NormalizedParameter, StandardizedPatient

GRADE_SEVERITY = {
    "normal": 0.0, "protective": 0.0, "unknown": 0.0,
    "mild_low": 0.25, "mild_high": 0.25,
    "moderate_low": 0.55, "moderate_high": 0.55,
    "severe_low": 0.8, "severe_high": 0.8,
    "critical_low": 1.0, "critical_high": 1.0,
}

_COUNT_RANGE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[-\u2013]\s*(\d+(?:\.\d+)?)\s*$")

# A result cell saying the result does not exist yet. The whole cell must say so: a
# comment that merely mentions a pending test is not itself a pending result.
_PENDING_VALUE = re.compile(
    r"^\s*(?:(?:test\s+)?results?\s+|reports?\s+)?"
    r"(?:pending|awaited|to\s+follow|will\s+follow|in\s+process|under\s+process|"
    r"not\s+(?:yet\s+)?reported)\s*\.?\s*$", re.I)
_DATE_OR_TIME = re.compile(r"^\s*(?:\d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4}|\d{1,2}:\d{2}(?::\d{2})?)"
                           r"(?:\s*(?:hrs?|am|pm|\d{1,2}:\d{2}(?::\d{2})?(?:\s*(?:hrs?|am|pm))?))?\s*$", re.I)
_GARBLED_NUMBER = re.compile(r"^[-+]?\d[\d.,]*(?:(?![xX]\s*10)[A-Za-z]+\d|\.\.)")
_FLOAT = re.compile(r"[-+]?\d+(?:\.\d+)?[eE][-+]?\d+")
_DECIMAL_COMMA = re.compile(r"[-+]?\d+,\d{1,2}")
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
    # A date or a clock time in a result cell is not a result: "20/08/2025" read as 20,
    # "2025-08-20" as 2025, "12:30" as 12.
    if _DATE_OR_TIME.match(s):
        return None, None
    qual = None
    if s.startswith(("<", "≤")):
        qual = "less_than"
    elif s.startswith((">", "≥")):
        qual = "greater_than"
    token = s.lstrip("<>≤≥=~ ").split()[0] if s.lstrip("<>≤≥=~ ") else ""
    # A garbled number is not a smaller number: "1O5" (letter O) was read as 1 and graded
    # severe hypoglycaemia, "14..5" as 14. Scientific notation ("1e9") is a number.
    if _GARBLED_NUMBER.match(token) and not _FLOAT.fullmatch(token):
        return None, qual
    # "13,5": a comma followed by one or two final digits is a decimal comma - no
    # thousands grouping (Western "13,500" or Indian "2,50,000") ends in fewer than three.
    if _DECIMAL_COMMA.fullmatch(token):
        s = s.replace(token, token.replace(",", "."), 1)
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
            # 'reactive (1:8)', 'positive for IgM', 'growth of E. coli', and crucially
            # 'Non Reactive, 0.26'.
            #
            # LONGEST MATCH WINS, across both vocabularies together. Scanning positives
            # first meant 'reactive' matched inside 'non reactive' and a negative HBsAg
            # was read as POSITIVE - the phrase that negates a word is always longer and
            # more specific than the word it negates, so length is the right tiebreak
            # and it keeps working as vocabulary is added.
            best, best_status = None, None
            for status, vocab in (("positive", self.cfg.qual_positive),
                                  ("negative", self.cfg.qual_negative),
                                  ("indeterminate", self.cfg.qual_indeterminate)):
                for word in vocab:
                    if not word or len(word) <= len(best or ""):
                        continue
                    if re.search(r"\b%s\b" % re.escape(word), k):
                        best, best_status = word, status
            if best_status:
                return best_status
            # A numeric result on a qualitative test.
            num, qualifier = parse_numeric(candidate)
            if num is not None:
                return self._numeric_qual_status(s, num, qualifier, interpretation)
        return None

    @staticmethod
    def _numeric_qual_status(raw, num, qualifier, spec):
        """Read a number reported against a qualitative test.

        Notation is decided first, because notation is a fact about how the result was
        written rather than a clinical threshold:

          '3+'    dipstick grading    -> positive
          '1:80'  titre               -> positive; a titre is only quoted when the sample
                                         is reactive at that dilution
          0       explicit zero       -> negative

        A bare number with no declared convention is NOT interpretable. Calling it
        positive fabricates a finding out of an unreadable value - that is how HBsAg
        0.80 COI came to report Hepatitis B. Calling it negative is just as bad now that
        an explicit negative can veto a condition outright. So it is reported as
        equivocal, which fires neither a positive trigger nor a negative veto.

        To read such a result properly, give the parameter a `numeric_interpretation`
        block in config/parameters.json with that assay's cut-off.
        """
        if spec:
            return Normalizer._by_declared_cutoff(num, qualifier, spec)

        if re.match(r"^\s*\d+\s*\+", raw):           # 1+, 2+, 3+, 4+
            return "positive"
        if re.search(r"\d\s*:\s*\d", raw):           # 1:8, 1:160
            return "positive"
        if num == 0:
            return "negative"
        return "indeterminate"

    @staticmethod
    def _by_declared_cutoff(num, qualifier, spec):
        """Apply the parameter's own declared numeric convention."""
        neg = spec.get("negative_below")
        pos = spec.get("positive_at_or_above")

        # '<0.90' means the result did not reach 0.90, so it satisfies 'below 0.90'.
        # '>8.0' means it exceeded 8.0, so it satisfies 'at or above'.
        if qualifier == "less_than" and neg is not None and num <= neg:
            return "negative"
        if qualifier == "greater_than" and pos is not None and num >= pos:
            return "positive"

        if neg is not None and num < neg:
            return "negative"
        if pos is not None and num >= pos:
            return "positive"
        if neg is not None and pos is not None:
            return "indeterminate"      # the assay's declared grey zone
        if pos is not None:
            return "negative"
        return "indeterminate"

    # ---------- plausibility ----------

    # A value this many times the top of the reference interval is almost always a unit
    # mix-up rather than a real result. It is only WARNED about, never dropped: genuinely
    # extreme results do occur (ferritin in the thousands, WBC in the hundreds of
    # thousands), and silently discarding one would be far worse than flagging it.
    IMPLAUSIBLE_MULTIPLE = 50

    def _implausible(self, pdef, value, sex):
        """Return a reason string when the value cannot be a real result, else None."""
        low, high, _ = self._reference(pdef, sex, None)

        # Negative concentrations, counts and percentages are impossible by physics,
        # not by clinical convention. Only applied where the configured interval itself
        # starts at or above zero, so genuinely signed quantities are untouched.
        if value < 0 and low is not None and low >= 0:
            return ("a negative value is not possible for this measurement; the result "
                    "was not used")

        unit = (pdef.get("unit") or "").strip()
        if unit == "%" and value > 100:
            return "a percentage above 100 is not possible; the result was not used"
        return None

    def _magnitude_warning(self, pdef, value, sex, raw_unit):
        low, high, _ = self._reference(pdef, sex, None)
        if not high or high <= 0 or value <= high * self.IMPLAUSIBLE_MULTIPLE:
            return None
        return ("%s is more than %dx the top of its reference range - check the unit on "
                "the report (reported as '%s')"
                % (pdef["name"], self.IMPLAUSIBLE_MULTIPLE, raw_unit or "no unit given"))

    # ---------- units ----------

    def _unit_suspect(self, pdef, sex, raw_number, converted, obs):
        """A reason when the printed unit cannot be what the numbers are in, else None.

        "TSH 2.10 mIU/mL, 0.40 - 4.00" is a misprint of mIU/L: converting it faithfully
        made a TSH of 2100 uIU/mL, fired hypothyroid patterns and called the report's own
        interval 400 - 4000. With no unit at all, a platelet count of 150 against a printed
        "150 - 450" was taken as 150 /uL - thrombocytopenia. The test is scale, judged
        from the laboratory's own numbers against the configured interval with the same
        50x margin the magnitude warning uses; no clinical threshold is involved. The
        result is then compared only with the interval printed beside it, in its own unit.
        """
        if pdef.get("type") != "numeric":
            return None
        _, dhigh, _ = self._reference(pdef, sex, None)
        if not dhigh or dhigh <= 0:
            return None
        m = self.IMPLAUSIBLE_MULTIPLE

        def off_scale(x):
            return x is not None and x > 0 and (x > dhigh * m or x < dhigh / m)

        _, rhigh = parse_reference_range(obs.raw_range) if obs.raw_range else (None, None)
        if obs.raw_unit and converted != raw_number:
            if rhigh:
                converted_top = self._convert(pdef, rhigh, obs.raw_unit)[0]
                suspect = off_scale(converted_top) and not off_scale(rhigh)
            else:
                suspect = converted > dhigh * m and not off_scale(raw_number)
            if suspect:
                return ("the unit printed ('%s') would make this %.4g %s, which does not fit "
                        "the numbers on the report - the unit is probably misprinted, so the "
                        "result is compared only with the report's own interval"
                        % (obs.raw_unit, converted, pdef.get("unit")))
        elif not obs.raw_unit and rhigh and off_scale(rhigh):
            return ("no unit is printed and the report's interval is on a different scale "
                    "from %s in %s, so the result is compared only with the report's own "
                    "interval" % (pdef["name"], pdef.get("unit")))
        return None

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
            return value, ("unit '%s' is not recognised for %s, so the result is not "
                           "compared with thresholds written in %s" % (
                               raw_unit, pdef["name"], canonical))
        # An affine conversion - {"scale", "offset"} - for the few units that are not a
        # simple multiple, such as HbA1c in IFCC mmol/mol.
        if isinstance(factor, dict):
            converted = value * factor["scale"] + factor.get("offset", 0.0)
            return converted, "converted %s %s to %.4g %s (%s)" % (
                _fmt(value), raw_unit, converted, canonical,
                factor.get("source", "configured conversion"))
        if factor == 1:
            return value, None
        return value * factor, "converted %s %s to %.4g %s" % (
            _fmt(value), raw_unit, value * factor, canonical)

    def _unit_known(self, pdef, raw_unit):
        """True if the reported unit is absent, canonical, or convertible."""
        u = norm_unit(raw_unit)
        if u is None or u == norm_unit(pdef.get("unit")):
            return True
        return any(norm_unit(k) == u for k in (pdef.get("units") or {}))

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

    @staticmethod
    def _not_interpreted_for_sex(pdef, sex, np_):
        """A quantity whose configured interval is a criterion for one sex only - the
        LH/FSH ratio's 2.0 is the PCOS criterion - is shown for everyone but graded only
        for that sex. A man's ratio of 2.46 was being listed as an abnormal result."""
        only = pdef.get("interpret_for_sex")
        if not only or sex == only:
            return False
        np_.reference_low = np_.reference_high = None
        np_.reference_source = "none"
        np_.abnormal, np_.direction, np_.grade = False, None, "unknown"
        np_.grade_label = "Not interpreted - the configured interval applies to %s patients" % only
        np_.graded_by = "none"
        np_.severity_score = 0.0
        np_.notes.append("%s%s" % (
            "shown as reported; " if not np_.derived else "calculated; ",
            pdef.get("interpret_for_sex_basis") or ("interpreted only for %s patients" % only)))
        return True

    def _bands_for(self, pdef, sex):
        if "bands_by_sex" in pdef and sex in pdef["bands_by_sex"]:
            return pdef["bands_by_sex"][sex]
        if "bands_by_sex" in pdef and not sex:
            return None      # cannot pick a sex-specific band set without sex
        return pdef.get("bands")

    @staticmethod
    def _match_band(bands, value):
        for band in bands or []:
            if ("lt" in band and value < band["lt"]) or ("gte" in band and value >= band["gte"]):
                return band
        return None

    def _grade(self, pdef, value, low, high, sex, ref_source):
        """Return (abnormal, direction, grade, label, note, basis).

        `basis` names WHAT decided the verdict - "range" (the interval that applied),
        "decision_band" (a configured guideline band overruled a report interval that
        would have called this normal), or "none". The UI has to tell those apart:
        saying "the laboratory's own reference interval says this is out of range" is
        false when the lab's interval said nothing of the kind.

        Where a parameter has clinical decision bands (ADA glucose thresholds, KDIGO
        eGFR stages, NCEP lipid bands) those win, because they are absolute standards
        rather than assay-specific intervals - 'HbA1c 7.4% = diabetes range' is more
        useful than 'above reference range'. The reference interval still governs
        parameters that have no bands, and a disagreement between the two is reported
        rather than hidden.
        """
        if value is None:
            return False, None, "unknown", None, None, "none"

        bands = self._bands_for(pdef, sex)

        # Who decides WHETHER a value is abnormal, and who decides HOW abnormal, are two
        # different questions.
        #
        # A band set only outranks the laboratory's own interval on the first question
        # when it is a real clinical decision threshold - 'HbA1c >= 6.5% is the diabetes
        # range' holds whatever a lab prints. Bands with no cited guideline are an
        # encoded reference interval, and the lab that ran the assay knows its own
        # population better; overriding it flagged a WBC of 12,000 as abnormal against a
        # lab range of 4,000-15,000.
        #
        # But those same bands still carry real gradation (deficient / borderline /
        # normal), so once the lab's range says a value IS abnormal the band is the
        # better guide to severity than a deviation ratio. Dropping it outright graded a
        # B12 of 143 as "mild".
        defer_to_report = (bands and ref_source == "report"
                           and not pdef.get("bands_are_decision_thresholds"))
        if defer_to_report:
            outside = ((high is not None and value > high)
                       or (low is not None and value < low))
            if not outside:
                bands = None                      # the lab calls it normal; it is normal
            else:
                # Abnormal by the lab's range. Keep the band's severity, but only if the
                # band agrees it is abnormal - otherwise fall through to the deviation
                # grading so the label can never contradict the flag.
                match = self._match_band(bands, value)
                if match is None or not self._from_band(match)[0]:
                    bands = None

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
                        if value == high or value == low:
                            # "<5.7: Non-diabetes" with a result of 5.7: a value AT the
                            # printed limit is not one the report calls normal.
                            note = ("this value is exactly at the limit of the interval printed "
                                    "on the report; it is graded here against the standard "
                                    "clinical decision band")
                        else:
                            note = ("the reference interval on the report would call this normal; "
                                    "it is graded here against the standard clinical decision band")
                    basis = "decision_band" if (abnormal and not range_abnormal) else "range"
                    return abnormal, direction, grade, label, note, basis

        if low is None and high is None:
            return False, None, "unknown", None, None, "none"

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
            return True, "high", grade, "Above reference range", None, "range"
        if low is not None and value < low:
            dev = (low - value) / width if width else 1.0
            grade = "mild_low" if dev < 0.25 else ("moderate_low" if dev < 0.75 else "severe_low")
            return True, "low", grade, "Below reference range", None, "range"
        return False, None, "normal", "Within reference range", None, "range"

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
        patient.substitutes = {
            p["stands_in_for"]["parameter"]: p["id"]
            for p in self.cfg.parameters if p.get("stands_in_for")}
        patient.extraction_warnings = list(warnings or [])
        self._data_errors = []          # values rejected as impossible, surfaced below
        sex = context.sex

        candidates = {}
        for obs in observations:
            if obs.shape == "metadata":     # a date or identifier field: never a result
                patient.unmapped.append(obs)
                continue
            pid = self._resolve(obs)
            # "RESULT PENDING" is not a value. Unhandled, it vanished without trace when a
            # later page carried the final result, was listed as an unrecognised test when
            # it did not, and a categorical test would have stored "pending" as its answer.
            if isinstance(obs.raw_value, str) and _PENDING_VALUE.match(obs.raw_value):
                patient.pending_results.append({
                    "parameter_id": pid, "name": obs.raw_name,
                    "reported": obs.raw_value.strip(), "source": obs.source_path})
                continue
            if pid is None:
                patient.unmapped.append(obs)
                continue
            candidates.setdefault(pid, []).append(obs)

        for pid, group in candidates.items():
            pdef = self.cfg.param_by_id[pid]
            built = []
            for o in group:
                errors_before = len(self._data_errors)
                b = self._build_one(pdef, o, sex)
                if b is not None:
                    built.append(b)
                elif len(self._data_errors) == errors_before and o.shape == "result":
                    # A recognised test whose result cannot be read ("Sample hemolysed",
                    # "1O5", "--") is said so. It used to vanish silently whenever another
                    # record of the same parameter existed - an unreadable HIV-2 result
                    # disappeared behind a readable HIV-1 one.
                    self._data_errors.append({
                        "parameter": pdef["name"], "reported": o.raw_value, "unit": o.raw_unit,
                        "reason": "the reported result could not be read as a value for this "
                                  "test; it was not used"})
            if not built:
                for o in group:
                    patient.unmapped.append(o)
                continue
            chosen = self._resolve_duplicates(pdef, built, patient)
            patient.parameters[pid] = chosen

        self._compute_derived(patient, sex)

        # Missing is not normal: a test the laboratory has not reported yet is said so.
        for pend in patient.pending_results:
            used = patient.parameters.get(pend["parameter_id"]) if pend["parameter_id"] else None
            patient.extraction_warnings.append(
                "%s is printed as '%s' - " % (pend["name"], pend["reported"])
                + ("a result for %s reported elsewhere in this file was used." % used.name
                   if used is not None else
                   "no result for it was analysed, and it is not treated as normal."))

        # Values rejected as impossible are reported, never silently dropped - the user
        # needs to know a result on their report was not used, and why.
        for err in self._data_errors:
            patient.extraction_warnings.append(
                "%s was reported as '%s'%s - %s"
                % (err["parameter"], err["reported"],
                   (" " + err["unit"]) if err["unit"] else "", err["reason"]))
        patient.rejected_values = list(self._data_errors)
        return patient

    # ---------- name resolution ----------

    _URINE_SECTION = re.compile(r"\burin|\burinalysis\b|\bR/?M\b", re.I)

    def _unit_accepts(self, pdef, raw_unit):
        u = norm_unit(raw_unit)
        if u is None or u == norm_unit(pdef.get("unit")):
            return True
        return any(norm_unit(k) == u for k in (pdef.get("units") or {}))

    def _resolve(self, obs):
        """Name -> parameter, using what else the report says about the result.

        The name alone is not always enough, and the wrong answer is worse than none:
          - "Blood", "Colour", "Epithelial Cells" under a urine heading are urine tests
          - "Neutrophils." reported in 10^3/uL is the ABSOLUTE count, not the percentage
          - "PCT" in % is plateletcrit, not procalcitonin - whose sepsis rules fire at 0.5
          - "ESR - Erythrocyte Sedimentation Rate" names one test twice
        """
        resolve = self.cfg.resolve_alias
        name = obs.raw_name
        pid = None

        section = obs.section or ""
        if self._URINE_SECTION.search(section) and not re.search(r"\burine\b", name, re.I):
            cand = resolve("urine " + name)
            if cand and self.cfg.param_by_id[cand].get("profile") == "Urinalysis":
                pid = cand

        if pid is None:
            pid = resolve(name)

        if pid is None and re.search(r"\s[-\u2013]\s", name):
            sides = [resolve(s) for s in re.split(r"\s[-\u2013]\s", name, maxsplit=1)]
            if sides[0] and sides[0] == sides[1]:
                pid = sides[0]

        # "Remark" under the heading "RBC Morphology": the label names no test, the heading
        # does. The PDF row parser applies this rule to the heading directly above the row;
        # the innermost section is the same heading in a JSON or CSV export. Only a
        # descriptive test qualifies, as in the PDF rule - a number under a "Findings"
        # heading is not thereby that heading's result.
        if pid is None and section and GENERIC_RESULT_LABEL.fullmatch(name.strip(" :")):
            heading = section.split(">")[-1].strip()
            cand = resolve(heading) if heading else None
            if cand and self.cfg.param_by_id[cand].get("type") == "categorical":
                pid = cand

        if pid is None:
            return None

        pdef = self.cfg.param_by_id[pid]
        if obs.raw_unit and not self._unit_accepts(pdef, obs.raw_unit):
            alt = self._unit_alternative(pdef, obs)
            if alt:
                pid = alt
        elif not obs.raw_unit:
            alt = self._section_alternative(pdef, obs)
            if alt:
                pid = alt
        return pid

    def _alternatives(self, pdef, obs):
        """Parameters that may stand for this name instead: the same biological group
        (percentage vs absolute count), or a declared alternate reading of the name."""
        key = norm_key(obs.raw_name.split("(")[0])
        full = norm_key(obs.raw_name)
        out = []
        group = pdef.get("redundancy_group")
        for p in self.cfg.parameters:
            if p["id"] == pdef["id"]:
                continue
            alts = {norm_key(a) for a in p.get("alternate_aliases", [])}
            if (group and p.get("redundancy_group") == group) or key in alts or full in alts:
                out.append(p)
        return out

    def _unit_alternative(self, pdef, obs):
        fits = [p for p in self._alternatives(pdef, obs) if self._unit_accepts(p, obs.raw_unit)
                and norm_unit(obs.raw_unit) is not None
                and (norm_unit(obs.raw_unit) == norm_unit(p.get("unit"))
                     or any(norm_unit(k) == norm_unit(obs.raw_unit) for k in (p.get("units") or {})))]
        return fits[0]["id"] if len(fits) == 1 else None

    def _section_alternative(self, pdef, obs):
        """With no unit to go on, a declared alternate whose profile matches the heading."""
        section = (obs.section or "").lower()
        if not section:
            return None
        fits = [p for p in self._alternatives(pdef, obs)
                if norm_key(obs.raw_name) in {norm_key(a) for a in p.get("alternate_aliases", [])}
                and any(w in section for w in norm_key(p.get("profile") or "").split()
                        + [w for w in norm_key(p["name"]).split() if len(w) > 4])]
        return fits[0]["id"] if len(fits) == 1 else None

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

        count_range = _COUNT_RANGE.match(str(obs.raw_value or ""))
        if count_range and float(count_range.group(1)) <= float(count_range.group(2)):
            # "1-2 /hpf": a microscopy count given as a range. The UPPER bound is what is
            # compared with the reference - using the lower one would call "5-10 /hpf"
            # normal against "0 - 5".
            value, qualifier = float(count_range.group(2)), None
            np_.notes.append("reported as a range (%s); the upper value is compared with the "
                             "reference interval" % str(obs.raw_value).strip())
        else:
            value, qualifier = parse_numeric(obs.raw_value)
        if value is None:
            # A bare dash is "nil" on a urine dipstick but only a placeholder anywhere
            # else: a sodium of "--" was stored as a NEGATIVE sodium.
            if (re.fullmatch(r"\s*[-–—]+\s*", str(obs.raw_value or ""))
                    and pdef.get("profile") != "Urinalysis"):
                return None
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

        unit_known = self._unit_known(pdef, obs.raw_unit)
        raw_number = value
        value, conv_note = self._convert(pdef, value, obs.raw_unit)
        unit_suspect = self._unit_suspect(pdef, sex, raw_number, value, obs) if unit_known else None
        if unit_suspect:
            unit_known, value, conv_note = False, raw_number, unit_suspect

        # Reject results that are impossible rather than merely extreme. A haemoglobin
        # of -5 is a typo or a parse error, not a critical finding, and reporting it as
        # "critical low" turns a data fault into a clinical alarm. Both checks below are
        # derived from existing configuration - a reference interval that starts at or
        # above zero, and the parameter's own unit - so neither invents a threshold.
        impossible = self._implausible(pdef, value, sex) if unit_known else (
            "a negative value is not possible for this measurement" if value < 0 else None)
        if impossible:
            self._data_errors.append({
                "parameter": pdef["name"], "reported": obs.raw_value,
                "unit": obs.raw_unit, "reason": impossible})
            return None

        mag = self._magnitude_warning(pdef, value, sex, obs.raw_unit) if unit_known else None
        if mag:
            np_.notes.append(mag)

        np_.value = round(value, 6)
        np_.unit = pdef.get("unit") if unit_known else obs.raw_unit
        np_.interpretable = unit_known
        np_.conversion_note = conv_note
        if conv_note and ("not recognised" in conv_note or unit_suspect):
            np_.notes.append(conv_note)

        if self._not_interpreted_for_sex(pdef, sex, np_):
            return np_
        low, high, src = self._reference(pdef, sex, obs.raw_range)
        if not unit_known and src != "report":
            # The only interval that shares this unit is one the report itself printed.
            # Without it there is nothing honest to grade against.
            np_.reference_low = np_.reference_high = None
            np_.reference_source = "none"
            np_.abnormal, np_.grade = False, "unknown"
            np_.grade_label = "Not interpreted - unit not recognised"
            np_.graded_by = "none"
            return np_

        # A range printed on the report is quoted in the REPORT's unit, so it needs the
        # same conversion the value just had. Without this a platelet count of 233
        # 10^3/uL became 233,000 /uL and was then compared against the report's own
        # "140 - 440", flagging a perfectly normal count as high.
        if src == "report" and obs.raw_unit and not unit_suspect:
            if low is not None:
                low = self._convert(pdef, low, obs.raw_unit)[0]
            if high is not None:
                high = self._convert(pdef, high, obs.raw_unit)[0]

        np_.reference_low, np_.reference_high, np_.reference_source = low, high, src
        if src == "none":
            np_.notes.append("no reference interval available for this parameter")
        if "sex unknown" in src:
            np_.notes.append("sex was not supplied, so the widest reference interval was used; "
                             "a sex-specific range may change this result")

        # Guideline bands are written in the canonical unit; with an unconvertible unit
        # only the report's own interval (same unit) may grade the value.
        grade_def = pdef if unit_known else {k: v for k, v in pdef.items()
                                             if k not in ("bands", "bands_by_sex")}
        abnormal, direction, grade, label, gnote, gbasis = self._grade(
            grade_def, value, low, high, sex, src)
        np_.abnormal, np_.direction, np_.grade, np_.grade_label = abnormal, direction, grade, label
        np_.graded_by = gbasis
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
            # Completeness only. Severity deliberately does NOT rank: when two records
            # disagree on the value, the engine has no way to know which is true, and
            # preferring the more abnormal one would bias every duplicate towards the
            # more alarming reading while the audit line claimed it simply kept the
            # first. Ties fall back to the order the values appeared in the report,
            # which sorted() preserves, and the conflict is reported.
            # A reading from an explicit results table (header found in that table) or a
            # structured JSON field outranks one rebuilt from free text. A report that
            # restates its values on a designed summary page must not have the summary's
            # reading chosen over the laboratory page's just because it came first.
            return (
                1 if b.reference_source == "report" else 0,
                1 if b.value is not None or b.status is not None else 0,
                1 if b.raw and b.raw.origin in ("table", "json") else 0,
                1 if b.raw and b.raw.raw_unit else 0,
                # a two-sided interval says more than a one-sided one ("0.02-0.1" vs
                # "< 0.1" restating the same result on a summary page)
                (b.reference_low is not None) + (b.reference_high is not None),
            )

        ordered = sorted(built, key=rank, reverse=True)
        # Separate tests folded into one screen ("HIV-1 ANTIBODIES" and "HIV-2 ANTIBODIES"
        # -> one HIV screen) are not duplicates of one reading: the screen is positive if
        # any of them is. Keeping the first hid a reactive HIV-2 behind a non-reactive
        # HIV-1. Repeats of the same test name keep the completeness order above.
        names = {re.sub(r"[^a-z0-9]", "", (b.raw.raw_name if b.raw else "").lower()) for b in built}
        if len(names) > 1 and all(b.value is None and b.status is not None for b in built):
            # an equivocal component also keeps the screen from reading negative - and so
            # from vetoing the condition as excluded
            for wanted in (lambda b: b.abnormal, lambda b: b.status == "indeterminate"):
                hit = [b for b in ordered if wanted(b)]
                if hit:
                    ordered = hit[:1] + [b for b in ordered if b is not hit[0]]
                    break
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
            "reason": "kept the reactive/equivocal result: separate tests combine into one screen"
                      if (conflict and (chosen.abnormal or chosen.status == "indeterminate")
                          and len(names) > 1 and chosen.value is None) else
                      ("kept the record with a report-supplied reference range"
                       + (", read from a results table" if chosen.raw and chosen.raw.origin == "table"
                          else "")
                       if chosen.reference_source == "report"
                       else "kept the first fully-parsed record, in the order they appeared"),
        })
        combined = (conflict and (chosen.abnormal or chosen.status == "indeterminate")
                    and len(names) > 1 and chosen.value is None)
        if combined:
            chosen.notes.append(
                "reported as separate tests (%s); at least one is %s, so the combined screen "
                "is shown as %s - please check the original"
                % ("; ".join("%s: %s" % (b.raw.raw_name if b.raw else "?", b.status) for b in built),
                   chosen.status, chosen.status))
        elif conflict:
            # The completeness order above decides which reading is shown, deliberately
            # not severity. But a reading outside its own range must not vanish because a
            # normal one of the same parameter came first ("Haemoglobin 14" and
            # "Haemoglobin 9"): it is kept for the laboratory-marked list.
            for b in dropped:
                if b.abnormal and not chosen.abnormal:
                    patient.conflicting_readings.append({"kept": chosen, "other": b})
            chosen.notes.append(
                "this parameter appeared %d times with DIFFERENT values (%s); the most "
                "complete record was used, but which one is correct cannot be determined "
                "from the report - please check the original"
                % (len(built),
                   ", ".join(str(b.value if b.value is not None else b.status)
                             for b in built)))
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
                if p is None or p.value is None or p.derived or not p.interpretable:
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
            if self._not_interpreted_for_sex(pdef, sex, np_):
                patient.parameters[pdef["id"]] = np_
                continue
            low, high, src = self._reference(pdef, sex, None)
            np_.reference_low, np_.reference_high, np_.reference_source = low, high, src
            abnormal, direction, grade, label, gnote, gbasis = self._grade(
                pdef, value, low, high, sex, src)
            np_.abnormal, np_.direction, np_.grade, np_.grade_label = abnormal, direction, grade, label
            np_.graded_by = gbasis
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
