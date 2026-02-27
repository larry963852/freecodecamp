"""
Microsoft Planner service for DAPPS Bot.

Creates Planner tasks via Microsoft Graph API after the bot finishes
processing a REQUERIMIENTO (Excel + PDF uploaded to Blob).

Uses the same Azure App Registration (APP_ID / APP_PASSWORD) with
client_credentials flow.  Requires the application permission
  Tasks.ReadWrite.All  (Microsoft Graph)
granted and admin-consented in Entra ID.
"""

import logging
from typing import Optional

import aiohttp
import msal

from config import Config

logger = logging.getLogger(__name__)


class PlannerService:
    """Creates Planner tasks through Microsoft Graph."""

    def __init__(self):
        self._msal_app: Optional[msal.ConfidentialClientApplication] = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _ensure_msal(self):
        if self._msal_app:
            return
        if not Config.APP_ID or not Config.APP_PASSWORD:
            raise RuntimeError(
                "APP_ID / APP_PASSWORD not configured — cannot call Graph API."
            )
        authority = f"https://login.microsoftonline.com/{Config.TENANT_ID}"
        self._msal_app = msal.ConfidentialClientApplication(
            Config.APP_ID,
            authority=authority,
            client_credential=Config.APP_PASSWORD,
        )

    async def _get_token(self) -> str:
        """Acquire an access token for Microsoft Graph via client_credentials."""
        self._ensure_msal()
        scopes = ["https://graph.microsoft.com/.default"]
        result = self._msal_app.acquire_token_for_client(scopes=scopes)
        if "access_token" in result:
            return result["access_token"]
        error = result.get("error_description", result.get("error", "unknown"))
        raise RuntimeError(f"Failed to acquire Graph token: {error}")

    # ------------------------------------------------------------------
    # Create Planner Task
    # ------------------------------------------------------------------

    async def create_task(
        self,
        titulo: str,
        solicitante: str,
        req_id: str,
        descripcion: str = "",
        articulos: list[dict] | None = None,
        blob_urls: dict | None = None,
        assignee_id: str | None = None,
    ) -> dict:
        """
        Create a Planner task for a processed REQUERIMIENTO.

        Args:
            titulo: Short task title (from AI classification)
            solicitante: Display name of the requester
            req_id: Requirement ID (e.g. REQ-20260225-143052)
            descripcion: AI-generated description
            articulos: List of articles extracted from Excel
            blob_urls: Dict with excel_url & pdf_url (SAS URLs)
            assignee_id: AAD Object ID of the person to assign (optional)

        Returns:
            dict with task id, title, and other Graph response fields
        """
        token = await self._get_token()

        plan_id = Config.PLANNER_PLAN_ID
        bucket_id = Config.PLANNER_BUCKET_ID

        if not plan_id:
            raise RuntimeError("PLANNER_PLAN_ID not configured.")

        # Build title:  [1:1] REQUERIMIENTO - <titulo> (<solicitante>)
        task_title = f"[1:1] REQUERIMIENTO - {titulo} ({solicitante})"
        if len(task_title) > 255:
            task_title = task_title[:252] + "..."

        # Build assignments
        assignments = {}
        if assignee_id:
            assignments[assignee_id] = {
                "@odata.type": "#microsoft.graph.plannerAssignment",
                "orderHint": " !",
            }

        body = {
            "planId": plan_id,
            "title": task_title,
            "assignments": assignments,
        }
        if bucket_id:
            body["bucketId"] = bucket_id

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://graph.microsoft.com/v1.0/planner/tasks",
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp_body = await resp.json()
                if resp.status in (200, 201):
                    task_id = resp_body.get("id", "")
                    logger.info(
                        f"[PLANNER] Task created: {task_id} — {task_title}"
                    )

                    # Try to set task details (description + checklist)
                    await self._update_task_details(
                        session,
                        token,
                        task_id,
                        descripcion,
                        articulos or [],
                        blob_urls or {},
                        req_id,
                    )

                    return resp_body
                else:
                    error_msg = resp_body.get("error", {}).get("message", str(resp_body))
                    logger.error(
                        f"[PLANNER] Failed to create task: HTTP {resp.status} — {error_msg}"
                    )
                    raise RuntimeError(f"Planner API error: {error_msg}")

    async def _update_task_details(
        self,
        session: aiohttp.ClientSession,
        token: str,
        task_id: str,
        descripcion: str,
        articulos: list[dict],
        blob_urls: dict,
        req_id: str,
    ):
        """
        Update task details (description + references).
        Requires a separate PATCH call because Planner API splits task and taskDetails.
        """
        # First GET task details to obtain the @odata.etag
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with session.get(
                f"https://graph.microsoft.com/v1.0/planner/tasks/{task_id}/details",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"[PLANNER] Could not GET task details: {resp.status}")
                    return
                details = await resp.json()
                etag = details.get("@odata.etag", "")
        except Exception as e:
            logger.warning(f"[PLANNER] Error fetching task details: {e}")
            return

        # Build description
        desc_parts = [f"📋 Requerimiento: {req_id}", f"📝 {descripcion}"]
        if articulos:
            desc_parts.append(f"\n📊 Artículos ({len(articulos)}):")
            for i, art in enumerate(articulos[:10], 1):
                desc_parts.append(
                    f"  {i}. {art.get('articulo', '?')} × {art.get('cantidad', 1)}"
                    f" — {art.get('comentario', '')}"
                )
            if len(articulos) > 10:
                desc_parts.append(f"  ... y {len(articulos) - 10} más")

        if blob_urls.get("excel_url"):
            desc_parts.append(f"\n📎 Excel: {blob_urls['excel_url']}")
        if blob_urls.get("pdf_url"):
            desc_parts.append(f"📎 PDF: {blob_urls['pdf_url']}")

        description = "\n".join(desc_parts)

        patch_body = {"description": description}

        patch_headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "If-Match": etag,
        }

        try:
            async with session.patch(
                f"https://graph.microsoft.com/v1.0/planner/tasks/{task_id}/details",
                json=patch_body,
                headers=patch_headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status in (200, 204):
                    logger.info(f"[PLANNER] Task details updated for {task_id}")
                else:
                    body = await resp.text()
                    logger.warning(
                        f"[PLANNER] Failed to update task details: "
                        f"HTTP {resp.status} — {body[:200]}"
                    )
        except Exception as e:
            logger.warning(f"[PLANNER] Error updating task details: {e}")


# Singleton instance
planner_service = PlannerService()
