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

import csv
import io
import json
import re

from .models import RawObservation, PatientContext

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


def parse_reference_range(text):
    """'70 - 99', '< 150', '>= 40', '0.4-4.0 uIU/mL' -> (low, high) floats or Nones."""
    if text is None:
        return None, None
    s = str(text).strip()
    if not s:
        return None, None
    # drop a trailing unit so '0.4 - 4.0 uIU/mL' still parses as a numeric interval
    s_clean = re.sub(r"\s*(mg|g|ng|pg|ug|µg|mmol|umol|µmol|mcg|iu|miu|uiu|u|meq|fl|pg|cells|million|lakhs?|thou)\s*/?\s*"
                     r"(dl|l|ml|ul|µl|cumm|mm3|hpf|hr|g|m2|min)?\b\.?", " ", s, flags=re.I)
    s_clean = s_clean.replace("%", " ").strip()
    for pat in RANGE_PATTERNS:
        m = pat.match(s_clean)
        if m:
            gd = m.groupdict()
            low = float(gd["low"]) if gd.get("low") is not None else None
            high = float(gd["high"]) if gd.get("high") is not None else None
            return low, high
    return None, None


def _first(d, keys):
    lower = {str(k).lower().replace(" ", "_"): v for k, v in d.items()}
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
        for field, keys in CONTEXT_KEYS.items():
            if field in ctx_found:
                continue
            val = _first(d, keys)
            if val is not None and _scalar(val):
                ctx_found[field] = val
        if "name" not in ctx_found and _is_demographics_block(d):
            val = _first(d, BARE_NAME_KEYS)
            if val is not None and _scalar(val):
                ctx_found["name"] = val

    def emit(name, value, unit, rng, flag, path):
        obs.append(RawObservation(
            raw_name=str(name).strip(), raw_value=value,
            raw_unit=str(unit).strip() if unit is not None else None,
            raw_range=str(rng).strip() if rng is not None else None,
            raw_flag=str(flag).strip() if flag is not None else None,
            source_path=path, source_kind="json"))

    def from_test_object(d, path):
        name = _first(d, NAME_KEYS)
        value = _first(d, VALUE_KEYS)
        unit = _first(d, UNIT_KEYS)
        rng = _first(d, RANGE_KEYS)
        flag = _first(d, FLAG_KEYS)
        if rng is None:
            low, high = _first(d, LOW_KEYS), _first(d, HIGH_KEYS)
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
        emit(name, value, unit, rng, flag, path)

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
                    emit(k, v, None, None, None, child)
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


def extract_table_rows(rows, source_kind="csv", source_name="input"):
    """Consume a list of cell-lists. Re-detects a header whenever one appears,
    which is what multi-section lab reports actually look like."""
    obs, warnings = [], []
    cols = None
    for n, cells in enumerate(rows):
        cells = [("" if c is None else str(c).strip()) for c in cells]
        if not any(cells):
            continue
        header = _match_header(cells)
        if header:
            cols = header
            continue
        if cols is None:
            continue
        def cell(field):
            i = cols.get(field)
            if i is None or i >= len(cells):
                return None
            return cells[i] or None
        name, value = cell("name"), cell("value")
        if not name or value is None:
            continue
        if _match_header([name]):          # a repeated header inside the body
            continue
        obs.append(RawObservation(
            raw_name=name, raw_value=value, raw_unit=cell("unit"),
            raw_range=cell("range"), raw_flag=cell("flag"),
            source_path="row %d" % (n + 1), source_kind=source_kind))
    if cols is None:
        warnings.append("no recognisable result table header was found")
    return obs, warnings


def extract_csv(text, source_name="input.csv"):
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    obs, warnings = extract_table_rows(rows, "csv", source_name)
    ctx = _context_from_text(text, source_name)
    if not obs:
        # fall back to line parsing for reports exported as CSV without a header
        obs, w2 = extract_free_text(text, source_name)
        warnings += w2
    return obs, ctx, warnings


