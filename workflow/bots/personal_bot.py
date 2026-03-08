"""
DAPPS Bot — Lógica de CHAT PERSONAL (1:1).

Responsabilidades:
  - Persistir ConversationReferences por usuario (mensajes proactivos)
  - Gestionar estado conversacional (idle / esperando_excel)
  - Rutear mensajes: saludos, ayuda, cancelar, RAG consultas, n8n
  - Delegar procesamiento de requerimientos a handlers/

Toda la lógica pesada (adjuntos, Excel, Planner) vive en handlers/.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Dict, Optional

from botbuilder.core import ActivityHandler, TurnContext
from botbuilder.schema import Activity, ActivityTypes, ConversationReference

from config import Config
from handlers.attachments import extract_attachments
from handlers.requirement_processor import process_requirement
from services.blob_service import BlobStorageService
from services.n8n_client import n8n_client

logger = logging.getLogger(__name__)

# Rutas de persistencia (ahora en data/)
_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_REFS_FILE = os.path.join(_DATA_DIR, "conversation_references_personal.json")
_STATE_FILE = os.path.join(_DATA_DIR, "user_state.json")

# Conjuntos de comandos reconocidos
_GREETINGS = {"hola", "hi", "hey", "buenos dias", "buenas", "buenas tardes", "buenas noches", "buen dia"}
_HELP_CMDS = {"ayuda", "help", "?"}
_CANCEL_CMDS = {"cancelar", "cancel", "salir", "exit"}


class DappsPersonalBot(ActivityHandler):
    """
    Bot conversacional 1:1 para Microsoft Teams.

    Estado conversacional por usuario:
      - idle                → esperando nuevo mensaje
      - esperando_excel     → bot pidió que suba Excel con artículos
    """

    def __init__(self):
        super().__init__()
        self.personal_references: Dict[str, dict] = {}
        self.user_states: Dict[str, dict] = {}
        self._load_references()
        self._load_user_states()

    # ==================================================================
    # Persistencia de ConversationReferences
    # ==================================================================

    def _load_references(self):
        if not os.path.exists(_REFS_FILE):
            return
        try:
            with open(_REFS_FILE, "r") as f:
                self.personal_references = json.load(f)
            logger.info("Cargadas %d refs personales.", len(self.personal_references))
        except Exception as e:
            logger.warning("No se pudo cargar refs personales: %s", e)

    def _save_reference(self, turn_context: TurnContext):
        activity = turn_context.activity
        ref = TurnContext.get_conversation_reference(activity)
        user_id = (
            getattr(activity.from_property, "aad_object_id", None)
            or getattr(activity.from_property, "id", "unknown")
        )
        self.personal_references[user_id] = _serialize(ref)
        _write_json(_REFS_FILE, self.personal_references)
        logger.info("Ref personal guardada: %s", user_id)

    def get_reference_for_user(self, user_aad_id: str) -> Optional[ConversationReference]:
        ref_data = self.personal_references.get(user_aad_id)
        if not ref_data:
            return None
        try:
            return ConversationReference().from_dict(ref_data)
        except Exception:
            return ConversationReference(
                activity_id=ref_data.get("activity_id"),
                bot=ref_data.get("bot"),
                channel_id=ref_data.get("channel_id"),
                conversation=ref_data.get("conversation"),
                locale=ref_data.get("locale"),
                service_url=ref_data.get("service_url"),
                user=ref_data.get("user"),
            )

    # ==================================================================
    # Estado conversacional por usuario
    # ==================================================================

    def _load_user_states(self):
        if not os.path.exists(_STATE_FILE):
            return
        try:
            with open(_STATE_FILE, "r") as f:
                self.user_states = json.load(f)
            logger.info("Cargados %d estados de usuario.", len(self.user_states))
        except Exception as e:
            logger.warning("No se pudo cargar estados: %s", e)

    def _save_user_states(self):
        _write_json(_STATE_FILE, self.user_states)

    def _get_state(self, uid: str) -> dict:
        return self.user_states.get(uid, {"state": "idle"})

    def _set_state(self, uid: str, state: dict):
        self.user_states[uid] = state
        self._save_user_states()

    def _clear_state(self, uid: str):
        self.user_states.pop(uid, None)
        self._save_user_states()

    # ==================================================================
    # Activity Handlers
    # ==================================================================

    async def on_conversation_update_activity(self, turn_context: TurnContext):
        activity = turn_context.activity
        self._save_reference(turn_context)

        if activity.members_added:
            bot_id = activity.recipient.id
            for member in activity.members_added:
                if member.id != bot_id:
                    await _reply(turn_context, _MSG_WELCOME)

        logger.info("[PERSONAL] ConversationUpdate: %s", activity.conversation.id)

    async def on_message_activity(self, turn_context: TurnContext):
        self._save_reference(turn_context)

        activity = turn_context.activity
        user_text = (activity.text or "").strip()
        user_name = getattr(activity.from_property, "name", "usuario")
        user_aad_id = (
            getattr(activity.from_property, "aad_object_id", None)
            or getattr(activity.from_property, "id", "unknown")
        )
        user_email = getattr(activity.from_property, "email", "") or ""

        logger.info("[PERSONAL] %s: %s", user_name, user_text[:120])

        state = self._get_state(user_aad_id)
        state_name = state.get("state", "idle")

        # --- Cancelar (cualquier estado) ---
        if user_text.lower() in _CANCEL_CMDS and state_name != "idle":
            req_id = state.get("req_id", "")
            self._clear_state(user_aad_id)
            await _reply(
                turn_context,
                f"Requerimiento **{req_id}** cancelado.\n\n"
                "Puedes iniciar uno nuevo cuando lo necesites.",
            )
            return

        # --- Paso 2: esperando adjuntos ---
        if state_name == "esperando_excel":
            await self._handle_attachments(turn_context, user_aad_id, state)
            return

        # --- Paso 1: estado idle ---
        await self._handle_idle(
            turn_context, user_text, user_name, user_aad_id, user_email
        )

    async def on_unrecognized_activity_type(self, turn_context: TurnContext):
        logger.info("[PERSONAL] Actividad no manejada: %s", turn_context.activity.type)

    # ==================================================================
    # Paso 2 — Adjuntos
    # ==================================================================

    async def _handle_attachments(
        self, turn_context: TurnContext, user_aad_id: str, state: dict
    ):
        attachments = extract_attachments(turn_context.activity)

        if not (attachments["excel"] or attachments["pdf"]):
            await _reply(turn_context, _MSG_REMIND_EXCEL)
            return

        await process_requirement(
            turn_context=turn_context,
            user_aad_id=user_aad_id,
            user_state=state,
            attachments=attachments,
            clear_state_fn=self._clear_state,
        )

    # ==================================================================
    # Paso 1 — Estado idle
    # ==================================================================

    async def _handle_idle(
        self, turn_context, user_text, user_name, user_aad_id, user_email
    ):
        # Mensajes vacíos
        if not user_text:
            await _reply(turn_context, "El bot está activo. Escribe tu solicitud.")
            return

        # Saludos
        if user_text.lower() in _GREETINGS:
            await _reply(turn_context, _msg_greeting(user_name))
            return

        # Ayuda
        if user_text.lower() in _HELP_CMDS:
            await _reply(turn_context, _MSG_HELP)
            return

        # Mensaje operativo → acuse + clasificación n8n
        await _reply(
            turn_context,
            f"Recibido, **{user_name}**. Procesando tu solicitud...",
        )

        conversation_id = turn_context.activity.conversation.id
        n8n_result = await n8n_client.classify(
            user_text, user_name, user_aad_id, user_email, conversation_id
        )

        reply = await self._build_classified_reply(
            n8n_result, user_text, user_name, user_aad_id, user_email
        )
        await _reply(turn_context, reply)

    # ==================================================================
    # Construcción de respuesta según clasificación
    # ==================================================================

    async def _build_classified_reply(
        self, n8n_result, user_text, user_name, user_aad_id, user_email
    ) -> str:
        if not n8n_result or not n8n_result.get("tipo"):
            return (
                f"Mensaje recibido, **{user_name}**.\n\n"
                f"> _{user_text[:300]}_\n\n"
                "Tu solicitud ha sido registrada."
            )

        tipo = n8n_result["tipo"].upper()
        titulo = n8n_result.get("titulo", "Sin título")
        descripcion = n8n_result.get("descripcion", "")

        if tipo == "REQUERIMIENTO":
            return self._start_requirement_flow(
                n8n_result, user_text, user_name, user_aad_id, user_email, titulo, descripcion
            )

        if tipo == "INCIDENCIA":
            return (
                f"**INCIDENCIA registrada**\n\n"
                f"**{titulo}**\n\n{descripcion}\n\n"
                "Se creará una tarea en Planner. "
                "Recibirás una notificación cuando haya actualizaciones."
            )

        if tipo == "CONSULTA":
            return await self._handle_consulta(
                user_text, user_name, user_aad_id, user_email, titulo, descripcion
            )

        # IGNORAR u otro
        return f"Mensaje procesado.\n\n> _{user_text[:200]}_"

    def _start_requirement_flow(
        self, n8n_result, user_text, user_name, user_aad_id, user_email,
        titulo, descripcion,
    ) -> str:
        """Inicia el flujo de 2 pasos: guarda estado y pide Excel."""
        req_id = BlobStorageService.generate_req_id()
        self._set_state(user_aad_id, {
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

        return (
            f"**REQUERIMIENTO detectado**\n\n"
            f"**{titulo}**\n\n{descripcion}\n\n"
            f"ID asignado: **{req_id}**\n\n---\n\n"
            "**Siguiente paso:** Adjunta un archivo **Excel (.xlsx)** "
            "con las siguientes columnas:\n\n"
            "| Artículo | Cantidad | Comentario |\n"
            "|----------|----------|------------|\n"
            '| Monitor 24" | 5 | Pantallas con píxeles muertos |\n'
            "| Teclado USB | 10 | Teclas trabadas |\n\n"
            "También puedes adjuntar un **PDF de sustento** (opcional). "
            "Si no se adjunta, se generará automáticamente.\n\n"
            "_Escribe **cancelar** para abandonar este requerimiento._"
        )

    async def _handle_consulta(
        self, user_text, user_name, user_aad_id, user_email, titulo, descripcion
    ) -> str:
        """RAG → respuesta directa. Si no encuentra → escalar a Planner."""
        from services.rag_service import rag_service

        rag_result = await rag_service.answer(user_text, user_name)

        if rag_result.get("found") and rag_result.get("answer"):
            sources = ", ".join(rag_result.get("sources", [])) or "Base de conocimiento interna"
            logger.info(
                "[PERSONAL] RAG respondió (confianza: %.2f, fuentes: %s)",
                rag_result.get("confidence", 0), sources,
            )
            return (
                f"**{titulo}**\n\n"
                f"{rag_result['answer']}\n\n---\n"
                f"_📚 Fuente: {sources}_"
            )

        # Sin respuesta RAG → escalar
        logger.info("[PERSONAL] RAG sin respuesta, escalando a Planner.")
        solicitante = {
            "nombre": user_name,
            "aadObjectId": user_aad_id,
            "email": user_email,
        }
        escalation = await n8n_client.escalate_consulta(
            titulo, descripcion, solicitante, user_text
        )

        if escalation and escalation.get("status") == "ok":
            task_id = escalation.get("planner_task_id", "")
            id_display = f" (ID: `{task_id[:8]}...`)" if task_id else ""
            return (
                f"**Consulta escalada**\n\n"
                f"**{titulo}**\n\n{descripcion}\n\n"
                "No encontré información en la base de conocimiento interna.\n\n"
                f"Se ha creado una tarea en Planner{id_display} "
                "para que un miembro del equipo te responda.\n\n"
                "Recibirás una notificación cuando haya una actualización."
            )

        return (
            f"**Consulta recibida**\n\n"
            f"**{titulo}**\n\n{descripcion}\n\n"
            "No encontré información en la base de conocimiento.\n"
            "Un miembro del equipo te responderá a la brevedad."
        )


# ==================================================================
# Helpers (funciones puras, sin estado)
# ==================================================================

def _serialize(obj):
    """Serializa recursivamente un objeto Bot Framework a dict."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, list):
        return [_serialize(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if hasattr(obj, "__dict__"):
        return {
            k: _serialize(v) for k, v in obj.__dict__.items()
            if v is not None and not k.startswith("_")
        }
    return str(obj)


def _write_json(path: str, data):
    """Escribe JSON a disco, creando el directorio si no existe."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, default=str, indent=2)
    except Exception as e:
        logger.error("Error escribiendo %s: %s", path, e)


async def _reply(turn_context: TurnContext, text: str):
    """Envía un mensaje markdown al usuario."""
    await turn_context.send_activity(
        Activity(type=ActivityTypes.message, text_format="markdown", text=text)
    )


def _msg_greeting(user_name: str) -> str:
    return (
        f"Hola, **{user_name}**.\n\n"
        "Puedes escribir:\n\n"
        "- Un **problema técnico** que necesites reportar (incidencia)\n\n"
        "- Una **solicitud** de equipos o recursos (requerimiento)\n\n"
        "- Una **pregunta** sobre procesos internos (consulta)"
    )


_MSG_WELCOME = (
    "Bienvenido a **DAPPS Bot**.\n\n"
    "Este canal privado está habilitado para:\n\n"
    "- **Requerimientos de TI** (solicitud de equipos, con soporte de Excel y PDF)\n\n"
    "- **Incidencias** (reporte de fallos o problemas técnicos)\n\n"
    "- **Consultas** (preguntas sobre procesos internos)\n\n"
    "Escribe tu solicitud y será clasificada automáticamente."
)

_MSG_HELP = (
    "**Guía de uso — DAPPS Bot**\n\n"
    "Escribe un mensaje describiendo tu necesidad "
    "y será clasificado automáticamente:\n\n"
    "- **Incidencia** — reporte de fallos o problemas técnicos\n\n"
    "- **Requerimiento** — solicitud de equipos o recursos "
    "(se solicitará un archivo Excel con los artículos)\n\n"
    "- **Consulta** — pregunta sobre procesos internos\n\n"
    'Ejemplo: _"Necesito solicitar 5 monitores y 10 teclados '
    'porque los actuales están dañados"_'
)

_MSG_REMIND_EXCEL = (
    "No se detectaron archivos adjuntos en tu mensaje.\n\n"
    "Adjunta un archivo **Excel (.xlsx)** con las columnas:\n\n"
    "- **Artículo** — nombre del equipo o recurso\n\n"
    "- **Cantidad** — número de unidades\n\n"
    "- **Comentario** — descripción del desperfecto\n\n"
    "Opcionalmente puedes adjuntar un **PDF de sustento**.\n\n"
    "_Escribe **cancelar** para abandonar este requerimiento._"
)
