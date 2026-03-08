"""
DAPPS Bot — Lógica de CANAL.

Captura la ConversationReference del canal para notificaciones proactivas.
Los mensajes en canal solo reciben aviso de usar chat 1:1.
"""

import json
import logging
import os
from typing import Optional

from botbuilder.core import ActivityHandler, TurnContext
from botbuilder.schema import (
    Activity,
    ActivityTypes,
    ChannelAccount,
    ConversationAccount,
    ConversationReference,
)

from config import Config

logger = logging.getLogger(__name__)

_CONV_REF_FILE = os.path.join(
    os.path.dirname(__file__), "..", "data", "conversation_reference_channel.json"
)


class DappsChannelBot(ActivityHandler):
    """Bot de canal: captura referencia y redirige al chat 1:1."""

    def __init__(self):
        super().__init__()
        self.conversation_reference: Optional[ConversationReference] = None
        self._load_reference()

    # ------------------------------------------------------------------
    # Persistencia
    # ------------------------------------------------------------------

    def _load_reference(self):
        if not os.path.exists(_CONV_REF_FILE):
            return
        try:
            with open(_CONV_REF_FILE, "r") as f:
                data = json.load(f)
            self.conversation_reference = ConversationReference().from_dict(data)
            logger.info("ConversationReference de canal cargada.")
        except Exception as e:
            logger.warning("No se pudo cargar ref de canal: %s", e)

    def _save_reference(self, activity):
        ref = TurnContext.get_conversation_reference(activity)
        self.conversation_reference = ref
        try:
            os.makedirs(os.path.dirname(_CONV_REF_FILE), exist_ok=True)
            with open(_CONV_REF_FILE, "w") as f:
                json.dump(
                    ref.__dict__ if hasattr(ref, "__dict__") else ref,
                    f, default=str, indent=2,
                )
            logger.info("ConversationReference de canal guardada.")
        except Exception as e:
            logger.error("Error guardando ref de canal: %s", e)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def on_conversation_update_activity(self, turn_context: TurnContext):
        self._save_reference(turn_context.activity)
        logger.info(
            "[CANAL] ConversationUpdate: %s",
            turn_context.activity.conversation.id,
        )

    async def on_message_activity(self, turn_context: TurnContext):
        self._save_reference(turn_context.activity)
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
        logger.info(
            "[CANAL] Actividad no manejada: %s", turn_context.activity.type
        )


def build_channel_conversation_reference(
    bot_instance: DappsChannelBot,
) -> ConversationReference:
    """Construye una ConversationReference para el canal objetivo."""
    if bot_instance.conversation_reference:
        return bot_instance.conversation_reference

    logger.info("Construyendo ref de canal desde config.")
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
