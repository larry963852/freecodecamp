"""
Cliente HTTP unificado para webhooks de n8n.

Consolida las 3 llamadas a n8n en un solo cliente reutilizable:
  - classify()            → clasificación IA de mensajes personales
  - create_planner_task() → crear tarea Planner para requerimientos
  - escalate_consulta()   → escalar consulta sin respuesta RAG
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from config import Config

logger = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=30)


class N8nClient:
    """Cliente HTTP para los webhooks de n8n."""

    # ------------------------------------------------------------------
    # Internal helper
    # ------------------------------------------------------------------

    @staticmethod
    async def _post(url: str, payload: dict, label: str) -> Optional[dict]:
        """POST JSON a un webhook n8n. Retorna la respuesta o None."""
        if not url:
            logger.warning(f"[N8N] URL no configurada para {label}.")
            return None

        logger.info(f"[N8N] {label}: enviando a {url[:60]}...")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=_TIMEOUT,
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        logger.info(f"[N8N] {label}: OK → {result}")
                        return result
                    body = await resp.text()
                    logger.error(f"[N8N] {label}: HTTP {resp.status} → {body[:200]}")
                    return None
        except asyncio.TimeoutError:
            logger.error(f"[N8N] {label}: timeout.")
            return None
        except Exception as e:
            logger.error(f"[N8N] {label}: error → {e}")
            return None

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    async def classify(
        self,
        user_text: str,
        user_name: str,
        user_aad_id: str,
        user_email: str,
        conversation_id: str,
    ) -> Optional[dict]:
        """Envía un mensaje 1:1 a n8n para clasificación IA."""
        payload = {
            "origen": "personal",
            "mensaje": user_text,
            "solicitante": {
                "nombre": user_name,
                "aadObjectId": user_aad_id,
                "email": user_email,
            },
            "conversationId": conversation_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return await self._post(
            Config.N8N_PERSONAL_WEBHOOK_URL, payload, "Clasificación"
        )

    async def create_planner_task(
        self,
        req_id: str,
        titulo: str,
        descripcion: str,
        solicitante: dict,
        articulos: list,
        blob_urls: dict,
    ) -> Optional[dict]:
        """Llama al webhook de n8n para crear una tarea Planner."""
        payload = {
            "req_id": req_id,
            "titulo": titulo,
            "descripcion": descripcion,
            "solicitante": solicitante,
            "articulos": articulos,
            "blob_urls": blob_urls,
        }
        return await self._post(
            Config.N8N_CREATE_PLANNER_URL, payload, f"Planner/{req_id}"
        )

    async def escalate_consulta(
        self,
        titulo: str,
        descripcion: str,
        solicitante: dict,
        mensaje_original: str,
    ) -> Optional[dict]:
        """Escala una consulta sin respuesta RAG a Planner vía n8n."""
        payload = {
            "titulo": titulo,
            "descripcion": descripcion,
            "solicitante": solicitante,
            "mensaje_original": mensaje_original,
        }
        return await self._post(
            Config.N8N_CONSULTA_ESCALADA_URL, payload, "Escalar consulta"
        )


# Singleton
n8n_client = N8nClient()
