# DEVLOG — registro de desarrollo y co-work con IA

Registro honesto de cómo se desarrolló NEXO usando IA como copiloto (Claude Code),
qué funcionó, qué falló y qué sorprendió. Alimenta la sección "IA en co-work" del
informe final.

---

## 2026-08-15 — Día 1: arranque

**Decisiones tomadas antes de codear** (surgidas de analizar la retroalimentación del
medio ciclo con IA):

- **Arquitectura**: pipeline offline (extracción con LLM por lotes, grafo en SQLite
  versionado en el repo) + app pública que solo lee el grafo. Motivo: una app viva que
  llame a un LLM por request expone la API key, genera costo por abuso y puede morir
  sin crédito. Con el grafo precomputado la app corre gratis 24/7. El único chequeo en
  vivo es la novedad contra PubMed (API gratuita, sin key).
- **Caso de estudio precableado**: SGLT2 → insuficiencia cardíaca con corte 2013.
  Se eligió un caso donde la conexión existe y fue confirmada después del corte, para
  que la evaluación sea medible (metodología estándar del área: *time-slicing*).
  Se descartó replicar el caso clásico de Swanson (1986) porque los registros de
  PubMed anteriores a 1975 casi no tienen abstract disponible.
- **Alcance recortado a propósito**: la "memoria que aprende" se limita a
  aceptar/rechazar hipótesis con persistencia; no se promete aprendizaje que modifique
  la extracción (sería prometer algo que no se puede demostrar).

**Co-work con IA**: el análisis de factibilidad (¿existen los datos? ¿qué APIs son
gratis? ¿cuánto costaría la extracción?) se hizo con agentes de IA que verificaron
contra fuentes reales antes de escribir una línea de código. Sorpresa útil: la IA
detectó que el paso técnicamente crítico no es ninguno de los "vistosos" sino la
**normalización de entidades** (que "viscosidad sanguínea" y "blood viscosity" sean el
mismo nodo del grafo) — y que PubMed regala los términos MeSH en cada registro, que
sirven justo para eso.

**Hecho hoy**: esqueleto del repo, entorno verificado (Python 3.12, APIs accesibles).

**Corpus descargado y validado** (`pipeline/01_fetch_corpus.py`):
- Calibración previa con `--probe`: 2.040 papers pre-2013 en el dominio SGLT2, 9.972 en
  insuficiencia cardíaca, y solo **7 papers** que mencionan ambos temas antes del corte
  (contaminación). Que sean tan pocos confirma que los dominios estaban genuinamente
  desconectados antes de 2013 — condición necesaria para el experimento.
- Corpus final: **865 papers** de SGLT2 (1910-2013, incluye la literatura vieja de
  florizina) y **1.122** de insuficiencia cardíaca (1963-2013), todos con abstract y
  términos MeSH, en `data/corpus.db`.

**Decisión: extracción 100% local.** Se evaluaron tres caminos (API de Anthropic,
modelo local, o ambos comparados) y se eligió el modelo local: costo cero, los datos
no salen de la máquina, y convierte la "Parte 2" del informe (LLM local) en el corazón
real del sistema en vez de una reflexión teórica. Hardware: RTX 5070 Ti (16 GB).
Modelo elegido: Qwen2.5-14B-Instruct cuantizado Q4_K_M (~9 GB), servido con
llama-server (llama.cpp) y salida forzada por JSON schema — el modelo no puede
responder fuera del formato, lo que elimina el parseo frágil de JSON.

**Qué falló #1**: el build existente de llama.cpp era solo CPU (extraer 2.000
abstracts habría tomado 20-30 horas). Se recompiló con CUDA 13.1 en un directorio
aparte (`build-cuda`) sin tocar el build original.

**Decisión chica pero deliberada**: el prompt de extracción va en inglés aunque el
proyecto esté en español — los abstracts están en inglés y las entidades deben salir
en inglés canónico para poder normalizarlas contra los términos MeSH. Las relaciones
sí llevan nombre en español (aumenta/reduce/causa…) porque son la interfaz con el
resto del sistema y con la app.

