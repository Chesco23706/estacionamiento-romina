from io import BytesIO
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from .pricing import format_duration


def build_ticket_pdf(record, datetime_formatter):
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter

    gold = HexColor("#bf8a28")
    blue = HexColor("#0f436b")
    ink = HexColor("#2f2416")
    muted = HexColor("#7b6950")

    pdf.setTitle(f"Ticket {record.display_ticket_number}")
    pdf.setFillColorRGB(1, 1, 1)
    pdf.setStrokeColor(gold)
    pdf.roundRect(18 * mm, 22 * mm, width - 36 * mm, height - 44 * mm, 8 * mm, stroke=1, fill=1)

    logo_path = Path(__file__).resolve().parent.parent / "public" / "logo.jpg"
    if logo_path.exists():
        pdf.drawImage(
            ImageReader(str(logo_path)),
            24 * mm,
            height - 55 * mm,
            26 * mm,
            26 * mm,
            preserveAspectRatio=True,
            mask="auto",
        )

    pdf.setFillColor(blue)
    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawString(55 * mm, height - 32 * mm, "Estacionamiento Romina")
    pdf.setFillColor(muted)
    pdf.setFont("Helvetica", 10)
    pdf.drawString(55 * mm, height - 38 * mm, "Izamal, Yucatan")
    pdf.drawString(55 * mm, height - 43 * mm, "Ticket de servicio")
    pdf.line(24 * mm, height - 60 * mm, width - 24 * mm, height - 60 * mm)

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

    x = 26 * mm
    y = height - 74 * mm
    for label, value in fields:
        pdf.setFillColor(ink)
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(x, y, f"{label}:")
        pdf.setFont("Helvetica", 10)
        pdf.drawString(x + 34 * mm, y, str(value))
        y -= 8 * mm
        if y < 48 * mm:
            break

    if record.notes:
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(26 * mm, y - 2 * mm, "Notas:")
        text = pdf.beginText(26 * mm, y - 8 * mm)
        text.setFont("Helvetica", 10)
        text.setFillColor(ink)
        for line in _wrap_text(record.notes, 72):
            text.textLine(line)
        pdf.drawText(text)

    pdf.setFillColor(muted)
    pdf.setFont("Helvetica-Oblique", 9)
    pdf.drawString(26 * mm, 30 * mm, "Presenta este documento para agilizar la salida del vehiculo.")

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
