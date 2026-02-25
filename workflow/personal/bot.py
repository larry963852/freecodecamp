"""
DAPPS Bot — Lógica de CHAT PERSONAL (1:1).

Este módulo maneja:
- Conversaciones 1:1 con usuarios en Teams
- Almacenamiento de references por usuario para mensajes proactivos 1:1
- Reenvío de mensajes operativos a n8n para clasificación IA
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

logger = logging.getLogger(__name__)

# Archivo donde se persisten las conversationReferences de chats 1:1
PERSONAL_REFS_FILE = os.path.join(
    os.path.dirname(__file__), "..", "conversation_references_personal.json"
)

# Mensajes que NO se reenvían a n8n (saludos, ayuda, etc.)
_GREETINGS = {"hola", "hi", "hey", "buenos dias", "buenas", "buenas tardes", "buenas noches", "buen dia"}
_HELP_CMDS = {"ayuda", "help", "?"}


class DappsPersonalBot(ActivityHandler):
    """
    Bot conversacional 1:1 de DAPPS para Microsoft Teams.
    Almacena una ConversationReference por cada usuario que escribe,
    permitiendo enviar mensajes proactivos a cualquier usuario 1:1.
    Reenvía mensajes operativos a n8n para clasificación y procesamiento.
    """

    def __init__(self):
        super().__init__()
        # Dict: user_aad_id → ConversationReference serializada
        self.personal_references: Dict[str, dict] = {}
        self._load_references()

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

    def _save_reference(self, turn_context: TurnContext):
        """Guarda la conversationReference del usuario actual."""
        activity = turn_context.activity
        ref = TurnContext.get_conversation_reference(activity)

        # Identificador único del usuario (AAD Object ID)
        user_id = getattr(activity.from_property, "aad_object_id", None)
        if not user_id:
            user_id = getattr(activity.from_property, "id", "unknown")

        # Serializar la reference
        ref_dict = ref.__dict__ if hasattr(ref, "__dict__") else ref
        self.personal_references[user_id] = json.loads(
            json.dumps(ref_dict, default=str)
        )

        try:
            with open(PERSONAL_REFS_FILE, "w") as f:
                json.dump(self.personal_references, f, default=str, indent=2)
            logger.info(f"Reference personal guardada para usuario {user_id}.")
        except Exception as e:
            logger.error(f"Error guardando reference personal: {e}")

    def get_reference_for_user(self, user_aad_id: str) -> Optional[ConversationReference]:
        """Obtiene la ConversationReference de un usuario por su AAD Object ID."""
        ref_data = self.personal_references.get(user_aad_id)
        if ref_data:
            return ConversationReference().from_dict(ref_data)
        return None

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
                            text=(
                                "👋 ¡Hola! Soy **DAPPS Bot**.\n\n"
                                "Este chat privado es para:\n"
                                "• 📝 **Crear requerimientos** de TI\n"
                                "• 🔴 **Reportar incidencias**\n"
                                "• ❓ **Hacer consultas**\n\n"
                                "📢 El canal de Teams es solo para "
                                "**notificaciones automáticas** del sistema.\n\n"
                                "Escríbeme lo que necesites y lo clasificaré "
                                "automáticamente. 🚀"
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
        Flujo:
          1. Saludos/ayuda → respuesta local (no n8n)
          2. Texto operativo → reenvía a n8n → responde con resultado
        """
        self._save_reference(turn_context)

        activity = turn_context.activity
        user_text = (activity.text or "").strip()
        user_name = getattr(activity.from_property, "name", "usuario")
        user_aad_id = getattr(activity.from_property, "aad_object_id", None) or \
                      getattr(activity.from_property, "id", "unknown")
        user_email = getattr(activity.from_property, "email", "") or ""

        logger.info(f"[PERSONAL] Mensaje de {user_name}: {user_text[:120]}")

        # -----------------------------------------------------------------
        # 1) Mensajes vacíos
        # -----------------------------------------------------------------
        if not user_text:
            await turn_context.send_activity(
                Activity(
                    type=ActivityTypes.message,
                    text="👋 Hola, el bot está activo. Escríbeme tu solicitud.",
                )
            )
            return

        # -----------------------------------------------------------------
        # 2) Saludos → respuesta local
        # -----------------------------------------------------------------
        if user_text.lower() in _GREETINGS:
            await turn_context.send_activity(
                Activity(
                    type=ActivityTypes.message,
                    text=(
                        f"👋 ¡Hola **{user_name}**!\n\n"
                        "¿En qué puedo ayudarte hoy?\n\n"
                        "Puedes escribirme:\n"
                        "• Un **problema** que tengas (incidencia)\n"
                        "• Una **solicitud** que necesites (requerimiento)\n"
                        "• Una **pregunta** sobre algún tema (consulta)"
                    ),
                )
            )
            return

        # -----------------------------------------------------------------
        # 3) Ayuda → respuesta local
        # -----------------------------------------------------------------
        if user_text.lower() in _HELP_CMDS:
            await turn_context.send_activity(
                Activity(
                    type=ActivityTypes.message,
                    text=(
                        "ℹ️ **Guía rápida de DAPPS Bot**\n\n"
                        "Escríbeme un mensaje describiendo lo que necesitas "
                        "y lo clasificaré automáticamente como:\n\n"
                        "🔴 **Incidencia** — algo que no funciona\n"
                        "🟡 **Requerimiento** — algo que necesitas\n"
                        "🔵 **Consulta** — una pregunta\n\n"
                        "Ejemplo: _\"No puedo acceder al sistema SAP desde ayer\"_"
                    ),
                )
            )
            return

        # -----------------------------------------------------------------
        # 4) Mensaje operativo → acuse + reenvío a n8n
        # -----------------------------------------------------------------

        # Acuse de recibo inmediato
        await turn_context.send_activity(
            Activity(
                type=ActivityTypes.message,
                text=(
                    f"⏳ Recibido, **{user_name}**. "
                    "Estoy clasificando tu solicitud..."
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

            tipo_emoji = {
                "INCIDENCIA": "🔴",
                "REQUERIMIENTO": "🟡",
                "CONSULTA": "🔵",
                "IGNORAR": "⚪",
            }.get(tipo, "⚪")

            if tipo in ("INCIDENCIA", "REQUERIMIENTO"):
                reply_text = (
                    f"{tipo_emoji} **{tipo}** registrada\n\n"
                    f"📌 **{titulo}**\n"
                    f"📝 {descripcion}\n\n"
                    "Se creará una tarea en Planner y te notificaré "
                    "cuando haya actualizaciones."
                )
            elif tipo == "CONSULTA":
                reply_text = (
                    f"{tipo_emoji} **Consulta recibida**\n\n"
                    f"📌 **{titulo}**\n"
                    f"📝 {descripcion}\n\n"
                    "Tu consulta ha sido registrada. "
                    "Un miembro del equipo te responderá pronto."
                )
            else:
                reply_text = (
                    f"✅ Mensaje procesado.\n\n"
                    f"> _{user_text[:200]}_"
                )
        else:
            # n8n no respondió o falló → confirmación genérica
            reply_text = (
                f"✅ Mensaje recibido, **{user_name}**.\n\n"
                f"> _{user_text[:300]}_\n\n"
                "📋 Tu solicitud ha sido registrada. "
                "Te notificaré cuando haya una actualización."
            )

        await turn_context.send_activity(
            Activity(
                type=ActivityTypes.message,
                text=reply_text,
            )
        )

    async def on_unrecognized_activity_type(self, turn_context: TurnContext):
        """Catch-all: loguear sin responder."""
        logger.info(
            f"[PERSONAL] Actividad no manejada: "
            f"type={turn_context.activity.type}"
        )