# 'Haemoglobin      13.5   g/dL     13.0 - 17.0'
LINE_RE = re.compile(
    r"^(?P<name>[A-Za-z][A-Za-z0-9 ,.\-/()%'+&]{2,60}?)"
    r"[\s:.]{2,}"
    r"(?P<value>[<>]?\s*[-+]?\d[\d,]*\.?\d*|[A-Za-z][A-Za-z \-]{1,24}?)"
    r"(?:\s{2,}(?P<unit>[A-Za-zµ%/^\d.\-]{1,18}))?"
    r"(?:\s{2,}(?P<range>[<>≤≥]?\s*[-+]?\d[\d.,]*\s*(?:[-–—]|to)?\s*[\d.,]*\s*[A-Za-zµ%/^\d.\-]*))?"
    r"\s*(?P<flag>\b(?:H|L|HIGH|LOW|ABNORMAL|NORMAL|BORDERLINE)\b)?\s*$")


def extract_free_text(text, source_name="input.txt"):
    """Line-oriented parsing for reports whose layout defeats table detection."""
    obs, warnings = [], []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.rstrip()
        if not line.strip() or len(line) > 220:
            continue
        m = LINE_RE.match(line)
        if not m:
            continue
        g = m.groupdict()
        name = g["name"].strip(" .:-")
        value = (g["value"] or "").strip()
        if not name or not value or len(name) < 3:
            continue
        if re.fullmatch(r"[\d\s.,/-]+", name):
            continue
        obs.append(RawObservation(
            raw_name=name, raw_value=value,
            raw_unit=(g["unit"] or None), raw_range=(g["range"] or None),
            raw_flag=(g["flag"] or None),
            source_path="line %d" % n, source_kind="text"))
    if not obs:
        warnings.append("no result lines could be parsed from the text layer")
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
    ("sex", re.compile(r"(?:sex|gender)\s*[:\-]\s*[\d\s/yr.]{0,12}(male|female|m|f)\b", re.I)),
    ("age", re.compile(r"age\s*(?:/\s*sex)?\s*[:\-]?\s*(\d{1,3})\s*(?:y|yr|yrs|years)?", re.I)),
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


def _context_from_text(text, source_name):
    head = text[:4000]
    found = {}
    for field, pat in CTX_TEXT:
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

def extract_pdf(data, source_name="input.pdf"):
    try:
        import pdfplumber
    except ImportError:
        return [], PatientContext(source_file=source_name), ["pdfplumber is not installed"]

    obs, warnings, text_parts = [], [], []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for pageno, page in enumerate(pdf.pages, 1):
            page_text = page.extract_text() or ""
            text_parts.append(page_text)
            for table in (page.extract_tables() or []):
                rows_obs, _ = extract_table_rows(table, "pdf", source_name)
                for o in rows_obs:
                    o.source_path = "page %d, %s" % (pageno, o.source_path)
                obs += rows_obs

    full_text = "\n".join(text_parts)
    if not obs:
        obs, w = extract_free_text(full_text, source_name)
        warnings += w
    if not full_text.strip():
        warnings.append("this PDF has no extractable text layer - it is probably a scan, "
                        "and would need OCR before it can be read")
    ctx = _context_from_text(full_text, source_name)
    return obs, ctx, warnings


# ------------------------- entry point -------------------------

def extract(data, filename):
    """Dispatch on file extension. `data` is bytes; JSON may also be passed as an object."""
    lower = (filename or "").lower()
    if isinstance(data, (dict, list)):
        return extract_json(data, filename or "payload.json")
    if isinstance(data, bytes):
        if lower.endswith(".pdf") or data[:5] == b"%PDF-":
            return extract_pdf(data, filename)
        text = data.decode("utf-8", errors="replace")
    else:
        text = str(data)

    stripped = text.lstrip()
    if lower.endswith(".json") or stripped[:1] in "{[":
        try:
            return extract_json(json.loads(text), filename)
        except json.JSONDecodeError as e:
            return [], PatientContext(source_file=filename), ["invalid JSON: %s" % e]
    if lower.endswith((".csv", ".tsv")):
        return extract_csv(text, filename)

    ctx = _context_from_text(text, filename)
    if text.count(",") + text.count("\t") > max(10, text.count("\n")):
        obs, _, w = extract_csv(text, filename)
        return obs, ctx, w
    obs, w = extract_free_text(text, filename)
    return obs, ctx, w
