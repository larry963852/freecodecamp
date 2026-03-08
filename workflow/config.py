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
    # Webhook para clasificación de mensajes 1:1
    N8N_PERSONAL_WEBHOOK_URL = os.getenv(
        "N8N_PERSONAL_WEBHOOK_URL",
        "https://special-lamp-xqwjww99xqxcp7r4-5678.app.github.dev/webhook/personal-intake"
    )
    # Webhook para creación de tarea Planner (usa OAuth delegado de n8n)
    N8N_CREATE_PLANNER_URL = os.getenv(
        "N8N_CREATE_PLANNER_URL",
        "https://special-lamp-xqwjww99xqxcp7r4-5678.app.github.dev/webhook/requerimiento-planner"
    )
    # Webhook para escalar consultas sin respuesta RAG → crear tarea Planner
    N8N_CONSULTA_ESCALADA_URL = os.getenv(
        "N8N_CONSULTA_ESCALADA_URL",
        "https://special-lamp-xqwjww99xqxcp7r4-5678.app.github.dev/webhook/consulta-escalada"
    )

    # --- Azure Blob Storage (archivos Excel / PDF + metadata.json) ---
    AZURE_BLOB_CONNECTION_STRING = os.getenv("AZURE_BLOB_CONNECTION_STRING", "")
    AZURE_BLOB_CONTAINER = os.getenv("AZURE_BLOB_CONTAINER", "requerimientos")

    # --- Azure AI Document Intelligence (análisis de Excel / PDF) ---
    DOC_INTELLIGENCE_ENDPOINT = os.getenv("DOC_INTELLIGENCE_ENDPOINT", "")
    DOC_INTELLIGENCE_KEY = os.getenv("DOC_INTELLIGENCE_KEY", "")

    # --- Azure OpenAI (RAG — embeddings + chat) ---
    AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
    AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "")
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT = os.getenv(
        "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-ada-002"
    )
    AZURE_OPENAI_CHAT_DEPLOYMENT = os.getenv(
        "AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4.1-mini"
    )

    # --- Microsoft Planner (IDs usados por n8n; el bot no llama Planner directamente) ---
    PLANNER_PLAN_ID = os.getenv("PLANNER_PLAN_ID", "LY1tOr8SKUqBNay8KAUlmmUAH3Ss")
    PLANNER_BUCKET_ID = os.getenv("PLANNER_BUCKET_ID", "yD84QLz5yUufFD50vAQvj2UAI0gy")

    # --- Servidor ---
    PORT = int(os.getenv("PORT", "3978"))

