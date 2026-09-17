"""Stage 1 - Extraction.

Turns an uploaded artefact into a flat list of RawObservation plus whatever patient
context the document states. Nothing here interprets clinical meaning, and nothing
here invents a value: if a field is absent it stays absent.

Supported inputs:
  - JSON  : any nesting. Walks the whole tree and recognises test-shaped objects.
  - PDF   : text and table extraction via pdfplumber.
  - CSV / TSV / TXT : delimited or whitespace-aligned report text.
"""
from __future__ import annotations

from functools import lru_cache

import csv
import io
import json
import re

from .models import RawObservation, PatientContext
from .config import norm_unit
from .layout import (rows_from_words, rows_from_text, drop_repeated, parse_rows, parse_row, Row,
                     BAND_WORDS_RE)

# --- keys a JSON payload might use for each field, in priority order ---
NAME_KEYS = ["test_name", "testname", "test", "name", "parameter", "parameter_name",
             "analyte", "investigation", "label", "title", "display_name", "key",
             "biomarker", "marker", "attribute"]
VALUE_KEYS = ["value", "result", "result_value", "observed_value", "reading",
              "measurement", "val", "test_value", "quantity", "obs_value", "data"]
UNIT_KEYS = ["unit", "units", "uom", "unit_of_measure", "measurement_unit", "result_unit"]
RANGE_KEYS = ["reference_range", "ref_range", "refrange", "normal_range", "biological_ref_interval",
              "bio_ref_interval", "range", "reference", "normal", "ref_interval", "interval",
              "reference_value", "ref"]
LOW_KEYS = ["low", "min", "ref_low", "range_low", "lower", "lower_limit", "min_value", "normal_low"]
HIGH_KEYS = ["high", "max", "ref_high", "range_high", "upper", "upper_limit", "max_value", "normal_high"]
FLAG_KEYS = ["flag", "status", "abnormal", "abnormal_flag", "interpretation", "indicator",
             "result_status", "is_abnormal", "remark"]

CONTEXT_KEYS = {
    "patient_id": ["patient_id", "patientid", "pid", "mrn", "uhid", "id", "reg_no",
                   "registration_no"],
    "name": ["patient_name", "patientname", "pname", "p_name", "ptname", "pt_name",
             "name_of_patient", "patient_full_name", "patient"],
    "sex": ["sex", "gender", "patient_sex", "patient_gender", "psex", "p_sex"],
    "age": ["age", "patient_age", "age_years", "page", "p_age"],
    "report_date": ["report_date", "reported_on", "collection_date", "date", "sample_date",
                    "collected_on", "test_date"],
}

# Context aliases that mean something else on a test record. "page" is here for a LIS
# export's "PAge", but on a result object it is the page the test was printed on - read
# as the patient's age, a JSON listing its results before the demographics block turned
# a 38-year-old into a 1-year-old. "id" on a result object is the test's own id. Both are
# read only from objects that are not tests; "age" and "patient_id" are read anywhere, so
# flat one-row-per-result exports keep their demographics.
AMBIGUOUS_ON_TEST = {"page", "id"}

# A bare "name" key is the patient's name only when the object around it is clearly a
# demographics block. Requiring a sex/age/DOB sibling keeps it from picking up a
# laboratory's name, a doctor's name, or the "name" field of a test object.
DEMOGRAPHIC_MARKERS = {"sex", "gender", "patient_sex", "patient_gender", "psex", "p_sex",
                       "age", "patient_age", "age_years", "page", "p_age",
                       "dob", "date_of_birth", "birth_date"}
BARE_NAME_KEYS = ["name", "full_name", "fullname"]

# Keys whose subtree is usually metadata rather than results. Only skipped when the
# subtree genuinely contains no test-shaped data - a payload that nests its results
# under 'laboratory' must not be thrown away.
SKIP_SUBTREES = {"meta", "_meta", "metadata", "header", "footer", "doctor", "physician",
                 "address", "contact", "signature", "qr", "barcode", "pagination",
                 "audit", "created_by", "updated_by", "lab_info", "laboratory_info",
                 "clinic", "hospital", "branch"}

# A loose field naming a date or time ("troponin_slip_date": "20/08/2025") is never a
# result. Offered to the alias index it matched "troponin" and read the day, 20, as a
# troponin I of 20 ng/mL - an urgent finding from a date.
DATE_KEY = re.compile(r"(?:^|_)(?:date|time|datetime|timestamp|dated|dob)(?:_|$)")
DATE_VALUE = re.compile(r"^\d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?$")

# A loose field whose key also names an identifier is metadata, whatever analyte word
# it contains: "troponin_id": 20, "potassium_phone": 9.1 and "hba1c_barcode": 5.5 were
# offered to the alias index, trimmed to "troponin" / "potassium" / "hba1c" and read as
# an urgent troponin, severe hyperkalaemia and an HbA1c. Only LOOSE fields are screened;
# a test object's own value key is untouched.
IDENTIFIER_KEY = re.compile(
    r"(?:^|_)(?:id|ids|no|nos|num|number|code|codes|barcode|accession|phone|mobile|tel|"
    r"fax|uhid|mrn|serial|seq|sequence|batch|lot|invoice|bill|receipt|order|version|"
    r"year|month|day|pincode|zip|page|email|url|sample|specimen|slip|ref|srno|sr)(?:_|$)")


def _snake(key):
    """'troponinId' / 'Troponin-ID' / 'troponin id' -> 'troponin_id'."""
    k = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    return re.sub(r"[^a-z0-9]+", "_", k.lower()).strip("_")


# Scalar leaves under these keys describe the container, not a result.
STRUCTURAL_KEYS = {"panel_name", "panel", "section", "section_name", "category", "group",
                   "group_name", "profile_name", "department", "type", "kind", "sort_order",
                   "order", "sequence", "method", "specimen", "sample_type", "status_code",
                   "template", "loinc", "code", "test_code", "comments", "comment", "note",
                   "notes", "interpretation_text", "footer_note"}

