from io import BytesIO
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
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


def build_current_cut_pdf(active_records, paid_today_records, generated_by, generated_at, datetime_formatter):
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    margin = 15 * mm
    y = height - margin

    blue = HexColor("#0f436b")
    gold = HexColor("#bf8a28")
    ink = HexColor("#2f2416")
    muted = HexColor("#7b6950")
    line_color = HexColor("#e1d2bc")

    def new_page():
        pdf.showPage()
        return height - margin

    def ensure_space(current_y, required):
        if current_y - required < margin:
            return new_page()
        return current_y

    def draw_text(text, x, text_y, font="Helvetica", size=9, color=ink):
        pdf.setFillColor(color)
        pdf.setFont(font, size)
        pdf.drawString(x, text_y, str(text))

    pdf.setTitle("Corte actual de vehiculos")
    draw_text("Estacionamiento Romina", margin, y, "Helvetica-Bold", 16, blue)
    draw_text("Corte actual de vehiculos", margin, y - 7 * mm, "Helvetica-Bold", 12, gold)
    draw_text(f"Generado: {datetime_formatter(generated_at)}", margin, y - 14 * mm, "Helvetica", 9, muted)
    draw_text(f"Generado por: {generated_by.full_name}", margin, y - 20 * mm, "Helvetica", 9, muted)
    y -= 31 * mm

    paid_total = sum(float(record.total_amount or 0) for record in paid_today_records)
    summary_items = [
        ("Fichas activas", len(active_records)),
        ("Pagos de hoy", len(paid_today_records)),
        ("Total cobrado hoy", f"${paid_total:,.2f}"),
    ]
    box_width = (width - (margin * 2) - (6 * mm)) / 3
    for index, (label, value) in enumerate(summary_items):
        x = margin + index * (box_width + 3 * mm)
        pdf.setStrokeColor(line_color)
        pdf.setFillColor(HexColor("#fffaf2"))
        pdf.roundRect(x, y - 20 * mm, box_width, 18 * mm, 3 * mm, stroke=1, fill=1)
        draw_text(label, x + 4 * mm, y - 8 * mm, "Helvetica", 8, muted)
        draw_text(value, x + 4 * mm, y - 15 * mm, "Helvetica-Bold", 12, ink)
    y -= 31 * mm

    y = _draw_cut_section(
        pdf,
        "Fichas activas",
        active_records,
        y,
        margin,
        width,
        height,
        ensure_space,
        draw_text,
        datetime_formatter,
        include_total=True,
    )
    y -= 6 * mm
    y = _draw_cut_section(
        pdf,
        "Pagos registrados hoy",
        paid_today_records,
        y,
        margin,
        width,
        height,
        ensure_space,
        draw_text,
        datetime_formatter,
        include_total=True,
    )

    pdf.showPage()
    pdf.save()
    buffer.seek(0)
    return buffer.getvalue()


