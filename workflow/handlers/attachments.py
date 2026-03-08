"""
Extracción y descarga de adjuntos de mensajes de Teams.

Funciones puras (sin estado) extraídas de personal/bot.py.
"""

import logging
from typing import Optional

import aiohttp
from botbuilder.schema import Activity

logger = logging.getLogger(__name__)


def extract_attachments(activity: Activity) -> dict:
    """
    Clasifica los adjuntos de un mensaje de Teams.

    Returns:
        {"excel": [...], "pdf": [...], "other": [...]}
        Cada item es {name, contentUrl, downloadUrl, contentType}.
    """
    result: dict[str, list] = {"excel": [], "pdf": [], "other": []}

    for att in activity.attachments or []:
        name = (att.name or "").lower()
        content_type = (att.content_type or "").lower()
        content_url = att.content_url or ""

        # URL pre-autenticada de Teams (OneDrive)
        download_url = ""
        if isinstance(att.content, dict):
            download_url = att.content.get("downloadUrl", "")

        # Ignorar adaptive cards y adjuntos sin URL
        if "adaptive" in content_type or (not content_url and not download_url):
            continue

        att_info = {
            "name": att.name or "unknown",
            "contentUrl": content_url,
            "downloadUrl": download_url,
            "contentType": att.content_type or "",
        }

        logger.info(
            "Attachment: name=%s, type=%s, hasDownload=%s",
            att.name, content_type, bool(download_url),
        )

        if name.endswith((".xlsx", ".xls")) or "spreadsheet" in content_type:
            result["excel"].append(att_info)
        elif name.endswith(".pdf") or "pdf" in content_type:
            result["pdf"].append(att_info)
        else:
            result["other"].append(att_info)

    return result


async def download_attachment(
    content_url: str, download_url: str = ""
) -> Optional[bytes]:
    """
    Descarga un adjunto de Teams.

    Estrategia (en orden):
      1. downloadUrl pre-autenticada (mejor opción)
      2. contentUrl sin autenticación (fallback)
    """
    try:
        async with aiohttp.ClientSession() as session:
            for url in (download_url, content_url):
                if not url:
                    continue
                data = await _fetch(session, url)
                if data is not None:
                    return data

        logger.error("No se pudo descargar adjunto: %s", content_url[:80])
        return None
    except Exception as e:
        logger.error("Error descargando adjunto: %s", e)
        return None


async def _fetch(
    session: aiohttp.ClientSession, url: str
) -> Optional[bytes]:
    """Intenta descargar bytes desde una URL."""
    async with session.get(
        url, timeout=aiohttp.ClientTimeout(total=60)
    ) as resp:
        if resp.status == 200:
            data = await resp.read()
            logger.info("Descargado: %d bytes de %s", len(data), url[:80])
            return data
        logger.warning("HTTP %d al descargar %s", resp.status, url[:80])
        return None