_NUM = r"[-+]?\d*\.?\d+"
RANGE_PATTERNS = [
    re.compile(r"^\s*(?P<low>%s)\s*(?:-|–|—|to|:)\s*(?P<high>%s)\s*$" % (_NUM, _NUM), re.I),
    re.compile(r"^\s*(?:<|less than|upto|up to|below|max)\s*(?P<high>%s)\s*$" % _NUM, re.I),
    re.compile(r"^\s*(?:>|greater than|above|min|at least)\s*(?P<low>%s)\s*$" % _NUM, re.I),
    re.compile(r"^\s*(?:<=|≤)\s*(?P<high>%s)\s*$" % _NUM, re.I),
    re.compile(r"^\s*(?:>=|≥)\s*(?P<low>%s)\s*$" % _NUM, re.I),
]


# How laboratories write "less than or equal to" and friends. Folded first, so every
# form below sees one spelling: "< or = 0.90", "< /= 30", ">/= 190", "=< 5", "Up to 1.2".
_COMPARATOR_FORMS = [
    (re.compile(r"<\s*or\s*=|<\s*/\s*=|=\s*<|≤", re.I), "<="),
    (re.compile(r">\s*or\s*=|>\s*/\s*=|=\s*>|≥", re.I), ">="),
    (re.compile(r"\bup\s*to\b|\bupto\b", re.I), "<="),
]

# The label a laboratory gives its healthy band. Matched against the WHOLE label, so
# "Near/Above Optimal", "Insufficiency", "Pre-diabetic" and "Borderline High" - which
# contain a healthy word - are not mistaken for it.
_NORMAL_LABEL = re.compile(
    r"^(?:normal(?:\s+or\s+high)?|optimal|optimum(?:\s+level)?|desirable(?:\s*/\s*low\s+risk)?|"
    r"sufficient|sufficiency|non[-\s]?diabet(?:ic|es)(?:\s+adults?)?|healthy|adequate|"
    r"reference(?:\s+range)?|acceptable|low\s+risk)$", re.I)

_RANGE_PIECE = (r"(?:[<>]=?\s*)?[-+]?\d[\d.,]*(?:\s*(?:-|–|—|to)\s*[-+]?\d[\d.,]*)?"
                r"(?:\s*[A-Za-zµμ%/^.\d]{0,12})?")
_LABEL_FIRST = re.compile(r"^\s*(?P<label>[A-Za-z][A-Za-z /()\-]*?)\s*[:=]*\s*(?P<rng>%s)\s*$"
                          % _RANGE_PIECE)
_RANGE_FIRST = re.compile(r"^\s*(?P<rng>%s?)\s*(?:[:\-–]\s*|\s)(?P<label>[A-Za-z][A-Za-z /()\-]*)\s*$"
                          % r"(?:[<>]=?\s*)?[-+]?\d[\d.,]*(?:\s*(?:-|–|—|to)\s*[-+]?\d[\d.,]*)")


def _is_unit_word(label):
    """'uIU/mL', 'mg/dL', 'Ratio' after an interval are its unit, not a band label."""
    lab = label.strip()
    return ((" " not in lab and ("/" in lab or "%" in lab))
            or lab.lower() in ("ratio", "index", "fl", "pg", "sec", "secs", "mm", "u", "iu", "units"))


def _band(line):
    """One printed line of a reference -> (label or None, low, high), or None.

    A word beside the interval that is no band vocabulary is the report's method column
    ("12 - 15.5 Colorimetric") and leaves the interval unlabelled.
    """
    for pat in (_LABEL_FIRST, _RANGE_FIRST):
        m = pat.match(line)
        if m and not _is_unit_word(m.group("label")):
            low, high = _plain_range(m.group("rng"))
            if low is not None or high is not None:
                label = re.sub(r"\s+", " ", m.group("label")).strip(" -/:")
                return (label if BAND_WORDS_RE.search(label) else None), low, high
    low, high = _plain_range(line)
    if low is not None or high is not None:
        return None, low, high
    return None


def parse_reference_range(text):
    """'70 - 99', '< 150', '>= 40', '0.4-4.0 uIU/mL' -> (low, high) floats or Nones.

    A reference printed as labelled bands yields the band the laboratory calls healthy -
    normal / optimal / desirable / sufficient / non-diabetic - whichever side of the
    interval the label is printed on ("Sufficient 30 - 100", "< 100 : Normal", "<200 -
    Desirable"). Where the laboratory calls more than one band acceptable ("< 100 :
    Normal", "100 - 129 : Desirable") the interval spans them. With no such label, a
    single unlabelled interval among labelled sub-population notes ("0.3-4.5" /
    "Pregnant women: First trimester ...") is the interval; anything else yields nothing,
    and the dictionary's interval applies. Taking none of them discarded the laboratory's
    own interval for vitamin D and eGFR; taking the first band read "Low: < 40" as HDL's
    normal range and called an HDL of 52 high.
    """
    if text is None:
        return None, None
    s = str(text).strip()
    if not s:
        return None, None
    for pat, rep in _COMPARATOR_FORMS:
        s = pat.sub(rep, s)

    lines = [ln.strip() for ln in re.split(r"[\n;|]+", s) if ln.strip()]
    bands = [b for b in (_band(ln) for ln in lines) if b is not None]
    labelled = [b for b in bands if b[0]]
    if len(lines) > 1 or labelled:
        normal = [b for b in labelled if _NORMAL_LABEL.match(b[0])]
        if normal:
            lows = [b[1] for b in normal]
            highs = [b[2] for b in normal]
            return (None if None in lows else min(lows)), (None if None in highs else max(highs))
        unlabelled = [b for b in bands if not b[0]]
        if len(lines) > 1 and len(unlabelled) == 1:
            return unlabelled[0][1], unlabelled[0][2]
        return None, None
    # One unlabelled interval, possibly with a method word after it ("12 - 15.5 Colorimetric")
    if bands:
        return bands[0][1], bands[0][2]
    return _plain_range(s)


