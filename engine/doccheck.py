"""Stage 0 - is this actually a laboratory report?

A generic document (an invoice, a CV, a letter) must be turned away with a clear
message rather than pushed through the engine to produce an empty dashboard.

The judgement is deliberately conservative in one direction: a real report with only a
couple of tests on it is still a real report, so a low parameter count alone is never
enough to reject. What separates a sparse lab report from an unrelated document is
whether the recognised values look like *measurements* - they carry units, reference
ranges, or sit in a document that talks like a lab report.

Verdicts:
  ok            -> analyse it
  unreadable    -> no text could be pulled out at all (scanned image, empty, encrypted)
  not_a_report  -> text was read, but it is not a laboratory report
"""
from __future__ import annotations

import re

# Wording that appears on laboratory reports and almost nowhere else. Matched against
# the raw document text, lowercased.
LAB_PHRASES = (
    "reference range", "reference interval", "reference value", "biological reference",
    "normal range", "bio. ref", "bio ref",
    "specimen", "sample type", "sample id", "collected on", "collection date",
    "reported on", "report date", "test name", "test report", "investigation",
    "laboratory", "lab no", "lab id", "pathology", "diagnostics",
    "haematology", "hematology", "biochemistry", "serology", "clinical pathology",
    "units", "u/l", "mg/dl", "mmol/l", "g/dl", "ng/ml", "iu/ml", "cells/cumm",
    "haemoglobin", "hemoglobin", "creatinine", "cholesterol", "glucose",
    "referred by", "registered on", "panel", "profile",
)

# A recognised parameter counts as a real measurement if it carries any of these.
def _is_measurement(p):
    return bool(p.unit) or p.reference_low is not None or p.reference_high is not None


def assess(patient, observations, raw_text, warnings):
    """Return a verdict dict. Never raises - a bad document is data, not an error."""
    params = list(patient.parameters.values())
    recognised = len(params)
    measured = sum(1 for p in params if _is_measurement(p))
    text = (raw_text or "").lower()

    phrase_hits = sorted({w for w in LAB_PHRASES if w in text})

    # Where a result came from is itself evidence. A JSON test object, a CSV row or a
    # PDF table is a deliberate "this is a result" structure that prose never has, so a
    # structured source needs no corroboration. Free text does.
    structured = any(getattr(o, "source_kind", "") in ("json", "csv", "pdf")
                     for o in observations)

    signals = {
        "observations_extracted": len(observations),
        "parameters_recognised": recognised,
        "parameters_with_unit_or_range": measured,
        "structured_source": structured,
        "report_phrases_found": len(phrase_hits),
        "example_phrases": phrase_hits[:6],
        "text_characters": len(raw_text or ""),
    }

    # --- nothing readable came out of the file at all ---
    # A scanned PDF says so in its own warning; otherwise the file is essentially blank.
    # A file that yielded *some* text is not unreadable - it is simply not a report,
    # and telling the user to run OCR on it would be wrong advice.
    no_text_layer = any("no extractable text layer" in str(w).lower()
                        for w in (warnings or []))
    if no_text_layer or (not observations and recognised == 0 and not text.strip()):
        # No suggestion to run it through OCR: on a real photographed report OCR read a
        # troponin slip's reference line as the result, which is worse than no answer.
        return _verdict(
            "unreadable", signals,
            ("This PDF appears to be scanned/image-based and could not be reliably read."
             if no_text_layer else "No readable text could be extracted from this file."),
            "Please upload a digital/text-based PDF or JSON. Nothing from this file has been "
            "analysed, so no result - normal or abnormal - is shown for it.")

    # --- text was read, but nothing in it is a known test ---
    if recognised == 0:
        return _verdict(
            "not_a_report", signals,
            "This does not look like a laboratory report.",
            "No recognised health parameters were found in this document. Upload a "
            "pathology or diagnostic report containing test names and their results.")

    # --- a stray keyword match in an unrelated document ---
    # e.g. a recipe mentioning "sugar", a CV listing "Iron Mountain". Real reports put
    # units or reference ranges next to their numbers, and read like reports.
    if measured == 0 and not structured and len(phrase_hits) < 2:
        return _verdict(
            "not_a_report", signals,
            "This does not look like a laboratory report.",
            "A few words matched the names of medical tests, but none of them had a unit "
            "or a reference range beside them and the document does not read like a "
            "report. Upload a pathology or diagnostic report instead.")

    verdict = _verdict("ok", signals, None, None)
    # Some pages were images: the analysis is real but INCOMPLETE, and must say so where
    # the user will see it - not only in a data-quality list - so nobody reads a partial
    # result as the whole report.
    for w in warnings or []:
        m = re.search(r"(\d+) page\(s\) of this PDF have no text layer", str(w))
        if m:
            verdict["incomplete"] = {
                "unreadable_pages": int(m.group(1)),
                "message": ("Part of this PDF appears to be scanned/image-based: %s page(s) "
                            "could not be reliably read, and any results printed on them are "
                            "NOT included below. Please upload a digital/text-based PDF or "
                            "JSON for a complete analysis." % m.group(1)),
            }
    return verdict


def _verdict(status, signals, title, guidance):
    return {
        "status": status,
        "is_report": status == "ok",
        "title": title,
        "guidance": guidance,
        "signals": signals,
        "accepted_formats": [
            "Pathology or diagnostic report (PDF with selectable text)",
            "Laboratory results exported as CSV or TXT",
            "Standardised patient JSON with test names, values and units",
        ],
    }
