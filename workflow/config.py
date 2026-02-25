"""
Configuración del DAPPS Bot.
Carga variables de entorno para la autenticación y configuración del bot.
"""

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Configuración centralizada del bot desde variables de entorno."""

    # --- Azure Bot / App Registration ---
    APP_ID = os.getenv("MICROSOFT_APP_ID", "")
    APP_PASSWORD = os.getenv("MICROSOFT_APP_PASSWORD", "")

    # --- Teams Target (canal donde se envían las notificaciones) ---
    TEAMS_CHANNEL_ID = os.getenv(
        "TEAMS_CHANNEL_ID",
        "19:5ba87851da9549b7a6b2f31e85f4cdb8@thread.tacv2"
    )
    TEAMS_TEAM_ID = os.getenv(
        "TEAMS_TEAM_ID",
        "ae0a0280-5e3e-4f89-8a0a-2aa9c9c5f0e1"
    )
    # Tenant ID de Azure AD (DAPPS)
    TENANT_ID = os.getenv(
        "TENANT_ID",
        "cddbd103-c38e-4f8c-92db-b28549147502"
    )
    # Service URL de Bot Framework para la región (AMER por defecto)
    SERVICE_URL = os.getenv(
        "SERVICE_URL",
        "https://smba.trafficmanager.net/amer/"
    )

        # --- n8n Integration ---
    # URL del webhook de n8n para procesar mensajes 1:1
    N8N_PERSONAL_WEBHOOK_URL = os.getenv(
        "N8N_PERSONAL_WEBHOOK_URL",
        "https://special-lamp-xqwjww99xqxcp7r4-5678.app.github.dev/webhook/personal-intake"
    )

    # --- Servidor ---
    PORT = int(os.getenv("PORT", "3978"))