def _plain_range(s):
    """A single interval with no band label."""
    s = str(s).strip()
    if not s:
        return None, None

    # A titre ('1:8', '1:160') is a dilution, not an interval. Reading it as the range
    # 1 to 8 would silently replace the real reference interval with nonsense.
    if re.search(r"\d\s*:\s*\d", s):
        return None, None

    # Thousands separators are routine on counts ('13,500 - 17,000'). Without this the
    # report's own range failed to parse and was silently discarded in favour of the
    # dictionary's, which is exactly the range the lab meant to override.
    # Indian grouping too ('1,50,000 - 4,10,000'), which the three-digit rule alone left
    # half-parsed, so the report's platelet interval was replaced by the dictionary's.
    s = re.sub(r"(?<=\d),(?=(?:\d{2},)*\d{3}\b)", "", s)

    # drop a trailing unit so '0.4 - 4.0 uIU/mL' still parses as a numeric interval
    s_clean = re.sub(r"\s*(mg|g|ng|pg|ug|µg|mmol|umol|µmol|mcg|iu|miu|uiu|u|meq|fl|pg|cells|million|lakhs?|thou)\s*/?\s*"
                     r"(dl|l|ml|ul|µl|cumm|mm3|hpf|hr|g|m2|min)?\b\.?", " ", s, flags=re.I)
    s_clean = re.sub(r"\s*\b(?:ratio|index)\s*$", " ", s_clean, flags=re.I)
    s_clean = s_clean.replace("%", " ").strip()
    for pat in RANGE_PATTERNS:
        m = pat.match(s_clean)
        if m:
            gd = m.groupdict()
            low = float(gd["low"]) if gd.get("low") is not None else None
            high = float(gd["high"]) if gd.get("high") is not None else None
            return low, high
    return None, None


_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


@lru_cache(maxsize=4096)
def _camel_to_snake(k):
    """'MinValue' -> 'min_value', 'RefRange' -> 'ref_range'.

    Cached: a report re-presents the same few dozen key spellings on every record, and
    this was being recomputed about fifteen thousand times per document.
    """
    return _CAMEL_BOUNDARY.sub("_", k)


def _key_index(d):
    """Index a record's keys under both their plain lowercase and de-camelised forms.

    Without the second, a CamelCase field such as 'MinValue' lowercased to 'minvalue'
    and never matched 'min_value', so the laboratory's own reference ranges were
    silently discarded for every report using that style - Metropolis among them.
    """
    lower = {}
    for k, v in d.items():
        ks = str(k)
        lower.setdefault(ks.lower().replace(" ", "_"), v)
        lower.setdefault(_camel_to_snake(ks).lower().replace(" ", "_"), v)
    return lower


def _first(d, keys, index=None):
    # `index` lets a caller reading eight field groups out of one record build the
    # index once instead of eight times; extraction was spending nearly half its time
    # rebuilding the same map.
    lower = index if index is not None else _key_index(d)
    for k in keys:
        if k in lower and lower[k] not in (None, "", []):
            return lower[k]
    return None


def _looks_like_test(obj):
    """True when a dict carries both something name-like and something value-like."""
    if not isinstance(obj, dict):
        return False
    return _first(obj, NAME_KEYS) is not None and _first(obj, VALUE_KEYS) is not None


def _is_valued_object(obj):
    """A dict that carries a value but no name - it is named by the key pointing at it,
    as in {"systolic_bp": {"value": 146, "unit": "mmHg"}}."""
    return (isinstance(obj, dict) and _first(obj, NAME_KEYS) is None
            and _first(obj, VALUE_KEYS) is not None)


def _has_test_shape(node, depth=0):
    """Does this subtree contain anything result-shaped? Guards the metadata skip list."""
    if depth > 6:
        return False
    if isinstance(node, dict):
        if _looks_like_test(node) or _is_valued_object(node):
            return True
        return any(_has_test_shape(v, depth + 1) for v in node.values())
    if isinstance(node, list):
        return any(_has_test_shape(v, depth + 1) for v in node)
    return False


def _scalar(v):
    return isinstance(v, (str, int, float, bool)) or v is None


def _is_demographics_block(d):
    """Does this object describe the patient rather than a test or an organisation?"""
    if not isinstance(d, dict) or _looks_like_test(d):
        return False
    keys = {str(k).lower().replace(" ", "_") for k in d}
    return bool(keys & DEMOGRAPHIC_MARKERS)


# ------------------------- JSON -------------------------

