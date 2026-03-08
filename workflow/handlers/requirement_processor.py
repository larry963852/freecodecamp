"""
Pipeline completo de procesamiento de requerimientos (Paso 2).

Extrae la lógica pesada de _process_requirement_attachments
del bot personal en un módulo dedicado, limpio y testeable.
"""

import logging
from datetime import datetime, timezone

from botbuilder.core import TurnContext
from botbuilder.schema import Activity, ActivityTypes

from config import Config
from handlers.attachments import download_attachment
from handlers.excel_parser import fallback_parse_excel
from services.blob_service import blob_service
from services.document_intelligence import doc_intelligence_service
from services.n8n_client import n8n_client
from services.pdf_generator import generate_sustento_pdf

logger = logging.getLogger(__name__)


async def process_requirement(
    turn_context: TurnContext,
    user_aad_id: str,
    user_state: dict,
    attachments: dict,
    clear_state_fn,
) -> None:
    """
    Procesa los adjuntos de un requerimiento:
      1. Excel → Document Intelligence (o fallback openpyxl)
      2. PDF subido o auto-generado
      3. Upload a Blob Storage + metadata.json
      4. Crear tarea Planner vía n8n
      5. Responder con resumen

    Args:
        turn_context: Contexto del turno actual.
        user_aad_id: AAD Object ID del usuario.
        user_state: Estado conversacional del usuario.
        attachments: {"excel": [...], "pdf": [...]}.
        clear_state_fn: Callable para limpiar el estado al finalizar.
    """
    req_id = user_state.get("req_id", "")
    n8n_result = user_state.get("n8n_result", {})
    solicitante = user_state.get("solicitante", {})
    titulo = n8n_result.get("titulo", "Sin título")
    descripcion = n8n_result.get("descripcion", "")
    mensaje_original = user_state.get("mensaje_original", "")

    excel_files = attachments.get("excel", [])
    pdf_files = attachments.get("pdf", [])

    # Sin Excel → pedir de nuevo
    if not excel_files:
        await _reply(turn_context, _MSG_NO_EXCEL)
        return

    # Acuse de procesamiento
    await _reply(turn_context, _ack_message(req_id, excel_files[0], pdf_files))

    try:
        # 1. EXCEL: Download → Analyze → Upload
        articulos, excel_blob_info, excel_analysis = await _process_excel(
            req_id, excel_files[0]
        )

        # 2. PDF: Download/Generate → Upload
        pdf_blob_info, pdf_was_generated = await _process_pdf(
            req_id, pdf_files, solicitante, titulo, descripcion,
            articulos, mensaje_original,
        )

        # 3. METADATA JSON en Blob
        metadata = _build_metadata(
            req_id, titulo, descripcion, mensaje_original, solicitante,
            excel_blob_info, excel_analysis, articulos,
            pdf_blob_info, pdf_was_generated,
        )
        await _upload_metadata(req_id, metadata)

        # 4. PLANNER vía n8n
        planner_task_id = await _create_planner_task(
            req_id, titulo, descripcion, solicitante,
            articulos, excel_blob_info, pdf_blob_info, metadata,
        )

        # 5. Respuesta de resumen
        reply_text = _build_summary(
            req_id, titulo, descripcion, articulos,
            pdf_was_generated, excel_blob_info, pdf_blob_info, planner_task_id,
        )
        clear_state_fn(user_aad_id)

    except Exception as e:
        logger.error("[REQ %s] Error inesperado: %s", req_id, e)
        reply_text = (
            f"Error al procesar el requerimiento **{req_id}**.\n\n"
            f"Detalle: {str(e)[:200]}\n\n"
            "Intenta nuevamente o escribe **cancelar** para abandonar."
        )

    await _reply(turn_context, reply_text)


# ------------------------------------------------------------------
# Pasos internos
# ------------------------------------------------------------------

