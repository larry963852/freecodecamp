"""
Azure Blob Storage service for DAPPS Bot.

Handles upload/download of Excel and PDF files related to requirements.
Each requirement gets its own virtual folder: requerimientos/{req_id}/

Container structure:
  requerimientos/
    ├── REQ-20260225-143052/
    │   ├── metadata.json       ← trazabilidad (reemplaza Cosmos DB)
    │   ├── excel_original.xlsx
    │   └── sustento.pdf
    ├── REQ-20260225-143210/
    │   ├── metadata.json
    │   ├── excel_original.xlsx
    │   └── sustento.pdf
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from azure.storage.blob import (
    BlobServiceClient,
    ContentSettings,
    ContainerClient,
    generate_blob_sas,
    BlobSasPermissions,
)

from config import Config

logger = logging.getLogger(__name__)


class BlobStorageService:
    """Manages file operations against Azure Blob Storage."""

    def __init__(self):
        self._client: Optional[BlobServiceClient] = None
        self._container: Optional[ContainerClient] = None
        self._initialized = False

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def _ensure_initialized(self):
        """Lazy initialization — creates client and container on first use."""
        if self._initialized:
            return

        conn_str = Config.AZURE_BLOB_CONNECTION_STRING
        if not conn_str:
            raise RuntimeError(
                "AZURE_BLOB_CONNECTION_STRING not configured. "
                "Set it in .env to enable file storage."
            )

        self._client = BlobServiceClient.from_connection_string(conn_str)
        container_name = Config.AZURE_BLOB_CONTAINER

        # Create container if it doesn't exist
        self._container = self._client.get_container_client(container_name)
        try:
            self._container.get_container_properties()
        except Exception:
            logger.info(f"Creating blob container: {container_name}")
            self._container.create_container()

        self._initialized = True
        logger.info(f"BlobStorageService initialized — container: {container_name}")

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    async def upload_excel(
        self, req_id: str, file_bytes: bytes, filename: str = "excel_original.xlsx"
    ) -> dict:
        """
        Upload an Excel file for a requirement.

        Args:
            req_id: Requirement ID (e.g., REQ-20260225-001)
            file_bytes: Raw bytes of the Excel file
            filename: Original filename (default: excel_original.xlsx)

        Returns:
            dict with blobName, blobUrl, uploadedAt
        """
        self._ensure_initialized()
        blob_name = f"{req_id}/{filename}"
        return self._upload_blob(blob_name, file_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    async def upload_pdf(
        self, req_id: str, file_bytes: bytes, filename: str = "sustento.pdf"
    ) -> dict:
        """
        Upload a PDF file for a requirement.

        Args:
            req_id: Requirement ID
            file_bytes: Raw bytes of the PDF
            filename: Original filename (default: sustento.pdf)

        Returns:
            dict with blobName, blobUrl, uploadedAt
        """
        self._ensure_initialized()
        blob_name = f"{req_id}/{filename}"
        return self._upload_blob(blob_name, file_bytes, "application/pdf")

    def _upload_blob(self, blob_name: str, data: bytes, content_type: str) -> dict:
        """Internal: upload bytes to a blob."""
        blob_client = self._container.get_blob_client(blob_name)
        blob_client.upload_blob(
            data,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
        )
        url = blob_client.url
        uploaded_at = datetime.now(timezone.utc).isoformat()
        logger.info(f"Blob uploaded: {blob_name} ({len(data)} bytes)")
        return {
            "blobName": blob_name,
            "blobUrl": url,
            "uploadedAt": uploaded_at,
            "sizeBytes": len(data),
        }

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    async def download_file(self, req_id: str, filename: str) -> Optional[bytes]:
        """Download a file from a requirement folder."""
        self._ensure_initialized()
        blob_name = f"{req_id}/{filename}"
        blob_client = self._container.get_blob_client(blob_name)
        try:
            download = blob_client.download_blob()
            return download.readall()
        except Exception as e:
            logger.error(f"Error downloading blob {blob_name}: {e}")
            return None

    # ------------------------------------------------------------------
    # SAS URL (for sharing links in notifications)
    # ------------------------------------------------------------------

    def generate_sas_url(self, blob_name: str, expiry_hours: int = 24) -> str:
        """
        Generate a time-limited SAS URL for a blob.
        Useful for sharing download links in Teams messages.
        """
        self._ensure_initialized()

        # Extract account name and key from connection string
        account_name = self._client.account_name
        # We need the account key for SAS generation
        # Parse it from connection string
        conn_parts = dict(
            part.split("=", 1)
            for part in Config.AZURE_BLOB_CONNECTION_STRING.split(";")
            if "=" in part
        )
        account_key = conn_parts.get("AccountKey", "")

        sas_token = generate_blob_sas(
            account_name=account_name,
            container_name=Config.AZURE_BLOB_CONTAINER,
            blob_name=blob_name,
            account_key=account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(hours=expiry_hours),
        )

        blob_client = self._container.get_blob_client(blob_name)
        return f"{blob_client.url}?{sas_token}"

    # ------------------------------------------------------------------
    # List files for a requirement
    # ------------------------------------------------------------------

    async def list_files(self, req_id: str) -> list[dict]:
        """List all files stored for a given requirement."""
        self._ensure_initialized()
        prefix = f"{req_id}/"
        blobs = []
        for blob in self._container.list_blobs(name_starts_with=prefix):
            blobs.append({
                "name": blob.name.replace(prefix, ""),
                "fullPath": blob.name,
                "size": blob.size,
                "lastModified": blob.last_modified.isoformat() if blob.last_modified else None,
            })
        return blobs

    # ------------------------------------------------------------------
    # Metadata JSON (reemplaza Cosmos DB)
    # ------------------------------------------------------------------

    async def upload_metadata(self, req_id: str, metadata: dict) -> dict:
        """
        Upload or overwrite a metadata.json for a requirement.
        This JSON file replaces Cosmos DB as the traceability store.
        """
        self._ensure_initialized()
        blob_name = f"{req_id}/metadata.json"
        data = json.dumps(metadata, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        return self._upload_blob(blob_name, data, "application/json")

    async def read_metadata(self, req_id: str) -> Optional[dict]:
        """Read the metadata.json for a requirement. Returns None if not found."""
        raw = await self.download_file(req_id, "metadata.json")
        if raw:
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception as e:
                logger.error(f"Error parsing metadata.json for {req_id}: {e}")
        return None

    async def list_all_requirements(self) -> list[str]:
        """
        List all requirement IDs by scanning top-level virtual folders.
        Returns list of req_id strings, e.g. ['REQ-20260225-143052', ...].
        """
        self._ensure_initialized()
        req_ids = set()
        for blob in self._container.list_blobs():
            parts = blob.name.split("/")
            if len(parts) >= 2 and parts[0].startswith("REQ-"):
                req_ids.add(parts[0])
        return sorted(req_ids, reverse=True)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def generate_req_id() -> str:
        """Generate a unique requirement ID: REQ-YYYYMMDD-HHMMSS."""
        now = datetime.now(timezone.utc)
        return f"REQ-{now.strftime('%Y%m%d')}-{now.strftime('%H%M%S')}"


# Singleton instance
blob_service = BlobStorageService()