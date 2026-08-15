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

**Qué falló (y lo encontró la revisión, no el plan)**: el filtro de fecha de PubMed
(`[PDAT]`) dejó pasar **31 papers de 2014** — registros con fecha electrónica 2013 pero
publicación impresa 2014. Una fuga temporal así invalidaría la evaluación por corte
(el sistema "descubriría" con información del futuro). Se detectó inspeccionando una
muestra de la base, se agregó un filtro estricto por año en el código y se limpiaron
los 31 registros. Lección: nunca confiar en el filtro de la API para un corte temporal;
verificar siempre contra el dato descargado. También había 3 registros con formato de
fecha viejo (`MedlineDate`) sin parsear; se resolvieron consultando la API.
