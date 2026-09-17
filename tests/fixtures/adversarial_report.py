"""A synthetic report built to break extraction. Invented subject and values.

    python tests/fixtures/adversarial_report.py      (needs reportlab; writes the PDF)

Traits, each a way a real laboratory layout can put a number where a result is not:
  - a header block on EVERY page with an age, lab ID, sample number, barcode, phone
    number, collection/report dates and times and a referring doctor's number
  - a footer "Page 1 of 3", a report number and an accreditation number
  - an interpretation paragraph full of analyte names followed by numbers and units
    ("HbA1c of 6.5 % or higher", "Troponin I above 0.04 ng/mL", "Potassium 3.5 - 5.1")
  - a table continued on the next page under a repeated column header
  - the same test printed on two pages with the same result
  - ratios written without the word "ratio": "LDL/HDL", "Albumin/Globulin"
  - a TSH printed in "mIU/mL" with an interval in the same numbers (a unit misprint)
  - two HIV components, one equivocal
  - Indian-grouped counts, a lakh unit, labelled multi-line bands
  - a closing note "Sample received on 20/08/2025 ... Method: CLIA 2026"
"""
from pathlib import Path

OUT = Path(__file__).resolve().parent / "pdf_formats" / "adversarial_layout.pdf"


def build(path=OUT):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    w, h = A4

    def header(c, page, total):
        c.setFont("Helvetica", 9)
        c.drawString(40, h - 40, "Patient Name : TEST SUBJECT          Age/Sex : 36 Years / Female")
        c.drawString(40, h - 54, "Lab ID : 2026   Sample No : 36   Barcode : 123456789   Phone : 9876543210")
        c.drawString(40, h - 68, "Collected : 20/08/2025 10:30   Reported : 21/08/2025 12:45   Ref. Dr : 140")
        c.drawString(40, 40, "Page %d of %d        Report No: 20082025        Accreditation No. MC-2345"
                     % (page, total))
        c.setFont("Helvetica-Bold", 9)
        for x, t in ((40, "Test Name"), (250, "Result"), (320, "Unit"), (400, "Bio. Ref. Interval")):
            c.drawString(x, h - 95, t)
        c.setFont("Helvetica", 9)

    def row(c, y, name, val, unit, rng, flag=None):
        c.drawString(40, y, name)
        c.drawString(250, y, val + ("  " + flag if flag else ""))
        c.drawString(320, y, unit)
        for i, part in enumerate(rng.split("\n")):
            c.drawString(400, y - 11 * i, part)
        return 16 + 11 * rng.count("\n")

    c = canvas.Canvas(str(path), pagesize=A4)
    header(c, 1, 3)
    y = h - 115
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y, "HAEMATOLOGY")
    c.setFont("Helvetica", 9)
    y -= 16
    for r in (("Haemoglobin", "11.2", "g/dL", "12.0 - 15.0", "L"),
              ("Total Leucocyte Count", "7,800", "cells/cumm", "4000 - 11000"),
              ("Platelet Count", "1.2", "lakh/cumm", "1.5 - 4.1"),
              ("Lymphocytes", "42", "%", "20 - 40", "H"),
              ("Absolute Lymphocyte Count", "3276", "cells/cumm", "1000 - 3000")):
        y -= row(c, y, *r)
    y -= 10
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y, "BIOCHEMISTRY")
    c.setFont("Helvetica", 9)
    y -= 16
    for r in (("Glucose Fasting", "92", "mg/dL", "70 - 100"),
              ("HbA1c", "5.9", "%", "Non-diabetic: < 5.7\nPrediabetic: 5.7 - 6.4\nDiabetic: >= 6.5")):
        y -= row(c, y, *r)
    y -= 8
    for text in ("Interpretation: HbA1c of 6.5 % or higher on two occasions indicates diabetes. A fasting glucose",
                 "above 126 mg/dL is diagnostic. TSH 0.4 - 4.0 uIU/mL is the adult interval; Troponin I above 0.04 ng/mL",
                 "suggests myocardial injury. Potassium 3.5 - 5.1 mmol/L. Creatinine 1.3 mg/dL in males."):
        c.drawString(40, y, text)
        y -= 12
    c.showPage()

    header(c, 2, 3)
    y = h - 115
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y, "BIOCHEMISTRY (continued)")
    c.setFont("Helvetica", 9)
    y -= 16
    for r in (("Cholesterol - Total", "210", "mg/dL", "Desirable: < 200\nBorderline: 200 - 239\nHigh: >= 240"),
              ("HDL Cholesterol", "38", "mg/dL", "Low: < 40\nHigh: >= 60"),
              ("Chol/HDL Ratio", "5.5", "", "< 5.0"),
              ("LDL/HDL", "3.9", "", "< 3.5"),
              ("Albumin/Globulin", "1.6", "", "1.1 - 2.2"),
              ("Haemoglobin", "11.2", "g/dL", "12.0 - 15.0", "L")):
        y -= row(c, y, *r)
    c.showPage()

    header(c, 3, 3)
    y = h - 115
    for r in (("TSH", "2.10", "mIU/mL", "0.40 - 4.00"),
              ("HIV 1 Antibody", "Non Reactive", "", "Non Reactive"),
              ("HIV 2 Antibody", "Equivocal", "", "Non Reactive"),
              ("Vitamin D (25-OH)", "18.5", "ng/mL", "Deficient: < 20\nInsufficient: 20 - 29\nSufficient: 30 - 100")):
        y -= row(c, y, *r)
    y -= 10
    c.drawString(40, y, "Note: Sample received on 20/08/2025. Result verified by Dr. 36. Method: CLIA 2026.")
    y -= 12
    c.drawString(40, y, "*** End of Report ***")
    c.save()
    return path


if __name__ == "__main__":
    print(build())
