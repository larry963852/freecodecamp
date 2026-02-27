"""
Azure AI Document Intelligence service for DAPPS Bot.

Uses the prebuilt-layout model to:
- Extract tables from Excel files (artículo, cantidad, comentario)
- Extract text and structure from PDF files (sustento)

Requires: azure-ai-documentintelligence SDK
"""

import io
import logging
from typing import Optional

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
from azure.core.credentials import AzureKeyCredential

from config import Config

logger = logging.getLogger(__name__)


class DocumentIntelligenceService:
    """Analyze Excel and PDF documents using Azure AI Document Intelligence."""

    def __init__(self):
        self._client: Optional[DocumentIntelligenceClient] = None
        self._initialized = False

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def _ensure_initialized(self):
        """Lazy initialization of the Document Intelligence client."""
        if self._initialized:
            return

        endpoint = Config.DOC_INTELLIGENCE_ENDPOINT
        key = Config.DOC_INTELLIGENCE_KEY
        if not endpoint or not key:
            raise RuntimeError(
                "DOC_INTELLIGENCE_ENDPOINT and DOC_INTELLIGENCE_KEY must be "
                "configured in .env to enable document analysis."
            )

        self._client = DocumentIntelligenceClient(
            endpoint=endpoint,
            credential=AzureKeyCredential(key),
        )
        self._initialized = True
        logger.info("DocumentIntelligenceService initialized.")

    # ------------------------------------------------------------------
    # Excel Analysis
    # ------------------------------------------------------------------

    async def analyze_excel(self, file_bytes: bytes) -> dict:
        """
        Analyze an Excel file and extract table data.

        Expected columns: Artículo (or similar), Cantidad, Comentario.
        Uses prebuilt-layout model which can read tables from XLSX.

        Args:
            file_bytes: Raw bytes of the Excel file

        Returns:
            {
                "status": "success" | "error",
                "extractedRows": int,
                "articulos": [
                    { "articulo": str, "cantidad": int, "comentario": str }
                ],
                "rawTables": [...],
                "error": str (if status == "error")
            }
        """
        self._ensure_initialized()

        try:
            poller = self._client.begin_analyze_document(
                model_id="prebuilt-layout",
                body=io.BytesIO(file_bytes),
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            result = poller.result()

            articulos = []
            raw_tables = []

            if result.tables:
                for i, table in enumerate(result.tables):
                    table_data = self._parse_table(table)
                    raw_tables.append(table_data)
                    logger.info(
                        f"Table {i}: {table_data['rowCount']} rows, "
                        f"{table_data['columnCount']} cols, "
                        f"headers={table_data['headers']}"
                    )

                    # Try to map columns to our expected schema
                    mapped = self._map_to_articulos(table_data)
                    articulos.extend(mapped)
            else:
                logger.warning("Document Intelligence found NO tables in Excel.")

            logger.info(
                f"Excel analysis complete: {len(articulos)} articles found "
                f"in {len(raw_tables)} table(s)"
            )

            return {
                "status": "success",
                "extractedRows": len(articulos),
                "articulos": articulos,
                "rawTables": raw_tables,
            }

        except Exception as e:
            logger.error(f"Excel analysis failed: {e}")
            # Re-raise so the bot can fall back to openpyxl parser
            raise

    # ------------------------------------------------------------------
    # PDF Analysis
    # ------------------------------------------------------------------

    async def analyze_pdf(self, file_bytes: bytes) -> dict:
        """
        Analyze a PDF file and extract text content and any tables.

        Args:
            file_bytes: Raw bytes of the PDF file

        Returns:
            {
                "status": "success" | "error",
                "content": str (full text),
                "pages": int,
                "tables": [...],
                "error": str (if status == "error")
            }
        """
        self._ensure_initialized()

        try:
            poller = self._client.begin_analyze_document(
                model_id="prebuilt-layout",
                body=io.BytesIO(file_bytes),
                content_type="application/pdf",
            )
            result = poller.result()

            content = result.content or ""
            pages = len(result.pages) if result.pages else 0
            tables = []

            if result.tables:
                for table in result.tables:
                    tables.append(self._parse_table(table))

            logger.info(
                f"PDF analysis complete: {pages} pages, "
                f"{len(tables)} table(s), {len(content)} chars"
            )

            return {
                "status": "success",
                "content": content,
                "pages": pages,
                "tables": tables,
            }

        except Exception as e:
            logger.error(f"PDF analysis failed: {e}")
            return {
                "status": "error",
                "content": "",
                "pages": 0,
                "tables": [],
                "error": str(e),
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_table(table) -> dict:
        """
        Parse a Document Intelligence table into a structured dict.

        Returns:
            {
                "rowCount": int,
                "columnCount": int,
                "headers": [str],
                "rows": [ [str, str, ...] ]
            }
        """
        rows_dict: dict[int, dict[int, str]] = {}
        for cell in table.cells:
            r = cell.row_index
            c = cell.column_index
            rows_dict.setdefault(r, {})[c] = (cell.content or "").strip()

        if not rows_dict:
            return {"rowCount": 0, "columnCount": 0, "headers": [], "rows": []}

        col_count = table.column_count or max(
            max(cols.keys()) + 1 for cols in rows_dict.values()
        )

        # First row is assumed to be headers
        sorted_rows = sorted(rows_dict.keys())
        headers = [rows_dict[sorted_rows[0]].get(c, "") for c in range(col_count)]

        data_rows = []
        for r in sorted_rows[1:]:
            row = [rows_dict[r].get(c, "") for c in range(col_count)]
            # Skip completely empty rows
            if any(cell.strip() for cell in row):
                data_rows.append(row)

        return {
            "rowCount": len(data_rows),
            "columnCount": col_count,
            "headers": headers,
            "rows": data_rows,
        }

    @staticmethod
    def _map_to_articulos(table_data: dict) -> list[dict]:
        """
        Map table columns to the expected schema: articulo, cantidad, comentario.

        Uses fuzzy header matching to find the right columns.
        """
        headers = [h.lower().strip() for h in table_data.get("headers", [])]
        rows = table_data.get("rows", [])

        if not headers or not rows:
            return []

        # Find column indices by fuzzy matching
        art_idx = _find_column(headers, ["articulo", "artículo", "item", "producto", "equipo", "nombre", "descripcion", "descripción"])
        qty_idx = _find_column(headers, ["cantidad", "qty", "cant", "unidades", "numero", "número"])
        com_idx = _find_column(headers, ["comentario", "observacion", "observación", "nota", "detalle", "desperfecto", "motivo", "estado"])

        if art_idx is None:
            # If we can't find article column, use first column
            art_idx = 0

        articulos = []
        for row in rows:
            articulo = row[art_idx] if art_idx < len(row) else ""
            if not articulo.strip():
                continue  # Skip rows without article name

            cantidad_str = row[qty_idx] if qty_idx is not None and qty_idx < len(row) else "1"
            try:
                cantidad = int(float(cantidad_str))
            except (ValueError, TypeError):
                cantidad = 1

            comentario = row[com_idx] if com_idx is not None and com_idx < len(row) else ""

            articulos.append({
                "articulo": articulo.strip(),
                "cantidad": cantidad,
                "comentario": comentario.strip(),
            })

        return articulos


def _find_column(headers: list[str], candidates: list[str]) -> Optional[int]:
    """Find the index of a column by checking against candidate names."""
    for i, header in enumerate(headers):
        for candidate in candidates:
            if candidate in header or header in candidate:
                return i
    return None


# Singleton instance
doc_intelligence_service = DocumentIntelligenceService()
