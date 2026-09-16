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


def summary_then_lab(path):
    """A designed summary page followed by laboratory pages. See
    tests/fixtures/summary_lab_report.py for the traits this reproduces."""
    import summary_lab_report as S

    style = getSampleStyleSheet()["Normal"]
    style.fontSize, style.leading = 8, 9.5
    c = canvas.Canvas(str(path), pagesize=A4)
    widths = [220, 80, 80, 150]
    x0 = 40

    # ---------------- page 1: designed summary, no patient labels ----------------
    c.setFont("Helvetica-Bold", 16)
    c.drawString(x0, H - 50, "SMART HEALTH SUMMARY")
    c.setFont("Helvetica", 8)
    c.drawString(x0, H - 72, "Patient ID")
    c.drawString(200, H - 72, "Age")
    c.setFont("Helvetica-Bold", 10)
    c.drawString(x0, H - 84, S.PATIENT["id"])
    c.drawString(200, H - 84, str(S.PATIENT["age"]))
    grid = [["Profile", "Abnormal / Total", "Key Results"]]
    for name, value, unit, normal in S.SUMMARY_TILES[:2]:
        grid.append(["Vitamin Profile" if "Vitamin" in name else "Liver Profile", "1 / 2",
                     Paragraph("%s: %s %s (Normal: %s)" % (name, value, unit, normal), style)])
    grid.append(["Mineral Profile", "0 / 1", "All Normal"])
    t = Table(grid, colWidths=[120, 80, 310])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                           ("FONTSIZE", (0, 0), (-1, -1), 8)]))
    _w, h = t.wrapOn(c, W, H)
    t.drawOn(c, x0, H - 120 - h)
    y = H - 160 - h
    name, value, unit, _n = S.SUMMARY_TILES[2]
    c.setFont("Helvetica", 9)
    c.drawString(x0, y, "%s: %s %s" % (name, value, unit))
    c.drawString(320, y, "HIGHLY SENSITIVE C-REACTIVE PROTEIN (hs-")
    c.drawString(320, y - 14, "CRP): 24.60")
    c.drawString(520, y - 14, "HIGH")
    c.drawString(x0, y - 22, "NORMAL")
    c.drawString(160, y - 22, "HIGH")
    c.drawString(x0, y - 34, "< 100")
    c.showPage()

    # ---------------- laboratory pages ----------------
    def patient_box(top):
        rows = [["Patient NAME : %s" % S.PATIENT["name"], "", "", ""],
                ["DOB/Age/Gender : %d Y/%s" % (S.PATIENT["age"], S.PATIENT["sex"]), "", "", ""],
                ["Patient ID / UHID : %s" % S.PATIENT["id"], "", "", ""],
                ["Test Description", "Value(s)", "Unit(s)", "Reference Range"]]
        tb = Table(rows, colWidths=widths)
        tb.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.8, colors.black),
                                ("GRID", (0, 3), (-1, 3), 0.8, colors.black),
                                ("FONTSIZE", (0, 0), (-1, -1), 8)]))
        _w, hh = tb.wrapOn(c, W, H)
        tb.drawOn(c, x0, top - hh)
        c.setFont("Helvetica-Bold", 12)
        c.drawString(x0, H - 40, "LABORATORY REPORT")
        return top - hh - 14

    def results_grid(top, rows):
        data = []
        for kind, item in rows:
            if kind == "heading":
                data.append([Paragraph("<b>%s</b>" % item, style), "", "", ""])
                continue
            section, name, method, value, unit, rng = item
            label = name.replace("(hs-CRP)", "(hs-<br/>CRP)") if "hs-CRP" in name else name
            if method:
                label += "<br/><i>%s</i>" % method
            data.append([Paragraph(label, style), value, unit,
                         Paragraph(rng.replace("\n", "<br/>"), style)])
        tb = Table(data, colWidths=widths)
        tb.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                                ("FONTSIZE", (0, 0), (-1, -1), 8),
                                ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        _w, hh = tb.wrapOn(c, W, H)
        tb.drawOn(c, x0, top - hh)
        return top - hh

    def rows_for(prefix, skip=()):
        out, last = [], None
        for item in S.TESTS:
            sec = item[0]
            if not sec.startswith(prefix) or item[1] in skip:
                continue
            for part in sec.split(" > "):
                if part != last and (not out or part not in [r[1] for r in out if r[0] == "heading"]):
                    out.append(("heading", part))
            last = sec.split(" > ")[-1]
            out.append(("test", item))
        return out

    # page 2: CBC, with the absolute basophils row falling across the page break
    top = patient_box(H - 60)
    cbc = [r for r in rows_for("Complete Blood Count")] + rows_for("Differential") + \
        rows_for("Absolute", skip=(S.PAGE_BREAK_ROW,))
    bottom = results_grid(top, cbc)
    split = [t for t in S.TESTS if t[1] == S.PAGE_BREAK_ROW][0]
    c.setFont("Helvetica", 8)
    c.drawString(x0 + 3, bottom - 12, split[1])
    c.drawString(x0 + widths[0] + 3, bottom - 12, split[3])
    c.drawString(x0 + widths[0] + widths[1] + 3, bottom - 12, split[4])
    c.drawString(x0 + sum(widths[:3]) + 3, bottom - 12, split[5])
    c.showPage()

    # page 3: continuation - the method line of the split row, then platelets
    top = patient_box(H - 60)
    cont = Table([[Paragraph("<i>%s</i>" % split[2], style), "", "", ""]], colWidths=widths)
    cont.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey)]))
    _w, hh = cont.wrapOn(c, W, H)
    cont.drawOn(c, x0, top - hh)
    results_grid(top - hh - 4, rows_for("Platelet"))
    c.showPage()

    # page 4: HbA1c, liver, kidney
    top = patient_box(H - 60)
    results_grid(top, rows_for("HbA1C") + rows_for("Liver") + rows_for("Kidney"))
    c.showPage()

    # page 5: hs-CRP and vitamin D, then an interpretation table with 4 columns
    top = patient_box(H - 60)
    bottom = results_grid(top, rows_for("High Sensitivity") + rows_for("Vitamin D"))
    interp = Table([["TSH", "T4", "T3", "Interpretation"],
                    ["High", "Normal", "Normal", "Mild (subclinical) hypothyroidism"],
                    ["Low", "High", "High", "Hyperthyroidism"]], colWidths=widths)
    interp.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                                ("FONTSIZE", (0, 0), (-1, -1), 8)]))
    _w, hh = interp.wrapOn(c, W, H)
    interp.drawOn(c, x0, bottom - 60 - hh)
    c.showPage()

    # page 6: urine routine with sub-headings
    top = patient_box(H - 60)
    results_grid(top, rows_for("Urine Routine"))
    c.showPage()

    # page 7: HPLC peak table
    c.setFont("Helvetica-Bold", 10)
    c.drawString(x0, H - 60, "HbA1c HPLC chromatogram")
    peaks = Table([["Peak Name", "NGSP %", "Area %", "Retention Time (min)", "Peak Area"],
                   ["A1b", "---", "1.7", "0.222", "42556"],
                   ["A1c", "6.3", "---", "0.495", "115559"]],
                  colWidths=[90, 70, 70, 120, 90])
    peaks.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                               ("FONTSIZE", (0, 0), (-1, -1), 8)]))
    _w, hh = peaks.wrapOn(c, W, H)
    peaks.drawOn(c, x0, H - 80 - hh)
    c.showPage()
    c.save()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    text_columns(OUT / "text_columns.pdf")
    ruled_table(OUT / "ruled_table.pdf")
    hard(OUT / "hard.pdf")
    # kept apart: it is a different panel from lab_panel.py, compared against its own JSON
    (ROOT / "tests" / "fixtures" / "pdf_summary").mkdir(parents=True, exist_ok=True)
    summary_then_lab(ROOT / "tests" / "fixtures" / "pdf_summary" / "summary_then_lab.pdf")
    try:
        mixed(OUT / "mixed.pdf")
    except ImportError:
        print("pypdf not installed - skipping mixed.pdf")
    for f in sorted(OUT.glob("*.pdf")):
        print("wrote", f.relative_to(ROOT), f.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
