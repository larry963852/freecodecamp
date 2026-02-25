"""
DAPPS Bot — Servidor HTTP unificado.

Servidor único en puerto 3978 que ruteapor tipo de conversación:
  - personal (1:1)   → DappsPersonalBot
  - channel/groupchat → DappsChannelBot

Endpoints:
  POST /api/messages          → Bot Framework webhook (Azure Bot Service apunta aquí)
  POST /api/notify            → n8n → notificación proactiva al CANAL
  POST /api/notify-personal   → n8n → notificación proactiva a un usuario en 1:1
  GET  /health                → Health check
"""

import logging
import traceback

from aiohttp import web
from botbuilder.core import (
    BotFrameworkAdapter,
    BotFrameworkAdapterSettings,
    TurnContext,
)
from botbuilder.schema import Activity, ActivityTypes, ConversationReference

from config import Config
from channel.bot import DappsChannelBot, build_channel_conversation_reference
from personal.bot import DappsPersonalBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ============================================================
# ADAPTADOR (compartido — mismas credenciales de Azure App)
# ============================================================

def create_adapter() -> BotFrameworkAdapter:
    """Crea el adaptador de Bot Framework con las credenciales de Azure."""
    settings = BotFrameworkAdapterSettings(
        app_id=Config.APP_ID,
        app_password=Config.APP_PASSWORD,
        channel_auth_tenant=Config.TENANT_ID,
    )
    return BotFrameworkAdapter(settings)


ADAPTER = create_adapter()

# --- Instancias de cada bot ---
CHANNEL_BOT = DappsChannelBot()
PERSONAL_BOT = DappsPersonalBot()


async def on_adapter_error(context: TurnContext, error: Exception):
    """Manejo global de errores del adaptador."""
    logger.error(f"Error en el adaptador: {error}")
    logger.error(traceback.format_exc())
    try:
        await context.send_activity("⚠️ Error interno del bot.")
    except Exception:
        pass  # Si ni siquiera podemos responder, no propagamos


ADAPTER.on_turn_error = on_adapter_error


# ============================================================
# POST /api/messages — Webhook principal de Azure Bot Service
# ============================================================

async def handle_messages(request: web.Request) -> web.Response:
    """
    Azure Bot Service envía TODAS las actividades aquí.
    Rutea internamente según conversationType:
      - 'personal'  → DappsPersonalBot (1:1)
      - 'channel' / 'groupChat' → DappsChannelBot
    SIEMPRE retorna HTTP 200.
    """
    if request.content_type != "application/json":
        return web.Response(status=415, text="Content-Type must be application/json")

    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    activity = Activity().deserialize(body)
    auth_header = request.headers.get("Authorization", "")

    # Determinar tipo de conversación
    conv_type = getattr(activity.conversation, "conversation_type", None) or ""
    conv_type = conv_type.lower()

    logger.info(
        f"[ROUTER] type={activity.type} | "
        f"conversationType={conv_type or 'N/A'} | "
        f"conversation={getattr(activity.conversation, 'id', 'N/A')} | "
        f"from={getattr(activity.from_property, 'name', 'N/A')}"
    )

    # Seleccionar bot handler según tipo de conversación
    if conv_type == "personal":
        bot_handler = PERSONAL_BOT.on_turn
    else:
        # channel, groupchat, o vacío (conversationUpdate de instalación)
        bot_handler = CHANNEL_BOT.on_turn

    try:
        response = await ADAPTER.process_activity(
            activity, auth_header, bot_handler
        )
        if response:
            return web.json_response(data=response.body, status=response.status)
        return web.Response(status=200)
    except Exception as e:
        logger.error(f"Error procesando actividad: {e}")
        logger.error(traceback.format_exc())
        return web.Response(status=200)


# ============================================================
# POST /api/notify — n8n envía notificaciones al CANAL
# ============================================================