def build_cash_cut_pdf(cut, paid_records, datetime_formatter):
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    margin = 15 * mm
    y = height - margin

    blue = HexColor("#0f436b")
    gold = HexColor("#bf8a28")
    ink = HexColor("#2f2416")
    muted = HexColor("#7b6950")
    line_color = HexColor("#e1d2bc")

    def new_page():
        pdf.showPage()
        return height - margin

    def ensure_space(current_y, required):
        if current_y - required < margin:
            return new_page()
        return current_y

    def draw_text(text, x, text_y, font="Helvetica", size=9, color=ink):
        pdf.setFillColor(color)
        pdf.setFont(font, size)
        pdf.drawString(x, text_y, str(text))

    cut_label = "diario" if cut.cut_type == "daily" else "semanal"
    pdf.setTitle(f"Informe de corte {cut_label} #{cut.id}")
    draw_text("Estacionamiento Romina", margin, y, "Helvetica-Bold", 16, blue)
    draw_text(f"Informe de corte {cut_label} #{cut.id}", margin, y - 7 * mm, "Helvetica-Bold", 12, gold)
    draw_text(f"Periodo: {datetime_formatter(cut.period_start)} a {datetime_formatter(cut.period_end)}", margin, y - 14 * mm, "Helvetica", 9, muted)
    draw_text(f"Generado por: {cut.generated_by.full_name}", margin, y - 20 * mm, "Helvetica", 9, muted)
    y -= 31 * mm

    summary_items = [
        ("Total cobrado", f"${float(cut.total_income or 0):,.2f}"),
        ("Vehiculos cobrados", cut.vehicles_paid),
    ]
    box_width = (width - (margin * 2) - (3 * mm)) / 2
    for index, (label, value) in enumerate(summary_items):
        x = margin + index * (box_width + 3 * mm)
        pdf.setStrokeColor(line_color)
        pdf.setFillColor(HexColor("#fffaf2"))
        pdf.roundRect(x, y - 20 * mm, box_width, 18 * mm, 3 * mm, stroke=1, fill=1)
        draw_text(label, x + 3 * mm, y - 8 * mm, "Helvetica", 7, muted)
        draw_text(value, x + 3 * mm, y - 15 * mm, "Helvetica-Bold", 10, ink)
    y -= 31 * mm

    y = _draw_cut_section(
        pdf,
        "Vehiculos cobrados",
        paid_records,
        y,
        margin,
        width,
        height,
        ensure_space,
        draw_text,
        datetime_formatter,
        include_total=True,
    )

    pdf.showPage()
    pdf.save()
    buffer.seek(0)
    return buffer.getvalue()


def _draw_cut_section(
    pdf,
    title,
    records,
    y,
    margin,
    width,
    height,
    ensure_space,
    draw_text,
    datetime_formatter,
    include_total=False,
):
    ink = HexColor("#2f2416")
    muted = HexColor("#7b6950")
    line_color = HexColor("#e1d2bc")
    y = ensure_space(y, 18 * mm)
    draw_text(title, margin, y, "Helvetica-Bold", 12, HexColor("#0f436b"))
    y -= 8 * mm

    if not records:
        draw_text("Sin registros.", margin, y, "Helvetica", 9, muted)
        return y - 8 * mm

    headers = ["Ficha", "Cliente", "Vehiculo", "Placas", "Entrada", "Estado"]
    if include_total:
        headers.append("Total")
    col_widths = [20 * mm, 35 * mm, 24 * mm, 22 * mm, 31 * mm, 27 * mm]
    if include_total:
        col_widths.append(width - (margin * 2) - sum(col_widths))

    x = margin
    for index, header in enumerate(headers):
        draw_text(header, x, y, "Helvetica-Bold", 7, muted)
        x += col_widths[index]
    y -= 5 * mm

    for record in records:
        y = ensure_space(y, 15 * mm)
        row_values = [
            record.display_ticket_number,
            record.client_name,
            record.vehicle_type,
            record.plate_number,
            datetime_formatter(record.entry_at),
            record.status,
        ]
        if include_total:
            row_values.append(f"${float(record.total_amount or 0):,.2f}")

        x = margin
        max_lines = 1
        wrapped_columns = []
        for index, value in enumerate(row_values):
            max_chars = max(8, int(col_widths[index] / (2.2 * mm)))
            lines = _wrap_text(str(value or "-"), max_chars)
            wrapped_columns.append(lines)
            max_lines = max(max_lines, len(lines))

        row_height = max_lines * 4 * mm + 3 * mm
        y = ensure_space(y, row_height + 2 * mm)
        for index, lines in enumerate(wrapped_columns):
            line_y = y
            for line in lines:
                draw_text(line, x, line_y, "Helvetica", 7, ink)
                line_y -= 4 * mm
            x += col_widths[index]
        y -= row_height
        pdf.setStrokeColor(line_color)
        pdf.line(margin, y + 1.5 * mm, width - margin, y + 1.5 * mm)

    return y


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
