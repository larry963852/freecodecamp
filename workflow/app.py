"""
DAPPS Bot — Servidor HTTP.

Endpoints:
  POST /api/messages  → Bot Framework (recibe actividades de Teams)
  POST /api/notify    → n8n lo llama para enviar mensajes proactivos al canal
  GET  /health        → Health check
"""

import asyncio
import logging
import traceback
from functools import wraps

from aiohttp import web
from botbuilder.core import TurnContext
from botbuilder.schema import Activity, ActivityTypes

from config import Config
from bot import DappsBot, create_adapter, build_conversation_reference

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# --- Instancias globales ---
ADAPTER = create_adapter()
BOT = DappsBot()


async def on_adapter_error(context: TurnContext, error: Exception):
    """Manejo global de errores del adaptador."""
    logger.error(f"Error en el adaptador: {error}")
    logger.error(traceback.format_exc())
    await context.send_activity("⚠️ Error interno del bot.")


ADAPTER.on_turn_error = on_adapter_error


# ============================================================
# ENDPOINTS
# ============================================================

async def handle_messages(request: web.Request) -> web.Response:
    """
    POST /api/messages
    Endpoint principal de Bot Framework (conversacional).
    Teams envía aquí TODAS las actividades: mensajes 1:1, en canal,
    conversationUpdate, invoke, etc.
    DEBE retornar HTTP 200 SIEMPRE para que Teams no bloquee la conversación.
    """
    if request.content_type != "application/json":
        return web.Response(status=415, text="Content-Type must be application/json")

    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    activity = Activity().deserialize(body)
    auth_header = request.headers.get("Authorization", "")

    logger.info(
        f"Activity recibida: type={activity.type}, "
        f"conversation={getattr(activity.conversation, 'id', 'N/A')}, "
        f"from={getattr(activity.from_property, 'name', 'N/A')}"
    )

    try:
        response = await ADAPTER.process_activity(
            activity, auth_header, BOT.on_turn
        )
        if response:
            return web.json_response(data=response.body, status=response.status)
        # ✅ CRÍTICO: SIEMPRE retornar 200 OK para que Teams no corte la conversación
        return web.Response(status=200)
    except Exception as e:
        logger.error(f"Error procesando actividad: {e}")
        logger.error(traceback.format_exc())
        # Aún con error, retornar 200 para que Teams no bloquee el canal
        return web.Response(status=200)


async def handle_notify(request: web.Request) -> web.Response:
    """
    POST /api/notify
    Endpoint que n8n llama para enviar un mensaje proactivo al canal de Teams.
    El mensaje aparecerá como "DAPPS Bot".

    Body esperado:
    {
      "message": "Texto del mensaje o HTML"
    }

    Body opcional con Adaptive Card:
    {
      "message": "Texto fallback",
      "card": { ... adaptive card JSON ... }
    }
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"error": "Body JSON inválido"},
            status=400
        )

    message_text = body.get("message", "")
    if not message_text:
        return web.json_response(
            {"error": "Campo 'message' es requerido"},
            status=400
        )

    conversation_ref = build_conversation_reference(BOT)

    async def send_proactive(turn_context: TurnContext):
        """Callback que se ejecuta dentro del contexto del bot."""
        # Convertir \n a <br> para que Teams muestre saltos de línea
        html_message = message_text.replace("\n", "<br>")

        # Si se envía Adaptive Card
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
            conversation_ref,
            send_proactive,
            Config.APP_ID,
        )
        logger.info(f"Mensaje proactivo enviado: {message_text[:80]}...")
        return web.json_response({
            "status": "ok",
            "message": "Notificación enviada como DAPPS Bot"
        })
    except Exception as e:
        logger.error(f"Error enviando mensaje proactivo: {e}")
        logger.error(traceback.format_exc())
        return web.json_response(
            {"error": str(e)},
            status=500
        )


async def handle_health(request: web.Request) -> web.Response:
    """GET /health — Health check."""
    has_ref = BOT.conversation_reference is not None
    return web.json_response({
        "status": "healthy",
        "bot": "DAPPS Bot",
        "conversation_reference_captured": has_ref,
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
    app.router.add_get("/health", handle_health)
    return app


if __name__ == "__main__":
    logger.info(f"Iniciando DAPPS Bot en puerto {Config.PORT}...")
    logger.info(f"App ID: {Config.APP_ID[:8]}..." if Config.APP_ID else "⚠️  APP_ID no configurado")
    logger.info(f"Endpoints: POST /api/messages, POST /api/notify, GET /health")

    app = create_app()
    web.run_app(app, host="0.0.0.0", port=Config.PORT)
