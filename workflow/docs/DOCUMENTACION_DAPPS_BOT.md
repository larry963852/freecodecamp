# DAPPS Bot — Documentación Funcional

## 1. ¿Qué es DAPPS Bot?

DAPPS Bot es un asistente automatizado integrado en **Microsoft Teams** que permite a los colaboradores gestionar sus necesidades de TI directamente desde el chat. El bot recibe mensajes en lenguaje natural, los clasifica con inteligencia artificial y ejecuta acciones automáticas según el tipo de solicitud.

---

## 2. Arquitectura y Componentes

| Componente | Tecnología | Función |
|---|---|---|
| **Bot Framework** | Python (aiohttp + botbuilder) | Servidor que recibe y responde mensajes de Teams |
| **n8n** | Workflows JSON | Clasifica mensajes con IA (GPT-4.1-mini) y crea tareas en Planner |
| **RAG Service** | SQLite-vec + Azure OpenAI | Responde consultas internas buscando en la base de conocimiento |
| **Blob Storage** | Azure Blob | Almacena archivos Excel, PDF y metadata de requerimientos |
| **Document Intelligence** | Azure AI | Analiza archivos Excel/PDF para extraer artículos automáticamente |
| **Microsoft Planner** | Graph API | Gestión de tareas asignadas al equipo de TI |

### Flujo General

```
Usuario (Teams 1:1)
       │
       ▼
   Bot Python (puerto 3978)
       │
       ├──► n8n (clasificación IA)
       │       │
       │       ├── REQUERIMIENTO → Flujo de 2 pasos (Excel + Planner)
       │       ├── INCIDENCIA    → Tarea Planner directa
       │       ├── CONSULTA      → RAG primero, si no → Planner
       │       └── IGNORAR       → No accionable
       │
       ├──► RAG Service (consultas internas)
       │       └── Knowledge Base (procesos_internos_dapps.txt)
       │
       └──► Azure Blob Storage (archivos de requerimientos)
```

### Estructura del Proyecto

```
workflow/
├── app.py                         # Servidor HTTP unificado (aiohttp, puerto 3978)
├── config.py                      # Configuración desde variables de entorno
├── requirements.txt               # Dependencias Python
├── .gitignore
├── bots/                          # Handlers de Azure Bot Framework
│   ├── channel_bot.py             # Lógica de canal (notificaciones proactivas)
│   └── personal_bot.py            # Lógica de chat 1:1 (clasificación, estado)
├── handlers/                      # Lógica de negocio extraída
│   ├── attachments.py             # Descarga y extracción de adjuntos
│   ├── excel_parser.py            # Parser Excel fallback (openpyxl)
│   └── requirement_processor.py   # Pipeline de procesamiento de requerimientos
├── services/                      # Integraciones con servicios externos
│   ├── blob_service.py            # Azure Blob Storage
│   ├── document_intelligence.py   # Azure AI Document Intelligence
│   ├── n8n_client.py              # Cliente HTTP unificado para webhooks n8n
│   ├── pdf_generator.py           # Generación de PDF sustento (fpdf2)
│   ├── planner_service.py         # Microsoft Planner vía Graph API
│   └── rag_service.py             # RAG con SQLite Vector + Azure OpenAI
├── knowledge/                     # Base de conocimiento para RAG
│   └── procesos_internos_dapps.txt
├── data/                          # Estado runtime (gitignored)
├── n8n_workflows/                 # Workflows de n8n (JSON)
│   ├── consulta.json
│   ├── principal.json
│   └── requerimiento.json
├── tests/                         # Pruebas
│   └── test_rag.py
└── docs/                          # Documentación
    └── DOCUMENTACION_DAPPS_BOT.md
```

---

## 3. Tipos de Clasificación

El bot clasifica cada mensaje en **4 categorías**:

### 3.1 REQUERIMIENTO

Solicitudes de equipos, recursos, accesos o cualquier necesidad material/tecnológica.

**Flujo de 2 pasos:**
1. El usuario envía un mensaje de texto → la IA lo clasifica como REQUERIMIENTO → el bot genera un ID (`REQ-YYYYMMDD-HHMMSS`) y pide un archivo Excel.
2. El usuario adjunta un Excel (.xlsx) con los artículos → el bot lo analiza con Document Intelligence → lo sube a Azure Blob → genera un PDF de sustento (o usa el que el usuario adjunte) → crea una tarea en Microsoft Planner → notifica al responsable por email.

#### Ejemplos de mensajes que se clasifican como REQUERIMIENTO:

| # | Mensaje de ejemplo |
|---|---|
| 1 | *"Necesito solicitar 5 monitores y 10 teclados porque los actuales están dañados"* |
| 2 | *"Requiero una laptop nueva para el practicante que ingresa el lunes"* |
| 3 | *"Solicito acceso a Azure DevOps para el proyecto Falcon"* |
| 4 | *"Necesitamos 3 licencias de Visual Studio Enterprise para el equipo de desarrollo"* |
| 5 | *"Pido una extensión de memoria RAM para mi equipo, está muy lento con 8GB"* |
| 6 | *"Necesito una cuenta de servicio en Azure para el pipeline de CI/CD"* |
| 7 | *"Solicito acceso VPN para los 4 nuevos consultores que empiezan la próxima semana"* |
| 8 | *"Requiero un disco duro externo de 2TB para respaldo del proyecto"* |

---

### 3.2 INCIDENCIA

