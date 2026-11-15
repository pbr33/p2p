"""Writes demo/documents/acme-wave2-company-codes.xlsx: the kind of workbook a client sends.

Deliberately imperfect, like the real thing: a title row above the header, country and
currency names instead of codes, Indian number formats ("15,00,000", "2 lakh"), one
finance approver still "TBC", a tax matrix on a separate sheet — and IN40's limit is ten
times its peers (a likely lakh/crore slip the consistency check should catch).

    python demo/make_client_workbook.py
"""

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

OUT = Path(__file__).parent / "documents" / "acme-wave2-company-codes.xlsx"
BOLD = Font(bold=True)
HEAD = PatternFill("solid", fgColor="DDE7FF")


def main() -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Wave 2 company codes"
    ws.append(["ACME Group — S/4HANA Wave 2: company codes for Coupa invoicing (draft v3, from Finance)"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([])
    ws.append(["Co. Code", "Legal entity", "Country", "Local currency", "AP approver (Coupa login)",
               "Finance controller (Coupa login)", "Finance approval above", "Comments"])
    for c in ws[3]:
        c.font, c.fill = BOLD, HEAD
    ws.append(["IN40", "Acme Chennai Pvt Ltd", "India", "Indian Rupee", "ap.india", "cfo.india", "15,00,000", ""])
    ws.append(["IN50", "Acme Hyderabad Pvt Ltd", "India", "INR", "ap.india", "TBC", "2 lakh",
               "Finance controller to be confirmed by Group Finance"])
    ws.append(["SG20", "Acme Asia Holdings Pte Ltd", "Singapore", "SGD", "ap.sg", "apac.controller", "50000", ""])
    ws.append(["MY10", "Acme Malaysia Sdn Bhd", "Malaysia", "Ringgit", "ap.my", "cfo.my", "80,000",
               "New country for Coupa — users still to be created"])
    for col, width in zip("ABCDEFGH", (10, 28, 12, 15, 26, 30, 22, 48)):
        ws.column_dimensions[col].width = width

    tax = wb.create_sheet("Tax matrix")
    tax.append(["Country", "Tax code", "Rate %", "Description", "Use for"])
    for c in tax[1]:
        c.font, c.fill = BOLD, HEAD
    for row in (
        ["India", "GST18", 18, "GST 18%", "standard goods & services"],
        ["India", "GST5", 5, "GST 5%", "reduced rate"],
        ["India", "GST12", 12, "GST 12%", "IN50 only (pharma supplies)"],
        ["Singapore", "GST9", 9, "Singapore GST 9%", "all"],
        ["Malaysia", "SST10", 10, "SST 10%", "sales tax"],
        ["Malaysia", "SST8", 8, "Service tax 8%", "services (was 6% until Feb 2024)"],
    ):
        tax.append(row)
    tax.append([])
    tax.append(["Note: Tax team to confirm whether IN40 also needs GST12."])
    for col, width in zip("ABCDE", (12, 10, 8, 20, 34)):
        tax.column_dimensions[col].width = width

    OUT.parent.mkdir(parents=True, exist_ok=True)
    wb.save(OUT)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
