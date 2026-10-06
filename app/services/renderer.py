"""The single, predefined certificate template, rendered to PDF with ReportLab.

ReportLab is pure Python (no headless browser or system libraries), fast enough to render
hundreds of certificates per second per core, and produces vector output that prints well.
"""

from dataclasses import dataclass
from datetime import date
from io import BytesIO

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

TEMPLATE_NAME = "classic-landscape-v1"

NAVY = HexColor("#1F2A44")
GOLD = HexColor("#B8892B")
INK = HexColor("#374151")
MUTED = HexColor("#6B7280")
PAPER = HexColor("#FFFDF7")

PAGE_WIDTH, PAGE_HEIGHT = landscape(A4)


@dataclass(frozen=True)
class CertificateContent:
    recipient_name: str
    course_name: str
    issuer_name: str
    issue_date: date
    title: str
    verification_code: str
    verification_url: str
    grade: str | None = None
    description: str | None = None


def supports_text(text: str) -> bool:
    """Whether the template's fonts can draw `text`.

    The template uses the PDF standard-14 fonts, which cover the Windows-1252 character set
    (English and most Western European names, e.g. "José Müller"). Text outside that set would
    render as empty boxes, so it is rejected during validation instead of producing a broken
    certificate. Supporting other scripts means registering a Unicode TTF font (see README).
    """
    if not all(ch.isprintable() for ch in text):
        return False
    try:
        text.encode("cp1252")
    except UnicodeEncodeError:
        return False
    return True


def render_certificate(content: CertificateContent) -> bytes:
    buffer = BytesIO()
    # invariant=True makes the output deterministic (no embedded timestamps or random IDs),
    # so re-generating a certificate yields byte-identical files.
    pdf = Canvas(buffer, pagesize=(PAGE_WIDTH, PAGE_HEIGHT), invariant=True)
    pdf.setTitle(f"{content.title} - {content.recipient_name}")
    pdf.setAuthor(content.issuer_name)
    pdf.setSubject(content.course_name)
    pdf.setCreator("Bulk Certificate Generator")

    _draw_frame(pdf)
    _draw_body(pdf, content)
    _draw_footer(pdf, content)

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _draw_frame(pdf: Canvas) -> None:
    pdf.setFillColor(PAPER)
    pdf.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, stroke=0, fill=1)

    pdf.setStrokeColor(NAVY)
    pdf.setLineWidth(6)
    pdf.rect(18, 18, PAGE_WIDTH - 36, PAGE_HEIGHT - 36)

    pdf.setStrokeColor(GOLD)
    pdf.setLineWidth(1.5)
    pdf.rect(30, 30, PAGE_WIDTH - 60, PAGE_HEIGHT - 60)

    # Small diamonds at the inner corners.
    pdf.setFillColor(GOLD)
    right, top = PAGE_WIDTH - 30, PAGE_HEIGHT - 30
    for x, y in ((30, 30), (30, top), (right, 30), (right, top)):
        path = pdf.beginPath()
        path.moveTo(x, y + 7)
        path.lineTo(x + 7, y)
        path.lineTo(x, y - 7)
        path.lineTo(x - 7, y)
        path.close()
        pdf.drawPath(path, stroke=0, fill=1)


