"""Reading result rows out of report text - PDF word geometry and plain text alike.

Why this exists. The previous line parser was a pair of regular expressions that let
the TEST NAME absorb digits. On the text layer pdfplumber actually produces - column
gaps collapsed to single spaces - that made the name swallow the real result and the
reference range's upper bound become the "value":

    HbA1c (Glycosylated Haemoglobin) 7.9 % 4.0 - 5.6 H
        -> name "HbA1c (Glycosylated Haemoglobin) 7.9 % 4.0", value 5.6, unit "H"

A diabetic HbA1c was read as 5.6, hs-CRP 18.4 was read as "< 1.0", and nothing was
reported as missing, because something had been extracted. Separately, text parsing
only ran when a document contained no table at all, so a single table on page 1
silenced every text-layer result on every other page.

The approach here:

  1. Rows are rebuilt from word POSITIONS, not from the flattened text, so the gaps
     between columns are known. A run of words with a column-sized gap between them is
     a separate cell.
  2. A row is split into name / value / tail by ANCHORING on a value token. Name
     tokens can hold digits (Vitamin B12, T4, CA 19-9) but a bare number that starts a
     cell, or that follows a name the dictionary recognises, is the result.
  3. The tail is classified token by token - flag, unit, reference range - in whatever
     order the report prints them.
  4. A handful of cross-line repairs cover the layouts that genuinely span lines: a
     long name wrapped onto a second line, and a reference range dropped below the
     result.
  5. Lines repeated on every page (headers, footers) and patient/administrative labels
     are never read as results.

Nothing here decides what a result MEANS. The optional `resolver` (the parameter
dictionary's alias lookup) is used only to choose between otherwise ambiguous splits
and to accept a unit-less, range-less row whose name is a known test.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- tokens

_NUM = r"(?:[-+]?(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d+)?|[-+]?\.\d+)"
NUMERIC_RE = re.compile(r"^(?:[<>≤≥]=?|=)?\s*(?:%s)$" % _NUM)
# 4.5x10^3, 4.5 x10^9/L - value with a scientific multiplier fused to it
SCI_RE = re.compile(r"^(%s)\s*[x×*]\s*10\^?(\d+)$" % _NUM, re.I)
VALUE_WITH_FLAG_RE = re.compile(r"^((?:[<>≤≥]=?)?(?:%s))(H|L|HH|LL|\*|↑|↓)$" % _NUM)
COMPARATOR_RE = re.compile(r"^(?:[<>≤≥]=?|=<|=>)$")
COUNT_RANGE_TOKEN = re.compile(r"^\d+(?:\.\d+)?[-–]\d+(?:\.\d+)?$")

QUALITATIVE_WORDS = {
    "positive", "negative", "reactive", "nonreactive", "non-reactive", "nil", "absent",
    "present", "trace", "detected", "notdetected", "equivocal", "indeterminate",
    "borderline-positive", "weakly", "normal", "abnormal",
}
# Multi-word qualitative results, longest first so "Non Reactive" beats "Reactive".
QUALITATIVE_PHRASES = [
    ("not", "detected"), ("non", "reactive"), ("weakly", "reactive"),
    ("weakly", "positive"), ("not", "seen"),
]
PLUS_RE = re.compile(r"^(?:\+{1,4}|[1-4]\+)$")

FLAG_WORDS = {"h", "l", "hh", "ll", "high", "low", "abnormal", "critical", "borderline",
              "*", "**", "↑", "↓", "(h)", "(l)", "[h]", "[l]", "a"}

# A unit contains a letter, %, µ or a power of ten, and none of the characters that make
# something a sentence.
UNIT_RE = re.compile(
    r"^(?:%|‰|[a-zA-Zµμ/^*\[\]\d.\-]*[a-zA-Zµμ%][a-zA-Zµμ/^*\[\]\d.\-%²³]*|x?10\^?\d+/[a-zA-Zµμ]+)$")
# Shapes a laboratory unit takes. Used only to decide whether a row the dictionary does
# NOT recognise is still a measurement - "Payment within 30 days" has a unit-looking word
# too, but no laboratory unit.
LAB_UNIT_RE = re.compile(
    r"(?:/|%|\^|\b(?:fl|pg|sec|secs|seconds|s|mmhg|ratio|index|iu|u|coi|s/co|ph|cells|"
    r"lakhs?|million|mill|thou|cumm|hpf|mm|cm|kg|g|gm|mg|mcg|ug|µg|ng|pmol|nmol|mmol|umol|"
    r"meq|miu|uiu|µiu|ku|kua|ml|l|units?)\b)", re.I)

UNIT_WORDS_NOT_UNITS = {"and", "or", "the", "of", "in", "to", "is", "for", "with", "on",
                        "at", "by", "as", "method", "remark", "note", "see", "range"}

# Administrative labels. A row whose name starts with one of these is never a result.
META_LABELS = re.compile(
    r"^(?:patient|pt\.?|name|mr\.?|mrs\.?|ms\.?|age|sex|gender|uhid|mrn|ip\b|op\b|lab\s*no|"
    r"lab\s*id|sample|specimen|collected|collection|received|reported|registered|"
    r"registration|ref\.?|referred|referring|doctor|dr\.?|consultant|page|barcode|bill|"
    r"phone|ph\.?|mobile|email|address|date|time|visit|report|accession|client|printed|"
    r"approved|authenticated|authorised|authorized|verified|signature|nabl|cap\b|"
    r"test\s*name|investigation|parameter|result|unit|biological|reference|method|"
    r"department|end\s*of\s*report|remarks?|interpretation|comments?|note|"
    r"dob|d\.o\.b|passport|aadhaar|ward|bed|location|centre|center|branch|reg\.?)\b",
    re.I)

RANGE_TAIL_RE = re.compile(
    r"^\s*(?:"
    r"(?P<lo>%(n)s)\s*(?:-|–|—|to)\s*(?P<hi>%(n)s)"
    r"|(?P<cmp>[<>]\s*or\s*=|[<>]\s*/\s*=|=\s*[<>]|[<>≤≥]=?|up\s*to|upto|less\s*than|more\s*than|"
    r"greater\s*than)\s*(?P<one>%(n)s)"
    r")" % {"n": _NUM}, re.I)

# A power-of-ten multiplier printed apart from the rest of its unit: "10^3 /uL",
# "10^3 / µl", "10^3 cells/uL". Read alone, "10^3" was dropped and a platelet count of
# 285 x10^3/uL became 285 /uL - a thrombocytopenia that was never there.
POWER_RE = re.compile(r"^[x×]?\s*10(?:\^|\*|e)?[369]$|^[x×]?\s*10[³⁶⁹]$", re.I)


def _is_numeric(tok):
    return bool(NUMERIC_RE.match(tok))


def _norm_flag(tok):
    t = tok.strip().lower()
    return t if t in FLAG_WORDS else None


# --------------------------------------------------------------------------- rows

@dataclass
class Row:
    """One visual line: its text, the start x of every cell, and where it came from."""
    cells: list                       # list of (x0, text)
    top: float = 0.0
    page: int = 0
    source: str = ""

    @property
    def text(self):
        return "  ".join(t for _x, t in self.cells)

    @property
    def x0(self):
        return self.cells[0][0] if self.cells else 0.0


def rows_from_words(words, page_no, page_width, y_tol=2.6):
    """Group pdfplumber words into visual rows, then into cells by horizontal gap."""
    if not words:
        return []
    words = sorted(words, key=lambda w: (round(w["top"], 1), w["x0"]))
    lines, cur, cur_top = [], [], None
    for w in words:
        if cur_top is None or abs(w["top"] - cur_top) <= y_tol:
            cur.append(w)
            cur_top = w["top"] if cur_top is None else (cur_top + w["top"]) / 2.0
        else:
            lines.append(cur)
            cur, cur_top = [w], w["top"]
    if cur:
        lines.append(cur)

    rows = []
    for line in lines:
        line.sort(key=lambda w: w["x0"])
        widths = [(w["x1"] - w["x0"]) / max(1, len(w["text"])) for w in line]
        char_w = sorted(widths)[len(widths) // 2] if widths else 4.0
        # A gap wider than ~2 characters is a column boundary. Ordinary word spacing in
        # a name is well under one character width.
        gap_limit = max(2.2 * char_w, 7.0)
        cells, buf, buf_x, last_x1 = [], [], None, None
        for w in line:
            if last_x1 is not None and (w["x0"] - last_x1) > gap_limit:
                cells.append((buf_x, " ".join(buf)))
                buf, buf_x = [], None
            if buf_x is None:
                buf_x = w["x0"]
            buf.append(w["text"])
            last_x1 = w["x1"]
        if buf:
            cells.append((buf_x, " ".join(buf)))
        rows.append(Row(cells=cells, top=line[0]["top"], page=page_no,
                        source="page %d, y %d" % (page_no, int(line[0]["top"]))))
    return rows


def rows_from_text(text, source_prefix="line"):
    """Rows from plain text. Two or more spaces (or a tab) separate cells."""
    rows = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not line.strip() or len(line) > 260:
            continue
        cells, x = [], 0
        for part in re.split(r"(\t+|\s{2,})", line):
            if not part or part.isspace():
                x += len(part or "")
                continue
            cells.append((float(x), part.strip()))
            x += len(part)
        rows.append(Row(cells=cells, top=float(n), page=0, source="%s %d" % (source_prefix, n)))
    return rows


def _repeat_key(text):
    """Key used to spot a running header/footer, or None if the line cannot be one.

    Digits are masked so 'Page 1' and 'Page 2' match. A line with no real words is never
    a header: masking "4.04 - 15.2" to "#.# - #.#" made every reference interval printed
    on its own line look like the same repeated footer, and they were all discarded.
    """
    low = text.lower().strip()
    if not re.search(r"[a-z]{3,}", low):
        return None
    return re.sub(r"\d+", "#", low)


def drop_repeated(rows_by_page, resolver=None):
    """Remove lines printed on more than one page - running headers and footers.

    A repeated line that parses as a result for a KNOWN test is kept. Trend reports
    reprint "Haemoglobin (Hb): 15" on every visit page, and those were all being
    discarded as a running header.
    """
    if len(rows_by_page) < 2:
        return rows_by_page
    seen = {}
    for page_rows in rows_by_page:
        keys = {_repeat_key(r.text) for r in page_rows} - {None}
        for k in keys:
            seen[k] = seen.get(k, 0) + 1
    repeated = {k for k, c in seen.items() if c >= 2}

    out = []
    for page_rows in rows_by_page:
        kept = []
        for r in page_rows:
            k = _repeat_key(r.text)
            if k is not None and k in repeated:
                parsed = parse_row(r, resolver) if resolver else None
                if not (parsed is not None and resolver(parsed.name)):
                    continue
            kept.append(r)
        out.append(kept)
    return out


# --------------------------------------------------------------------------- parsing

@dataclass
class Parsed:
    name: str
    value: str
    unit: str = None
    range: str = None
    flag: str = None
    source: str = ""
    value_x: float = 0.0
    top: float = 0.0
    x0: float = 0.0
    section: str = None
    band_label: bool = False          # the range cell began with a band label ("Deficient <20")
    complete: int = 0            # how much structure the row carried; used to pick duplicates
    notes: list = field(default_factory=list)


_FUSED_UNIT_NUM = re.compile(r"^([A-Za-zµμ][A-Za-zµμ/^.%]*[A-Za-zµμ%])(\d[\d.,]*)$")


def _split_fused(parts):
    out = []
    for p in parts:
        m = _FUSED_UNIT_NUM.match(p)
        # Only where the letters are clearly a unit (contain "/" or "%"): "B12", "T4" and
        # "A1c" are names and must stay whole.
        if m and ("/" in m.group(1) or "%" in m.group(1)):
            out += [m.group(1), m.group(2)]
        else:
            out.append(p)
    return out


def _tokens_with_cells(row):
    """Flatten a row to tokens, remembering which tokens start a new cell."""
    toks, starts = [], []
    for cx, text in row.cells:
        parts = _split_fused(text.split())
        for i, p in enumerate(parts):
            toks.append(p)
            starts.append(i == 0)
    return toks, starts


def _value_at(toks, i):
    """If a result value starts at token i, return (value_text, tokens_consumed, flag)."""
    t = toks[i]
    nxt = toks[i + 1] if i + 1 < len(toks) else None

    # "< 0.5" / "> 1000" written with a space
    if COMPARATOR_RE.match(t) and nxt is not None and _is_numeric(nxt):
        return "%s%s" % (t, nxt), 2, None
    m = VALUE_WITH_FLAG_RE.match(t)
    if m:
        return m.group(1), 1, m.group(2)
    if _is_numeric(t):
        # "4.5 x10^3" / "4.5 x 10^3"
        if nxt and re.match(r"^[x×*]$", nxt, re.I) and i + 2 < len(toks) and \
                re.match(r"^10\^?\d+", toks[i + 2]):
            return "%s x %s" % (t, toks[i + 2]), 3, None
        return t, 1, None
    if SCI_RE.match(t):
        return t, 1, None
    if COUNT_RANGE_TOKEN.match(t) and nxt is not None:
        # "1-2 /hpf": a microscopy count range is the result, not a reference interval.
        # Without a per-field unit it must look like a count - whole numbers - and not be
        # followed by a comparator band: "TNI 0.00-0.02 >0.02" is a reference line, and
        # reading 0.00-0.02 as the result called a troponin of 0.17 normal.
        per_field = bool(re.search(r"/\s*(?:hpf|lpf|field)", " ".join(toks[i + 1:i + 3]), re.I))
        whole = not re.search(r"\.", t)
        after = " ".join(toks[i + 1:])
        if per_field or (whole and RANGE_TAIL_RE.match(after) and not COMPARATOR_RE.match(nxt)
                         and not re.match(r"^[<>≤≥]", nxt)):
            return t, 1, None

    low = t.lower().strip(".,:;")
    if nxt is not None:
        # "Non Reactive,0.26": the verdict with the assay's index value fused to it
        nxt_word = re.sub(r"[,;]\s*[<>]?\d[\d.]*$", "", nxt)
        pair = (low, nxt_word.lower().strip(".,:;"))
        if pair in QUALITATIVE_PHRASES:
            return "%s %s" % (t, nxt), 2, None
    if low in QUALITATIVE_WORDS or PLUS_RE.match(t):
        return t, 1, None
    return None


# Words that make a "name" prose or a dashboard caption rather than a test name.
PROSE_NAME_RE = re.compile(
    r"\b(?:increased|decreased|improved|improving|worsened|your|summary|score|parameters?|"
    r"tests?\s+at|detected|seek|advice|consult|recommend\w*|since|compared|previous|"
    r"what\s+it\s+means|reasons?|range|risk\s+score)\b", re.I)
STATUS_WORDS = {"normal", "abnormal", "borderline", "high", "low", "critical", "optimal",
                "improving", "monitor", "alert", "good", "average", "poor"}


def _is_prose_name(name, toks):
    if not toks:
        return True
    first = toks[0]
    # starts with a unit or a number: "K/uL Increased by", "0.45 mg/dL", "gm/dL cu.mm"
    # ("A/G Ratio" is a name: "G" is not a unit's denominator)
    if "/" in first and not re.search(r"[A-Za-z]{3,}", first.split("/")[0]) and \
            re.match(r"^(?:[uµμmdkn]?[lL]|dL|uL|µL|hpf|HPF|cumm|cu\.?mm|mm|hr|min|kg|g|sec)\b",
                     first.split("/", 1)[1]):
        return True
    if re.match(r"^[<>]?\d", first):
        return True
    if name.strip(" :").lower() in STATUS_WORDS or first.strip(":").lower() in STATUS_WORDS:
        return True
    # a bracketed NUMBER inside a name is a quoted result, not part of a test name:
    # "Albumin (3.7) and TSH (5.35)"
    if re.search(r"\(\s*[<>]?\d[\d.,]*\s*\)", name):
        return True
    if PROSE_NAME_RE.search(name):
        return True
    return False


def _clean_name(tokens):
    name = " ".join(tokens).strip(" :-–.")
    return re.sub(r"\s*:\s*$", "", name)


# One labelled band of a multi-band reference: "Insufficient 21 - 29", "Normal Or High:
# >= 90", "Kidney Failure: < 15".
# Words a band label is made of. A word after an interval that is none of these is the
# report's METHOD column ("12 - 15.5  Colorimetric", "0 - 30  Calculation"), not a band.
BAND_WORDS_RE = re.compile(
    r"\b(?:normal|optimal|optimum|desirable|acceptable|borderline|high|low|very|risk|"
    r"deficien\w*|insufficien\w*|sufficien\w*|toxic\w*|non|diabet\w*|prediabet\w*|impaired|"
    r"elevated|decrease\w*|increase\w*|failure|reactive|negative|positive|moderate|mild|"
    r"severe|average|adequate|healthy|reference|pregnan\w*|trimester|adults?|males?|females?|"
    r"men|women|child\w*|smokers?|nonsmokers?|target|action|upper|limit|hypervitaminosis|"
    r"possible|near|above|below|intermediate|ideal|recommended|goal|range|level)\b", re.I)

_BAND_NUM = (r"(?:(?:[<>≤≥]=?|[<>]\s*/\s*=|[<>]\s*or\s*=)\s*)?\d[\d.,]*"
             r"(?:\s*(?:-|–|—|to)\s*\d[\d.,]*)?")
# ...printed label first ("Optimum>60", "Sufficient : 30 - 100") or label last ("< 40 : Low",
# "<200 - Desirable", "0.5 - 3.0 Desirable/Low Risk").
BAND_LINE_RE = re.compile(
    r"^\s*(?:[A-Za-z][A-Za-z /()\-]{1,40}?\s*:{0,2}\s*%(n)s\s*[A-Za-zµμ/%%]{0,8}"
    r"|%(n)s\s*(?:[:\-–]\s*|\s)[A-Za-z][A-Za-z /()\-]{1,40})\s*$" % {"n": _BAND_NUM})
NORMAL_BAND_LABEL = re.compile(
    r"^(?:normal(?:\s+or\s+high)?|sufficient|optimal|desirable|reference)\s*:?\s*", re.I)
RANGE_LABEL_RE = re.compile(
    r"^\(?(?:normal|ref\.?|reference|range|bio\.?|biological|interval|limit)\s*[:=]?\)?$", re.I)


def _parse_tail(tail, leftovers=None):
    """Classify what follows the value: flag, unit and reference range, in any order.

    `leftovers`, when given a list, receives every token that is none of those. A split
    that leaves words - or, worse, another number - unaccounted for is probably wrong:
    "Vitamin D 25 - Hydroxy: 9.9 ng/mL" read as the value 25 leaves "Hydroxy:" and 9.9
    behind, while the reading 9.9 leaves nothing.
    """
    unit = rng = flag = None
    # "(13.0-17.0)" / "[0.5 - 5.0]": a bracketed reference interval is still one.
    rest = []
    for tok in tail:
        inner = tok.strip("()[]")
        rest.append(inner if (inner != tok and re.search(r"\d", inner)) else tok)
    # "Optimum>60", "Normal:4.6-5.6": a band label fused to its interval.
    fused = []
    for tok in rest:
        m = re.match(r"^([A-Za-z]{3,}):?((?:[<>≤≥]=?)?\d[\d.,]*(?:[-–]\d[\d.,]*)?)$", tok)
        fused += [m.group(1), m.group(2)] if m else [tok]
    rest = fused
    # "10 3 / µl": a superscript exponent sits on its own baseline and is read as a
    # separate "3" (its caret a line above). Rejoined when a unit denominator follows.
    k = 0
    while k + 2 < len(rest):
        if rest[k] == "10" and rest[k + 1] in ("3", "6", "9") and rest[k + 2].startswith("/"):
            rest[k:k + 2] = ["10^" + rest[k + 1]]
        k += 1
    i = 0
    while i < len(rest):
        tok = rest[i]
        joined = " ".join(rest[i:])
        if rng is None:
            m = RANGE_TAIL_RE.match(joined)
            if m:
                rng = m.group(0).strip()
                i += len(rng.split())
                # a unit printed straight after the range belongs to the range line
                continue
        f = _norm_flag(tok)
        if f is not None and flag is None:
            flag = tok.strip("()[]")
            i += 1
            continue
        # A labelled normal band ahead of the interval: "Normal Or High: >= 90".
        if rng is None:
            m = NORMAL_BAND_LABEL.match(joined)
            if m and RANGE_TAIL_RE.match(joined[m.end():]):
                inner = RANGE_TAIL_RE.match(joined[m.end():]).group(0).strip()
                rng = inner
                i += len(joined[:m.end()].split()) + len(inner.split())
                continue
        # A qualitative reference ("Negative", "Non Reactive") for a categorical test.
        # Checked before units, which would otherwise take the word "Non". Not when a
        # numeric interval follows - then the word is that interval's label.
        if rng is None and tok.lower().strip(".") in QUALITATIVE_WORDS | {"non"} and                 not re.search(r"\d", joined):
            if i + 1 < len(rest) and (tok.lower(), rest[i + 1].lower()) in QUALITATIVE_PHRASES:
                rng = "%s %s" % (tok, rest[i + 1])
                i += 2
                continue
            if tok.lower().strip(".") in QUALITATIVE_WORDS:
                rng = tok
                i += 1
                continue
        if unit is None and POWER_RE.match(tok) and i + 1 < len(rest):
            # Take "/", "/uL", "cells/uL", "µl" until the unit has a denominator.
            parts, j = [tok], i + 1
            while j < len(rest) and len(parts) < 4 and re.match(
                    r"^(?:/|/\S+|cells?/\S*|cells?|[uµμ]l|cumm|cu\.?mm)$", rest[j], re.I):
                parts.append(rest[j])
                j += 1
                if re.search(r"/[A-Za-zµμ]", rest[j - 1]) or (parts[-2] == "/" and rest[j - 1] != "/"):
                    break
            joined_unit = parts[0] + " " + "".join(parts[1:])
            if len(parts) > 1 and re.search(r"/\s*[A-Za-zµμ]", joined_unit):
                unit = joined_unit
                i = j
                continue
        if unit is None and UNIT_RE.match(tok) and tok.lower() not in UNIT_WORDS_NOT_UNITS \
                and not _is_numeric(tok) and len(tok) <= 18 and (rng is None):
            unit = tok
            i += 1
            if "/" in unit and i < len(rest) and re.match(r"^(?:hr|hrs|h|hour|min|m2|m²)$",
                                                          rest[i], re.I):
                unit = "%s %s" % (unit, rest[i])
                i += 1
            elif "/" in unit and i + 1 < len(rest) and rest[i].lower().rstrip(".") == "sq"                     and rest[i + 1].lower() == "m":
                unit = "%s sq m" % unit                 # "ml/min/1.73 sq m"
                i += 2
            continue
        if unit is None and rng is not None and UNIT_RE.match(tok) and len(tok) <= 18 \
                and tok.lower() not in UNIT_WORDS_NOT_UNITS and not _is_numeric(tok) \
                and _norm_flag(tok) is None and LAB_UNIT_RE.search(tok):
            # ...only a word shaped like a laboratory unit: after the interval a report
            # may print its METHOD column, and "Calculation" is not a unit.
            # "70 - 100 mg/dL": unit after the range
            unit = tok
            i += 1
            continue
        if RANGE_LABEL_RE.match(tok) or (unit and tok.strip("()").lower() == unit.lower()):
            i += 1                              # "(Normal: 30-100 ng/mL)" labels / repeats
            continue
        if leftovers is not None:
            leftovers.append(tok)
        i += 1
    return unit, rng, flag


def parse_row(row, resolver=None):
    """Split one row into a result, or return None if it is not a result row."""
    toks, starts = _tokens_with_cells(row)
    if len(toks) < 2:
        return None
    text = row.text.strip()
    if META_LABELS.match(text):
        return None
    # Obviously a sentence: lots of words, no column structure.
    if len(row.cells) == 1 and len(toks) > 14:
        return None

    # A descriptive result in its own column - "Colour | Pale yellow | - | Pale yellow".
    # Only for a name the dictionary recognises, and only a short run of plain words
    # that is not a unit, a flag or a status caption.
    # With only two cells ("Blood group (ABO typing) | O", "RBC Morphology | Normocytic
    # Normochromic") there is no range column to confirm the layout, so it is accepted
    # only for a test the dictionary declares descriptive (categorical).
    cfg = getattr(resolver, "config", None) or getattr(resolver, "__self__", None)
    if resolver and len(row.cells) >= 2:
        first, second = row.cells[0][1], row.cells[1][1]
        pid = resolver(first)
        kind = cfg.param_by_id.get(pid, {}).get("type") if (pid and cfg) else None
        categorical = kind == "categorical"
        if kind == "numeric":
            # A measured test never has a word as its result: "Serum Ferritin | Decreased |
            # Increased" is a row of an interpretation table.
            pid = None
        descriptive = (re.fullmatch(r"[A-Za-z]+(?: [A-Za-z]+){0,2}", second)
                       and (categorical or _norm_flag(second) is None)
                       and second.lower() not in STATUS_WORDS
                       # qualitative results keep going through the normal path
                       and second.lower() not in QUALITATIVE_WORDS
                       and second.lower().split()[0] not in ("non", "not", "weakly"))
        if descriptive and pid and (len(row.cells) >= 3 or categorical):
            rest = [c for _x, c in row.cells[2:]]
            rng = next((c for c in rest if c.lower() == second.lower()), None)
            # "Colour | Yellow | | Pale Yellow": the expected description, when it differs
            if rng is None and rest and re.fullmatch(r"[A-Za-z]+(?: [A-Za-z]+){0,2}", rest[-1]):
                rng = rest[-1]
            return Parsed(name=first, value=second, unit=None, range=rng, flag=None,
                          source=row.source, value_x=row.cells[1][0], top=row.top,
                          x0=row.x0, complete=3)

    candidates = []
    for i in range(1, len(toks)):
        if (toks[i - 1].lower().strip(".,:;"), toks[i].lower().strip(".,:;")) in QUALITATIVE_PHRASES:
            continue
        v = _value_at(toks, i)
        if v is None:
            continue
        name_toks = toks[:i]
        if not any(re.search(r"[A-Za-z]{2,}", t) for t in name_toks):
            continue
        candidates.append((i, v))
    if not candidates:
        return None

    seen_pids = []

    def score(cand):
        i, (value, used, _f) = cand
        name = _clean_name(toks[:i])
        s = 0
        pid = resolver(name) if resolver else None
        if pid:
            s += 8
            if seen_pids and pid not in seen_pids:
                # "CA 125 34.5": "CA" is calcium, "CA 125" is CA-125. A longer name that
                # names a DIFFERENT test is the more specific reading.
                s += 3
            seen_pids.append(pid)
        if starts[i]:
            s += 4                              # the value opens its own column
        if any(starts[j] and (_is_numeric(toks[j]) or (
                _value_at(toks, j) is not None and not _is_numeric(toks[i])))
               for j in range(1, i)):
            # (...or a qualitative result opening its own column: "HBsAg Screening | Non
            # Reactive,0.26 | COI | Non Reactive: < 0.90" is not a test named "HBsAg
            # Screening Non Reactive,0.26 COI" whose result is the band label.)
            # The name would contain a number that opens its own column - a value column
            # swallowed into the name. "eGFR 111.85 ml/min | Normal Or High: >= 90" read
            # as a result of ">= 90".
            s -= 8
        if toks[i - 1].endswith(":"):
            s += 3                              # "Label: value" - the value follows the colon
        if _is_numeric(toks[i]) or COMPARATOR_RE.match(toks[i]):
            s += 1
        tail = toks[i + used:]
        if tail and _is_numeric(tail[0]) and not RANGE_TAIL_RE.match(" ".join(tail)):
            s -= 5                              # "125 34.5 U/mL": 125 is not the result
        left = []
        u, r, _fl = _parse_tail(tail, left)
        # Tokens the tail cannot account for. A number among them means the real
        # result is probably still to come.
        s -= 1.5 * sum(1 for x in left if re.search(r"[A-Za-z]{2,}", x))
        if any(_is_numeric(x.strip("()[]:,")) for x in left):
            s -= 6
        if u:
            s += 2
        if r:
            s += 2
        # Earlier is better only as a tie-break; a name should not swallow a result.
        s -= 0.01 * i
        return s, pid

    best, best_score, best_pid = None, None, None
    for cand in candidates:
        sc, pid = score(cand)
        if best_score is None or sc > best_score:
            best, best_score, best_pid = cand, sc, pid

    i, (value, used, inline_flag) = best
    name = _clean_name(toks[:i])
    if len(name) < 2 or META_LABELS.match(name) or _is_prose_name(name, toks[:i]):
        return None
    unit, rng, flag = _parse_tail(toks[i + used:])
    flag = inline_flag or flag
    # A word after the value that is not a laboratory unit ("6 parameters", "30 days")
    # means the row is a sentence, whatever its name resolves to.
    if unit and not LAB_UNIT_RE.search(unit) and not rng:
        return None
    # A dashboard status word is not a qualitative result for an unknown name.
    if not best_pid and value.lower() in STATUS_WORDS:
        return None

    # A value that is only a word needs a recognised name - otherwise "Colour Pale
    # Yellow" style rows, and prose, would all become results.
    numeric = bool(re.search(r"\d", value))
    if not best_pid:
        lab_unit = bool(unit and LAB_UNIT_RE.search(unit))
        if not (lab_unit or rng or (numeric and starts[i] and len(row.cells) >= 3)):
            return None
        # For a name the dictionary does not know, these mark a band table, a dashboard
        # tile or a chromatogram rather than an unrecognised TEST: a value that is itself
        # a threshold ("<50", ">=18") or a band ("100-129"), a "unit" that is not a
        # laboratory unit ("All", "RDW"), or bare numbers inside the name ("LA1c --- 1.7
        # 0.392", "Kidney Profile 1 / 13 Uric Acid"). None of this applies to a known test.
        if COMPARATOR_RE.match(toks[i]) or re.match(r"^[<>≤≥]", value) or                 COUNT_RANGE_TOKEN.match(value):
            return None
        if unit and not lab_unit:
            return None
        if any(_is_numeric(tk) or tk in ("---", "--") for tk in toks[:i]):
            return None
        if re.search(r"\b(?:profile|panel|studies|monitoring)\b", name, re.I):
            return None                         # a panel heading, never a single test
        # Fragments of interpretation prose: "... greater than 17 mg/dl", "Above 100
        # ng/ml", "and Poor Control - More than 10 %", a method line "(Serum, ..."
        # beside a band, and a "unit" that is a cut word ("D10/").
        if re.search(r"\b(?:than|above|below|over|under|upto|up\s+to)\s*$", name, re.I) or \
                re.match(r"^(?:and|or|of|with|in|to|for|the)\b", name) or name.startswith("(") or \
                (unit and unit.endswith("/")):
            return None

    value_x = 0.0
    k = 0
    for cx, ctext in row.cells:
        n = len(ctext.split())
        if k <= i < k + n:
            value_x = cx
            break
        k += n

    # A word range ("normal") beside a NUMERIC result is a caption from a neighbouring
    # column, not the reference interval.
    if rng and numeric and not re.search(r"\d", rng):
        rng = None
    # Was the interval printed after a band label - the first line of a multi-band
    # reference ("Deficient <20" / "Insufficient 21 - 29" / "Sufficient 30 - 100")?
    # The label and its interval may fall in separate cells ("Deficiency | : < 20").
    band_label = False
    if rng:
        right = [(cx, t) for cx, t in row.cells if cx > value_x + 1 and t != unit]
        # The smallest run of cells holding the interval - a method column may follow
        # ("Optimal: < 100 | Direct").
        spans = sorted(((k, m) for k in range(len(right)) for m in range(k + 1, len(right) + 1)),
                       key=lambda km: km[1] - km[0])
        for k, m in spans:
            joined = "  ".join(t for _x, t in right[k:m])
            if rng in re.sub(r"\s{2,}", " ", joined) or rng in joined:
                if BAND_LINE_RE.match(joined) and BAND_WORDS_RE.search(joined):
                    rng, band_label = re.sub(r"\s{2,}", " ", joined).strip(), True
                    break
    return Parsed(name=name, value=value, unit=unit, range=rng, flag=flag,
                  source=row.source, value_x=value_x, top=row.top, x0=row.x0,
                  band_label=band_label,
                  complete=(2 if numeric else 1) + (1 if unit else 0) + (1 if rng else 0)
                           + (2 if best_pid else 0))


_NAME_ONLY_RE = re.compile(r"^[A-Za-z(][A-Za-z0-9 ,.'()/&+\-]{1,70}$")
_RANGE_ONLY_RE = re.compile(r"^\s*(?:[<>≤≥]=?\s*)?%(n)s(?:\s*(?:-|–|—|to)\s*%(n)s)?\s*"
                            r"[A-Za-zµ%%/^.\d]{0,12}\s*$" % {"n": _NUM})


def split_side_by_side(row, resolver=None):
    """Split a row that holds two results next to each other into one row each.

    "Haemoglobin (Hb): 15 | RBC Count: 02 millions/" is two results. A later cell starts
    a new result when it opens with a name the dictionary recognises and carries its own
    value - and the cells before it form a complete result of their own.
    """
    if not resolver or len(row.cells) < 2:
        return [row]
    starts = [0]
    for k in range(1, len(row.cells)):
        cell_text = row.cells[k][1]
        if not re.match(r"^[A-Za-z(]", cell_text):
            continue
        tail = parse_row(Row(cells=row.cells[k:], top=row.top, page=row.page,
                             source=row.source), resolver)
        if tail is None or not resolver(tail.name):
            continue
        if not cell_text.lower().startswith(tail.name.lower()[:max(3, len(tail.name) // 2)]):
            continue
        head = parse_row(Row(cells=row.cells[starts[-1]:k], top=row.top, page=row.page,
                             source=row.source), resolver)
        if head is None:
            continue
        starts.append(k)
    if len(starts) == 1:
        return [row]
    bounds = starts + [len(row.cells)]
    return [Row(cells=row.cells[a:b], top=row.top, page=row.page,
                source="%s, col %d" % (row.source, n + 1))
            for n, (a, b) in enumerate(zip(bounds, bounds[1:]))]


# Plain-text rows count lines (top = line number); PDF rows use points.
def _line_gap(a_top, b_top, is_text):
    return (b_top - a_top) if is_text else (b_top - a_top) / 12.0


def _directly_below(parsed, row):
    is_text = row.page == 0
    gap = _line_gap(parsed.top, row.top, is_text)
    if not (0 < gap <= 1.6):
        return False
    if is_text:
        return True
    # in the value / range columns, i.e. not left of where the result value began
    return row.x0 >= parsed.value_x - 6


def _directly_below_row(above, row):
    if above is None:
        return False
    is_text = row.page == 0
    gap = _line_gap(above.top, row.top, is_text)
    if not (0 < gap <= 1.6):
        return False
    return is_text or abs(row.x0 - above.x0) <= 12


def _join_cut_cells(rows):
    """Carry a name cut at the end of a cell onto the continuation directly below it.

    Two-column dashboards put "... C-REACTIVE PROTEIN (hs-" at the end of one row and
    "CRP): 31.98" at the same x on the next. Whole-row joining cannot see that; the
    continuation is found by column position.
    """
    for i in range(len(rows) - 1):
        row = rows[i]
        if not row.cells:
            continue
        x, text = row.cells[-1]
        if not (text.count("(") > text.count(")") or text.rstrip().endswith("-")):
            continue
        if re.search(r"\d", text.split("(")[-1]):
            continue
        for j in (i + 1, i + 2):
            if j >= len(rows) or not (0 < _line_gap(row.top, rows[j].top, row.page == 0) <= 1.6):
                break
            nxt = rows[j]
            for k, (nx, ntext) in enumerate(nxt.cells):
                if abs(nx - x) <= 12 and re.match(r"^[A-Za-z0-9)\]]", ntext):
                    joiner = "" if text.rstrip().endswith("-") else " "
                    nxt.cells[k] = (x, text.rstrip() + joiner + ntext)
                    row.cells = row.cells[:-1]
                    break
            else:
                continue
            break
    return [r for r in rows if r.cells]


def parse_rows(rows, resolver=None, state=None):
    """Parse a page's rows, repairing results that span two lines.

    `state`, a dict shared across the pages of one document, carries the section heading
    over a page break: a urine microscopy table that continues onto the next page
    ("Bacteria", "Yeast cells") is still under its urine heading, and without it those
    rows were unrecognised. A heading on the new page replaces the carried one rather than
    adding to it, so a new panel never inherits the previous page's specimen.
    """
    out = []
    rows = _join_cut_cells(list(rows))
    pending_name = None          # a name-only line that may be the first half of a name
    pending_row = None
    section = state.get("section") if state else None
    carried = section is not None
    expanded = []
    for r in rows:
        expanded.extend(split_side_by_side(r, resolver))
    rows = expanded

    def in_context(name):
        """The dictionary lookup, told about the heading the row sits under: a bare
        "Epithelial Cells" or "Colour" under a urine heading is the urine test. The same
        rule the normalizer applies, so text rows are not rejected as unknown before
        the normalizer ever sees them."""
        if not resolver:
            return None
        pid = resolver(name)
        if pid is None and section and re.search(r"\burin", section, re.I) and \
                not re.search(r"\burine\b", name, re.I):
            pid = resolver("urine " + name)
        return pid
    in_context.config = getattr(resolver, "config", None) or getattr(resolver, "__self__", None)

    for idx, row in enumerate(rows):
        text = row.text.strip()
        parsed = parse_row(row, in_context if resolver else None)

        # "RBC Morphology" / "Remark | Normocytic Normochromic": a generic label row whose
        # test is named by the heading directly above it. Only for a descriptive test.
        if parsed is None and pending_name is not None and resolver and len(row.cells) >= 2 and \
                re.fullmatch(r"(?:remarks?|comments?|impression|findings?)", row.cells[0][1].strip(" :"), re.I) and \
                _directly_below_row(pending_row, row):
            pid = in_context(pending_name)
            cfg = in_context.config
            if pid and cfg and cfg.param_by_id.get(pid, {}).get("type") == "categorical":
                parsed = Parsed(name=pending_name, value=row.cells[1][1], source=row.source,
                                value_x=row.cells[1][0], top=row.top, x0=row.x0, complete=3)

        # 2a. Further bands of a multi-band reference, one per line below the first. The
        #     name column beside a band line may hold the test's method ("(Method:
        #     Chemiluminescence)   Insufficiency : 20-29"); only the cells in the value /
        #     range columns are the band. Checked before the row is read as a result of its
        #     own: "(Method: Lipase / Glycerol Kinase)   150 - 199 : High" is a band line,
        #     not a test called "(Method: ...)" with a result of 150.
        if out and out[-1].band_label and \
                (parsed is None or not (resolver and in_context(parsed.name))) and \
                out[-1].source.split(",")[0] == row.source.split(",")[0] and \
                0 < _line_gap(out[-1].top, row.top, row.page == 0) <= 1.6 * (1 + out[-1].range.count("\n")):
            right = [t for x, t in row.cells if x >= out[-1].value_x - 6]
            left = [t for x, t in row.cells if x < out[-1].value_x - 6]
            band = "  ".join(right)
            if right and BAND_LINE_RE.match(band) and BAND_WORDS_RE.search(band) and \
                    (not left or (row.page and all(not re.search(r"\d", t) or t.startswith("(")
                                                   for t in left))):
                out[-1].range = out[-1].range + "\n" + re.sub(r"\s{2,}", " ", band)
                continue

        if parsed is not None:
            # 1. Wrapped name: "HIGHLY SENSITIVE C-REACTIVE PROTEIN" / "(hs-CRP) 18.4 mg/L".
            if pending_name is not None and _directly_below_row(pending_row, row):
                merged = "%s %s" % (pending_name, parsed.name)
                own = resolver(parsed.name) if resolver else None
                both = resolver(merged) if resolver else None
                alone = resolver(pending_name) if resolver else None
                # A line visibly cut mid-name - an unclosed bracket, or a trailing hyphen -
                # always continues onto the next: "HIGHLY SENSITIVE C-REACTIVE PROTEIN
                # (hs-" / "CRP) 31.98". A heading never ends that way.
                cut = (pending_name.count("(") > pending_name.count(")")
                       or pending_name.rstrip().endswith("-"))
                if cut:
                    joined = (pending_name + parsed.name) if pending_name.endswith("-") \
                        else merged
                    if not resolver or resolver(joined):
                        parsed.name = joined
                # Join when the two lines together name a test that neither the second
                # half ("PROTEIN (hs-CRP)" -> total protein) nor the first half alone
                # names. That second condition keeps a section heading such as "PROTEIN"
                # from being glued onto the next test.
                elif parsed.name.startswith("(") and not own:
                    parsed.name = merged
                elif both and both != own and both != alone and (
                        not own or resolver(
                            "%s %s" % (pending_name,
                                       re.sub(r"\s*\([^)]*\)", "", parsed.name))) == both):
                    # "HIGHLY SENSITIVE C-REACTIVE" / "PROTEIN (hs-CRP)": the joined words
                    # themselves name the test. "Chemical Examination" / "Reaction (pH)"
                    # only resolved through the bracket, which the heading does not change -
                    # joining it turned urine pH into blood pH.
                    parsed.name = merged
            parsed.section = section
            out.append(parsed)
            pending_name = None
            continue

        # 2. Reference range dropped onto the next line below the result. Only directly
        #    below it and in the value/range columns: a dashboard gauge caption "<13"
        #    elsewhere on the page must not become haemoglobin's reference interval.
        if out and _RANGE_ONLY_RE.match(text) and out[-1].range is None and \
                out[-1].source.split(",")[0] == row.source.split(",")[0] and \
                _directly_below(out[-1], row):
            m = RANGE_TAIL_RE.match(text)
            if m:
                out[-1].range = m.group(0).strip()
                pending_name = None
                continue

        # 2b. A unit printed a fraction of a line below the value it belongs to.
        if out and out[-1].unit is None and len(text.split()) == 1 and "/" in text and \
                UNIT_RE.match(text) and row.page and 0 < (row.top - out[-1].top) <= 6 and \
                row.x0 >= out[-1].value_x - 6:
            out[-1].unit = text
            continue

        # 3. Continuation of a name that was cut off after its result ("... Haemoglobin)").
        if out and text.endswith(")") and out[-1].name.count("(") > out[-1].name.count(")") \
                and _NAME_ONLY_RE.match(text) and len(text.split()) <= 4:
            out[-1].name = "%s %s" % (out[-1].name, text)
            pending_name = None
            continue

        if _NAME_ONLY_RE.match(text) and not META_LABELS.match(text) and \
                len(text.split()) <= 8 and not re.search(r"\d{2,}", text):
            pending_name, pending_row = text, row
            # A name-only line is also a heading candidate. Headings accumulate over the
            # page, so "Microscopic Examination" does not erase "Urine Routine ...".
            if not (resolver and resolver(text)):
                section = (section + " > " + text) if (section and not carried) else text
                carried = False
        else:
            pending_name = None
    if state is not None:
        state["section"] = section
    return out
