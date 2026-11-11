from datetime import timedelta
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .owner_payroll import MOVEMENT_LABELS, movement_cut_label, pay_totals
from .routes import localize_datetime


NAVY = HexColor("#13223A")
GREEN = HexColor("#116759")
GRAY = HexColor("#52627A")
LINE = HexColor("#E1E7ED")
LIGHT = HexColor("#F3F5F8")


def money(amount):
    return f"{'-' if amount < 0 else ''}${abs(amount):,.2f}"


def build_weekly_pay_pdf(pay, movements, cuts):
    output = BytesIO()
    doc = SimpleDocTemplate(
        output, pagesize=letter, leftMargin=42, rightMargin=42, topMargin=42, bottomMargin=46,
        title="Pago semanal - Estacionamiento Romina", author="Estacionamiento Romina",
    )
    title = ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=25, leading=31, textColor=NAVY)
    section = ParagraphStyle("section", fontName="Helvetica-Bold", fontSize=13, leading=17, textColor=NAVY)
    body = ParagraphStyle("body", fontName="Helvetica", fontSize=9, leading=13, textColor=NAVY)
    small = ParagraphStyle("small", parent=body, fontSize=8, leading=11, textColor=GRAY)
    amount = ParagraphStyle("amount", parent=body, fontName="Helvetica-Bold", alignment=TA_RIGHT)
    total_style = ParagraphStyle("total", parent=body, fontName="Helvetica-Bold", fontSize=13, leading=17, textColor=white)
    total_amount = ParagraphStyle("total_amount", parent=total_style, alignment=TA_RIGHT)
    heading = ParagraphStyle("heading", parent=body, fontName="Helvetica-Bold", textColor=white)
    heading_amount = ParagraphStyle("heading_amount", parent=heading, alignment=TA_RIGHT)

    sunday = pay.week_start + timedelta(days=6)
    totals = pay_totals(pay, movements)
    net = totals["net"]
    net_label = "Saldo pendiente" if net is not None and net < 0 else "Total a pagar"
    net_value = money(abs(net)) if net is not None else "Sueldo por capturar"
    totals_table = Table([[Paragraph(net_label, total_style), Paragraph(net_value, total_amount)]],
                         colWidths=[doc.width * .55, doc.width * .45])
    totals_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), GREEN),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]))

    salary = money(totals["salary"]) if totals["salary"] is not None else "Pendiente"
    summary_rows = [[Paragraph("Sueldo base", body), Paragraph(salary, amount)]]
    for key, label in MOVEMENT_LABELS.items():
        value = totals["amounts"][key]
        if key != "reimbursement":
            value = -value
        summary_rows.append([Paragraph(label, body), Paragraph(money(value), amount)])
    summary = Table(summary_rows, colWidths=[doc.width * .72, doc.width * .28])
    summary.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -1), .5, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))

    story = [
        Paragraph("ESTACIONAMIENTO ROMINA", small),
        Spacer(1, 6),
        Paragraph("Pago semanal", title),
        Paragraph(f"<b>{escape(pay.employee_name)}</b>", section),
        Spacer(1, 4),
        Paragraph(f"{pay.week_start:%d/%m/%Y} al {sunday:%d/%m/%Y} · MXN", body),
        Spacer(1, 18),
        totals_table,
        Spacer(1, 9),
        summary,
        Spacer(1, 18),
        Paragraph("Detalle de movimientos", section),
        Spacer(1, 6),
    ]
    if movements:
        rows = [[Paragraph("Fecha", heading), Paragraph("Concepto", heading),
                 Paragraph("Corte asignado", heading), Paragraph("Importe", heading_amount)]]
        for movement in reversed(movements):
            label = MOVEMENT_LABELS[movement.kind] + (" · Anulado" if movement.voided_at else "")
            rows.append([
                Paragraph(localize_datetime(movement.created_at).strftime("%d/%m/%Y"), small),
                Paragraph(f"<b>{escape(label)}</b><br/>{escape(movement.description)}", body),
                Paragraph(escape(movement_cut_label(movement, cuts)), small),
                Paragraph(money(movement.signed_amount), amount),
            ])
        detail = LongTable(rows, colWidths=[72, 214, 152, 90], repeatRows=1)
        detail.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, LIGHT]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(detail)
    else:
        story.append(Paragraph("Sin movimientos en esta semana.", body))

    def footer(canvas, document):
        canvas.setStrokeColor(LINE)
        canvas.line(42, 33, letter[0] - 42, 33)
        canvas.setFillColor(GRAY)
        canvas.setFont("Helvetica", 8)
        canvas.drawString(42, 21, "Estacionamiento Romina")
        canvas.drawRightString(letter[0] - 42, 21, f"Página {document.page}")

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    output.seek(0)
    return output