async def _process_excel(req_id: str, excel_file: dict):
    """Download → analyze → upload Excel. Returns (articulos, blob_info, analysis)."""
    excel_bytes = await download_attachment(
        excel_file["contentUrl"], excel_file.get("downloadUrl", "")
    )
    if not excel_bytes:
        raise RuntimeError("No se pudo descargar el archivo Excel.")

    # Análisis con Document Intelligence
    articulos, excel_analysis = await _analyze_excel(req_id, excel_bytes)

    # Upload a Blob
    excel_blob_info = {}
    try:
        excel_blob_info = await blob_service.upload_excel(
            req_id, excel_bytes, excel_file["name"]
        )
    except Exception as e:
        logger.error("[REQ %s] Blob upload Excel: %s", req_id, e)
        excel_blob_info = {"error": str(e)}

    return articulos, excel_blob_info, excel_analysis


async def _analyze_excel(req_id: str, excel_bytes: bytes):
    """Intenta Document Intelligence; si falla, usa openpyxl."""
    articulos = []
    analysis: dict = {}

    try:
        if not Config.DOC_INTELLIGENCE_ENDPOINT or not Config.DOC_INTELLIGENCE_KEY:
            raise RuntimeError("Document Intelligence no configurado")

        analysis = await doc_intelligence_service.analyze_excel(excel_bytes)
        articulos = analysis.get("articulos", [])
        logger.info("[REQ %s] DI: %d artículos", req_id, len(articulos))

        # DI OK pero 0 artículos → fallback
        if not articulos:
            logger.warning("[REQ %s] DI devolvió 0 artículos, fallback openpyxl", req_id)
            articulos = fallback_parse_excel(excel_bytes)
            if articulos:
                analysis.update(
                    status="fallback_after_di",
                    extractedRows=len(articulos),
                    articulos=articulos,
                )
    except Exception as e:
        logger.warning("[REQ %s] DI falló: %s — usando openpyxl", req_id, e)
        analysis = {"status": "fallback", "reason": str(e), "extractedRows": 0}
        articulos = fallback_parse_excel(excel_bytes)

    return articulos, analysis


async def _process_pdf(
    req_id, pdf_files, solicitante, titulo, descripcion, articulos, mensaje_original
):
    """Descarga PDF del usuario o auto-genera. Returns (blob_info, was_generated)."""
    pdf_blob_info: dict = {}
    pdf_was_generated = False

    if pdf_files:
        pdf_bytes = await download_attachment(
            pdf_files[0]["contentUrl"], pdf_files[0].get("downloadUrl", "")
        )
        if pdf_bytes:
            # Analizar (best-effort)
            try:
                await doc_intelligence_service.analyze_pdf(pdf_bytes)
            except Exception as e:
                logger.error("[REQ %s] DI PDF: %s", req_id, e)

            try:
                pdf_blob_info = await blob_service.upload_pdf(
                    req_id, pdf_bytes, pdf_files[0]["name"]
                )
            except Exception as e:
                logger.error("[REQ %s] Blob upload PDF: %s", req_id, e)
                pdf_blob_info = {"error": str(e)}
            return pdf_blob_info, False
        # No se pudo descargar → auto-generar
        logger.warning("[REQ %s] PDF no descargable, auto-generando", req_id)

    # Auto-generar PDF
    try:
        pdf_bytes = generate_sustento_pdf(
            req_id=req_id,
            solicitante=solicitante,
            titulo=titulo,
            descripcion=descripcion,
            articulos=articulos,
            mensaje_original=mensaje_original,
        )
        pdf_blob_info = await blob_service.upload_pdf(
            req_id, pdf_bytes, "sustento_autogenerado.pdf"
        )
        pdf_was_generated = True
        logger.info("[REQ %s] PDF auto-generado subido", req_id)
    except Exception as e:
        logger.error("[REQ %s] Generación/upload PDF: %s", req_id, e)
        pdf_blob_info = {"error": str(e)}
        pdf_was_generated = True

    return pdf_blob_info, pdf_was_generated


async def _upload_metadata(req_id: str, metadata: dict):
    try:
        await blob_service.upload_metadata(req_id, metadata)
        logger.info("[REQ %s] metadata.json guardado", req_id)
    except Exception as e:
        logger.error("[REQ %s] Blob metadata: %s", req_id, e)


