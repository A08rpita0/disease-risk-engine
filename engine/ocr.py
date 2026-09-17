"""Optional OCR for scanned PDFs - pages with no text layer.

A photographed or scanned report has no characters to read, and before this the engine
returned nothing for it at all: a troponin of 0.17 ng/mL against a 0.02 limit on a
scanned slip was simply invisible.

This module is OPTIONAL. It runs only when `rapidocr_onnxruntime` and `pypdfium2` are
installed (they are not in requirements.txt: together they add roughly 270 MB, beyond a
serverless function's size limit). Without them the engine says the document needs OCR,
exactly as before.

Design:
  - OCR returns text boxes with their corner points. Each box becomes a "word" with the
    same x0/x1/top/bottom geometry pdfplumber gives, so the SAME row reconstruction and
    value-anchored parser read a scan as read a text PDF. There is no second parser.
  - Nothing read by OCR is trusted silently. Every observation is marked origin="ocr",
    and the document carries a warning to check values against the original. Boxes the
    recogniser itself scored low are dropped rather than guessed.
"""
from __future__ import annotations

MIN_BOX_SCORE = 0.6          # recogniser confidence below which a box is discarded
RENDER_SCALE = 2.0           # pixels per PDF point when rasterising a page


def available():
    """OCR runs only when explicitly enabled AND installed.

    Validated on a real photographed report it read 31 of 39 results, but it also read a
    troponin slip's reference line ("0.00-0.02") in place of the result and so called an
    abnormal troponin normal. A misread that CLEARS a critical result is worse than an
    honest "this page could not be read", so it is off unless DRE_ENABLE_OCR=1.
    """
    import os
    if os.environ.get("DRE_ENABLE_OCR") != "1":
        return False
    try:
        import pypdfium2                    # noqa: F401
        from rapidocr_onnxruntime import RapidOCR   # noqa: F401
        return True
    except Exception:
        return False


_ENGINE = None


def _engine():
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR
        _ENGINE = RapidOCR()
    return _ENGINE


def page_words(data, pages_without_text):
    """{page_no: [word dicts]} for the listed 1-based pages, in PDF point coordinates."""
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(bytes(data))
    out = {}
    for pageno in pages_without_text:
        page = pdf[pageno - 1]
        image = page.render(scale=RENDER_SCALE).to_pil()
        result, _elapsed = _engine()(image)
        words = []
        for box, text, score in result or []:
            if not text or not str(text).strip() or float(score) < MIN_BOX_SCORE:
                continue
            xs = [pt[0] / RENDER_SCALE for pt in box]
            ys = [pt[1] / RENDER_SCALE for pt in box]
            words.append({"text": str(text).strip(), "x0": min(xs), "x1": max(xs),
                          "top": min(ys), "bottom": max(ys), "ocr_score": float(score)})
        out[pageno] = (words, page.get_width())
    return out