Reportes de problemas técnicos, fallos, errores o interrupciones de servicio.

**Flujo:** El mensaje se clasifica → se crea una tarea en Planner directamente → se notifica al responsable y al usuario.

#### Ejemplos de mensajes que se clasifican como INCIDENCIA:

| # | Mensaje de ejemplo |
|---|---|
| 1 | *"No puedo conectarme a la VPN desde esta mañana, me da error de timeout"* |
| 2 | *"El servidor de desarrollo se cayó y no podemos hacer deploy"* |
| 3 | *"Mi cuenta de correo está bloqueada, no puedo iniciar sesión en Outlook"* |
| 4 | *"La impresora del 3er piso no imprime, se queda en cola"* |
| 5 | *"El pipeline de CI/CD falla en el paso de build con error de dependencias"* |
| 6 | *"Internet está muy lento en la oficina, las videollamadas se cortan"* |
| 7 | *"No puedo acceder a SharePoint, me sale error 403 Forbidden"* |
| 8 | *"El sistema SAP se cuelga cuando intento generar el reporte mensual"* |

---

### 3.3 CONSULTA

Preguntas sobre procesos internos, políticas, procedimientos o información general de TI.

**Flujo:**
1. El mensaje se clasifica como CONSULTA.
2. El bot busca en la **base de conocimiento interna** (RAG) usando búsqueda vectorial + Azure OpenAI.
3. **Si encuentra respuesta** → responde directamente con la información y la fuente.
4. **Si NO encuentra respuesta** → escala a Planner creando una tarea para que un humano responda.

#### Ejemplos de mensajes que se clasifican como CONSULTA:

| # | Mensaje de ejemplo | ¿RAG responde? |
|---|---|---|
| 1 | *"¿Cuál es la política de contraseñas?"* | ✅ Sí |
| 2 | *"¿Cómo me conecto a la VPN corporativa?"* | ✅ Sí |
| 3 | *"¿Cuál es el horario de soporte técnico?"* | ✅ Sí |
| 4 | *"¿Cómo solicito acceso a Azure DevOps?"* | ✅ Sí |
| 5 | *"¿Qué hago si mi cuenta está bloqueada?"* | ✅ Sí |
| 6 | *"¿Cuánto tarda el onboarding de un nuevo colaborador?"* | ✅ Sí |
| 7 | *"¿Cuánto cuesta el almuerzo en la cafetería?"* | ❌ No → Escala a Planner |
| 8 | *"¿La empresa tiene programa de vacaciones en julio?"* | ❌ No → Escala a Planner |

---

### 3.4 IGNORAR

Mensajes que no son accionables: saludos sueltos, mensajes ambiguos, emojis sin contexto, etc. El bot no crea tareas ni escala; simplemente acusa recibo.

#### Ejemplos de mensajes que se clasifican como IGNORAR:

| # | Mensaje de ejemplo |
|---|---|
| 1 | *"ok"* |
| 2 | *"👍"* |
| 3 | *"jajaja"* |
| 4 | *"gracias"* |

> **Nota:** Los saludos como "Hola", "Buenos días", etc. son manejados localmente por el bot (sin enviar a n8n) y responden con un menú de opciones.

---

## 4. Funcionalidades Adicionales

### 4.1 Notificaciones Proactivas
El bot puede enviar mensajes al usuario en su chat 1:1 sin que el usuario escriba primero (por ejemplo, cuando se actualiza una tarea en Planner).

### 4.2 Canal de Notificaciones
Existe un canal de Teams donde el bot publica notificaciones automáticas. Los mensajes en el canal no se procesan; el bot indica que las solicitudes se deben hacer en chat 1:1.

### 4.3 Generación Automática de PDF
Si el usuario no adjunta un PDF de sustento con su requerimiento, el bot genera uno automáticamente con la información del requerimiento y los artículos del Excel.

### 4.4 Análisis de Documentos con IA
Los archivos Excel se analizan con Azure Document Intelligence para extraer artículos, cantidades y comentarios. Si el servicio falla, hay un fallback con `openpyxl`.

### 4.5 Base de Conocimiento (RAG)
La base de conocimiento (`procesos_internos_dapps.txt`) contiene información sobre:
- Políticas de contraseñas y seguridad
- Procedimientos de acceso a sistemas
- Horarios de soporte
- Proceso de onboarding
- Configuración de VPN
- Reporte de incidencias
- Y más procesos internos de TI

El RAG usa embeddings (`text-embedding-ada-002`) almacenados en SQLite con búsqueda vectorial para encontrar la información relevante y GPT-4.1-mini para generar respuestas contextualizadas.

---

## 5. Stack Tecnológico Completo

| Capa | Tecnología |
|---|---|
| Runtime | Python 3.x (aiohttp) |
| Bot Framework | Microsoft Bot Framework SDK v4 |
| IA — Clasificación | Azure OpenAI GPT-4.1-mini (vía n8n) |
| IA — RAG | Azure OpenAI Embeddings + Chat |
| Vector DB | SQLite + sqlite-vec |
| Orquestación | n8n (3 workflows: principal, requerimiento, consulta) |
| Almacenamiento | Azure Blob Storage |
| Análisis documental | Azure AI Document Intelligence |
| Gestión tareas | Microsoft Planner (Graph API) |
| Autenticación | Azure App Registration + MSAL |
| Notificaciones | Email (Graph API) + Teams proactivo |

---

*Documentación generada el 27 de febrero de 2026.*
