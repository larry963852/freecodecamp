"""Handlers — lógica de negocio extraída de los bots."""

from handlers.attachments import extract_attachments, download_attachment
from handlers.excel_parser import fallback_parse_excel
from handlers.requirement_processor import process_requirement

__all__ = [
    "extract_attachments",
    "download_attachment",
    "fallback_parse_excel",
    "process_requirement",
]
