"""
DAPPS Bot — Lógica de CANAL.

Este módulo maneja:
- Captura de conversationReference del canal cuando el bot es instalado
- Construcción de ConversationReference para enviar notificaciones proactivas
- NO maneja conversaciones 1:1 (eso lo hace personal/bot.py)
"""

import json
import os
import logging
from typing import Optional

from botbuilder.core import ActivityHandler, TurnContext
from botbuilder.schema import (
    Activity,
    ActivityTypes,
    ConversationReference,
    ConversationAccount,
    ChannelAccount,
)

# Config se importa desde el directorio padre (bot-server/)
# app.py se ejecuta desde bot-server/, así que config está en sys.path
from config import Config

logger = logging.getLogger(__name__)

# Archivo donde se persiste la conversationReference del canal
CONV_REF_FILE = os.path.join(
    os.path.dirname(__file__), "..", "conversation_reference_channel.json"
)


class DappsChannelBot(ActivityHandler):
    """
    Bot que captura la conversationReference del canal
    y permite enviar mensajes proactivos desde n8n.
    Solo responde en canal/groupchat con un aviso de usar 1:1.
    """

    def __init__(self):
        super().__init__()
        self.conversation_reference: Optional[ConversationReference] = None
        self._load_conversation_reference()

    # ------------------------------------------------------------------
    # Persistencia de ConversationReference (canal)
    # ------------------------------------------------------------------

    def _load_conversation_reference(self):
        """Carga la conversationReference del canal desde disco."""
        if os.path.exists(CONV_REF_FILE):
            try:
                with open(CONV_REF_FILE, "r") as f:
                    data = json.load(f)
                self.conversation_reference = ConversationReference().from_dict(data)
                logger.info("ConversationReference de canal cargada desde disco.")
            except Exception as e:
                logger.warning(f"No se pudo cargar conversationReference de canal: {e}")

    def _save_conversation_reference(self, activity: Activity):
        """Guarda la conversationReference del canal a disco."""
        ref = TurnContext.get_conversation_reference(activity)
        self.conversation_reference = ref
        try:
            with open(CONV_REF_FILE, "w") as f:
                json.dump(
                    ref.__dict__ if hasattr(ref, "__dict__") else ref,
                    f, default=str, indent=2,
                )
            logger.info("ConversationReference de canal guardada.")
        except Exception as e:
            logger.error(f"Error guardando conversationReference de canal: {e}")

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def on_conversation_update_activity(self, turn_context: TurnContext):
        """Captura la referencia cuando el bot se instala en un canal."""
        self._save_conversation_reference(turn_context.activity)
        logger.info(
            f"[CANAL] ConversationUpdate en: "
            f"{turn_context.activity.conversation.id}"
        )

    async def on_message_activity(self, turn_context: TurnContext):
        """
        Mensaje recibido en canal o groupchat.
        Solo captura la referencia y envía un aviso de usar 1:1.
        """
        self._save_conversation_reference(turn_context.activity)

        await turn_context.send_activity(
            Activity(
                type=ActivityTypes.message,
                text=(
                    "📢 Este canal es solo para **notificaciones automáticas**.\n\n"
                    "Para crear requerimientos o incidencias, "
                    "escríbeme por **chat privado (1:1)**."
                ),
            )
        )

    async def on_unrecognized_activity_type(self, turn_context: TurnContext):
        """Catch-all: loguear sin responder."""
        logger.info(
            f"[CANAL] Actividad no manejada: type={turn_context.activity.type}"
        )


def build_channel_conversation_reference(
    bot_instance: DappsChannelBot,
) -> ConversationReference:
    """
    Construye una ConversationReference para el canal objetivo.
    Usa la referencia capturada si existe, o construye una manualmente.
    """
    if bot_instance.conversation_reference:
        return bot_instance.conversation_reference

    logger.info("Construyendo conversationReference de canal manualmente desde config.")
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