def extract_json(payload, source_name="input.json"):
    """Recursively pull observations and context out of arbitrarily nested JSON."""
    obs, ctx_found, warnings = [], {}, []

    def note_context(d):
        on_test = _looks_like_test(d)
        for field, keys in CONTEXT_KEYS.items():
            if field in ctx_found:
                continue
            if on_test:
                keys = [k for k in keys if k not in AMBIGUOUS_ON_TEST]
            val = _first(d, keys)
            if val is not None and _scalar(val):
                ctx_found[field] = val
        if "name" not in ctx_found and _is_demographics_block(d):
            val = _first(d, BARE_NAME_KEYS)
            if val is not None and _scalar(val):
                ctx_found["name"] = val

    def emit(name, value, unit, rng, flag, path, shape="result", section=None, method=None):
        # A loose scalar that nevertheless carries a unit or a reference range is a
        # result however it was nested, so the shape is upgraded on that evidence
        # rather than on where it happened to sit.
        if shape == "field" and (unit is not None or rng is not None):
            shape = "result"
        obs.append(RawObservation(
            raw_name=str(name).strip(), raw_value=value,
            raw_unit=str(unit).strip() if unit is not None else None,
            raw_range=str(rng).strip() if rng is not None else None,
            raw_flag=str(flag).strip() if flag is not None else None,
            source_path=path, source_kind="json", shape=shape, origin="json",
            section=str(section).strip() if section else None,
            method=str(method).strip() if method else None))

    def from_test_object(d, path):
        idx = _key_index(d)
        name = _first(d, NAME_KEYS, idx)
        value = _first(d, VALUE_KEYS, idx)
        unit = _first(d, UNIT_KEYS, idx)
        rng = _first(d, RANGE_KEYS, idx)
        flag = _first(d, FLAG_KEYS, idx)
        if rng is None:
            low, high = _first(d, LOW_KEYS, idx), _first(d, HIGH_KEYS, idx)
            if low is not None or high is not None:
                if low is not None and high is not None:
                    rng = "%s - %s" % (low, high)
                elif high is not None:
                    rng = "< %s" % high
                else:
                    rng = "> %s" % low
        if isinstance(rng, dict):
            low, high = _first(rng, LOW_KEYS), _first(rng, HIGH_KEYS)
            rng = ("%s - %s" % (low, high)) if (low is not None and high is not None) else None
        if not _scalar(value):
            warnings.append("non-scalar value for '%s' at %s - skipped" % (name, path))
            return
        emit(name, value, unit, rng, flag, path,
             section=_first(d, ["section", "panel", "panel_name", "category", "group",
                                "department", "profile", "test_group"], idx),
             method=_first(d, ["method", "methodology"], idx))

    context_key_names = {x for ks in CONTEXT_KEYS.values() for x in ks}
    # Keys that name a FIELD rather than a test. They appear as loose scalars when a
    # sibling value was null, and must not become observations of their own.
    field_key_names = set(NAME_KEYS + VALUE_KEYS + UNIT_KEYS + RANGE_KEYS
                          + LOW_KEYS + HIGH_KEYS + FLAG_KEYS)

    def walk(node, path):
        if isinstance(node, dict):
            note_context(node)
            if _looks_like_test(node):
                from_test_object(node, path)
                # A test object may still nest sub-tests, e.g. a differential count
                # hanging off the CBC entry.
                for k, v in node.items():
                    if isinstance(v, (dict, list)) and str(k).lower() not in SKIP_SUBTREES:
                        walk(v, "%s.%s" % (path, k))
                return

            for k, v in node.items():
                kl = str(k).lower().replace(" ", "_")
                child = "%s.%s" % (path, k)

                if isinstance(v, dict):
                    if kl in SKIP_SUBTREES and not _has_test_shape(v):
                        continue
                    if _is_valued_object(v):
                        # named by the key that points at it
                        merged = dict(v)
                        merged["test_name"] = k
                        from_test_object(merged, child)
                        continue
                    walk(v, child)
                elif isinstance(v, list):
                    if kl in SKIP_SUBTREES and not _has_test_shape(v):
                        continue
                    walk(v, child)
                elif _scalar(v) and v is not None and str(v).strip() != "":
                    # A leaf scalar under a plain key is a candidate 'name: value' pair.
                    # Whether it is a real parameter is decided later by the alias index;
                    # extraction only offers it.
                    if (kl in context_key_names or kl in STRUCTURAL_KEYS
                            or kl in field_key_names or kl.startswith("_")):
                        continue
                    if (DATE_KEY.search(_snake(k)) or IDENTIFIER_KEY.search(_snake(k))
                            or DATE_VALUE.match(str(v).strip())):
                        # listed as a document field, never offered as a result
                        emit(k, v, None, None, None, child, shape="metadata")
                        continue
                    emit(k, v, None, None, None, child, shape="field")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, "%s[%d]" % (path, i))

    walk(payload, "$")

    ctx = PatientContext(
        patient_id=_str_or_none(ctx_found.get("patient_id")),
        name=_str_or_none(ctx_found.get("name")),
        sex=_norm_sex(ctx_found.get("sex")),
        age=_norm_age(ctx_found.get("age")),
        report_date=_str_or_none(ctx_found.get("report_date")),
        source_file=source_name,
    )
    return obs, ctx, warnings


