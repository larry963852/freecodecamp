"""
DAPPS Bot — Lógica de CHAT PERSONAL (1:1).

Este módulo maneja:
- Conversaciones 1:1 con usuarios en Teams
- Almacenamiento de references por usuario para mensajes proactivos 1:1
- Reenvío de mensajes operativos a n8n para clasificación IA
- Flujo de 2 pasos para REQUERIMIENTOS:
    Paso 1: mensaje de texto → clasificación IA → si es REQUERIMIENTO →
            pedir Excel adjunto
    Paso 2: usuario sube Excel (.xlsx) y opcionalmente PDF →
            Document Intelligence analiza → Blob Storage (archivos + metadata.json)
            → genera PDF sustento si no se subió uno
            → notifica a n8n para crear tarea en Planner (OAuth delegado)
- Respuesta a todos los mensajes (nunca deja un turn sin respuesta)
"""

import asyncio
import json
import os
import logging
from datetime import datetime, timezone
from typing import Dict, Optional

import aiohttp
from botbuilder.core import ActivityHandler, TurnContext
from botbuilder.schema import (
    Activity,
    ActivityTypes,
    ConversationReference,
)

# Config se importa desde el directorio padre (bot-server/)
# app.py se ejecuta desde bot-server/, así que config está en sys.path
from config import Config

# Azure services
from services.blob_service import blob_service, BlobStorageService
from services.document_intelligence import doc_intelligence_service
from services.pdf_generator import generate_sustento_pdf

logger = logging.getLogger(__name__)

# Archivo donde se persisten las conversationReferences de chats 1:1
PERSONAL_REFS_FILE = os.path.join(
    os.path.dirname(__file__), "..", "conversation_references_personal.json"
)

# Archivo donde se persiste el estado conversacional por usuario
USER_STATE_FILE = os.path.join(
    os.path.dirname(__file__), "..", "user_state.json"
)

# Mensajes que NO se reenvían a n8n (saludos, ayuda, etc.)
_GREETINGS = {"hola", "hi", "hey", "buenos dias", "buenas", "buenas tardes", "buenas noches", "buen dia"}
_HELP_CMDS = {"ayuda", "help", "?"}
_CANCEL_CMDS = {"cancelar", "cancel", "salir", "exit"}