def _draw_body(pdf: Canvas, content: CertificateContent) -> None:
    cx = PAGE_WIDTH / 2

    pdf.setFillColor(GOLD)
    _draw_fitted(pdf, content.issuer_name.upper(), "Helvetica-Bold", 12, 8, 640, cx, 500)

    pdf.setFillColor(NAVY)
    _draw_fitted(pdf, content.title.upper(), "Times-Bold", 34, 20, 680, cx, 448)

    pdf.setStrokeColor(GOLD)
    pdf.setLineWidth(1.2)
    pdf.line(cx - 60, 430, cx + 60, 430)

    pdf.setFillColor(INK)
    pdf.setFont("Times-Italic", 15)
    pdf.drawCentredString(cx, 392, "This is to certify that")

    pdf.setFillColor(NAVY)
    _draw_fitted(pdf, content.recipient_name, "Times-BoldItalic", 40, 22, 620, cx, 340)

    pdf.setStrokeColor(GOLD)
    pdf.setLineWidth(0.8)
    pdf.line(cx - 230, 326, cx + 230, 326)

    pdf.setFillColor(INK)
    pdf.setFont("Times-Italic", 15)
    pdf.drawCentredString(cx, 294, "has successfully completed")

    pdf.setFillColor(NAVY)
    y = _draw_fitted(pdf, content.course_name, "Helvetica-Bold", 20, 13, 640, cx, 264)

    if content.grade:
        y -= 24
        pdf.setFillColor(INK)
        pdf.setFont("Times-Italic", 13)
        pdf.drawCentredString(cx, y, f"with grade: {content.grade}")

    if content.description:
        pdf.setFillColor(MUTED)
        pdf.setFont("Helvetica", 10.5)
        lines = simpleSplit(content.description, "Helvetica", 10.5, 560)[:3]
        y -= 24
        for line in lines:
            pdf.drawCentredString(cx, y, line)
            y -= 14


def _draw_footer(pdf: Canvas, content: CertificateContent) -> None:
    left_x, right_x, cx = 190, PAGE_WIDTH - 190, PAGE_WIDTH / 2

    pdf.setFillColor(NAVY)
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawCentredString(left_x, 104, f"{content.issue_date.day} {content.issue_date:%B %Y}")
    _draw_fitted(pdf, content.issuer_name, "Times-BoldItalic", 15, 9, 200, right_x, 104)

    pdf.setStrokeColor(INK)
    pdf.setLineWidth(0.6)
    for x in (left_x, right_x):
        pdf.line(x - 95, 96, x + 95, 96)

    pdf.setFillColor(MUTED)
    pdf.setFont("Helvetica", 9)
    pdf.drawCentredString(left_x, 82, "Date of Issue")
    pdf.drawCentredString(right_x, 82, "Authorized Signatory")

    _draw_qr(pdf, content.verification_url, x=cx - 34, y=70, size=68)
    pdf.setFillColor(MUTED)  # _draw_qr leaves the fill colour white
    pdf.setFont("Helvetica", 7.5)
    pdf.drawCentredString(cx, 58, f"Certificate ID: {content.verification_code}")
    pdf.drawCentredString(cx, 47, "Scan to verify")


def _draw_fitted(
    pdf: Canvas,
    text: str,
    font: str,
    max_size: float,
    min_size: float,
    max_width: float,
    x: float,
    y: float,
) -> float:
    """Draw centred text, shrinking it to fit `max_width`; wrap only if even `min_size` is too wide.

    Returns the baseline of the last line drawn.
    """
    size = max_size
    while size > min_size and stringWidth(text, font, size) > max_width:
        size -= 1
    pdf.setFont(font, size)
    lines = simpleSplit(text, font, size, max_width) or [text]
    for i, line in enumerate(lines[:2]):
        pdf.drawCentredString(x, y - i * size * 1.1, line)
    return y - (min(len(lines), 2) - 1) * size * 1.1


def _draw_qr(pdf: Canvas, value: str, x: float, y: float, size: float) -> None:
    # White quiet zone around the code so scanners read it reliably on the tinted paper.
    pdf.setFillColor(HexColor("#FFFFFF"))
    pdf.rect(x - 4, y - 4, size + 8, size + 8, stroke=0, fill=1)
    widget = QrCodeWidget(value, barLevel="M", barBorder=0)
    x1, y1, x2, y2 = widget.getBounds()
    drawing = Drawing(size, size, transform=[size / (x2 - x1), 0, 0, size / (y2 - y1), 0, 0])
    drawing.add(widget)
    renderPDF.draw(drawing, pdf, x, y)
