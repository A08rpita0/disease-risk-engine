"""Render the ground-truth panel into the PDF layouts real laboratory reports use.

    python tools/make_pdf_fixtures.py

Writes tests/fixtures/pdf/*.pdf. Development-only (needs reportlab); the PDFs are
committed so the test suite itself does not.

Every layout carries EXACTLY the results in tests/fixtures/lab_panel.py, so any
difference between what the engine reads from a PDF and what it reads from the JSON is
an extraction defect, not a data difference.

  text_columns  each cell placed at a column x-position with no ruling lines - the most
                common real layout. pdfplumber's text layer collapses the column gaps to
                single spaces and finds no table, which is what the old line parser
                could not read.
  ruled_table   a gridded table pdfplumber can detect as a table.
  mixed         page 1 a gridded table, page 2 column-placed text. The old extractor
                ran text parsing only when NO table was found anywhere, so one table
                silenced every text-layer result in the document.
  hard          wrapped names, the flag between value and unit, reference range on the
                line below, two results side by side, repeated page headers/footers
                and a patient block full of numbers that are not results.
"""
from __future__ import annotations

import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
from lab_panel import PANEL, PATIENT  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "pdf"
W, H = A4
COLS = (40, 300, 370, 430, 530)          # name, value, unit, range, flag


def _header(c, page):
    c.setFont("Helvetica-Bold", 13)
    c.drawString(40, H - 40, "CITY DIAGNOSTIC LABORATORY")
    c.setFont("Helvetica", 8)
    c.drawString(40, H - 52, "NABL Accredited  |  Plot 12, Sector 4  |  Ph: 022-4000 1234")
    c.setFont("Helvetica", 9)
    c.drawString(40, H - 72, "Patient Name : %s" % PATIENT["name"])
    c.drawString(330, H - 72, "UHID : %s" % PATIENT["uhid"])
    c.drawString(40, H - 84, "Age / Sex : %d Y / %s" % (PATIENT["age"], PATIENT["sex"]))
    c.drawString(330, H - 84, "Collected : 12/03/2026 08:14")
    c.drawString(40, H - 96, "Ref. By : SELF")
    c.drawString(330, H - 96, "Reported : 12/03/2026 17:40")
    c.setFont("Helvetica-Bold", 9)
    y = H - 118
    for x, label in zip(COLS, ("TEST NAME", "RESULT", "UNIT", "BIOLOGICAL REF. INTERVAL", "")):
        c.drawString(x, y, label)
    c.line(40, y - 4, W - 40, y - 4)
    c.setFont("Helvetica", 7)
    c.drawString(40, 30, "This is an electronically authenticated report.  Page %d" % page)
    return y - 18


def text_columns(path, rows=PANEL, pages=2):
    c = canvas.Canvas(str(path), pagesize=A4)
    per_page = (len(rows) + pages - 1) // pages
    section = None
    for p in range(pages):
        y = _header(c, p + 1)
        for sec, name, value, unit, rng, flag, _pid, _abn in rows[p * per_page:(p + 1) * per_page]:
            if sec != section:
                c.setFont("Helvetica-Bold", 9)
                c.drawString(40, y, sec.upper())
                y -= 14
                section = sec
            c.setFont("Helvetica", 9)
            c.drawString(COLS[0], y, name)
            c.drawString(COLS[1], y, value)
            c.drawString(COLS[2], y, unit)
            c.drawString(COLS[3], y, rng)
            c.drawString(COLS[4], y, flag)
            y -= 14
        c.showPage()
    c.save()


def _table_story(rows):
    style = getSampleStyleSheet()["Normal"]
    data = [["Test Name", "Result", "Unit", "Reference Range", "Flag"]]
    for _s, name, value, unit, rng, flag, _pid, _abn in rows:
        data.append([Paragraph(name, style), value, unit, rng, flag])
    t = Table(data, colWidths=[210, 60, 60, 110, 40])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                           ("FONTSIZE", (0, 0), (-1, -1), 8),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey)]))
    return t


def ruled_table(path, rows=PANEL):
    doc = SimpleDocTemplate(str(path), pagesize=A4)
    style = getSampleStyleSheet()["Normal"]
    doc.build([Paragraph("Patient Name : %s &nbsp;&nbsp; Age / Sex : %d Y / %s"
                         % (PATIENT["name"], PATIENT["age"], PATIENT["sex"]), style),
               Spacer(1, 10), _table_story(rows)])


def mixed(path):
    """Page 1 a detectable table, page 2 column-placed text."""
    cbc = [r for r in PANEL if r[0] == "Complete Blood Count"]
    rest = [r for r in PANEL if r[0] != "Complete Blood Count"]
    tmp_table = path.with_name("_mixed_p1.pdf")
    tmp_text = path.with_name("_mixed_p2.pdf")
    ruled_table(tmp_table, cbc)
    text_columns(tmp_text, rest, pages=1)
    from pypdf import PdfWriter, PdfReader  # noqa: F401  (optional)
    writer = PdfWriter()
    for part in (tmp_table, tmp_text):
        for page in PdfReader(str(part)).pages:
            writer.add_page(page)
    with open(path, "wb") as fh:
        writer.write(fh)
    tmp_table.unlink()
    tmp_text.unlink()


def hard(path):
    c = canvas.Canvas(str(path), pagesize=A4)
    rows = list(PANEL)
    half = len(rows) // 2
    for p, chunk in enumerate((rows[:half], rows[half:]), 1):
        y = _header(c, p)
        i = 0
        while i < len(chunk):
            sec, name, value, unit, rng, flag, _pid, _abn = chunk[i]
            c.setFont("Helvetica", 9)
            if len(name) > 30 and "(" in name:
                # Long name wrapped: the bracketed abbreviation drops to the next line,
                # and the RESULT is printed on that second line.
                head, tail = name.split("(", 1)
                c.drawString(COLS[0], y, head.strip())
                y -= 11
                c.drawString(COLS[0], y, "(" + tail)
            else:
                c.drawString(COLS[0], y, name)
            # Flag printed BETWEEN value and unit, as many reports do.
            c.drawString(COLS[1], y, (value + (" " + flag if flag else "")))
            c.drawString(COLS[2] + 18, y, unit)
            if rng and i % 3 == 0:
                # reference interval dropped to the line below the result
                y -= 11
                c.setFont("Helvetica", 8)
                c.drawString(COLS[3], y, rng)
            else:
                c.drawString(COLS[3], y, rng)
            y -= 15
            i += 1
        c.showPage()
    c.save()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    text_columns(OUT / "text_columns.pdf")
    ruled_table(OUT / "ruled_table.pdf")
    hard(OUT / "hard.pdf")
    try:
        mixed(OUT / "mixed.pdf")
    except ImportError:
        print("pypdf not installed - skipping mixed.pdf")
    for f in sorted(OUT.glob("*.pdf")):
        print("wrote", f.relative_to(ROOT), f.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
