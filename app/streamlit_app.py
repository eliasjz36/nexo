#!/usr/bin/env python3
"""
NEXO — interfaz web.

Tres modos de uso:
  1) Hipótesis: explorar las 772 hipótesis precomputadas por el pipeline,
     con su evidencia, novedad y veredicto del escéptico.
  2) Explorar el grafo: buscar cualquier concepto y ver sus relaciones.
  3) Descubrí vos: elegir dos conceptos y buscar puentes EN VIVO, con
     narración y crítica de un LLM.

El pipeline pesado (extracción con LLM local) corre offline y sus resultados
viajan en data/corpus.db. La capa viva usa la API gratuita de Gemini con tope
de uso por sesión; si la key no está configurada, la app funciona igual sin
esas funciones.

Correr local:  streamlit run app/streamlit_app.py
"""
import json
import sqlite3
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests
import streamlit as st

DB = Path(__file__).resolve().parent.parent / "data" / "corpus.db"
MAX_LLM_POR_SESION = 20

st.set_page_config(page_title="NEXO", page_icon="🔗", layout="wide")


# ── acceso a datos ─────────────────────────────────────────────────────────
def conexion():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS veredictos (
        hipotesis_id INTEGER PRIMARY KEY, veredicto TEXT, mecanismo TEXT,
        a_favor TEXT, en_contra TEXT, como_probar TEXT, modelo TEXT)""")
    if "como_probar" not in [c[1] for c in
                             con.execute("PRAGMA table_info(veredictos)")]:
        con.execute("ALTER TABLE veredictos ADD COLUMN como_probar TEXT")
    con.execute("""CREATE TABLE IF NOT EXISTS feedback (
        hipotesis_id INTEGER PRIMARY KEY, valor TEXT, ts TEXT)""")
    return con


def registrar(accion: str):
    con = conexion()
    con.execute("CREATE TABLE IF NOT EXISTS registro_uso (ts TEXT, accion TEXT)")
    con.execute("INSERT INTO registro_uso VALUES (?,?)",
                (datetime.now().isoformat(timespec="seconds"), accion))
    con.commit()
    con.close()


def guardar_feedback(hid: int, valor: str):
    con = conexion()
    con.execute("INSERT OR REPLACE INTO feedback VALUES (?,?,?)",
                (hid, valor, datetime.now().isoformat(timespec="seconds")))
    con.commit()
    con.close()
    registrar(f"feedback:{valor}:hipotesis={hid}")


@st.cache_data(ttl=300)
def estadisticas():
    con = conexion()
    e = {
        "papers": con.execute("SELECT COUNT(*) FROM papers").fetchone()[0],
        "relaciones": con.execute("SELECT COUNT(*) FROM relaciones_norm").fetchone()[0],
        "nodos": con.execute("SELECT COUNT(*) FROM nodos").fetchone()[0],
        "hipotesis": con.execute("SELECT COUNT(*) FROM hipotesis").fetchone()[0],
    }
    con.close()
    return e


def contar_pubmed_vivo(a: str, b: str) -> int:
    r = requests.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                     params={"db": "pubmed",
                             "term": f'"{a}"[tiab] AND "{b}"[tiab]',
                             "rettype": "count"}, timeout=15)
    r.raise_for_status()
    return int(ET.fromstring(r.text).findtext("Count"))


# ── LLM en vivo (Gemini, capa gratuita) ────────────────────────────────────
def clave_llm() -> str:
    try:
        return st.secrets.get("GEMINI_API_KEY", "")
    except Exception:  # sin archivo de secrets (p. ej. entorno local limpio)
        return ""


def hay_llm() -> bool:
    return bool(clave_llm())


def llm_disponibles() -> int:
    usados = st.session_state.get("llm_usos", 0)
    return max(0, MAX_LLM_POR_SESION - usados)


def gemini(prompt: str, contar: bool = True) -> str | None:
    """Llamada a Gemini. `contar=False` para las exploraciones en vivo, que
    llevan su propio límite por sesión. Devuelve None si no se puede."""
    if not hay_llm() or (contar and llm_disponibles() <= 0):
        return None
    key = clave_llm()
    for modelo in ("gemini-2.5-flash", "gemini-2.0-flash"):
        try:
            r = requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{modelo}:generateContent",
                params={"key": key},
                json={"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                      "generationConfig": {"temperature": 0.4,
                                           "maxOutputTokens": 900}},
                timeout=60)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            partes = r.json()["candidates"][0]["content"]["parts"]
            if contar:
                st.session_state["llm_usos"] = \
                    st.session_state.get("llm_usos", 0) + 1
            return " ".join(p.get("text", "") for p in partes).strip()
        except requests.RequestException:
            return None
    return None


def evidencia_de_hipotesis(con, hid: int) -> str:
    """Texto de contexto (cadenas + frases textuales) para anclar al LLM."""
    h = con.execute("SELECT * FROM hipotesis WHERE id=?", (hid,)).fetchone()
    lineas = [f"HIPÓTESIS: «{h['a']}» ⇒ «{h['b']}» (tipo: {h['tipo']}, "
              f"papers pre-2014 que los co-mencionan: {h['pubmed_pre2014']}, "
              f"hoy: {h['pubmed_total']})"]
    v = con.execute("SELECT * FROM veredictos WHERE hipotesis_id=?", (hid,)).fetchone()
    if v:
        lineas.append(f"VEREDICTO DEL ESCÉPTICO: {v['veredicto']} — {v['mecanismo']} "
                      f"| A favor: {v['a_favor']} | En contra: {v['en_contra']}")
    for p in con.execute("SELECT * FROM puentes WHERE hipotesis_id=? LIMIT 4",
                         (hid,)):
        lineas.append(f"PUENTE vía «{p['c']}» (signos {p['s1']},{p['s2']}, "
                      f"{p['orientacion']}):")
        for pmid in (p["pmids_lado_a"].split(",")[:2] +
                     p["pmids_lado_b"].split(",")[:2]):
            fila = con.execute(
                "SELECT sujeto, relacion, objeto, frase FROM relaciones_norm "
                "WHERE pmid=? AND (sujeto=? OR objeto=?) LIMIT 1",
                (pmid, p["c"], p["c"])).fetchone()
            if fila:
                lineas.append(f'  - «{fila["sujeto"]}» {fila["relacion"]} '
                              f'«{fila["objeto"]}» (PMID {pmid}): "{fila["frase"]}"')
    return "\n".join(lineas)


# ── encabezado ─────────────────────────────────────────────────────────────
st.title("🔗 NEXO")
st.caption("Hipótesis científicas a partir de conexiones no exploradas en la literatura")

st.warning(
    "**Prototipo académico.** Las hipótesis que se muestran son candidatos "
    "generados automáticamente a partir de literatura científica: **no son "
    "consejo médico** y requieren validación de expertos antes de cualquier uso.",
    icon="⚠️")

# ── barra lateral ──────────────────────────────────────────────────────────
with st.sidebar:
    st.header("El experimento")
    st.markdown(
        "El sistema leyó **solo papers anteriores a 2013** de dos áreas que "
        "no se citaban entre sí:\n"
        "- farmacología de **inhibidores SGLT2** (diabetes)\n"
        "- **insuficiencia cardíaca** (manejo de sodio y volumen)\n\n"
        "La conexión real entre ambas la confirmó la ciencia recién en "
        "2015-2019. Si aparece acá, el método funciona.")
    e = estadisticas()
    c1, c2 = st.columns(2)
    c1.metric("Papers leídos", e["papers"])
    c2.metric("Relaciones", e["relaciones"])
    c1.metric("Conceptos", e["nodos"])
    c2.metric("Hipótesis", e["hipotesis"])
    st.divider()
    solo_beneficio = st.checkbox("Solo las que se oponen al blanco (posible beneficio)")
    solo_sobrevive = st.checkbox("Solo las que superaron al escéptico")
    if hay_llm():
        st.divider()
        st.caption(f"🤖 Consultas al LLM disponibles en esta sesión: "
                   f"{llm_disponibles()}/{MAX_LLM_POR_SESION}")

con = conexion()
ICONO = {"sobrevive": "✅", "dudosa": "🤔", "refutada": "❌", None: "⏳"}

tab_h, tab_g, tab_d, tab_v = st.tabs(
    ["📋 Hipótesis", "🔎 Explorar el grafo", "🧪 Descubrí vos",
     "🌐 NEXO en vivo"])

# ═══════════════════════════════════════════════ TAB 1: HIPÓTESIS
with tab_h:
    consulta = """
        SELECT h.*, v.veredicto FROM hipotesis h
        LEFT JOIN veredictos v ON v.hipotesis_id = h.id WHERE 1=1"""
    if solo_beneficio:
        consulta += " AND h.tipo='opuesto'"
    if solo_sobrevive:
        consulta += " AND v.veredicto='sobrevive'"
    consulta += " ORDER BY h.score DESC LIMIT 50"
    hipotesis = con.execute(consulta).fetchall()

    col_lista, col_detalle = st.columns([2, 3], gap="large")

    with col_lista:
        st.subheader(f"Hipótesis ({len(hipotesis)})")
        opciones = {
            f"{ICONO[h['veredicto']]} {h['a']}  ⇒  {h['b']}   ·  "
            f"{h['n_puentes']} puente(s)": h["id"]
            for h in hipotesis}
        if not opciones:
            st.info("No hay hipótesis con esos filtros.")
            st.stop()
        try:
            hip_param = int(st.query_params.get("hip", -1))
        except ValueError:
            hip_param = -1
        indice = (list(opciones.values()).index(hip_param)
                  if hip_param in opciones.values() else 0)
        eleccion = st.radio("Elegí una para ver el detalle:",
                            list(opciones), index=indice,
                            label_visibility="collapsed")
        hid = opciones[eleccion]

    h = con.execute("""SELECT h.*, v.veredicto, v.mecanismo, v.a_favor,
                       v.en_contra, v.como_probar
                       FROM hipotesis h LEFT JOIN veredictos v
                       ON v.hipotesis_id=h.id WHERE h.id=?""", (hid,)).fetchone()
    registrar(f"ver:hipotesis={hid}:{h['a']}=>{h['b']}")

    with col_detalle:
        st.subheader(f"{h['a']}  ⇒  {h['b']}")
        if h["tipo"] == "opuesto":
            tipo_txt = ("actuaría en sentido **opuesto** a **{b}** — si el blanco "
                        "es una patología o proceso dañino, sugiere un posible "
                        "beneficio")
        else:
            tipo_txt = ("actuaría en el **mismo sentido** que **{b}** — si el "
                        "blanco es un fármaco, sugiere un efecto similar; si es "
                        "una patología, un posible riesgo")
        st.markdown(f"Hipótesis: **{h['a']}** " + tipo_txt.format(b=h["b"]) +
                    f". Los dos conceptos **nunca aparecen juntos** en el corpus.")

        st.markdown("##### Novedad")
        n1, n2, n3 = st.columns([1, 1, 2])
        n1.metric("Papers juntos pre-2014", h["pubmed_pre2014"]
                  if h["pubmed_pre2014"] is not None else "—")
        n2.metric("Papers juntos hoy", h["pubmed_total"]
                  if h["pubmed_total"] is not None else "—")
        pre, tot = h["pubmed_pre2014"], h["pubmed_total"]
        if pre is not None and tot and tot >= 50 and pre * 20 <= tot:
            n3.success(f"★ Redescubrimiento: al corte casi no había literatura "
                       f"conjunta ({pre} papers) y hoy hay {tot}. El sistema "
                       f"encontró la conexión antes de poder leerla.")
        if n3.button("🔄 Re-chequear novedad en PubMed (en vivo)"):
            try:
                vivo = contar_pubmed_vivo(h["a"], h["b"])
                n3.info(f"PubMed hoy: {vivo} papers que mencionan ambos.")
                registrar(f"novedad_viva:hipotesis={hid}:{vivo}")
            except Exception:
                n3.error("PubMed no respondió; probá de nuevo.")

        st.markdown("##### Veredicto del escéptico")
        if h["veredicto"]:
            st.markdown(f"{ICONO[h['veredicto']]} **{h['veredicto'].upper()}** — "
                        f"*{h['mecanismo']}*")
            cf, cc = st.columns(2)
            cf.success(f"**A favor:** {h['a_favor']}")
            cc.error(f"**En contra:** {h['en_contra']}")
        else:
            st.caption("Esta hipótesis aún no fue evaluada por el agente escéptico.")

        if h["veredicto"] and h["como_probar"]:
            st.markdown("##### Cómo probarla")
            st.markdown(f"🧪 {h['como_probar']}")

        st.markdown("##### Puentes y evidencia")
        puentes = con.execute(
            "SELECT * FROM puentes WHERE hipotesis_id=? ORDER BY producto LIMIT 8",
            (hid,)).fetchall()
        for p in puentes:
            f1 = "↑" if p["s1"] > 0 else "↓"
            f2 = "↑" if p["s2"] > 0 else "↓"
            titulo = (f"{h['a']} {f1} → **{p['c']}** → {f2} {h['b']}"
                      if p["orientacion"] == "C→B" else
                      f"{h['a']} {f1} → **{p['c']}** ← {f2} {h['b']}")
            with st.expander(titulo):
                for lado, pmids in (("lado A", p["pmids_lado_a"]),
                                    ("lado B", p["pmids_lado_b"])):
                    for pmid in pmids.split(",")[:3]:
                        fila = con.execute(
                            """SELECT sujeto, relacion, objeto, frase
                               FROM relaciones_norm WHERE pmid=? AND
                               (sujeto=? OR objeto=? OR sujeto=? OR objeto=?)
                               LIMIT 1""",
                            (pmid, p["c"], p["c"], h["a"], h["b"])).fetchone()
                        if fila:
                            st.markdown(
                                f"- ({lado}) «{fila['sujeto']}» *{fila['relacion']}* "
                                f"«{fila['objeto']}» — "
                                f"[PMID {pmid}](https://pubmed.ncbi.nlm.nih.gov/{pmid}/)")
                            st.caption(f"“{fila['frase']}”")

        # ── LLM en vivo: interrogar la evidencia ──────────────────────────
        st.markdown("##### 💬 Interrogá la evidencia (LLM en vivo)")
        if not hay_llm():
            st.caption("Función no disponible: falta configurar la API key del "
                       "LLM en los Secrets del despliegue.")
        elif llm_disponibles() <= 0:
            st.caption("Se agotaron las consultas al LLM de esta sesión "
                       "(recargá la página para renovarlas).")
        else:
            pregunta = st.text_input(
                "Preguntale a NEXO sobre esta hipótesis",
                placeholder="¿Qué experimento haría falta para confirmarla? "
                            "¿Por qué dudó el escéptico?",
                key=f"preg_{hid}")
            if st.button("Preguntar", key=f"btn_preg_{hid}") and pregunta.strip():
                contexto = evidencia_de_hipotesis(con, hid)
                respuesta = gemini(
                    "Sos el asistente de NEXO, un sistema que propone hipótesis "
                    "científicas conectando literatura. Respondé la pregunta del "
                    "usuario SOLO con base en la evidencia de abajo; si algo no "
                    "está en la evidencia, decilo explícitamente. Sé conciso "
                    "(máximo 2 párrafos), en español.\n\n=== EVIDENCIA ===\n"
                    f"{contexto}\n\n=== PREGUNTA ===\n{pregunta}")
                registrar(f"llm_pregunta:hipotesis={hid}")
                if respuesta:
                    st.info(respuesta)
                else:
                    st.error("El LLM no respondió (límite de la capa gratuita "
                             "o error de red). Probá en un momento.")

        st.markdown("##### Tu evaluación")
        fb = con.execute("SELECT valor FROM feedback WHERE hipotesis_id=?",
                         (hid,)).fetchone()
        if fb:
            st.info(f"Ya la marcaste como: **{fb['valor']}**")
        b1, b2, b3 = st.columns(3)
        if b1.button("👍 Prometedora"):
            guardar_feedback(hid, "prometedora")
            st.rerun()
        if b2.button("👎 Descartar"):
            guardar_feedback(hid, "descartada")
            st.rerun()
        if b3.button("🤷 Ya se sabía"):
            guardar_feedback(hid, "conocida")
            st.rerun()

# ═══════════════════════════════════════════════ TAB 2: EXPLORAR EL GRAFO
with tab_g:
    st.subheader("Buscá cualquier concepto del grafo")
    q = st.text_input("Concepto", placeholder="natriuresis, dapagliflozin, "
                      "blood pressure…", key="busqueda")
    if q.strip():
        registrar(f"buscar:{q.strip()[:60]}")
        nodos = con.execute(
            "SELECT nombre, menciones FROM nodos WHERE nombre LIKE ? "
            "ORDER BY menciones DESC LIMIT 15", (f"%{q.strip().lower()}%",)
        ).fetchall()
        if not nodos:
            st.info("Ningún concepto del grafo coincide con esa búsqueda.")
        else:
            elegido = st.selectbox(
                "Conceptos que coinciden",
                [f"{n['nombre']}  ·  {n['menciones']} menciones" for n in nodos])
            nodo = elegido.split("  ·")[0]
            st.markdown(f"#### Relaciones de «{nodo}»")
            filas = con.execute(
                """SELECT sujeto, relacion, objeto, pmid, frase
                   FROM relaciones_norm WHERE sujeto=? OR objeto=?
                   ORDER BY pmid DESC LIMIT 25""", (nodo, nodo)).fetchall()
            for f in filas:
                with st.expander(f"«{f['sujeto']}» {f['relacion']} «{f['objeto']}»"
                                 f"  — PMID {f['pmid']}"):
                    st.caption(f"“{f['frase']}”")
                    st.markdown(f"[Ver paper en PubMed]"
                                f"(https://pubmed.ncbi.nlm.nih.gov/{f['pmid']}/)")
            hs = con.execute(
                """SELECT DISTINCT h.id, h.a, h.b, h.tipo FROM hipotesis h
                   LEFT JOIN puentes p ON p.hipotesis_id = h.id
                   WHERE h.a=? OR h.b=? OR p.c=? LIMIT 10""",
                (nodo, nodo, nodo)).fetchall()
            if hs:
                st.markdown(f"#### Hipótesis en las que participa")
                for hh in hs:
                    st.markdown(f"- [{hh['a']} ⇒ {hh['b']}](?hip={hh['id']}) "
                                f"({hh['tipo']})")

# ═══════════════════════════════════════════════ TAB 3: DESCUBRÍ VOS
with tab_d:
    st.subheader("Elegí dos conceptos y NEXO busca puentes en vivo")
    st.caption("El mismo método del sistema, pero manejado por vos: caminos "
               "A→C→B con signos coherentes, novedad en PubMed y análisis del LLM.")
    candidatos = [r["nombre"] for r in con.execute(
        "SELECT nombre FROM nodos WHERE menciones >= 3 ORDER BY menciones DESC")]
    ca, cb = st.columns(2)
    nodo_a = ca.selectbox("Concepto A (por ej. un fármaco o intervención)",
                          candidatos, index=None,
                          placeholder="escribí para buscar…")
    nodo_b = cb.selectbox("Concepto B (por ej. una enfermedad o proceso)",
                          candidatos, index=None,
                          placeholder="escribí para buscar…")

    if st.button("🧪 Buscar puentes", disabled=not (nodo_a and nodo_b)) \
            and nodo_a and nodo_b and nodo_a != nodo_b:
        registrar(f"descubrir:{nodo_a}×{nodo_b}")
        # aristas firmadas alrededor de A y de B
        desde_a = defaultdict(list)
        for f in con.execute(
                "SELECT objeto, signo, pmid, frase, relacion FROM relaciones_norm "
                "WHERE sujeto=? AND signo != 0", (nodo_a,)):
            desde_a[f["objeto"]].append(f)
        lado_b = defaultdict(list)
        for f in con.execute(
                "SELECT sujeto AS otro, signo, pmid, frase, relacion, 'C→B' AS ori "
                "FROM relaciones_norm WHERE objeto=? AND signo != 0 "
                "UNION ALL "
                "SELECT objeto, signo, pmid, frase, relacion, 'B→C' "
                "FROM relaciones_norm WHERE sujeto=? AND signo != 0",
                (nodo_b, nodo_b)):
            lado_b[f["otro"]].append(f)

        puentes_c = sorted(set(desde_a) & set(lado_b) - {nodo_a, nodo_b})
        if not puentes_c:
            st.info("No hay puentes con relaciones firmadas entre esos dos "
                    "conceptos en el corpus. Probá con conceptos más "
                    "mencionados (el grafo se limita a 2 dominios pre-2013).")
        else:
            st.success(f"{len(puentes_c)} puente(s) encontrados")
            resumen = []
            for c in puentes_c[:6]:
                e1, e2 = desde_a[c][0], lado_b[c][0]
                f1 = "↑" if e1["signo"] > 0 else "↓"
                f2 = "↑" if e2["signo"] > 0 else "↓"
                flecha = "→" if e2["ori"] == "C→B" else "←"
                with st.expander(f"{nodo_a} {f1} → **{c}** {flecha} {f2} {nodo_b}"):
                    st.markdown(f"- «{nodo_a}» *{e1['relacion']}* «{c}» — "
                                f"[PMID {e1['pmid']}]"
                                f"(https://pubmed.ncbi.nlm.nih.gov/{e1['pmid']}/)")
                    st.caption(f"“{e1['frase']}”")
                    st.markdown(f"- lado B ({e2['ori']}): *{e2['relacion']}* — "
                                f"[PMID {e2['pmid']}]"
                                f"(https://pubmed.ncbi.nlm.nih.gov/{e2['pmid']}/)")
                    st.caption(f"“{e2['frase']}”")
                resumen.append(
                    f"puente «{c}»: «{nodo_a}» {e1['relacion']} «{c}» "
                    f'("{e1["frase"][:140]}"); lado B ({e2["ori"]}): '
                    f'«{nodo_b if e2["ori"] == "B→C" else c}» {e2["relacion"]} '
                    f'«{c if e2["ori"] == "B→C" else nodo_b}» ("{e2["frase"][:140]}")')

            try:
                vivo = contar_pubmed_vivo(nodo_a, nodo_b)
                st.metric("Papers en PubMed que ya mencionan ambos (hoy, en vivo)",
                          vivo)
            except Exception:
                vivo = None

            if hay_llm() and llm_disponibles() > 0:
                with st.spinner("El LLM analiza los puentes…"):
                    analisis = gemini(
                        "Sos el analista de NEXO. Un usuario propuso conectar "
                        f"«{nodo_a}» con «{nodo_b}». Los puentes encontrados en "
                        "papers reales (anteriores a 2013) son:\n\n"
                        + "\n".join(resumen[:5]) +
                        f"\n\nPapers actuales en PubMed que ya mencionan ambos: "
                        f"{vivo if vivo is not None else 'desconocido'}.\n\n"
                        "Respondé en español, conciso: (1) la hipótesis que "
                        "surge, en una frase; (2) el mecanismo propuesto; (3) una "
                        "crítica escéptica honesta (¿qué debilita esta cadena?); "
                        "(4) si los papers actuales son muchos, aclarar que ya "
                        "no sería novedosa. Basate SOLO en la evidencia dada.")
                if analisis:
                    st.markdown("#### 🤖 Análisis del LLM")
                    st.info(analisis)
                else:
                    st.caption("El LLM no respondió (límite o error de red).")
            elif not hay_llm():
                st.caption("Análisis del LLM no disponible: falta configurar la "
                           "API key en los Secrets del despliegue.")

# ═══════════════════════════════════════════════ TAB 4: NEXO EN VIVO
MAX_EXPLORACIONES = 2
SIGNOS_VIVO = {"aumenta": +1, "causa": +1, "reduce": -1, "previene": -1,
               "trata": -1, "inhibe": -1, "se_asocia": 0, "no_afecta": 0}


def _norm(t: str) -> str:
    import re
    t = t.lower().replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", t).strip()


def pubmed_corpus_vivo(tema: str, n: int = 60) -> list[dict]:
    """Baja en el momento los n abstracts más relevantes de un tema."""
    r = requests.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                     params={"db": "pubmed", "term": tema, "retmax": n,
                             "sort": "relevance"}, timeout=30)
    r.raise_for_status()
    ids = [e.text for e in ET.fromstring(r.text).findall(".//Id")]
    papers = []
    for i in range(0, len(ids), 100):
        rf = requests.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                          params={"db": "pubmed", "id": ",".join(ids[i:i + 100]),
                                  "retmode": "xml"}, timeout=60)
        rf.raise_for_status()
        for art in ET.fromstring(rf.text).findall(".//PubmedArticle"):
            pmid = art.findtext(".//PMID")
            trozos = [t.text or "" for t in art.findall(".//Abstract/AbstractText")]
            abstract = " ".join(trozos).strip()
            if abstract:
                papers.append({"pmid": pmid, "abstract": abstract[:1400]})
        time.sleep(0.4)
    return papers


def extraer_lote_vivo(lote: list[dict]) -> list[dict]:
    """Extrae relaciones de un lote de abstracts con UNA llamada a Gemini."""
    textos = "\n\n".join(f"[PMID {p['pmid']}]\n{p['abstract']}" for p in lote)
    prompt = (
        "Extract explicit biomedical relations from each abstract below.\n"
        "Return ONLY a JSON array; one object per abstract that has relations:\n"
        '[{"pmid": "...", "relaciones": [{"sujeto": "...", "relacion": "...", '
        '"objeto": "...", "frase": "..."}]}]\n'
        "Rules: sujeto/objeto in English, lowercase, 1-4 words. relacion must be "
        "one of: aumenta, reduce, causa, previene, trata, inhibe, se_asocia, "
        "no_afecta. frase = exact verbatim fragment from that abstract. Max 5 "
        "relations per abstract. Skip methodology details.\n\n" + textos)
    salida = gemini(prompt, contar=False)
    if not salida:
        return []
    try:
        inicio, fin = salida.find("["), salida.rfind("]") + 1
        datos = json.loads(salida[inicio:fin])
    except (ValueError, json.JSONDecodeError):
        return []
    abstracts = {p["pmid"]: _norm(p["abstract"]) for p in lote}
    filas = []
    for item in datos:
        ab = abstracts.get(str(item.get("pmid", "")), "")
        for rel in item.get("relaciones", []):
            if (rel.get("relacion") in SIGNOS_VIVO and rel.get("sujeto")
                    and rel.get("objeto")
                    and _norm(rel.get("frase", "")) in ab):  # capa 0 en vivo
                filas.append({
                    "pmid": str(item["pmid"]),
                    "sujeto": rel["sujeto"].strip().lower(),
                    "objeto": rel["objeto"].strip().lower(),
                    "relacion": rel["relacion"],
                    "signo": SIGNOS_VIVO[rel["relacion"]],
                    "frase": rel["frase"].strip(),
                })
    return filas


with tab_v:
    st.subheader("Explorá dos áreas NUEVAS, en vivo")
    st.caption(
        "La versión liviana del pipeline completo, ejecutada en el momento: "
        "baja los ~60 papers más relevantes de cada tema desde PubMed, extrae "
        "relaciones con el LLM (verificando cada frase contra el abstract) y "
        "busca puentes con signos coherentes. Tarda 2-4 minutos. Limitación "
        "honesta: corpus chico y sin la pasada profunda del escéptico — la "
        "versión completa corre offline.")
    usadas = st.session_state.get("exploraciones", 0)
    if not hay_llm():
        st.info("Esta función necesita la API key del LLM en los Secrets del "
                "despliegue.")
    elif usadas >= MAX_EXPLORACIONES:
        st.info("Límite de exploraciones de esta sesión alcanzado (protección "
                "de la capa gratuita). Recargá la página para renovar.")
    else:
        cta, ctb = st.columns(2)
        tema_a = cta.text_input("Tema A (fármaco, intervención, molécula…)",
                                placeholder="ej: melatonin", key="vivo_a")
        tema_b = ctb.text_input("Tema B (enfermedad, proceso…)",
                                placeholder="ej: parkinson disease", key="vivo_b")
        st.caption(f"Exploraciones disponibles en esta sesión: "
                   f"{MAX_EXPLORACIONES - usadas}/{MAX_EXPLORACIONES}. "
                   "Consejo: temas en inglés encuentran más literatura.")
        if st.button("🌐 Explorar en vivo",
                     disabled=not (tema_a.strip() and tema_b.strip())):
            st.session_state["exploraciones"] = usadas + 1
            registrar(f"vivo:{tema_a.strip()}×{tema_b.strip()}")
            barra = st.progress(0, text="Bajando papers de PubMed…")
            try:
                corpus_a = pubmed_corpus_vivo(tema_a.strip())
                barra.progress(15, text=f"Tema A: {len(corpus_a)} abstracts. "
                                        "Bajando tema B…")
                corpus_b = pubmed_corpus_vivo(tema_b.strip())
                barra.progress(30, text=f"Tema B: {len(corpus_b)} abstracts. "
                                        "Extrayendo relaciones con el LLM…")
            except requests.RequestException:
                barra.empty()
                st.error("PubMed no respondió. Probá de nuevo en un momento.")
                st.stop()
            if not corpus_a or not corpus_b:
                barra.empty()
                st.warning("Alguno de los temas no tiene papers con abstract en "
                           "PubMed. Probá con otro término (mejor en inglés).")
            else:
                rel_a, rel_b = [], []
                lotes = [(corpus_a[i:i + 8], "a") for i in range(0, len(corpus_a), 8)]
                lotes += [(corpus_b[i:i + 8], "b") for i in range(0, len(corpus_b), 8)]
                for k, (lote, lado) in enumerate(lotes):
                    (rel_a if lado == "a" else rel_b).extend(extraer_lote_vivo(lote))
                    barra.progress(30 + int(60 * (k + 1) / len(lotes)),
                                   text=f"Extrayendo… lote {k + 1}/{len(lotes)} "
                                        f"({len(rel_a) + len(rel_b)} relaciones "
                                        "verificadas)")
                    time.sleep(4)  # respeto del límite por minuto de la capa gratis
                barra.progress(95, text="Buscando puentes…")

                # anclas: entidades que contienen el tema; si no, las más citadas
                def anclas(rels, tema):
                    ents = defaultdict(int)
                    for r in rels:
                        ents[r["sujeto"]] += 1
                        ents[r["objeto"]] += 1
                    con_tema = [e for e in ents if tema.lower() in e]
                    if con_tema:
                        return sorted(con_tema, key=ents.get, reverse=True)[:3]
                    return sorted(ents, key=ents.get, reverse=True)[:3]

                an_a = anclas(rel_a, tema_a.strip())
                an_b = anclas(rel_b, tema_b.strip())
                desde_a = defaultdict(list)
                for r in rel_a:
                    if r["sujeto"] in an_a and r["signo"] != 0:
                        desde_a[r["objeto"]].append(r)
                alrededor_b = defaultdict(list)
                for r in rel_b:
                    if r["signo"] == 0:
                        continue
                    if r["objeto"] in an_b:
                        alrededor_b[r["sujeto"]].append({**r, "ori": "C→B"})
                    if r["sujeto"] in an_b:
                        alrededor_b[r["objeto"]].append({**r, "ori": "B→C"})
                puentes_c = sorted(set(desde_a) & set(alrededor_b))
                barra.progress(100, text="Listo")
                barra.empty()

                st.markdown(f"**Relaciones verificadas:** {len(rel_a)} del tema A, "
                            f"{len(rel_b)} del tema B · **Anclas:** "
                            f"{', '.join(an_a)} × {', '.join(an_b)}")
                if not puentes_c:
                    st.info("Sin puentes firmados entre las anclas con este "
                            "corpus chico. No significa que no exista conexión: "
                            "la versión completa (offline) usa 20 veces más "
                            "papers. Probá términos más específicos.")
                else:
                    st.success(f"{len(puentes_c)} puente(s) encontrados en vivo")
                    resumen = []
                    for c in puentes_c[:5]:
                        e1, e2 = desde_a[c][0], alrededor_b[c][0]
                        f1 = "↑" if e1["signo"] > 0 else "↓"
                        f2 = "↑" if e2["signo"] > 0 else "↓"
                        flecha = "→" if e2["ori"] == "C→B" else "←"
                        with st.expander(
                                f"{e1['sujeto']} {f1} → **{c}** {flecha} {f2} "
                                f"{e2['objeto'] if e2['ori'] == 'C→B' else e2['sujeto']}"):
                            st.markdown(f"- «{e1['sujeto']}» *{e1['relacion']}* "
                                        f"«{e1['objeto']}» — [PMID {e1['pmid']}]"
                                        f"(https://pubmed.ncbi.nlm.nih.gov/{e1['pmid']}/)")
                            st.caption(f"“{e1['frase']}”")
                            st.markdown(f"- «{e2['sujeto']}» *{e2['relacion']}* "
                                        f"«{e2['objeto']}» — [PMID {e2['pmid']}]"
                                        f"(https://pubmed.ncbi.nlm.nih.gov/{e2['pmid']}/)")
                            st.caption(f"“{e2['frase']}”")
                        resumen.append(
                            f"vía «{c}»: «{e1['sujeto']}» {e1['relacion']} «{c}» "
                            f'("{e1["frase"][:120]}"); «{e2["sujeto"]}» '
                            f'{e2["relacion"]} «{e2["objeto"]}» ("{e2["frase"][:120]}")')
                    try:
                        juntos = contar_pubmed_vivo(tema_a.strip(), tema_b.strip())
                        st.metric("Papers que ya mencionan ambos temas (PubMed, "
                                  "en vivo)", juntos)
                    except Exception:
                        juntos = None
                    analisis = gemini(
                        f"Un usuario exploró conectar «{tema_a}» con «{tema_b}». "
                        "Puentes hallados en papers reales:\n" + "\n".join(resumen)
                        + f"\nPapers que ya co-mencionan ambos: {juntos}.\n"
                        "En español y conciso: (1) hipótesis en una frase; (2) "
                        "mecanismo; (3) crítica escéptica honesta; (4) veredicto "
                        "de novedad según los papers co-mencionantes. Basate solo "
                        "en la evidencia dada.", contar=False)
                    if analisis:
                        st.markdown("#### 🤖 Análisis del LLM")
                        st.info(analisis)

con.close()
