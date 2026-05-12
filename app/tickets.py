from io import BytesIO
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.graphics import renderPDF, renderSVG
from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing
from reportlab.pdfgen import canvas

from .pricing import format_duration


def build_ticket_pdf(record, datetime_formatter, qr_value=None):
    page_width = 80 * mm
    margin_x = 5 * mm
    content_width = page_width - (margin_x * 2)

    notes_lines = _wrap_text(record.notes, 28) if record.notes else []
    buffer = BytesIO()
    estimated_height = 130 * mm
    estimated_height += len(notes_lines) * 4.8 * mm
    if qr_value:
        estimated_height += 34 * mm

    pdf = canvas.Canvas(buffer, pagesize=(page_width, estimated_height))
    width, height = page_width, estimated_height

    gold = HexColor("#bf8a28")
    blue = HexColor("#0f436b")
    ink = HexColor("#2f2416")
    muted = HexColor("#7b6950")

    pdf.setTitle(f"Ticket {record.display_ticket_number}")
    pdf.setFillColorRGB(1, 1, 1)
    pdf.setStrokeColor(gold)
    pdf.roundRect(2 * mm, 2 * mm, width - 4 * mm, height - 4 * mm, 3 * mm, stroke=1, fill=1)

    logo_path = Path(__file__).resolve().parent.parent / "public" / "logo.jpg"
    header_top = height - 8 * mm
    if logo_path.exists():
        pdf.drawImage(
            ImageReader(str(logo_path)),
            margin_x,
            height - 24 * mm,
            14 * mm,
            14 * mm,
            preserveAspectRatio=True,
            mask="auto",
        )

    pdf.setFillColor(blue)
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(margin_x + 17 * mm, header_top - 2 * mm, "Estacionamiento Romina")
    pdf.setFillColor(muted)
    pdf.setFont("Helvetica", 7)
    pdf.drawString(margin_x + 17 * mm, header_top - 6 * mm, "Izamal, Yucatan")
    pdf.drawString(margin_x + 17 * mm, header_top - 10 * mm, "Ticket de servicio")
    pdf.line(margin_x, height - 27 * mm, width - margin_x, height - 27 * mm)

    fields = [
        ("Ficha", record.display_ticket_number),
        ("Cliente", record.client_name),
        ("Vehiculo", record.vehicle_type),
        ("Placas / ID", record.plate_number),
        ("Entrada", datetime_formatter(record.entry_at)),
        ("Estado", record.status),
        ("Registro por", record.entry_user.full_name),
        ("Salida", datetime_formatter(record.exit_at) if record.exit_at else "Pendiente"),
        ("Tarifa aplicada", record.applied_rate_label or "Se calcula al registrar salida"),
        ("Servicios", record.services_label),
        ("Tiempo", format_duration(record.duration_seconds) if record.duration_seconds else "En curso"),
        ("Total", f"${float(record.total_amount):,.2f}"),
    ]

    x = margin_x
    y = height - 33 * mm
    for label, value in fields:
        pdf.setFillColor(ink)
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(x, y, f"{label}:")
        pdf.setFont("Helvetica", 8)
        value_x = x + 22 * mm
        wrapped_value = _wrap_text(str(value), 24)
        line_y = y
        for index, value_line in enumerate(wrapped_value):
            if index == 0:
                pdf.drawString(value_x, line_y, value_line)
            else:
                line_y -= 4.2 * mm
                pdf.drawString(value_x, line_y, value_line)
        y = line_y - 5.2 * mm

    if notes_lines:
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(margin_x, y, "Notas:")
        text = pdf.beginText(margin_x, y - 5 * mm)
        text.setFont("Helvetica", 8)
        text.setFillColor(ink)
        for line in notes_lines:
            text.textLine(line)
        pdf.drawText(text)
        y -= (len(notes_lines) + 1) * 4.8 * mm

    if qr_value:
        qr_size = 24 * mm
        qr_x = (width - qr_size) / 2
        qr_y = y - 28 * mm
        draw_qr_on_canvas(pdf, qr_value, qr_x, qr_y, qr_size)
        pdf.setFillColor(ink)
        pdf.setFont("Helvetica-Bold", 7)
        pdf.drawCentredString(width / 2, qr_y - 3 * mm, "Escanea para abrir este ticket")
        y = qr_y - 8 * mm

    pdf.setFillColor(muted)
    pdf.setFont("Helvetica-Oblique", 7)
    footer_text = "Presenta este ticket para agilizar la salida."
    for index, line in enumerate(_wrap_text(footer_text, 34)):
        pdf.drawCentredString(width / 2, 8 * mm - (index * 3.4 * mm), line)

    pdf.showPage()
    pdf.save()
    buffer.seek(0)
    return buffer.getvalue()


def _wrap_text(value, max_length):
    words = str(value).split()
    if not words:
        return [""]

    lines = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if len(candidate) <= max_length:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def build_ticket_qr_svg(qr_value, size=168):
    qr_widget = qr.QrCodeWidget(qr_value)
    bounds = qr_widget.getBounds()
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    drawing = Drawing(size, size, transform=[size / width, 0, 0, size / height, 0, 0])
    drawing.add(qr_widget)
    return renderSVG.drawToString(drawing)


def draw_qr_on_canvas(pdf, qr_value, x, y, size):
    qr_widget = qr.QrCodeWidget(qr_value)
    bounds = qr_widget.getBounds()
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    drawing = Drawing(size, size, transform=[size / width, 0, 0, size / height, 0, 0])
    drawing.add(qr_widget)
    renderPDF.draw(drawing, pdf, x, y)
