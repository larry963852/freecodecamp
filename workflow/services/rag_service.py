"""
DAPPS Bot — RAG Service con SQLite Vector.

Retrieval-Augmented Generation para responder consultas internas usando:
  - sqlite-vec  → búsqueda vectorial embebida en SQLite
  - Azure OpenAI → embeddings (text-embedding-ada-002) + chat (gpt-4.1-mini)
  - Knowledge base local (knowledge/procesos_internos_dapps.txt)

Flujo:
  1. Startup: chunk knowledge → embeddings → almacenar en SQLite vec0
  2. Query:   embed pregunta → top-k chunks → LLM genera respuesta
  3. Si LLM no puede responder con el contexto → found=False → bot escala a Planner

El índice se reconstruye solo si el archivo de conocimiento cambia (hash MD5).
"""

import asyncio
import hashlib
import logging
import os
import re
import sqlite3
import struct
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────
_BASE_DIR = Path(__file__).resolve().parent.parent
_KNOWLEDGE_DIR = _BASE_DIR / "knowledge"
_KNOWLEDGE_FILE = _KNOWLEDGE_DIR / "procesos_internos_dapps.txt"
_DB_PATH = _KNOWLEDGE_DIR / "rag_vectors.db"

# ── Config por defecto (override via Config class / env vars) ────────
EMBEDDING_DIM = 1536          # text-embedding-ada-002 / text-embedding-3-small
TOP_K = 5                     # chunks a recuperar
SIMILARITY_THRESHOLD = 0.40   # distancia coseno máxima (menor = más similar)
CHUNK_MAX_CHARS = 1500        # tamaño máximo de chunk
CHUNK_OVERLAP_CHARS = 200     # overlap entre chunks del mismo section


# ── Helpers ──────────────────────────────────────────────────────────

def _serialize_f32(vector: list[float]) -> bytes:
    """Empaqueta lista de floats como bytes para sqlite-vec."""
    return struct.pack(f"{len(vector)}f", *vector)


# =====================================================================
# RAGService
# =====================================================================