async def handle_notify(request: web.Request) -> web.Response:
    """
    Endpoint que n8n llama para enviar un mensaje proactivo al canal de Teams.
    El mensaje aparecerá como "DAPPS Bot" en el canal configurado.

    Body esperado:
    {
      "message": "Texto del mensaje o HTML",
      "card": { ... adaptive card JSON ... }   ← opcional
    }
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Body JSON inválido"}, status=400)

    message_text = body.get("message", "")
    if not message_text:
        return web.json_response(
            {"error": "Campo 'message' es requerido"}, status=400
        )

    conversation_ref = build_channel_conversation_reference(CHANNEL_BOT)

    async def send_proactive(turn_context: TurnContext):
        html_message = message_text.replace("\n", "<br>")
        card_data = body.get("card")
        if card_data:
            card_activity = Activity(
                type=ActivityTypes.message,
                text=message_text,
                text_format="xml",
                attachments=[{
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": card_data,
                }],
            )
            await turn_context.send_activity(card_activity)
        else:
            await turn_context.send_activity(
                Activity(
                    type=ActivityTypes.message,
                    text=html_message,
                    text_format="xml",
                )
            )

    try:
        await ADAPTER.continue_conversation(
            conversation_ref, send_proactive, Config.APP_ID,
        )
        logger.info(f"[CANAL] Notificación enviada: {message_text[:80]}...")
        return web.json_response({
            "status": "ok",
            "target": "channel",
            "message": "Notificación enviada al canal",
        })
    except Exception as e:
        logger.error(f"Error enviando notificación al canal: {e}")
        logger.error(traceback.format_exc())
        return web.json_response({"error": str(e)}, status=500)


# ============================================================
# POST /api/notify-personal — n8n envía mensaje proactivo a un usuario 1:1
# ============================================================

async def handle_notify_personal(request: web.Request) -> web.Response:
    """
    Endpoint que n8n (u otro sistema) llama para enviar un mensaje
    proactivo a un usuario específico en su chat 1:1 con el bot.

    Body esperado:
    {
      "user_id": "AAD Object ID del usuario destino",
      "message": "Texto del mensaje",
      "card": { ... adaptive card ... }  ← opcional
    }
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Body JSON inválido"}, status=400)

    user_id = body.get("user_id", "").strip()
    message_text = body.get("message", "").strip()

    if not user_id:
        return web.json_response(
            {"error": "Campo 'user_id' (AAD Object ID) es requerido"}, status=400
        )
    if not message_text:
        return web.json_response(
            {"error": "Campo 'message' es requerido"}, status=400
        )

    # Buscar la ConversationReference del usuario
    conv_ref = PERSONAL_BOT.get_reference_for_user(user_id)
    if not conv_ref:
        return web.json_response(
            {
                "error": f"No se encontró conversación 1:1 para el usuario {user_id}. "
                         "El usuario debe escribirle al bot primero.",
                "available_users": list(PERSONAL_BOT.personal_references.keys()),
            },
            status=404,
        )

    async def send_personal(turn_context: TurnContext):
        card_data = body.get("card")
        if card_data:
            card_activity = Activity(
                type=ActivityTypes.message,
                text=message_text,
                attachments=[{
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": card_data,
                }],
            )
            await turn_context.send_activity(card_activity)
        else:
            await turn_context.send_activity(
                Activity(
                    type=ActivityTypes.message,
                    text=message_text,
                )
            )

    try:
        await ADAPTER.continue_conversation(
            conv_ref, send_personal, Config.APP_ID,
        )
        logger.info(
            f"[PERSONAL] Notificación enviada a {user_id}: {message_text[:80]}..."
        )
        return web.json_response({
            "status": "ok",
            "target": "personal",
            "user_id": user_id,
            "message": "Notificación enviada al usuario 1:1",
        })
    except Exception as e:
        logger.error(f"Error enviando notificación personal a {user_id}: {e}")
        logger.error(traceback.format_exc())
        return web.json_response({"error": str(e)}, status=500)


# ============================================================
# GET /health
# ============================================================

async def handle_health(request: web.Request) -> web.Response:
    """Health check con estado de ambos bots."""
    return web.json_response({
        "status": "healthy",
        "bot": "DAPPS Bot (unified)",
        "port": Config.PORT,
        "channel": {
            "conversation_reference_captured": CHANNEL_BOT.conversation_reference is not None,
        },
        "personal": {
            "active_users": len(PERSONAL_BOT.personal_references),
            "user_ids": list(PERSONAL_BOT.personal_references.keys()),
        },
        "app_id": Config.APP_ID[:8] + "..." if Config.APP_ID else "NOT SET",
    })


# ============================================================
# SERVIDOR
# ============================================================

def create_app() -> web.Application:
    """Crea y configura la aplicación aiohttp."""
    app = web.Application()
    app.router.add_post("/api/messages", handle_messages)
    app.router.add_post("/api/notify", handle_notify)
    app.router.add_post("/api/notify-personal", handle_notify_personal)
    app.router.add_get("/health", handle_health)
    return app


if __name__ == "__main__":
    logger.info(f"Iniciando DAPPS Bot (unified) en puerto {Config.PORT}...")
    logger.info(
        f"App ID: {Config.APP_ID[:8]}..."
        if Config.APP_ID
        else "⚠️  APP_ID no configurado"
    )
    logger.info(
        "Endpoints:\n"
        "  POST /api/messages          ← Azure Bot Service webhook\n"
        "  POST /api/notify            ← n8n → canal\n"
        "  POST /api/notify-personal   ← n8n → usuario 1:1\n"
        "  GET  /health                ← Health check"
    )
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=Config.PORT)