"""
PDF Generator for DAPPS Bot.

Generates a formal PDF document (sustento) from requirement data.
The PDF includes:
- Header with company name and date
- Requirement metadata (ID, type, requester, date)
- Description of the need / deficiency
- Table of articles with quantities and observations
- Footer with generation timestamp

Uses fpdf2 for pure-Python PDF generation (no binary dependencies).
"""

import io
import logging
from datetime import datetime, timezone

from fpdf import FPDF

logger = logging.getLogger(__name__)


class SustentoPDF(FPDF):
    """Custom FPDF class with header and footer for DAPPS sustento documents."""

    def __init__(self, req_id: str, solicitante: str):
        super().__init__()
        self.req_id = req_id
        self.solicitante = solicitante

    def header(self):
        """Page header with company branding."""
        self.set_font("Helvetica", "B", 14)
        self.set_text_color(0, 51, 102)  # Dark blue
        self.cell(0, 10, "DAPPS - Documento de Sustento", new_x="LMARGIN", new_y="NEXT", align="C")
        self.set_font("Helvetica", "", 9)
        self.set_text_color(100, 100, 100)
        self.cell(0, 5, "Sistema de Gestión de Requerimientos de TI", new_x="LMARGIN", new_y="NEXT", align="C")
        self.line(10, self.get_y() + 2, 200, self.get_y() + 2)
        self.ln(6)

    def footer(self):
        """Page footer with page number and timestamp."""
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(128, 128, 128)
        self.cell(
            0, 10,
            f"Generado automáticamente por DAPPS Bot | Página {self.page_no()}/{{nb}} | "
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            align="C",
        )


def generate_sustento_pdf(
    req_id: str,
    solicitante: dict,
    titulo: str,
    descripcion: str,
    articulos: list[dict],
    mensaje_original: str = "",
) -> bytes:
    """
    Generate a PDF sustento document for a requirement.

    Args:
        req_id: Requirement ID (e.g., REQ-20260225-103045)
        solicitante: { nombre, aadObjectId, email }
        titulo: Short title from classification
        descripcion: Description of the need
        articulos: List of { articulo, cantidad, comentario }
        mensaje_original: Original message from the user

    Returns:
        PDF file as bytes
    """
    nombre = solicitante.get("nombre", "Desconocido")
    email = solicitante.get("email", "N/A")

    pdf = SustentoPDF(req_id=req_id, solicitante=nombre)
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=20)

    # ------------------------------------------------------------------
    # 1. Document Title
    # ------------------------------------------------------------------
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(0, 0, 0)
    pdf.cell(0, 12, "Solicitud de Nuevos Equipos / Recursos", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(4)

    # ------------------------------------------------------------------
    # 2. Metadata Section
    # ------------------------------------------------------------------
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_fill_color(230, 240, 250)
    pdf.cell(0, 8, "  Información del Requerimiento", new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(2)

    _add_field(pdf, "ID Requerimiento:", req_id)
    _add_field(pdf, "Fecha:", datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC"))
    _add_field(pdf, "Solicitante:", nombre)
    _add_field(pdf, "Email:", email)
    _add_field(pdf, "Título:", titulo)
    pdf.ln(3)

    # ------------------------------------------------------------------
    # 3. Description / Justification
    # ------------------------------------------------------------------
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_fill_color(230, 240, 250)
    pdf.cell(0, 8, "  Exposición de Carencia y Justificación", new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(2)

    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 6, descripcion or "Sin descripción proporcionada.")
    pdf.ln(3)

    if mensaje_original:
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(80, 80, 80)
        pdf.multi_cell(0, 5, f"Mensaje original del usuario: \"{mensaje_original[:500]}\"")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(3)

    # ------------------------------------------------------------------
    # 4. Articles Table
    # ------------------------------------------------------------------
    if articulos:
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_fill_color(230, 240, 250)
        pdf.cell(0, 8, f"  Detalle de Artículos ({len(articulos)} items)", new_x="LMARGIN", new_y="NEXT", fill=True)
        pdf.ln(2)

        _draw_articles_table(pdf, articulos)
        pdf.ln(3)

        # Summary
        total_items = sum(a.get("cantidad", 1) for a in articulos)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, f"Total de artículos: {len(articulos)} tipos, {total_items} unidades", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)

    # ------------------------------------------------------------------
    # 5. Signature Section
    # ------------------------------------------------------------------
    pdf.ln(10)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(90, 6, "________________________", new_x="RIGHT", new_y="NEXT")
    pdf.set_x(10)
    pdf.cell(90, 6, f"Firma del Solicitante", new_x="RIGHT", new_y="NEXT")
    pdf.set_x(10)
    pdf.set_font("Helvetica", "I", 9)
    pdf.cell(90, 6, nombre, new_x="LMARGIN", new_y="NEXT")

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    pdf_bytes = bytes(pdf.output())
    logger.info(f"PDF sustento generated for {req_id}: {len(pdf_bytes)} bytes")
    return pdf_bytes


def _add_field(pdf: FPDF, label: str, value: str):
    """Add a label-value pair to the PDF."""
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(45, 6, label, new_x="RIGHT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, value, new_x="LMARGIN", new_y="NEXT")


def _draw_articles_table(pdf: FPDF, articulos: list[dict]):
    """Draw the articles table with headers and data rows."""
    # Column widths
    col_widths = [10, 70, 20, 90]  # #, Artículo, Cant., Comentario
    headers = ["#", "Artículo", "Cant.", "Comentario / Desperfecto"]

    # Header row
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(0, 51, 102)
    pdf.set_text_color(255, 255, 255)
    for i, header in enumerate(headers):
        pdf.cell(col_widths[i], 7, header, border=1, fill=True, align="C")
    pdf.ln()

    # Data rows
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(0, 0, 0)

    for idx, art in enumerate(articulos, 1):
        fill = idx % 2 == 0
        if fill:
            pdf.set_fill_color(245, 248, 252)

        articulo = str(art.get("articulo", ""))[:40]
        cantidad = str(art.get("cantidad", 1))
        comentario = str(art.get("comentario", ""))[:55]

        pdf.cell(col_widths[0], 6, str(idx), border=1, fill=fill, align="C")
        pdf.cell(col_widths[1], 6, articulo, border=1, fill=fill)
        pdf.cell(col_widths[2], 6, cantidad, border=1, fill=fill, align="C")
        pdf.cell(col_widths[3], 6, comentario, border=1, fill=fill)
        pdf.ln()