class DappsPersonalBot(ActivityHandler):
    """
    Bot conversacional 1:1 de DAPPS para Microsoft Teams.

    Almacena una ConversationReference por cada usuario que escribe,
    permitiendo enviar mensajes proactivos a cualquier usuario 1:1.

    Implementa un flujo de 2 pasos para REQUERIMIENTOS:
      - Paso 1: Clasificación del mensaje → si es REQUERIMIENTO, pide Excel
      - Paso 2: Recibe adjuntos (Excel + PDF opcional) → procesa y persiste

    Estado conversacional por usuario:
      - None / "idle": esperando nuevo mensaje
      - "esperando_excel": el bot pidió que suba el Excel con artículos
    """

    def __init__(self):
        super().__init__()
        # Dict: user_aad_id → ConversationReference serializada
        self.personal_references: Dict[str, dict] = {}
        # Dict: user_aad_id → { state, req_id, n8n_result, ... }
        self.user_states: Dict[str, dict] = {}
        self._load_references()
        self._load_user_states()

    # ------------------------------------------------------------------
    # Persistencia de ConversationReferences (1:1, por usuario)
    # ------------------------------------------------------------------

    def _load_references(self):
        """Carga las references de chats personales desde disco."""
        if os.path.exists(PERSONAL_REFS_FILE):
            try:
                with open(PERSONAL_REFS_FILE, "r") as f:
                    self.personal_references = json.load(f)
                logger.info(
                    f"Cargadas {len(self.personal_references)} "
                    f"references personales desde disco."
                )
            except Exception as e:
                logger.warning(f"No se pudo cargar references personales: {e}")

    @staticmethod
    def _serialize_ref(obj):
        """Serializa recursivamente un ConversationReference a dict puro."""
        if obj is None:
            return None
        if isinstance(obj, (str, int, float, bool)):
            return obj
        if isinstance(obj, list):
            return [DappsPersonalBot._serialize_ref(item) for item in obj]  # type: ignore[attr-defined]
        if isinstance(obj, dict):
            return {k: DappsPersonalBot._serialize_ref(v) for k, v in obj.items()}  # type: ignore[attr-defined]
        if hasattr(obj, "__dict__"):
            return {
                k: DappsPersonalBot._serialize_ref(v)  # type: ignore[attr-defined]
                for k, v in obj.__dict__.items()
                if v is not None and not k.startswith("_")
            }
        return str(obj)

    def _save_reference(self, turn_context: TurnContext):
        """Guarda la conversationReference del usuario actual."""
        activity = turn_context.activity
        ref = TurnContext.get_conversation_reference(activity)

        # Identificador único del usuario (AAD Object ID)
        user_id = getattr(activity.from_property, "aad_object_id", None)
        if not user_id:
            user_id = getattr(activity.from_property, "id", "unknown")

        # Serializar la reference recursivamente (no usar default=str)
        self.personal_references[user_id] = self._serialize_ref(ref)

        try:
            with open(PERSONAL_REFS_FILE, "w") as f:
                json.dump(self.personal_references, f, indent=2)
            logger.info(f"Reference personal guardada para usuario {user_id}.")
        except Exception as e:
            logger.error(f"Error guardando reference personal: {e}")

    def get_reference_for_user(self, user_aad_id: str) -> Optional[ConversationReference]:
        """Obtiene la ConversationReference de un usuario por su AAD Object ID."""
        ref_data = self.personal_references.get(user_aad_id)
        if not ref_data:
            return None

        try:
            return ConversationReference().from_dict(ref_data)
        except Exception as e:
            logger.warning(f"from_dict falló para {user_aad_id}: {e}, construyendo manualmente")
            # Fallback: construir manualmente si from_dict falla
            ref = ConversationReference(
                activity_id=ref_data.get("activity_id"),
                bot=ref_data.get("bot") if isinstance(ref_data.get("bot"), dict) else None,
                channel_id=ref_data.get("channel_id"),
                conversation=ref_data.get("conversation") if isinstance(ref_data.get("conversation"), dict) else None,
                locale=ref_data.get("locale"),
                service_url=ref_data.get("service_url"),
                user=ref_data.get("user") if isinstance(ref_data.get("user"), dict) else None,
            )
            return ref

    # ------------------------------------------------------------------
    # Persistencia de estado conversacional por usuario
    # ------------------------------------------------------------------

    def _load_user_states(self):
        """Carga el estado conversacional de cada usuario desde disco."""
        if os.path.exists(USER_STATE_FILE):
            try:
                with open(USER_STATE_FILE, "r") as f:
                    self.user_states = json.load(f)
                logger.info(
                    f"Cargados {len(self.user_states)} estados de usuario desde disco."
                )
            except Exception as e:
                logger.warning(f"No se pudo cargar estados de usuario: {e}")

    def _save_user_states(self):
        """Persiste el estado conversacional a disco."""
        try:
            with open(USER_STATE_FILE, "w") as f:
                json.dump(self.user_states, f, default=str, indent=2)
        except Exception as e:
            logger.error(f"Error guardando estados de usuario: {e}")

    def _get_user_state(self, user_aad_id: str) -> dict:
        """Obtiene el estado actual de un usuario."""
        return self.user_states.get(user_aad_id, {"state": "idle"})

    def _set_user_state(self, user_aad_id: str, state: dict):
        """Establece el estado de un usuario y lo persiste."""
        self.user_states[user_aad_id] = state
        self._save_user_states()

    def _clear_user_state(self, user_aad_id: str):
        """Limpia el estado de un usuario (vuelve a idle)."""
        self.user_states.pop(user_aad_id, None)
        self._save_user_states()

    # ------------------------------------------------------------------
    # Integración con n8n
    # ------------------------------------------------------------------

    async def _forward_to_n8n(
        self,
        user_text: str,
        user_name: str,
        user_aad_id: str,
        user_email: str,
        conversation_id: str,
    ) -> Optional[dict]:
        """
        Envía el mensaje del usuario al webhook de n8n para clasificación IA.
        Retorna la respuesta de n8n (JSON) o None si falla.
        """
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

        webhook_url = Config.N8N_PERSONAL_WEBHOOK_URL
        if not webhook_url:
            logger.warning("[PERSONAL] N8N_PERSONAL_WEBHOOK_URL no configurada.")
            return None

        logger.info(f"[PERSONAL] Enviando a n8n: {user_name} → {user_text[:80]}...")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        logger.info(f"[PERSONAL] n8n respondió: {result}")
                        return result
                    else:
                        body = await resp.text()
                        logger.error(
                            f"[PERSONAL] n8n error HTTP {resp.status}: {body[:200]}"
                        )
                        return None
        except asyncio.TimeoutError:
            logger.error("[PERSONAL] Timeout esperando respuesta de n8n.")
            return None
        except Exception as e:
            logger.error(f"[PERSONAL] Error conectando a n8n: {e}")
            return None

    async def _call_n8n_create_planner(
        self,
        req_id: str,
        titulo: str,
        descripcion: str,
        solicitante: dict,
        articulos: list,
        blob_urls: dict,
    ) -> Optional[dict]:
        """
        Llama al webhook de n8n para crear la tarea Planner.

        n8n usa credenciales OAuth delegadas (Graph API) que sí tienen permiso
        de escritura en Planner, a diferencia del flujo client_credentials del bot.

        Returns:
            dict con "planner_task_id" si tuvo éxito, None si falló.
        """
        webhook_url = Config.N8N_CREATE_PLANNER_URL
        if not webhook_url:
            logger.warning("[PERSONAL] N8N_CREATE_PLANNER_URL no configurada — omitiendo Planner.")
            return None

        payload = {
            "req_id": req_id,
            "titulo": titulo,
            "descripcion": descripcion,
            "solicitante": solicitante,
            "articulos": articulos,
            "blob_urls": blob_urls,
        }

        logger.info(f"[PERSONAL] Llamando webhook Planner de n8n para {req_id}...")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        logger.info(
                            f"[PERSONAL] n8n Planner webhook OK: {result}"
                        )
                        return result
                    else:
                        body = await resp.text()
                        logger.error(
                            f"[PERSONAL] n8n Planner webhook HTTP {resp.status}: {body[:200]}"
                        )
                        return None
        except asyncio.TimeoutError:
            logger.error("[PERSONAL] Timeout esperando respuesta del webhook Planner de n8n.")
            return None
        except Exception as e:
            logger.error(f"[PERSONAL] Error llamando webhook Planner n8n: {e}")
            return None

    # ------------------------------------------------------------------
    # Attachment helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_attachments(activity: Activity) -> dict:
        """
        Extract attachments from a Teams message.

        Returns:
            {
                "excel": [{ "name": ..., "contentUrl": ..., "contentType": ... }],
                "pdf":   [{ "name": ..., "contentUrl": ..., "contentType": ... }],
                "other": [...]
            }
        """
        result = {"excel": [], "pdf": [], "other": []}
        attachments = activity.attachments or []

        for att in attachments:
            name = (att.name or "").lower()
            content_type = (att.content_type or "").lower()
            content_url = att.content_url or ""

            # Extract downloadUrl from content (Teams pre-authenticated URL)
            download_url = ""
            if isinstance(att.content, dict):
                download_url = att.content.get("downloadUrl", "")

            # Skip inline/adaptive cards
            if "adaptive" in content_type or (not content_url and not download_url):
                continue

            att_info = {
                "name": att.name or "unknown",
                "contentUrl": content_url,
                "downloadUrl": download_url,
                "contentType": att.content_type or "",
            }

            logger.info(
                f"Attachment detected: name={att.name}, "
                f"contentType={content_type}, "
                f"hasDownloadUrl={bool(download_url)}, "
                f"contentUrl={content_url[:80] if content_url else 'N/A'}"
            )

            if name.endswith(".xlsx") or name.endswith(".xls") or "spreadsheet" in content_type:
                result["excel"].append(att_info)
            elif name.endswith(".pdf") or "pdf" in content_type:
                result["pdf"].append(att_info)
            else:
                result["other"].append(att_info)

        return result

    async def _download_attachment(
        self, content_url: str, download_url: str = ""
    ) -> Optional[bytes]:
        """
        Download an attachment from Teams.

        Teams file uploads in 1:1 chats are stored in the user's OneDrive.
        The contentUrl is a SharePoint URL that requires SharePoint auth.

        Strategy (in order):
          1. Use downloadUrl from att.content (Teams pre-authenticated URL)
          2. Try contentUrl without auth (works for inline/blob uploads)
        """
        async def _fetch(
            session: aiohttp.ClientSession, url: str, headers: dict
        ) -> Optional[bytes]:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    logger.info(
                        f"Attachment downloaded: {len(data)} bytes "
                        f"from {url[:80]}"
                    )
                    return data
                else:
                    logger.warning(
                        f"Download attempt HTTP {resp.status} from {url[:80]}"
                    )
                    return None

        try:
            async with aiohttp.ClientSession() as session:
                # 1️⃣ Pre-authenticated downloadUrl (best option)
                if download_url:
                    logger.info("Trying pre-authenticated downloadUrl...")
                    data = await _fetch(session, download_url, {})
                    if data is not None:
                        return data

                # 2️⃣ Try contentUrl without auth
                if content_url:
                    data = await _fetch(session, content_url, {})
                    if data is not None:
                        return data

                # All attempts failed
                logger.error(
                    f"Failed to download attachment. "
                    f"contentUrl={content_url[:80]}"
                )
                return None
        except Exception as e:
            logger.error(f"Error downloading attachment: {e}")
            return None

    # ------------------------------------------------------------------
    # Requirement processing (Paso 2)
    # ------------------------------------------------------------------

    async def _process_requirement_attachments(
        self,
        turn_context: TurnContext,
        user_aad_id: str,
        user_state: dict,
        attachments: dict,
    ):
        """
        Process attachments for a requirement in Paso 2.

        1. Download & analyze Excel with Document Intelligence
        2. Upload Excel to Blob Storage
        3. If PDF attached → analyze & upload; else → auto-generate PDF
        4. Save metadata to Blob Storage
        5. Create Planner task via Graph API
        6. Reply with confirmation
        """
        req_id = user_state.get("req_id", "")
        n8n_result = user_state.get("n8n_result", {})
        solicitante = user_state.get("solicitante", {})
        titulo = n8n_result.get("titulo", "Sin título")
        descripcion = n8n_result.get("descripcion", "")
        mensaje_original = user_state.get("mensaje_original", "")

        excel_files = attachments.get("excel", [])
        pdf_files = attachments.get("pdf", [])

        if not excel_files:
            await turn_context.send_activity(
                Activity(
                    type=ActivityTypes.message,
                    text_format="markdown",
                    text=(
                        "No se encontró un archivo **Excel (.xlsx)** adjunto.\n\n"
                        "Adjunta un archivo Excel con las columnas:\n\n"
                        "- **Artículo** — nombre del equipo o recurso\n\n"
                        "- **Cantidad** — unidades necesarias\n\n"
                        "- **Comentario** — descripción del desperfecto\n\n"
                        "También puedes adjuntar un **PDF de sustento** (opcional).\n\n"
                        "_Escribe **cancelar** para abandonar este requerimiento._"
                    ),
                )
            )
            return

        # --- Acuse de procesamiento ---
        await turn_context.send_activity(
            Activity(
                type=ActivityTypes.message,
                text_format="markdown",
                text=(
                    f"Archivos recibidos. Procesando requerimiento **{req_id}**...\n\n"
                    f"Excel: {excel_files[0]['name']}\n\n"
                    + (f"PDF: {pdf_files[0]['name']}\n\n" if pdf_files else "PDF: se generará automáticamente\n\n")
                    + "Analizando documentos..."
                ),
            )
        )

        articulos = []
        excel_blob_info = {}
        excel_analysis = {}
        pdf_blob_info = {}
        pdf_analysis = {}
        pdf_was_generated = False

        try:
            # === 1. EXCEL: Download → Analyze → Upload ===
            excel_bytes = await self._download_attachment(
                excel_files[0]["contentUrl"],
                excel_files[0].get("downloadUrl", ""),
            )
            if not excel_bytes:
                await turn_context.send_activity(
                    Activity(
                        type=ActivityTypes.message,
                        text="No se pudo descargar el archivo Excel. Intenta subirlo nuevamente.",
                    )
                )
                return

            # Analyze with Document Intelligence
            try:
                if not Config.DOC_INTELLIGENCE_ENDPOINT or not Config.DOC_INTELLIGENCE_KEY:
                    raise RuntimeError("DOC_INTELLIGENCE_ENDPOINT / KEY not configured")
                excel_analysis = await doc_intelligence_service.analyze_excel(excel_bytes)
                articulos = excel_analysis.get("articulos", [])
                logger.info(f"[REQ {req_id}] Excel analyzed via Document Intelligence: {len(articulos)} articles")

                # If DI succeeded but found 0 articles, fall back to openpyxl
                if not articulos:
                    logger.warning(
                        f"[REQ {req_id}] Document Intelligence returned 0 articles "
                        f"(tables={len(excel_analysis.get('rawTables', []))}). "
                        f"Falling back to openpyxl..."
                    )
                    articulos = self._fallback_parse_excel(excel_bytes)
                    if articulos:
                        excel_analysis["status"] = "fallback_after_di"
                        excel_analysis["extractedRows"] = len(articulos)
                        excel_analysis["articulos"] = articulos
                        logger.info(f"[REQ {req_id}] openpyxl fallback recovered {len(articulos)} articles")

            except Exception as e:
                logger.warning(f"[REQ {req_id}] Document Intelligence skipped/failed: {e}")
                excel_analysis = {"status": "fallback", "reason": str(e), "extractedRows": 0}
                # Try fallback with openpyxl
                logger.info(f"[REQ {req_id}] Using openpyxl fallback parser...")
                articulos = self._fallback_parse_excel(excel_bytes)

            # Upload to Blob Storage
            try:
                excel_blob_info = await blob_service.upload_excel(
                    req_id, excel_bytes, excel_files[0]["name"]
                )
            except Exception as e:
                logger.error(f"[REQ {req_id}] Blob upload Excel error: {e}")
                excel_blob_info = {"error": str(e)}

            # === 2. PDF: Download/Generate → Analyze → Upload ===
            if pdf_files:
                # User uploaded a PDF
                pdf_bytes = await self._download_attachment(
                    pdf_files[0]["contentUrl"],
                    pdf_files[0].get("downloadUrl", ""),
                )
                if pdf_bytes:
                    pdf_was_generated = False
                    try:
                        pdf_analysis = await doc_intelligence_service.analyze_pdf(pdf_bytes)
                    except Exception as e:
                        logger.error(f"[REQ {req_id}] Document Intelligence PDF error: {e}")
                        pdf_analysis = {"status": "error", "error": str(e)}
                    try:
                        pdf_blob_info = await blob_service.upload_pdf(
                            req_id, pdf_bytes, pdf_files[0]["name"]
                        )
                    except Exception as e:
                        logger.error(f"[REQ {req_id}] Blob upload PDF error: {e}")
                        pdf_blob_info = {"error": str(e)}
                else:
                    logger.warning(f"[REQ {req_id}] Could not download user PDF, will auto-generate")
                    pdf_files = []  # Fall through to auto-generation

            if not pdf_files:
                # Auto-generate PDF sustento
                try:
                    pdf_bytes = generate_sustento_pdf(
                        req_id=req_id,
                        solicitante=solicitante,
                        titulo=titulo,
                        descripcion=descripcion,
                        articulos=articulos,
                        mensaje_original=mensaje_original,
                    )
                    pdf_was_generated = True
                    pdf_blob_info = await blob_service.upload_pdf(
                        req_id, pdf_bytes, "sustento_autogenerado.pdf"
                    )
                    logger.info(f"[REQ {req_id}] Auto-generated PDF uploaded")
                except Exception as e:
                    logger.error(f"[REQ {req_id}] PDF generation/upload error: {e}")
                    pdf_blob_info = {"error": str(e)}

            # === 3. METADATA JSON en Blob (reemplaza Cosmos DB) ===
            metadata = {
                "req_id": req_id,
                "tipo": "REQUERIMIENTO",
                "titulo": titulo,
                "descripcion": descripcion,
                "mensaje_original": mensaje_original,
                "solicitante": solicitante,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "PROCESADO",
                "excel": {
                    "blob_info": excel_blob_info,
                    "analysis": excel_analysis,
                    "articulos": articulos,
                },
                "pdf": {
                    "blob_info": pdf_blob_info,
                    "generated": pdf_was_generated,
                    "analysis": pdf_analysis if not pdf_was_generated else None,
                },
            }

            try:
                await blob_service.upload_metadata(req_id, metadata)
                logger.info(f"[REQ {req_id}] metadata.json saved to Blob")
            except Exception as e:
                logger.error(f"[REQ {req_id}] Blob metadata error: {e}")

            # === 4. NOTIFICAR A n8n PARA CREAR TAREA EN PLANNER ===
            planner_task_id = ""
            try:
                blob_urls = {}
                if excel_blob_info.get("blobName"):
                    blob_urls["excel_url"] = blob_service.generate_sas_url(
                        excel_blob_info["blobName"], expiry_hours=168
                    )
                if pdf_blob_info.get("blobName"):
                    blob_urls["pdf_url"] = blob_service.generate_sas_url(
                        pdf_blob_info["blobName"], expiry_hours=168
                    )

                planner_result = await self._call_n8n_create_planner(
                    req_id=req_id,
                    titulo=titulo,
                    descripcion=descripcion,
                    solicitante=solicitante,
                    articulos=articulos,
                    blob_urls=blob_urls,
                )
                planner_task_id = (
                    planner_result.get("planner_task_id", "") if planner_result else ""
                )
                if planner_task_id:
                    logger.info(
                        f"[REQ {req_id}] Tarea Planner creada vía n8n: {planner_task_id}"
                    )
                    metadata["planner_task_id"] = planner_task_id
                    await blob_service.upload_metadata(req_id, metadata)
            except Exception as e:
                logger.error(f"[REQ {req_id}] Error al crear tarea Planner: {e}")

            # === 5. Build summary response ===
            total_qty = sum(a.get("cantidad", 1) for a in articulos)

            # Articles preview (max 5)
            art_preview = ""
            for i, art in enumerate(articulos[:5], 1):
                art_preview += (
                    f"  {i}. **{art['articulo']}** × {art['cantidad']}"
                    f" — _{art.get('comentario', 'Sin comentario')}_\n"
                )
            if len(articulos) > 5:
                art_preview += f"  ... y {len(articulos) - 5} artículos más\n"

            planner_line = (
                f"**Tarea en Planner:** creada (ID: `{planner_task_id[:8]}...`)"
                if planner_task_id
                else "**Tarea en Planner:** pendiente de creación"
            )

            reply_text = (
                f"**Requerimiento {req_id} procesado exitosamente**\n\n"
                f"**{titulo}**\n\n"
                f"{descripcion}\n\n"
                f"**Excel procesado:** {len(articulos)} artículos, "
                f"{total_qty} unidades totales\n\n"
                f"{art_preview}\n"
                f"**PDF de sustento:** "
                f"{'Generado automáticamente' if pdf_was_generated else 'Subido por el usuario'}\n\n"
                f"Archivos almacenados en Azure:\n\n"
                f"- Excel: `{excel_blob_info.get('blobName', 'N/A')}`\n\n"
                f"- PDF: `{pdf_blob_info.get('blobName', 'N/A')}`\n\n"
                f"{planner_line}\n\n"
                "Recibirás una notificación cuando haya actualizaciones."
            )

            # Clear user state → back to idle
            self._clear_user_state(user_aad_id)

        except Exception as e:
            logger.error(f"[REQ {req_id}] Unexpected error: {e}")
            reply_text = (
                f"Error al procesar el requerimiento **{req_id}**.\n\n"
                f"Detalle: {str(e)[:200]}\n\n"
                "Intenta nuevamente o escribe **cancelar** para abandonar."
            )

        await turn_context.send_activity(
            Activity(type=ActivityTypes.message, text_format="markdown", text=reply_text)
        )

    @staticmethod
    def _fallback_parse_excel(file_bytes: bytes) -> list[dict]:
        """
        Fallback parser using openpyxl when Document Intelligence fails.
        Tries to read the first sheet and map columns heuristically.
        """
        try:
            import io
            import openpyxl

            wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True)
            ws = wb.active
            if ws is None:
                logger.warning("Fallback parser: no active sheet found")
                return []

            rows = list(ws.iter_rows(values_only=True))
            logger.info(f"Fallback parser: {len(rows)} rows found in sheet '{ws.title}'")
            if len(rows) < 2:
                logger.warning("Fallback parser: less than 2 rows (need header + data)")
                return []

            # First row = headers
            headers = [str(h or "").lower().strip() for h in rows[0]]
            logger.info(f"Fallback parser: headers = {headers}")

            # Find columns
            art_idx = None
            qty_idx = None
            com_idx = None
            for i, h in enumerate(headers):
                if any(k in h for k in ["articulo", "artículo", "item", "producto", "equipo", "nombre"]):
                    art_idx = i
                elif any(k in h for k in ["cantidad", "cant", "qty", "unidades"]):
                    qty_idx = i
                elif any(k in h for k in ["comentario", "observacion", "observación", "nota", "desperfecto", "motivo"]):
                    com_idx = i

            if art_idx is None:
                art_idx = 0

            articulos = []
            for row in rows[1:]:
                art = str(row[art_idx] or "").strip() if art_idx < len(row) else ""
                if not art:
                    continue
                qty_val = row[qty_idx] if qty_idx is not None and qty_idx < len(row) else 1
                try:
                    qty = int(float(qty_val)) if qty_val else 1
                except (ValueError, TypeError):
                    qty = 1
                com = str(row[com_idx] or "").strip() if com_idx is not None and com_idx < len(row) else ""
                articulos.append({"articulo": art, "cantidad": qty, "comentario": com})

            wb.close()
            logger.info(f"Fallback Excel parser: {len(articulos)} articles extracted")
            return articulos

        except Exception as e:
            logger.error(f"Fallback Excel parser failed: {e}")
            return []

    # ------------------------------------------------------------------
    # Handlers de actividades 1:1
    # ------------------------------------------------------------------

    async def on_conversation_update_activity(self, turn_context: TurnContext):
        """
        Se dispara cuando el usuario abre un chat 1:1 con el bot
        o el bot es agregado a la conversación personal.
        """
        activity = turn_context.activity
        self._save_reference(turn_context)

        # Saludo de bienvenida al iniciar 1:1
        if activity.members_added:
            bot_id = activity.recipient.id
            for member in activity.members_added:
                if member.id != bot_id:
                    await turn_context.send_activity(
                        Activity(
                            type=ActivityTypes.message,
                            text_format="markdown",
                            text=(
                                "Bienvenido a **DAPPS Bot**.\n\n"
                                "Este canal privado está habilitado para:\n\n"
                                "- **Requerimientos de TI** (solicitud de equipos, con soporte de Excel y PDF)\n\n"
                                "- **Incidencias** (reporte de fallos o problemas técnicos)\n\n"
                                "- **Consultas** (preguntas sobre procesos internos)\n\n"
                                "Escribe tu solicitud y será clasificada automáticamente."
                            ),
                        )
                    )
        logger.info(
            f"[PERSONAL] ConversationUpdate en: {activity.conversation.id}"
        )

    async def on_message_activity(self, turn_context: TurnContext):
        """
        Mensaje recibido en chat 1:1.
        SIEMPRE responde con una Activity de tipo 'message'.

        Flujo de 2 pasos para Requerimientos:
          Paso 1: Texto → n8n clasificación → si REQUERIMIENTO → pedir Excel
          Paso 2: Adjuntos recibidos → procesar Excel/PDF → Blob Storage
            → metadata.json + Planner task vía Graph API

        Otros tipos (INCIDENCIA, CONSULTA, IGNORAR):
          Flujo normal sin cambios.
        """
        self._save_reference(turn_context)

        activity = turn_context.activity
        user_text = (activity.text or "").strip()
        user_name = getattr(activity.from_property, "name", "usuario")
        user_aad_id = getattr(activity.from_property, "aad_object_id", None) or \
                      getattr(activity.from_property, "id", "unknown")
        user_email = getattr(activity.from_property, "email", "") or ""

        logger.info(f"[PERSONAL] Mensaje de {user_name}: {user_text[:120]}")

        # Check current conversational state
        current_state = self._get_user_state(user_aad_id)
        state_name = current_state.get("state", "idle")

        # -----------------------------------------------------------------
        # Cancel command (works in any state)
        # -----------------------------------------------------------------
        if user_text.lower() in _CANCEL_CMDS and state_name != "idle":
            req_id = current_state.get("req_id", "")
            self._clear_user_state(user_aad_id)
            await turn_context.send_activity(
                Activity(
                    type=ActivityTypes.message,
                    text=(
                        f"Requerimiento **{req_id}** cancelado.\n\n"
                        "Puedes iniciar uno nuevo cuando lo necesites."
                    ),
                )
            )
            return

        # -----------------------------------------------------------------
        # PASO 2: Si estamos esperando archivos adjuntos
        # -----------------------------------------------------------------
        if state_name == "esperando_excel":
            attachments = self._extract_attachments(activity)
            has_files = attachments["excel"] or attachments["pdf"]

            if has_files:
                await self._process_requirement_attachments(
                    turn_context, user_aad_id, current_state, attachments
                )
                return
            else:
                # No files detected — remind user
                await turn_context.send_activity(
                    Activity(
                        type=ActivityTypes.message,
                        text_format="markdown",
                        text=(
                            "No se detectaron archivos adjuntos en tu mensaje.\n\n"
                            "Adjunta un archivo **Excel (.xlsx)** con las columnas:\n\n"
                            "- **Artículo** — nombre del equipo o recurso\n\n"
                            "- **Cantidad** — número de unidades\n\n"
                            "- **Comentario** — descripción del desperfecto\n\n"
                            "Opcionalmente puedes adjuntar un **PDF de sustento**.\n\n"
                            "_Escribe **cancelar** para abandonar este requerimiento._"
                        ),
                    )
                )
                return

        # -----------------------------------------------------------------
        # PASO 1: Estado idle — flujo normal
        # -----------------------------------------------------------------

        # 1) Mensajes vacíos
        if not user_text:
            await turn_context.send_activity(
                Activity(
                    type=ActivityTypes.message,
                    text="El bot está activo. Escribe tu solicitud para continuar.",
                )
            )
            return

        # 2) Saludos → respuesta local
        if user_text.lower() in _GREETINGS:
            await turn_context.send_activity(
                Activity(
                    type=ActivityTypes.message,
                    text_format="markdown",
                    text=(
                        f"Hola, **{user_name}**.\n\n"
                        "Puedes escribir:\n\n"
                        "- Un **problema técnico** que necesites reportar (incidencia)\n\n"
                        "- Una **solicitud** de equipos o recursos (requerimiento)\n\n"
                        "- Una **pregunta** sobre procesos internos (consulta)"
                    ),
                )
            )
            return

        # 3) Ayuda → respuesta local
        if user_text.lower() in _HELP_CMDS:
            await turn_context.send_activity(
                Activity(
                    type=ActivityTypes.message,
                    text_format="markdown",
                    text=(
                        "**Guía de uso — DAPPS Bot**\n\n"
                        "Escribe un mensaje describiendo tu necesidad "
                        "y será clasificado automáticamente:\n\n"
                        "- **Incidencia** — reporte de fallos o problemas técnicos\n\n"
                        "- **Requerimiento** — solicitud de equipos o recursos "
                        "(se solicitará un archivo Excel con los artículos)\n\n"
                        "- **Consulta** — pregunta sobre procesos internos\n\n"
                        "Ejemplo: _\"Necesito solicitar 5 monitores y 10 teclados "
                        "porque los actuales están dañados\"_"
                    ),
                )
            )
            return

        # 4) Mensaje operativo → acuse + reenvío a n8n
        await turn_context.send_activity(
            Activity(
                type=ActivityTypes.message,
                text=(
                    f"Recibido, **{user_name}**. "
                    "Procesando tu solicitud..."
                ),
            )
        )

        # Reenviar a n8n
        n8n_result = await self._forward_to_n8n(
            user_text=user_text,
            user_name=user_name,
            user_aad_id=user_aad_id,
            user_email=user_email,
            conversation_id=activity.conversation.id,
        )

        # Construir respuesta según resultado de n8n
        if n8n_result and n8n_result.get("tipo"):
            tipo = n8n_result.get("tipo", "").upper()
            titulo = n8n_result.get("titulo", "Sin título")
            descripcion = n8n_result.get("descripcion", "")

            if tipo == "REQUERIMIENTO":
                # *** PASO 1 → Iniciar flujo de 2 pasos ***
                req_id = BlobStorageService.generate_req_id()
                self._set_user_state(user_aad_id, {
                    "state": "esperando_excel",
                    "req_id": req_id,
                    "n8n_result": n8n_result,
                    "solicitante": {
                        "nombre": user_name,
                        "aadObjectId": user_aad_id,
                        "email": user_email,
                    },
                    "mensaje_original": user_text,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })

                reply_text = (
                    f"**REQUERIMIENTO detectado**\n\n"
                    f"**{titulo}**\n\n"
                    f"{descripcion}\n\n"
                    f"ID asignado: **{req_id}**\n\n"
                    "---\n\n"
                    "**Siguiente paso:** Adjunta un archivo **Excel (.xlsx)** "
                    "con las siguientes columnas:\n\n"
                    "| Artículo | Cantidad | Comentario |\n"
                    "|----------|----------|------------|\n"
                    "| Monitor 24\" | 5 | Pantallas con píxeles muertos |\n"
                    "| Teclado USB | 10 | Teclas trabadas |\n\n"
                    "También puedes adjuntar un **PDF de sustento** (opcional). "
                    "Si no se adjunta, se generará automáticamente.\n\n"
                    "_Escribe **cancelar** para abandonar este requerimiento._"
                )

            elif tipo == "INCIDENCIA":
                reply_text = (
                    f"**INCIDENCIA registrada**\n\n"
                    f"**{titulo}**\n\n"
                    f"{descripcion}\n\n"
                    "Se creará una tarea en Planner. "
                    "Recibirás una notificación cuando haya actualizaciones."
                )

            elif tipo == "CONSULTA":
                reply_text = (
                    f"**Consulta recibida**\n\n"
                    f"**{titulo}**\n\n"
                    f"{descripcion}\n\n"
                    "Tu consulta ha sido registrada. "
                    "Un miembro del equipo te responderá a la brevedad."
                )

            else:  # IGNORAR
                reply_text = (
                    f"Mensaje procesado.\n\n"
                    f"> _{user_text[:200]}_"
                )
        else:
            # n8n no respondió o falló → confirmación genérica
            reply_text = (
                f"Mensaje recibido, **{user_name}**.\n\n"
                f"> _{user_text[:300]}_\n\n"
                "Tu solicitud ha sido registrada. "
                "Recibirás una notificación cuando haya una actualización."
            )

        await turn_context.send_activity(
            Activity(
                type=ActivityTypes.message,
                text_format="markdown",
                text=reply_text,
            )
        )

    async def on_unrecognized_activity_type(self, turn_context: TurnContext):
        """Catch-all: loguear sin responder."""
        logger.info(
            f"[PERSONAL] Actividad no manejada: "
            f"type={turn_context.activity.type}"
        )
