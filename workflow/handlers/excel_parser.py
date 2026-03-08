"""
Parser de Excel con openpyxl (fallback).

Se usa cuando Azure Document Intelligence no está disponible o
no logra extraer artículos de la tabla.
"""

import io
import logging

logger = logging.getLogger(__name__)

# Palabras clave para detectar cada columna por su header
_ARTICLE_KEYS = (
    "articulo", "artículo", "item", "producto",
    "equipo", "nombre",
)
_QUANTITY_KEYS = ("cantidad", "cant", "qty", "unidades")
_COMMENT_KEYS = (
    "comentario", "observacion", "observación",
    "nota", "desperfecto", "motivo",
)


def fallback_parse_excel(file_bytes: bytes) -> list[dict]:
    """
    Lee la primera hoja de un Excel y mapea columnas heurísticamente.

    Returns:
        Lista de {"articulo": str, "cantidad": int, "comentario": str}.
    """
    try:
        import openpyxl
    except ImportError:
        logger.error("openpyxl no instalado — no se puede parsear Excel.")
        return []

    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True)
        ws = wb.active
        if ws is None:
            logger.warning("Excel sin hoja activa.")
            return []

        rows = list(ws.iter_rows(values_only=True))
        if len(rows) < 2:
            logger.warning("Excel con menos de 2 filas (se necesita header + datos).")
            return []

        headers = [str(h or "").lower().strip() for h in rows[0]]
        logger.info("Fallback Excel headers: %s", headers)

        art_idx = _find_column(headers, _ARTICLE_KEYS) or 0
        qty_idx = _find_column(headers, _QUANTITY_KEYS)
        com_idx = _find_column(headers, _COMMENT_KEYS)

        articulos = []
        for row in rows[1:]:
            articulo = _cell_str(row, art_idx)
            if not articulo:
                continue
            articulos.append({
                "articulo": articulo,
                "cantidad": _cell_int(row, qty_idx),
                "comentario": _cell_str(row, com_idx),
            })

        wb.close()
        logger.info("Fallback Excel: %d artículos extraídos.", len(articulos))
        return articulos

    except Exception as e:
        logger.error("Fallback Excel parser falló: %s", e)
        return []


# ------------------------------------------------------------------
# Helpers internos
# ------------------------------------------------------------------

def _find_column(headers: list[str], keywords: tuple[str, ...]) -> int | None:
    """Busca el índice de columna cuyo header contenga alguna keyword."""
    for i, h in enumerate(headers):
        if any(k in h for k in keywords):
            return i
    return None


def _cell_str(row: tuple, idx: int | None) -> str:
    """Extrae un valor string de una celda, seguro ante índices inválidos."""
    if idx is None or idx >= len(row):
        return ""
    return str(row[idx] or "").strip()


def _cell_int(row: tuple, idx: int | None, default: int = 1) -> int:
    """Extrae un valor entero de una celda, con fallback."""
    if idx is None or idx >= len(row):
        return default
    try:
        return int(float(row[idx])) if row[idx] else default
    except (ValueError, TypeError):
        return default