async def _create_planner_task(
    req_id, titulo, descripcion, solicitante,
    articulos, excel_blob_info, pdf_blob_info, metadata,
) -> str:
    """Crea tarea Planner vía n8n y retorna el task ID (o vacío)."""
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

        result = await n8n_client.create_planner_task(
            req_id=req_id,
            titulo=titulo,
            descripcion=descripcion,
            solicitante=solicitante,
            articulos=articulos,
            blob_urls=blob_urls,
        )
        task_id = (result or {}).get("planner_task_id", "")
        if task_id:
            logger.info("[REQ %s] Planner task: %s", req_id, task_id)
            metadata["planner_task_id"] = task_id
            await _upload_metadata(req_id, metadata)
        return task_id
    except Exception as e:
        logger.error("[REQ %s] Planner: %s", req_id, e)
        return ""


# ------------------------------------------------------------------
# Mensajes y helpers de presentación
# ------------------------------------------------------------------

def _build_metadata(
    req_id, titulo, descripcion, mensaje_original, solicitante,
    excel_blob_info, excel_analysis, articulos,
    pdf_blob_info, pdf_was_generated,
) -> dict:
    return {
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
        },
    }


def _build_summary(
    req_id, titulo, descripcion, articulos,
    pdf_was_generated, excel_blob_info, pdf_blob_info, planner_task_id,
) -> str:
    total_qty = sum(a.get("cantidad", 1) for a in articulos)

    art_lines = []
    for i, art in enumerate(articulos[:5], 1):
        art_lines.append(
            f"  {i}. **{art['articulo']}** × {art['cantidad']}"
            f" — _{art.get('comentario', 'Sin comentario')}_"
        )
    if len(articulos) > 5:
        art_lines.append(f"  ... y {len(articulos) - 5} artículos más")

    planner_line = (
        f"**Tarea en Planner:** creada (ID: `{planner_task_id[:8]}...`)"
        if planner_task_id
        else "**Tarea en Planner:** pendiente de creación"
    )

    return (
        f"**Requerimiento {req_id} procesado exitosamente**\n\n"
        f"**{titulo}**\n\n"
        f"{descripcion}\n\n"
        f"**Excel procesado:** {len(articulos)} artículos, "
        f"{total_qty} unidades totales\n\n"
        + "\n".join(art_lines) + "\n\n"
        f"**PDF de sustento:** "
        f"{'Generado automáticamente' if pdf_was_generated else 'Subido por el usuario'}\n\n"
        f"Archivos almacenados en Azure:\n\n"
        f"- Excel: `{excel_blob_info.get('blobName', 'N/A')}`\n\n"
        f"- PDF: `{pdf_blob_info.get('blobName', 'N/A')}`\n\n"
        f"{planner_line}\n\n"
        "Recibirás una notificación cuando haya actualizaciones."
    )


def _ack_message(req_id: str, excel_file: dict, pdf_files: list) -> str:
    pdf_status = (
        f"PDF: {pdf_files[0]['name']}" if pdf_files
        else "PDF: se generará automáticamente"
    )
    return (
        f"Archivos recibidos. Procesando requerimiento **{req_id}**...\n\n"
        f"Excel: {excel_file['name']}\n\n"
        f"{pdf_status}\n\n"
        "Analizando documentos..."
    )


_MSG_NO_EXCEL = (
    "No se encontró un archivo **Excel (.xlsx)** adjunto.\n\n"
    "Adjunta un archivo Excel con las columnas:\n\n"
    "- **Artículo** — nombre del equipo o recurso\n\n"
    "- **Cantidad** — unidades necesarias\n\n"
    "- **Comentario** — descripción del desperfecto\n\n"
    "También puedes adjuntar un **PDF de sustento** (opcional).\n\n"
    "_Escribe **cancelar** para abandonar este requerimiento._"
)


async def _reply(turn_context: TurnContext, text: str):
    await turn_context.send_activity(
        Activity(type=ActivityTypes.message, text_format="markdown", text=text)
    )
