"""
DAPPS Bot — Test RAG Service.

Script para probar el RAG localmente con preguntas de ejemplo.
Ejecutar desde /workspaces/freecodecamp/workflow:

    python -m tests.test_rag
"""

import asyncio
import os
import sys

# Asegurar que el directorio workflow esté en el path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from services.rag_service import rag_service


# ── Preguntas de prueba ──────────────────────────────────────────────
PREGUNTAS_CON_RESPUESTA = [
    "¿Cuál es la política de contraseñas?",
    "¿Cómo conecto la VPN?",
    "¿Cuál es el horario de soporte?",
    "¿Cómo solicito acceso a Azure DevOps?",
    "¿Qué hago si mi cuenta está bloqueada?",
    "¿Cómo reporto una incidencia?",
    "¿Cuánto tarda el onboarding de un nuevo colaborador?",
]

PREGUNTAS_SIN_RESPUESTA = [
    "¿Cuánto cuesta el almuerzo en la cafetería?",
    "¿La empresa tiene programa de vacaciones en julio?",
    "¿Puedo llevar mascotas a la oficina?",
]


async def test_db_status():
    """Verifica el estado de la base de datos vectorial."""
    import sqlite3

    db_path = os.path.join(
        os.path.dirname(__file__), "..", "knowledge", "rag_vectors.db"
    )

    if not os.path.exists(db_path):
        print("❌ rag_vectors.db NO EXISTE. Ejecuta app.py primero.")
        return False

    size_kb = os.path.getsize(db_path) / 1024
    print(f"📦 DB: {db_path} ({size_kb:.1f} KB)")

    db = sqlite3.connect(db_path)
    try:
        import sqlite_vec
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        print("✅ sqlite-vec cargado")
    except Exception as e:
        print(f"⚠️  sqlite-vec no disponible: {e}")

    chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    print(f"📝 Chunks en DB: {chunks}")

    try:
        vecs = db.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]
        print(f"🔢 Vectores (vec0): {vecs}")
    except Exception:
        try:
            vecs = db.execute("SELECT COUNT(*) FROM vec_chunks_fallback").fetchone()[0]
            print(f"🔢 Vectores (fallback): {vecs}")
        except Exception:
            vecs = 0
            print("⚠️  No se encontraron vectores")

    sections = db.execute(
        "SELECT section, COUNT(*) FROM chunks GROUP BY section ORDER BY COUNT(*) DESC"
    ).fetchall()
    print(f"\n📋 Secciones indexadas ({len(sections)}):")
    for sec, cnt in sections:
        print(f"   • {sec}: {cnt} chunks")

    db.close()
    return chunks > 0 and vecs > 0


async def test_search(query: str):
    """Prueba solo la búsqueda vectorial (sin LLM)."""
    results = await rag_service.search(query, top_k=3)
    print(f'\n🔍 Búsqueda: "{query}"')
    if not results:
        print("   (sin resultados)")
    for i, r in enumerate(results, 1):
        print(f"   {i}. [{r['section']}] dist={r['distance']:.4f}")
        print(f"      {r['content'][:120]}...")


async def test_answer(query: str):
    """Prueba el pipeline completo RAG (búsqueda + LLM)."""
    print(f"\n{'=' * 60}")
    print(f"❓ {query}")
    print(f"{'=' * 60}")

    result = await rag_service.answer(query, "Usuario Test")

    if result["found"]:
        print(f"✅ ENCONTRADO (confianza: {result['confidence']:.2f})")
        print(f"📚 Fuentes: {', '.join(result['sources'])}")
        print(f"\n💬 Respuesta:\n{result['answer']}")
    else:
        print("🔄 NO ENCONTRADO → Se escalaría a Planner")

    return result


async def main():
    print("=" * 60)
    print("🧪 DAPPS Bot — Test RAG Service")
    print("=" * 60)

    # 1. Estado de la DB
    print("\n📊 ESTADO DE LA BASE DE DATOS")
    print("-" * 40)
    db_ok = await test_db_status()
    if not db_ok:
        print("\n⚠️  DB vacía o no existe. Inicializando RAG...")
        await rag_service.initialize()
        if not rag_service.is_available:
            print("❌ RAG no se pudo inicializar. Verifica variables de entorno.")
            return
        print("✅ RAG inicializado")
    else:
        await rag_service.initialize()
        if not rag_service.is_available:
            print("❌ RAG no disponible. Verifica AZURE_OPENAI_* en .env")
            return

    # 2. Búsqueda vectorial (sin LLM)
    print("\n\n📊 BÚSQUEDA VECTORIAL (sin LLM)")
    print("-" * 40)
    await test_search("política de contraseñas")
    await test_search("horario de soporte técnico")
    await test_search("precio pizza cuatro quesos")

    # 3. Pipeline completo (con LLM)
    print("\n\n📊 PIPELINE COMPLETO (con LLM)")
    print("-" * 40)

    encontradas = 0
    for q in PREGUNTAS_CON_RESPUESTA[:3]:
        r = await test_answer(q)
        if r["found"]:
            encontradas += 1

    no_encontradas = 0
    for q in PREGUNTAS_SIN_RESPUESTA[:2]:
        r = await test_answer(q)
        if not r["found"]:
            no_encontradas += 1

    # 4. Resumen
    print(f"\n\n{'=' * 60}")
    print("📊 RESUMEN")
    print(f"{'=' * 60}")
    print(f"✅ Encontradas: {encontradas}/{min(3, len(PREGUNTAS_CON_RESPUESTA))}")
    print(f"🔄 Escaladas: {no_encontradas}/{min(2, len(PREGUNTAS_SIN_RESPUESTA))}")
    print(f"📦 RAG disponible: {rag_service.is_available}")


if __name__ == "__main__":
    asyncio.run(main())
