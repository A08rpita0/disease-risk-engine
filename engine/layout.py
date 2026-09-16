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
    r"|(?P<cmp>[<>≤≥]=?|up\s*to|upto|less\s*than|more\s*than|greater\s*than)\s*(?P<one>%(n)s)"
    r")" % {"n": _NUM}, re.I)


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

    low = t.lower().strip(".,:;")
    if nxt is not None:
        pair = (low, nxt.lower().strip(".,:;"))
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
    if "/" in first and not re.search(r"[A-Za-z]{3,}", first.split("/")[0]):
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


def _parse_tail(tail):
    """Classify what follows the value: flag, unit and reference range, in any order."""
    unit = rng = flag = None
    # "(13.0-17.0)" / "[0.5 - 5.0]": a bracketed reference interval is still one.
    rest = []
    for tok in tail:
        inner = tok.strip("()[]")
        rest.append(inner if (inner != tok and re.search(r"\d", inner)) else tok)
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
        # A qualitative reference ("Negative", "Non Reactive") for a categorical test.
        # Checked before units, which would otherwise take the word "Non".
        if rng is None and tok.lower().strip(".") in QUALITATIVE_WORDS | {"non"}:
            if i + 1 < len(rest) and (tok.lower(), rest[i + 1].lower()) in QUALITATIVE_PHRASES:
                rng = "%s %s" % (tok, rest[i + 1])
                i += 2
                continue
            if tok.lower().strip(".") in QUALITATIVE_WORDS:
                rng = tok
                i += 1
                continue
        if unit is None and UNIT_RE.match(tok) and tok.lower() not in UNIT_WORDS_NOT_UNITS \
                and not _is_numeric(tok) and len(tok) <= 18 and (rng is None):
            unit = tok
            i += 1
            if "/" in unit and i < len(rest) and re.match(r"^(?:hr|hrs|h|hour|min|m2|m²)$",
                                                          rest[i], re.I):
                unit = "%s %s" % (unit, rest[i])
                i += 1
            continue
        if unit is None and rng is not None and UNIT_RE.match(tok) and len(tok) <= 18 \
                and tok.lower() not in UNIT_WORDS_NOT_UNITS and not _is_numeric(tok) \
                and _norm_flag(tok) is None:
            # "70 - 100 mg/dL": unit after the range
            unit = tok
            i += 1
            continue
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
            elif pid in seen_pids:
                # "HBsAg Non Reactive": "HBsAg Non" trims back to HBsAg. The extra word
                # belongs to the result, so this split is worse, not better.
                s -= 6
            seen_pids.append(pid)
        if starts[i]:
            s += 4                              # the value opens its own column
        if _is_numeric(toks[i]) or COMPARATOR_RE.match(toks[i]):
            s += 1
        tail = toks[i + used:]
        if tail and _is_numeric(tail[0]) and not RANGE_TAIL_RE.match(" ".join(tail)):
            s -= 5                              # "125 34.5 U/mL": 125 is not the result
        u, r, _fl = _parse_tail(tail)
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
    return Parsed(name=name, value=value, unit=unit, range=rng, flag=flag,
                  source=row.source, value_x=value_x, top=row.top, x0=row.x0,
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


def parse_rows(rows, resolver=None):
    """Parse a page's rows, repairing results that span two lines."""
    out = []
    pending_name = None          # a name-only line that may be the first half of a name
    pending_row = None
    expanded = []
    for r in rows:
        expanded.extend(split_side_by_side(r, resolver))
    rows = expanded
    for idx, row in enumerate(rows):
        text = row.text.strip()
        parsed = parse_row(row, resolver)

        if parsed is not None:
            # 1. Wrapped name: "HIGHLY SENSITIVE C-REACTIVE PROTEIN" / "(hs-CRP) 18.4 mg/L".
            if pending_name is not None and _directly_below_row(pending_row, row):
                merged = "%s %s" % (pending_name, parsed.name)
                own = resolver(parsed.name) if resolver else None
                both = resolver(merged) if resolver else None
                alone = resolver(pending_name) if resolver else None
                # Join when the two lines together name a test that neither the second
                # half ("PROTEIN (hs-CRP)" -> total protein) nor the first half alone
                # names. That second condition keeps a section heading such as "PROTEIN"
                # from being glued onto the next test.
                if parsed.name.startswith("(") and not own:
                    parsed.name = merged
                elif both and both != own and both != alone:
                    parsed.name = merged
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

        # 3. Continuation of a name that was cut off after its result ("... Haemoglobin)").
        if out and text.endswith(")") and out[-1].name.count("(") > out[-1].name.count(")") \
                and _NAME_ONLY_RE.match(text) and len(text.split()) <= 4:
            out[-1].name = "%s %s" % (out[-1].name, text)
            pending_name = None
            continue

        if _NAME_ONLY_RE.match(text) and not META_LABELS.match(text) and \
                len(text.split()) <= 8 and not re.search(r"\d{2,}", text):
            pending_name, pending_row = text, row
        else:
            pending_name = None
    return out