class RAGService:
    """
    Servicio RAG con SQLite Vector para consultas de procesos internos DAPPS.

    Uso:
        from services.rag_service import rag_service
        await rag_service.initialize()         # una sola vez
        result = await rag_service.answer(query, user_name)
        if result["found"]:
            print(result["answer"])
    """

    def __init__(self):
        self._db: Optional[sqlite3.Connection] = None
        self._openai_client = None
        self._embedding_deployment: str = ""
        self._chat_deployment: str = ""
        self._initialized = False
        self._available = False
        self._use_vec0 = False

    # ── Propiedades ──────────────────────────────────────────────────

    @property
    def is_available(self) -> bool:
        return self._available

    # ── Inicialización ───────────────────────────────────────────────

    async def initialize(self):
        """Inicializa el servicio: OpenAI client, SQLite DB, vector index."""
        if self._initialized:
            return

        try:
            from config import Config

            # 1. Verificar credenciales Azure OpenAI
            endpoint = getattr(Config, "AZURE_OPENAI_ENDPOINT", "") or ""
            api_key = getattr(Config, "AZURE_OPENAI_API_KEY", "") or ""
            self._embedding_deployment = (
                getattr(Config, "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "")
                or "text-embedding-ada-002"
            )
            self._chat_deployment = (
                getattr(Config, "AZURE_OPENAI_CHAT_DEPLOYMENT", "")
                or "gpt-4.1-mini"
            )

            if not endpoint or not api_key:
                logger.warning(
                    "[RAG] Azure OpenAI no configurado "
                    "(AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY). "
                    "RAG deshabilitado — consultas se escalarán a Planner."
                )
                self._initialized = True
                return

            # 2. Cliente OpenAI para Azure
            from openai import AzureOpenAI

            self._openai_client = AzureOpenAI(
                azure_endpoint=endpoint,
                api_key=api_key,
                api_version="2024-06-01",
            )

            # 3. SQLite + vec0
            self._init_db()

            # 4. Verificar knowledge base
            if not _KNOWLEDGE_FILE.exists():
                logger.warning(f"[RAG] Archivo de conocimiento no encontrado: {_KNOWLEDGE_FILE}")
                self._initialized = True
                return

            # 5. ¿Necesita re-indexar?
            current_hash = self._file_hash(_KNOWLEDGE_FILE)
            stored_hash = self._get_stored_hash()

            if current_hash != stored_hash:
                logger.info("[RAG] Knowledge base cambió o es primera ejecución — reconstruyendo índice...")
                await self._build_index(current_hash)
            else:
                count = self._get_chunk_count()
                logger.info(f"[RAG] Índice actualizado — {count} chunks cargados.")

            self._initialized = True
            self._available = True
            logger.info("[RAG] Servicio RAG inicializado correctamente.")

        except ImportError as e:
            logger.warning(f"[RAG] Dependencia faltante: {e}. RAG deshabilitado.")
            self._initialized = True
        except Exception as e:
            logger.error(f"[RAG] Error inicializando: {e}", exc_info=True)
            self._initialized = True

    # ── SQLite setup ─────────────────────────────────────────────────

    def _init_db(self):
        """Crea la base de datos SQLite con tablas de chunks y vectores."""
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(_DB_PATH))

        # Intentar cargar extensión sqlite-vec
        try:
            import sqlite_vec

            self._db.enable_load_extension(True)
            sqlite_vec.load(self._db)
            self._db.enable_load_extension(False)
            self._use_vec0 = True
            logger.info("[RAG] Extensión sqlite-vec cargada.")
        except (ImportError, Exception) as e:
            self._use_vec0 = False
            logger.warning(f"[RAG] sqlite-vec no disponible ({e}). Usando fallback coseno.")

        # Tabla de texto de chunks
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                section TEXT NOT NULL,
                content TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)

        # Tabla de vectores
        if self._use_vec0:
            try:
                self._db.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
                        embedding float[{EMBEDDING_DIM}] distance_metric=cosine
                    )
                """)
            except Exception:
                # vec0 ya existe o versión incompatible → fallback
                self._use_vec0 = False
        if not self._use_vec0:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS vec_chunks_fallback (
                    id INTEGER PRIMARY KEY,
                    embedding BLOB NOT NULL
                )
            """)
            logger.info("[RAG] Usando tabla fallback para vectores.")

        self._db.commit()

    # ── Utilidades ───────────────────────────────────────────────────

    @staticmethod
    def _file_hash(path: Path) -> str:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(8192), b""):
                h.update(block)
        return h.hexdigest()

    def _get_stored_hash(self) -> str:
        row = self._db.execute(
            "SELECT value FROM metadata WHERE key = 'knowledge_hash'"
        ).fetchone()
        return row[0] if row else ""

    def _get_chunk_count(self) -> int:
        row = self._db.execute("SELECT COUNT(*) FROM chunks").fetchone()
        return row[0] if row else 0

    # ── Chunking ─────────────────────────────────────────────────────

    def _chunk_knowledge(self, text: str) -> list[dict]:
        """
        Divide el knowledge base en chunks por secciones (## headers).
        Para secciones grandes, subdivide por ### sub-headers.
        """
        chunks = []

        # Dividir por secciones de nivel 2 (## N. TITULO)
        sections = re.split(r"\n(?=## \d+\.)", text)

        for section_text in sections:
            section_text = section_text.strip()
            if not section_text:
                continue

            # Extraer título de sección
            title_match = re.match(r"^##\s+\d+\.\s+(.+)", section_text)
            section_title = title_match.group(1).strip() if title_match else "General"

            if len(section_text) <= CHUNK_MAX_CHARS:
                chunks.append({"section": section_title, "content": section_text})
            else:
                # Sub-dividir por ### sub-secciones
                sub_sections = re.split(r"\n(?=### )", section_text)
                current_chunk = ""

                for sub in sub_sections:
                    if len(current_chunk) + len(sub) <= CHUNK_MAX_CHARS:
                        current_chunk += ("\n\n" if current_chunk else "") + sub
                    else:
                        if current_chunk.strip():
                            chunks.append({
                                "section": section_title,
                                "content": current_chunk.strip(),
                            })
                        current_chunk = sub

                if current_chunk.strip():
                    chunks.append({
                        "section": section_title,
                        "content": current_chunk.strip(),
                    })

        # Chunk especial: FAQ completo (suelen ser preguntas directas)
        faq_match = re.search(
            r"(## 13\. PREGUNTAS FRECUENTES.*?)(?=\n## \d+\.|\Z)",
            text,
            re.DOTALL,
        )
        if faq_match:
            faq_text = faq_match.group(1)
            # Cada pregunta-respuesta como chunk individual
            qa_pairs = re.split(r"\n(?=P: )", faq_text)
            for qa in qa_pairs:
                qa = qa.strip()
                if qa.startswith("P:") and len(qa) > 20:
                    chunks.append({"section": "FAQ", "content": qa})

        logger.info(f"[RAG] Knowledge dividido en {len(chunks)} chunks.")
        return chunks

    # ── Embeddings ───────────────────────────────────────────────────

    def _get_embedding_sync(self, text: str) -> list[float]:
        """Obtiene embedding via Azure OpenAI (sync)."""
        response = self._openai_client.embeddings.create(
            model=self._embedding_deployment,
            input=text[:8000],
        )
        return response.data[0].embedding

    async def _get_embedding(self, text: str) -> list[float]:
        """Wrapper async para embedding."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._get_embedding_sync, text)

    # ── Indexación ───────────────────────────────────────────────────

    async def _build_index(self, file_hash: str):
        """Reconstruye el índice vectorial completo."""
        text = _KNOWLEDGE_FILE.read_text(encoding="utf-8")
        chunks = self._chunk_knowledge(text)

        if not chunks:
            logger.warning("[RAG] No se generaron chunks.")
            return

        # Limpiar datos existentes
        self._db.execute("DELETE FROM chunks")
        if self._use_vec0:
            self._db.execute("DELETE FROM vec_chunks")
        else:
            self._db.execute("DELETE FROM vec_chunks_fallback")
        self._db.commit()

        indexed = 0
        for i, chunk in enumerate(chunks):
            cursor = self._db.execute(
                "INSERT INTO chunks (section, content) VALUES (?, ?)",
                (chunk["section"], chunk["content"]),
            )
            chunk_id = cursor.lastrowid

            try:
                embedding = await self._get_embedding(chunk["content"])

                if self._use_vec0:
                    self._db.execute(
                        "INSERT INTO vec_chunks (rowid, embedding) VALUES (?, ?)",
                        (chunk_id, _serialize_f32(embedding)),
                    )
                else:
                    self._db.execute(
                        "INSERT INTO vec_chunks_fallback (id, embedding) VALUES (?, ?)",
                        (chunk_id, _serialize_f32(embedding)),
                    )
                indexed += 1
                logger.debug(
                    f"[RAG] Chunk {i+1}/{len(chunks)} indexado: "
                    f"{chunk['section'][:40]}"
                )
            except Exception as e:
                logger.error(f"[RAG] Error embedding chunk {i+1}: {e}")

        # Guardar hash del knowledge
        self._db.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES ('knowledge_hash', ?)",
            (file_hash,),
        )
        self._db.commit()
        logger.info(f"[RAG] Índice construido: {indexed}/{len(chunks)} chunks indexados.")

    # ── Búsqueda vectorial ───────────────────────────────────────────

    async def search(self, query: str, top_k: int = TOP_K) -> list[dict]:
        """
        Busca chunks relevantes por similitud vectorial.

        Returns:
            Lista de {section, content, distance} ordenada por relevancia.
        """
        if not self._available or not self._db:
            return []

        try:
            query_embedding = await self._get_embedding(query)

            if self._use_vec0:
                results = self._db.execute(
                    """
                    SELECT c.id, c.section, c.content, v.distance
                    FROM vec_chunks v
                    JOIN chunks c ON c.id = v.rowid
                    WHERE v.embedding MATCH ?
                    ORDER BY v.distance
                    LIMIT ?
                    """,
                    (_serialize_f32(query_embedding), top_k),
                ).fetchall()

                return [
                    {"section": r[1], "content": r[2], "distance": r[3]}
                    for r in results
                ]
            else:
                return self._search_fallback(query_embedding, top_k)

        except Exception as e:
            logger.error(f"[RAG] Error en búsqueda: {e}")
            return []

    def _search_fallback(self, query_vec: list[float], top_k: int) -> list[dict]:
        """Búsqueda fallback con coseno en Python (sin sqlite-vec)."""
        import numpy as np

        query_arr = np.array(query_vec, dtype=np.float32)
        query_norm = np.linalg.norm(query_arr)
        if query_norm == 0:
            return []

        rows = self._db.execute(
            """
            SELECT vf.id, c.section, c.content, vf.embedding
            FROM vec_chunks_fallback vf
            JOIN chunks c ON c.id = vf.id
            """
        ).fetchall()

        scored = []
        for row in rows:
            stored_vec = np.frombuffer(row[3], dtype=np.float32)
            stored_norm = np.linalg.norm(stored_vec)
            if stored_norm == 0:
                continue
            cosine_sim = float(
                np.dot(query_arr, stored_vec) / (query_norm * stored_norm)
            )
            scored.append({
                "section": row[1],
                "content": row[2],
                "distance": 1.0 - cosine_sim,
            })

        scored.sort(key=lambda x: x["distance"])
        return scored[:top_k]

    # ── Pipeline principal ───────────────────────────────────────────

    async def answer(self, query: str, user_name: str = "usuario") -> dict:
        """
        Pipeline RAG completo: búsqueda → evaluación → respuesta.

        Returns:
            {
                "found": bool,          # Si se encontró respuesta relevante
                "answer": str,          # Texto de la respuesta (si found=True)
                "sources": list[str],   # Secciones fuente utilizadas
                "confidence": float,    # Score de confianza (0.0 – 1.0)
            }
        """
        # Lazy init
        if not self._initialized:
            await self.initialize()

        if not self._available:
            return {"found": False, "answer": "", "sources": [], "confidence": 0.0}

        try:
            # 1. Buscar chunks relevantes
            results = await self.search(query, top_k=TOP_K)

            if not results:
                logger.info("[RAG] Sin resultados de búsqueda.")
                return {"found": False, "answer": "", "sources": [], "confidence": 0.0}

            # 2. Filtrar por umbral de similitud
            relevant = [r for r in results if r["distance"] <= SIMILARITY_THRESHOLD]

            if not relevant:
                best = results[0]["distance"]
                logger.info(
                    f"[RAG] Ningún resultado supera el umbral. "
                    f"Mejor distancia: {best:.3f} (umbral: {SIMILARITY_THRESHOLD})"
                )
                return {"found": False, "answer": "", "sources": [], "confidence": 0.0}

            # 3. Construir contexto con los chunks relevantes
            context = "\n\n---\n\n".join(
                f"[{r['section']}]\n{r['content']}" for r in relevant
            )

            # 4. Generar respuesta con LLM
            answer_result = await self._generate_answer(query, context, user_name)

            # 5. Enriquecer con metadatos
            answer_result["sources"] = list(set(r["section"] for r in relevant))
            answer_result["confidence"] = round(1.0 - relevant[0]["distance"], 3)

            return answer_result

        except Exception as e:
            logger.error(f"[RAG] Error en pipeline answer: {e}", exc_info=True)
            return {"found": False, "answer": "", "sources": [], "confidence": 0.0}

    # ── Generación de respuesta ──────────────────────────────────────

    def _generate_answer_sync(
        self, query: str, context: str, user_name: str
    ) -> dict:
        """Genera respuesta con Azure OpenAI Chat (sync)."""
        system_prompt = (
            "Eres el asistente virtual de TI de DAPPS "
            "(Digital Applications & Platform Solutions).\n"
            "Tu función es responder consultas de los colaboradores sobre "
            "procesos internos de TI usando ÚNICAMENTE la información del "
            "contexto proporcionado.\n\n"
            "REGLAS:\n"
            "1. Responde SOLO con información del contexto. NO inventes datos.\n"
            "2. Si el contexto NO responde la pregunta, responde EXACTAMENTE: "
            "NO_ENCONTRADO\n"
            "3. Sé conciso, claro y profesional.\n"
            "4. Usa formato Markdown para legibilidad.\n"
            "5. Incluye contactos, horarios o pasos relevantes si aplican.\n"
            "6. Responde en español.\n"
            "7. No menciones que usas un contexto o base de conocimientos — "
            "responde de forma natural como si fueras un experto de TI de DAPPS."
        )

        user_prompt = (
            f"CONTEXTO (procesos internos DAPPS):\n{context}\n\n"
            f"PREGUNTA DE {user_name}:\n{query}\n\n"
            "Responde usando SOLO la información del contexto. "
            "Si no puedes responder, responde exactamente: NO_ENCONTRADO"
        )

        response = self._openai_client.chat.completions.create(
            model=self._chat_deployment,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=1000,
        )

        answer_text = response.choices[0].message.content.strip()

        if "NO_ENCONTRADO" in answer_text:
            return {"found": False, "answer": ""}

        return {"found": True, "answer": answer_text}

    async def _generate_answer(
        self, query: str, context: str, user_name: str
    ) -> dict:
        """Wrapper async."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._generate_answer_sync, query, context, user_name
        )


# ── Instancia singleton ─────────────────────────────────────────────
rag_service = RAGService()