def _str_or_none(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _norm_sex(v):
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("m", "male", "man", "boy", "1"):
        return "male"
    if s in ("f", "female", "woman", "girl", "2"):
        return "female"
    return None


def _norm_age(v):
    if v is None:
        return None
    m = re.search(r"\d+(?:\.\d+)?", str(v))
    if not m:
        return None
    age = float(m.group(0))
    if re.search(r"month", str(v), re.I):
        age = age / 12.0
    if re.search(r"day", str(v), re.I):
        age = age / 365.0
    return age if 0 <= age <= 130 else None


# ------------------------- Tabular / text -------------------------

HEADER_HINTS = {
    "name": ["test", "test name", "investigation", "parameter", "analyte", "description",
             "examination", "particulars"],
    "value": ["result", "value", "observed value", "obs value", "reading", "your value",
              "result value", "observed"],
    "unit": ["unit", "units", "uom"],
    "range": ["reference range", "ref range", "normal range", "bio ref interval",
              "biological reference interval", "reference value", "ref value",
              "normal value", "range", "reference interval"],
    "flag": ["flag", "status", "remark", "remarks", "interpretation", "indicator"],
}


def _match_header(cells):
    """Map a header row onto column indices. Returns None if it is not a header."""
    idx = {}
    for i, cell in enumerate(cells):
        c = re.sub(r"\s+", " ", str(cell or "")).strip().lower().rstrip(":")
        for field, hints in HEADER_HINTS.items():
            if field in idx:
                continue
            if c in hints or any(c.startswith(h) for h in hints):
                idx[field] = i
    return idx if "name" in idx and "value" in idx else None


# A table cell that holds only a number (optionally with a comparator), a count range
# such as "1-2" cells per field, or a clean qualitative word, needs no re-parsing.
_CLEAN_VALUE = re.compile(
    r"^\s*(?:[<>\u2264\u2265]=?\s*)?[-+]?[\d.,]+\s*$"
    r"|^\s*\d+(?:\.\d+)?\s*[-\u2013]\s*\d+(?:\.\d+)?\s*$"
    r"|^\s*[A-Za-z][A-Za-z \-]{1,24}\s*$")


def _join_wrapped(parts):
    """Join wrapped name lines; a line ending in a hyphen continues without a space."""
    out = ""
    for part in parts:
        out = (out + part) if out.endswith("-") else (out + " " + part).strip()
    return out


def _split_name_cell(text, resolver):
    """A results-table name cell often holds the test name and, on following lines, the
    method ("HbA1c" / "HPLC") - and a long name can itself wrap ("... (hs-" / "CRP)").

    The longest run of leading lines that names a known test is the name; the rest is
    the method. Without a resolver, or if no prefix resolves, the first line is the name.
    """
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    if len(lines) <= 1:
        return (lines[0] if lines else str(text).strip()), None
    # The name continues onto the next line only while it is visibly cut - an unclosed
    # bracket or a trailing hyphen. Otherwise the next line is the method. Taking the
    # longest resolvable run instead read "Reaction (pH)" + "Double Indicator" as one
    # name, and the bracket then matched BLOOD pH.
    k = 1
    while k < len(lines):
        joined = _join_wrapped(lines[:k])
        if not (joined.count("(") > joined.count(")") or joined.rstrip().endswith("-")):
            break
        k += 1
    name = _join_wrapped(lines[:k])
    # A name can also wrap at a plain word boundary with no visible cut ("HIGHLY
    # SENSITIVE C-REACTIVE" / "PROTEIN (hs-CRP)"). If what we have does not name a test,
    # take the SHORTEST run of lines that does - never a longer one, which is how a
    # method line gets absorbed.
    if resolver and not resolver(name):
        for j in range(1, len(lines) + 1):
            cand = _join_wrapped(lines[:j])
            if resolver(cand):
                name, k = cand, j
                break
    return name, (" ".join(lines[k:]) or None)


def extract_table_rows(rows, source_kind="csv", source_name="input", header=None,
                       resolver=None):
    """Consume a list of cell-lists. Re-detects a header whenever one appears,
    which is what multi-section lab reports actually look like.

    `header` is a column map from the table IMMEDIATELY before this one on the same page,
    passed only when that table ENDED with its header row and this table has the same
    number of columns - the layout where a patient-details box carries the column titles
    and the results sit in the next grid. Any looser carry-over applied a results layout
    to summary grids, interpretation tables and an HPLC chromatogram, producing readings
    such as "TSH = T4" and an HbA1c range that was really a retention time.

    Returns (observations, warnings, trailing_header): the column map if this table's
    last non-empty row was a header with no results after it, else None.
    """
    obs, warnings = [], []
    cols = header
    section = None
    trailing_header = None
    for n, cells in enumerate(rows):
        cells = [("" if c is None else str(c).strip()) for c in cells]
        if not any(cells):
            continue
        header = _match_header(cells)
        if header:
            cols = header
            trailing_header = header
            continue
        trailing_header = None
        if cols is None:
            continue
        filled = [c for c in cells if c]
        name_cell = cells[cols["name"]] if cols["name"] < len(cells) else ""
        if len(filled) == 1 and name_cell and not re.search(r"\d", name_cell):
            # A heading row inside the table: "Urine Routine ..." then "Physical
            # Examination". Headings accumulate, so a sub-heading does not erase the
            # panel it belongs to.
            heading = name_cell.replace("\n", " ")
            section = (section + " > " + heading) if section else heading
            continue
        def cell(field):
            i = cols.get(field)
            if i is None or i >= len(cells):
                return None
            return cells[i] or None
        name, value = cell("name"), cell("value")
        method = None
        if name and "\n" in name:
            name, method = _split_name_cell(name, resolver)
        if name and _match_header([name]):          # a repeated header inside the body
            continue
        # A merged cell ("18.40 H", "7.9 %") or a shifted column leaves the value cell
        # unusable; read the whole row with the value-anchored parser instead.
        if source_kind == "pdf" and (not name or value is None or
                                     not _CLEAN_VALUE.match(value or "")):
            parsed = parse_row(Row(cells=[(float(i), c) for i, c in enumerate(cells) if c],
                                   source="row %d" % (n + 1)), resolver)
            if parsed is not None:
                o = _obs_from_parsed(parsed, source_kind)
                o.raw_flag = o.raw_flag or cell("flag")
                o.origin, o.section = "table", section
                obs.append(o)
                continue
        if not name or value is None:
            continue
        obs.append(RawObservation(
            raw_name=name, raw_value=value, raw_unit=cell("unit"),
            raw_range=cell("range"), raw_flag=cell("flag"),
            source_path="row %d" % (n + 1), source_kind=source_kind,
            origin="table", section=section, method=method))
    if cols is None and source_kind != "pdf":
        warnings.append("no recognisable result table header was found")
    return obs, warnings, trailing_header


def extract_csv(text, source_name="input.csv", resolver=None):
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    obs, warnings, _ = extract_table_rows(rows, "csv", source_name, resolver=resolver)
    ctx = _context_from_text(text, source_name)
    if not obs:
        # fall back to line parsing for reports exported as CSV without a header
        obs, w2 = extract_free_text(text, source_name, resolver)
        warnings += w2
    return obs, ctx, warnings


def _obs_from_parsed(parsed, source_kind):
    return RawObservation(
        raw_name=parsed.name, raw_value=parsed.value, raw_unit=parsed.unit,
        raw_range=parsed.range, raw_flag=parsed.flag,
        source_path=parsed.source, source_kind=source_kind, origin="text",
        section=getattr(parsed, "section", None))


def extract_free_text(text, source_name="input.txt", resolver=None):
    """Line-oriented parsing for reports whose layout defeats table detection.

    Uses the same value-anchored row parser as the PDF path (engine/layout.py). The
    two regular expressions this replaced let a test name absorb digits, so on
    single-spaced text the reference range's upper bound was read as the result.
    """
    obs = [_obs_from_parsed(pr, "text")
           for pr in parse_rows(rows_from_text(text), resolver)]
    warnings = [] if obs else ["no result lines could be parsed from the text layer"]
    return obs, warnings


# Lab reports crowd several labelled fields onto one line ("Name: X  UHID: Y").
# The name is captured greedily up to the next colon, then `_trim_trailing_label` removes
# the label word that belongs to the following field.
CTX_TEXT = [
    ("name", re.compile(
        r"(?:patient\s*name|p\.?\s?name|pt\.?\s?name|name)\s*[:\-]\s*"
        r"(?:mr\.?|mrs\.?|ms\.?|miss)?\s*"
        r"([A-Za-z][A-Za-z .]{1,60})", re.I)),
    # 'Age/Sex : 45 Y / Male' is a very common single-field header, so allow a short
    # run of age text between the label and the sex value.
    ("sex", re.compile(r"\b(?:sex|gender)\s*[:\-]\s*[\d\s/yrs.()ea]{0,16}(male|female|m|f)\b", re.I)),
    # \b: "Page 1 of 26" contains "age 1", which was being read as the patient's age.
    ("age", re.compile(r"\bage\s*(?:/\s*sex)?\s*[:\-]?\s*(\d{1,3})\s*(?:y|yr|yrs|years)?", re.I)),
    ("patient_id", re.compile(
        r"(?:uhid|mrn|patient\s*id|reg(?:istration)?\s*(?:no|number))\s*[:\-]\s*"
        r"([A-Za-z0-9\-/]{2,24})", re.I)),
    ("report_date", re.compile(
        r"(?:report(?:ed)?\s*(?:date|on)|collected\s*on|sample\s*date)\s*[:\-]\s*"
        r"([0-9]{1,4}[-/][0-9]{1,2}[-/][0-9]{1,4})", re.I)),
]


def _trim_trailing_label(text, match, group=1):
    """Drop trailing words that are actually the NEXT field's label.

    'Patient Name: Sample Patient G UHID: NG-1' captures 'Sample Patient G UHID'
    because the regex stops at the colon. If a colon follows the match, the last word
    belongs to the next field, not to this one.
    """
    raw = match.group(group).strip()
    # Column-aligned layouts separate fields by a run of spaces, which is the cleanest cut.
    value = re.split(r"\s{2,}", raw)[0].strip()
    if value != raw:
        return value
    # Otherwise, a colon or slash right after the match means the last word was the
    # next field's label rather than part of this value.
    tail = text[match.end(group): match.end(group) + 3].lstrip()[:1]
    if tail in (":", "-", "/") and " " in value:
        value = value.rsplit(" ", 1)[0].strip()
    return value


# Labelled header lines as laboratory result pages print them. Tried first and over the
# WHOLE text: a report may open with a designed summary whose stacked "Name / Mr X"
# layout the looser patterns cannot read, while the laboratory pages that state the
# details plainly sit ten thousand characters in.
CTX_LABELLED = [
    # "Patient Name : X" or, in a two-column header, "Patient Name    X" - the column gap
    # stands in for the colon.
    ("name", re.compile(r"\bpatient\s*name\s*(?::\s*|\s{2,}:?\s*)"
                        r"([A-Za-z][A-Za-z .]{1,60}?)\s*(?:\n|\s{2,}|$)", re.I)),
    # ...or with neither, when an honorific makes plain where the name begins.
    ("name", re.compile(r"\bpatient\s*name\s+((?:mr|mrs|ms|miss|master|baby)\.?\s+"
                        r"[A-Za-z][A-Za-z .]{1,60}?)\s*(?:\n|\s{2,}|$)", re.I)),
    ("age", re.compile(r"(?:dob\s*/\s*)?\bage\s*(?:/\s*(?:sex|gender))?\s*:\s*(\d{1,3})\s*"
                       r"(?:y|yr|yrs|years?)(?:\(s\))?(?![a-z])", re.I)),
    ("sex", re.compile(r"\b(?:sex|gender)\s*:\s*(?:\d{1,3}\s*(?:y|yr|yrs|years?)?(?:\(s\))?\s*/\s*)?"
                       r"(male|female|m|f)\b", re.I)),
    ("sex", re.compile(r"\b\d{1,3}\s*(?:y|yr|yrs|years?)(?:\(s\))?\s*/\s*(male|female|m|f)\b", re.I)),
    ("patient_id", re.compile(r"(?:\bpatient\s*id|\buhid|\bpid\s*no\.?|\bpatient\s*no\.?)(?:\s*/\s*uhid)?\s*:\s*"
                              r"([A-Za-z0-9\-]{2,24})", re.I)),
]


def _context_from_text(text, source_name):
    head = text[:4000]
    found = {}
    for field, pat in CTX_LABELLED:
        if field in found:
            continue
        m = pat.search(text)
        if m:
            found[field] = m.group(1).strip()
    for field, pat in CTX_TEXT:
        if field in found:
            continue
        m = pat.search(head)
        if m:
            found[field] = (_trim_trailing_label(head, m) if field == "name"
                            else m.group(1).strip())
    return PatientContext(
        patient_id=_str_or_none(found.get("patient_id")),
        name=_str_or_none(found.get("name")),
        sex=_norm_sex(found.get("sex")),
        age=_norm_age(found.get("age")),
        report_date=_str_or_none(found.get("report_date")),
        source_file=source_name,
    )


# ------------------------- PDF -------------------------

def _value_key(v):
    return re.sub(r"[\s,]", "", str(v or "")).lower()


def _completeness(o, resolver):
    s = 2 if re.search(r"\d", str(o.raw_value or "")) else 1
    s += 1 if o.raw_unit else 0
    s += 1 if o.raw_range else 0
    s += 2 if (resolver and resolver(o.raw_name)) else 0
    return s


def _merge_table_and_text(table_obs, text_obs, resolver):
    """Tables and the text layer see the same results twice. Keep one of each.

    Matching is per parameter AND page, so a test genuinely reported twice in one
    document still reaches duplicate resolution, where the conflict is recorded.
    Where the two readings of the same row disagree, the one that carried more
    structure wins - a table cell that merged "18.40 H" is worse than a text row that
    separated value, flag, unit and range.
    """
    def key(o):
        pid = resolver(o.raw_name) if resolver else None
        page = (o.source_path or "").split(",")[0]
        # The unit is part of what a reading IS: "Basophils 0.4 %" and "Basophils. 0.03
        # 10^3/uL" on the same page are two tests, and matching on name and page alone
        # discarded the absolute count as a duplicate of the percentage.
        unit = norm_unit(o.raw_unit) or ""
        return (pid or re.sub(r"\W+", " ", o.raw_name.lower()).strip(), page, unit)

    merged = list(table_obs)
    index = {}
    printed = set()
    for n, o in enumerate(merged):
        index.setdefault(key(o), []).append(n)
        # Only a table reading whose own name is recognised may stand in for the text
        # reading of the same printed row; otherwise a badly split name cell would
        # discard a text reading that named the test correctly.
        if o.raw_range and re.search(r"\d", str(o.raw_value or "")) and \
                (not resolver or resolver(o.raw_name)):
            printed.add(((o.source_path or "").split(",")[0], _value_key(o.raw_value),
                         (o.raw_unit or "").lower(), _value_key(o.raw_range)))
    n_table = len(merged)
    for o in text_obs:
        # The same printed row read twice - once from the table, once from the text layer
        # where a wrapped name may have been cut - is the table's reading.
        sig = ((o.source_path or "").split(",")[0], _value_key(o.raw_value),
               (o.raw_unit or "").lower(), _value_key(o.raw_range))
        if o.raw_range and sig in printed:
            continue
        k = key(o)
        hits = index.get(k, [])
        # Only a reading of the SAME printed row is merged away. Two rows on one page that
        # resolve to one parameter are two results - "HIV 1 Antibody: Non Reactive" and
        # "HIV 2 Antibody: Equivocal", or a test printed twice with different values - and
        # both must reach duplicate resolution, which records the conflict. Matching on
        # parameter and page alone dropped the equivocal HIV-2 result without trace.
        same_row = [h for h in hits
                    if _value_key(merged[h].raw_value) == _value_key(o.raw_value)
                    or (h < n_table and _same_name(merged[h].raw_name, o.raw_name))]
        if not same_row:
            index.setdefault(k, []).append(len(merged))
            merged.append(o)
            continue
        if any(_value_key(merged[h].raw_value) == _value_key(o.raw_value) for h in same_row):
            continue
        h = same_row[0]
        if _completeness(o, resolver) > _completeness(merged[h], resolver):
            merged[h] = o
    return merged


def _same_name(a, b):
    """Two readings of one printed name: equal once punctuation and spacing are gone, or
    one a cut-off start of the other (a wrapped name cell)."""
    na, nb = (re.sub(r"[^a-z0-9]", "", str(x).lower()) for x in (a, b))
    return bool(na and nb) and (na == nb or na.startswith(nb) or nb.startswith(na))


def _is_drawing_glyph(text):
    """Box-drawing, block and geometric-shape characters, and private-use glyphs: the
    alphabet of barcode and ornament fonts, never part of a result or a patient field."""
    if not text:
        return False
    cp = ord(text[0])
    return 0x2500 <= cp <= 0x25FF or 0xE000 <= cp <= 0xF8FF


def _not_drawing(chars):
    """A page filter that drops drawing glyphs and the blank characters interleaved
    with them on the same line - the spaces of a barcode chain lines just as well."""
    spans = {}
    for ch in chars:
        if _is_drawing_glyph(ch.get("text")):
            key = round(ch["top"], 1)
            lo, hi = spans.get(key, (ch["x0"], ch["x1"]))
            spans[key] = (min(lo, ch["x0"]), max(hi, ch["x1"]))

    def keep(obj):
        if obj.get("object_type") != "char":
            return True
        text = obj.get("text") or ""
        if _is_drawing_glyph(text):
            return False
        if not text.strip():
            span = spans.get(round(obj["top"], 1))
            if span and span[0] - 1 <= obj["x0"] <= span[1] + 1:
                return False
        return True
    return keep


def _has_stacked_glyphs(page):
    """True if any glyph is drawn twice at the same position - one pass, no copies."""
    seen = set()
    for ch in page.chars:
        key = (ch.get("text"), round(ch.get("x0", 0), 1), round(ch.get("top", 0), 1))
        if key in seen:
            return True
        seen.add(key)
    return False


def extract_pdf(data, source_name="input.pdf", resolver=None):
    """Tables AND the text layer, on every page, reconciled.

    Previously the text layer was read only if no table existed anywhere in the file,
    so a single detected table - a patient-details box, one CBC grid - silenced every
    text-layout result on every other page.
    """
    try:
        import pdfplumber
    except ImportError:
        return [], PatientContext(source_file=source_name), ["pdfplumber is not installed"], ""

    table_obs, text_obs, warnings, text_parts, rows_by_page = [], [], [], [], []
    blank_pages = []
    header = None
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for pageno, page in enumerate(pdf.pages, 1):
            # A barcode drawn in a block-glyph font is a staircase of characters a couple
            # of points apart. Line grouping chains through it, fusing the two header
            # lines beside it ("Patient Name Mr X" / "Age : 35") into one garbled line, so
            # the patient's name, age and sex were all lost. They are not text.
            if any(_is_drawing_glyph(ch.get("text")) for ch in page.chars):
                page = page.filter(_not_drawing(page.chars))
            # Fake-bold text is drawn two or three times at the same spot, which reads as
            # "BBBooorrrdddeeerrr". De-duplicating glyphs is expensive (two thirds of the
            # time on an ordinary report), so it runs only on a page that has them.
            if _has_stacked_glyphs(page):
                try:
                    page = page.dedupe_chars()
                except Exception:                           # older pdfplumber
                    pass
            page_text = page.extract_text() or ""
            text_parts.append(page_text)
            carried = None
            for table in (page.extract_tables() or []):
                width = max((len(r) for r in table), default=0)
                header_in = carried[0] if (carried and carried[1] == width) else None
                rows_obs, _w, trailing = extract_table_rows(
                    table, "pdf", source_name, header=header_in, resolver=resolver)
                carried = (trailing, width) if trailing else None
                for o in rows_obs:
                    o.source_path = "page %d, %s" % (pageno, o.source_path)
                table_obs += rows_obs
            words = page.extract_words(x_tolerance=1.5, y_tolerance=2.5,
                                       keep_blank_chars=False, use_text_flow=False)
            rows_by_page.append(rows_from_words(words, pageno, page.width))
            if not page.chars:
                blank_pages.append(pageno)

    # A page with no text layer is a scan. With the optional OCR stage installed its text
    # boxes become words and go through the same row reconstruction and parser.
    ocr_pages = set()
    if blank_pages:
        from . import ocr
        if ocr.available():
            for pageno, (words, width) in ocr.page_words(data, blank_pages).items():
                heights = sorted(w["bottom"] - w["top"] for w in words) or [8.0]
                rows_by_page[pageno - 1] = rows_from_words(
                    words, pageno, width, y_tol=max(2.6, 0.45 * heights[len(heights) // 2]))
                text_parts[pageno - 1] = "\n".join(r.text for r in rows_by_page[pageno - 1])
                ocr_pages.add(pageno)
            if ocr_pages:
                warnings.append(
                    "%d page(s) had no text layer and were read by OCR (optical character "
                    "recognition). OCR can misread digits and decimal points - check every "
                    "value below against the original report before acting on it."
                    % len(ocr_pages))

    state = {}
    for page_rows in drop_repeated(rows_by_page, resolver):
        for pr in parse_rows(page_rows, resolver, state):
            o = _obs_from_parsed(pr, "pdf")
            if pr.source.startswith("page ") and int(pr.source.split(",")[0].split()[1]) in ocr_pages:
                o.origin = "ocr"
            text_obs.append(o)

    obs = _merge_table_and_text(table_obs, text_obs, resolver)
    full_text = "\n".join(text_parts)
    if not obs:
        warnings.append("no result rows could be read from this PDF")
    if blank_pages and not ocr_pages:
        warnings.append(("this PDF has no extractable text layer - it is probably a scan, "
                         "and would need OCR before it can be read")
                        if len(blank_pages) == len(rows_by_page) else
                        ("%d page(s) of this PDF have no text layer (scanned images) and could "
                         "not be read - results printed on those pages are missing from this "
                         "analysis" % len(blank_pages)))
    # Patient details are read first from the rebuilt rows, where a column gap separates
    # one header field from the next ("Name : Mr X   VID No. : 123"). The flattened text
    # joins them with a single space, which made the name "X VID".
    cell_text = "\n".join("   ".join(t for _x, t in r.cells)
                          for page_rows in rows_by_page for r in page_rows)
    ctx = _context_from_text(cell_text, source_name)
    plain = _context_from_text(full_text, source_name)
    for f in ("patient_id", "name", "sex", "age", "report_date"):
        if getattr(ctx, f) is None:
            setattr(ctx, f, getattr(plain, f))
    return obs, ctx, warnings, full_text


# ------------------------- entry point -------------------------

def extract(data, filename, resolver=None):
    """Dispatch on file extension. `data` is bytes; JSON may also be passed as an object.

    Returns (observations, context, warnings) - the historic three-value form.
    """
    return extract_with_text(data, filename, resolver)[:3]


def extract_with_text(data, filename, resolver=None):
    """As `extract`, plus the raw text the file yielded.

    That text is what lets the document check separate a sparse laboratory report from
    an unrelated document that happens to mention one test name.
    """
    lower = (filename or "").lower()

    if isinstance(data, (dict, list)):
        obs, ctx, w = extract_json(data, filename or "payload.json")
        return obs, ctx, w, json.dumps(data, default=str)

    if isinstance(data, bytes):
        if lower.endswith(".pdf") or data[:5] == b"%PDF-":
            return extract_pdf(data, filename, resolver)
        text = data.decode("utf-8", errors="replace")
    else:
        text = str(data)

    stripped = text.lstrip()
    if lower.endswith(".json") or stripped[:1] in "{[":
        try:
            obs, ctx, w = extract_json(json.loads(text), filename)
            return obs, ctx, w, text
        except json.JSONDecodeError as e:
            return [], PatientContext(source_file=filename), ["invalid JSON: %s" % e], text
    if lower.endswith((".csv", ".tsv")):
        obs, ctx, w = extract_csv(text, filename, resolver)
        return obs, ctx, w, text

    ctx = _context_from_text(text, filename)
    if text.count(",") + text.count("\t") > max(10, text.count("\n")):
        obs, _, w = extract_csv(text, filename, resolver)
        return obs, ctx, w, text
    obs, w = extract_free_text(text, filename, resolver)
    return obs, ctx, w, text