**Iteración del prompt de extracción (v1 → v2).** La primera prueba con 6 abstracts
mostró tres fallas del modelo local: (1) entidades que salían en español o con guiones
bajos inventados ("sistema_alto_afinidad") pese a pedir inglés — el contexto en español
del proyecto "contaminaba" la salida; (2) errores semánticos: clasificó *diagnóstico*
como "trata" (una ecografía no trata la disfunción diastólica) y un "decreases" textual
como "no_afecta"; (3) relaciones de química de laboratorio sin valor para el grafo.
El prompt v2 agrega reglas explícitas contra cada una. Resultado sobre la muestra de
validación de 50 papers: 248 relaciones, 0 errores de formato, 0 guiones bajos, ~5% de
entidades aún en español (se corrigen con una pasada del LLM en el paso 3), y en
revisión manual ~7/10 relaciones limpias con el resto como ruido inofensivo — precisión
comparable a las herramientas estándar del área (SemRep ronda 0,70-0,75).

**Velocidad medida**: ~5,7 s/abstract secuencial, ~1,9 s/abstract con 4 slots paralelos
en la RTX 5070 Ti → corpus completo en ~1 hora, costo $0.

**Qué falló #4 — el hallazgo más importante del proyecto: el extractor inyectó
conocimiento del futuro.** Inspeccionando las hipótesis apareció una arista imposible:
"sglt2 inhibition trata heart failure" en papers de **química sintética de 2008-2011**
que jamás mencionan la enfermedad. El modelo local *sabe* (por su pre-entrenamiento,
que incluye literatura post-2015) que los SGLT2i tratan la falla cardíaca, y lo
"extrajo" como si estuviera en el texto. En un experimento con corte temporal, eso es
contaminación del futuro — el riesgo metodológico conocido de evaluar LBD con LLMs.
La defensa quedó en dos capas dentro del paso 3: (0a) la frase de respaldo debe existir
textualmente en el abstract (mató 1.114 relaciones, 10%), y (0b) sujeto y objeto deben
estar anclados al texto del paper — porque el modelo aprendió a citar frases reales
pero atribuirles entidades inventadas (mató 792 más, 7%). El grafo final solo contiene
relaciones verificables contra su fuente: 9.271 aristas.

**RESULTADO DEL EXPERIMENTO (2026-08-15).** Con el corpus congelado en 2013:
- La hipótesis **«sglt2 inhibition ⇒ heart failure» (sentido opuesto = posible
  beneficio) emergió en el puesto #13 de 772**, vía dos puentes mecanísticos
  (hipertensión y diabetes).
- Verificación contra PubMed: **0 papers** co-mencionaban ambos términos hasta 2013;
  hoy hay **225** → redescubrimiento confirmado por corte temporal.
- Las tres primeras hipótesis del ranking dicen que la inhibición SGLT2 actúa **en el
  mismo sentido que captopril, furosemida y tolvaptán** — los fármacos del tratamiento
  de la insuficiencia cardíaca. El sistema encontró la conexión por dos ángulos.
- El agente escéptico calificó la hipótesis estrella como "dudosa", aceptando los
  puentes de hipertensión/diabetes pero atacando un tercer puente mal extraído
  (natriuresis con signo invertido) con una contradicción del propio grafo. Es el
  comportamiento deseado: en 2013 un revisor riguroso habría dicho exactamente eso.

**Smoke test de toda la cadena con datos parciales.** En vez de esperar la hora de
extracción completa, se corrió el pipeline aguas abajo (normalización → grafo →
hipótesis → app) con el 13% del corpus ya extraído. Sirvió: la maquinaria completa
funciona, y el test automatizado de la app (`tests/test_app.py`, con el framework
AppTest de Streamlit, sin navegador) **cazó un bug real** — la app leía la tabla
`feedback` antes de que existiera. Ese es el argumento de por qué se testea antes de
tener "todos los datos".

**Qué falló #3**: el servidor LLM no levantaba — el puerto 8080 ya estaba ocupado por
un SearXNG local. Movido a 8090. Detalle tonto, pero es el tipo de cosa que consume
tiempo real de desarrollo y que ningún plan anticipa.

**Qué falló #2 (y lo encontró la revisión, no el plan)**: el filtro de fecha de PubMed
(`[PDAT]`) dejó pasar **31 papers de 2014** — registros con fecha electrónica 2013 pero
publicación impresa 2014. Una fuga temporal así invalidaría la evaluación por corte
(el sistema "descubriría" con información del futuro). Se detectó inspeccionando una
muestra de la base, se agregó un filtro estricto por año en el código y se limpiaron
los 31 registros. Lección: nunca confiar en el filtro de la API para un corte temporal;
verificar siempre contra el dato descargado. También había 3 registros con formato de
fecha viejo (`MedlineDate`) sin parsear; se resolvieron consultando la API.
