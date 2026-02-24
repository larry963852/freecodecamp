"""
DAPPS Bot — Lógica del bot para mensajes proactivos en Teams.

Este módulo maneja:
- Captura automática de conversationReference cuando el bot interactúa con Teams
- Envío de mensajes proactivos a un canal como identidad del bot ("DAPPS Bot")
"""

import json
import os
import logging
from typing import Optional

from botbuilder.core import (
    ActivityHandler,
    TurnContext,
    BotFrameworkAdapter,
    BotFrameworkAdapterSettings,
)
from botbuilder.schema import (
    Activity,
    ActivityTypes,
    ConversationReference,
    ConversationAccount,
    ChannelAccount,
)

from config import Config

logger = logging.getLogger(__name__)

# Archivo donde se persiste la conversationReference capturada
CONV_REF_FILE = os.path.join(os.path.dirname(__file__), "conversation_reference.json")


class DappsBot(ActivityHandler):
    """
    Bot conversacional de DAPPS para Microsoft Teams.
    Funciona en 1:1, team y groupchat.
    Captura conversationReference para mensajes proactivos.
    Responde a TODOS los mensajes entrantes (nunca deja un turn sin respuesta).
    """

    def __init__(self):
        super().__init__()
        self.conversation_reference: Optional[ConversationReference] = None
        self._load_conversation_reference()

    # ------------------------------------------------------------------
    # Persistencia de ConversationReference
    # ------------------------------------------------------------------

    def _load_conversation_reference(self):
        """Carga la conversationReference desde disco si existe."""
        if os.path.exists(CONV_REF_FILE):
            try:
                with open(CONV_REF_FILE, "r") as f:
                    data = json.load(f)
                self.conversation_reference = ConversationReference().from_dict(data)
                logger.info("ConversationReference cargada desde disco.")
            except Exception as e:
                logger.warning(f"No se pudo cargar conversationReference: {e}")

    def _save_conversation_reference(self, activity: Activity):
        """Guarda la conversationReference a disco para persistencia."""
        ref = TurnContext.get_conversation_reference(activity)
        self.conversation_reference = ref
        try:
            with open(CONV_REF_FILE, "w") as f:
                json.dump(
                    ref.__dict__ if hasattr(ref, "__dict__") else ref,
                    f, default=str, indent=2,
                )
            logger.info("ConversationReference guardada.")
        except Exception as e:
            logger.error(f"Error guardando conversationReference: {e}")

    # ------------------------------------------------------------------
    # Handlers de actividades de Teams
    # ------------------------------------------------------------------

    async def on_conversation_update_activity(self, turn_context: TurnContext):
        """
        Se dispara cuando el bot es instalado/actualizado o un miembro
        se une a la conversación.
        """
        self._save_conversation_reference(turn_context.activity)
        activity = turn_context.activity

        # Saludo al ser agregado a una conversación
        if activity.members_added:
            bot_id = activity.recipient.id
            for member in activity.members_added:
                if member.id != bot_id:
                    await turn_context.send_activity(
                        Activity(
                            type=ActivityTypes.message,
                            text=(
                                "👋 ¡Hola! Soy **DAPPS Bot**.\n\n"
                                "Estoy listo para recibir tus mensajes. "
                                "Escríbeme lo que necesites."
                            ),
                        )
                    )
        logger.info(
            f"ConversationUpdate en: {activity.conversation.id}"
        )

    async def on_message_activity(self, turn_context: TurnContext):
        """
        Se dispara por CADA mensaje entrante (1:1, team o groupchat).
        SIEMPRE responde con una Activity de tipo 'message' para que
        Teams no bloquee la conversación.
        """
        self._save_conversation_reference(turn_context.activity)

        user_text = (turn_context.activity.text or "").strip()
        user_name = getattr(turn_context.activity.from_property, "name", "usuario")

        logger.info(f"Mensaje de {user_name}: {user_text[:120]}")

        # --- Respuestas simples (extensible luego con lógica de negocio / n8n) ---
        if not user_text:
            reply_text = "👋 Hola, el bot está activo."
        else:
            reply_text = (
                f"✅ Mensaje recibido, **{user_name}**.\n\n"
                f"> _{user_text[:200]}_\n\n"
                "Estoy procesando tu solicitud."
            )

        await turn_context.send_activity(
            Activity(
                type=ActivityTypes.message,
                text=reply_text,
            )
        )

    async def on_unrecognized_activity_type(self, turn_context: TurnContext):
        """
        Catch-all para actividades que no manejamos explícitamente.
        Logueamos pero NO respondemos (evita loops con invoke, typing, etc.).
        """
        logger.info(
            f"Actividad no manejada: type={turn_context.activity.type}"
        )


def create_adapter() -> BotFrameworkAdapter:
    """Crea y retorna el adaptador de Bot Framework con las credenciales configuradas."""
    settings = BotFrameworkAdapterSettings(
        app_id=Config.APP_ID,
        app_password=Config.APP_PASSWORD,
        channel_auth_tenant=Config.TENANT_ID,
    )
    return BotFrameworkAdapter(settings)


def build_conversation_reference(bot_instance: DappsBot) -> ConversationReference:
    """
    Construye una ConversationReference para el canal objetivo.
    Usa la referencia capturada si existe, o construye una manualmente
    a partir de la configuración.
    """
    if bot_instance.conversation_reference:
        return bot_instance.conversation_reference

    # Construcción manual con los IDs conocidos
    logger.info("Construyendo conversationReference manualmente desde config.")
    return ConversationReference(
        channel_id="msteams",
        service_url=Config.SERVICE_URL,
        conversation=ConversationAccount(
            id=Config.TEAMS_CHANNEL_ID,
            is_group=True,
            conversation_type="channel",
            tenant_id=Config.TENANT_ID,
        ),
        bot=ChannelAccount(
            id=f"28:{Config.APP_ID}",
            name="DAPPS Bot",
        ),
    )

