#!/usr/bin/env python3
"""
NEXO — interfaz web.

Lee el grafo y las hipótesis PRECOMPUTADAS por el pipeline (data/corpus.db).
La app no llama a ningún LLM: por eso puede estar publicada sin API keys y sin
costo de operación. El único servicio en vivo es el re-chequeo de novedad
contra PubMed (API pública gratuita).

Correr local:  streamlit run app/streamlit_app.py
"""
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import requests
import streamlit as st

DB = Path(__file__).resolve().parent.parent / "data" / "corpus.db"

st.set_page_config(page_title="NEXO", page_icon="🔗", layout="wide")


# ── acceso a datos ─────────────────────────────────────────────────────────
def conexion():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    # tablas que el pipeline crea en pasos posteriores: si aún no existen,
    # la app debe funcionar igual (robustez, no crashear por orden de corrida)
    con.execute("""CREATE TABLE IF NOT EXISTS veredictos (
        hipotesis_id INTEGER PRIMARY KEY, veredicto TEXT, mecanismo TEXT,
        a_favor TEXT, en_contra TEXT, modelo TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS feedback (
        hipotesis_id INTEGER PRIMARY KEY, valor TEXT, ts TEXT)""")
    return con


def registrar(accion: str):
    """Log de uso: cada interacción queda registrada (sirve además como
    'registro de sesión real' del informe)."""
    con = conexion()
    con.execute("""CREATE TABLE IF NOT EXISTS registro_uso (
        ts TEXT, accion TEXT)""")
    con.execute("INSERT INTO registro_uso VALUES (?,?)",
                (datetime.now().isoformat(timespec="seconds"), accion))
    con.commit()
    con.close()


def guardar_feedback(hid: int, valor: str):
    con = conexion()
    con.execute("""CREATE TABLE IF NOT EXISTS feedback (
        hipotesis_id INTEGER PRIMARY KEY, valor TEXT, ts TEXT)""")
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
    solo_beneficio = st.checkbox("Solo las que se oponen al blanco (posible beneficio)",
                                 value=False)
    solo_sobrevive = st.checkbox("Solo las que superaron al escéptico", value=False)

# ── lista de hipótesis ─────────────────────────────────────────────────────
con = conexion()
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

ICONO = {"sobrevive": "✅", "dudosa": "🤔", "refutada": "❌", None: "⏳"}

with col_lista:
    st.subheader(f"Hipótesis ({len(hipotesis)})")
    opciones = {
        f"{ICONO[h['veredicto']]} {h['a']}  ⇒  {h['b']}   ·  "
        f"{h['n_puentes']} puente(s)": h["id"]
        for h in hipotesis}
    if not opciones:
        st.info("No hay hipótesis con esos filtros.")
        st.stop()
    eleccion = st.radio("Elegí una para ver el detalle:",
                        list(opciones), label_visibility="collapsed")
    hid = opciones[eleccion]

# ── detalle ────────────────────────────────────────────────────────────────
h = con.execute("""SELECT h.*, v.veredicto, v.mecanismo, v.a_favor, v.en_contra
                   FROM hipotesis h LEFT JOIN veredictos v
                   ON v.hipotesis_id=h.id WHERE h.id=?""", (hid,)).fetchone()
registrar(f"ver:hipotesis={hid}:{h['a']}=>{h['b']}")

with col_detalle:
    st.subheader(f"{h['a']}  ⇒  {h['b']}")
    if h["tipo"] == "opuesto":
        tipo_txt = ("actuaría en sentido **opuesto** a **{b}** — si el blanco es "
                    "una patología o proceso dañino, sugiere un posible beneficio")
    else:
        tipo_txt = ("actuaría en el **mismo sentido** que **{b}** — si el blanco "
                    "es un fármaco, sugiere un efecto similar; si es una "
                    "patología, un posible riesgo")
    st.markdown(f"Hipótesis: **{h['a']}** " + tipo_txt.format(b=h["b"]) +
                f". Los dos conceptos **nunca aparecen juntos** en el corpus.")

    # novedad
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

    # veredicto del escéptico
    st.markdown("##### Veredicto del escéptico")
    if h["veredicto"]:
        st.markdown(f"{ICONO[h['veredicto']]} **{h['veredicto'].upper()}** — "
                    f"*{h['mecanismo']}*")
        cf, cc = st.columns(2)
        cf.success(f"**A favor:** {h['a_favor']}")
        cc.error(f"**En contra:** {h['en_contra']}")
    else:
        st.caption("Esta hipótesis aún no fue evaluada por el agente escéptico.")

    # puentes con evidencia trazable
    st.markdown("##### Puentes y evidencia")
    puentes = con.execute(
        "SELECT * FROM puentes WHERE hipotesis_id=? ORDER BY producto LIMIT 8",
        (hid,)).fetchall()
    for p in puentes:
        flecha1 = "↑" if p["s1"] > 0 else "↓"
        flecha2 = "↑" if p["s2"] > 0 else "↓"
        titulo = (f"{h['a']} {flecha1} → **{p['c']}** → {flecha2} {h['b']}"
                  if p["orientacion"] == "C→B" else
                  f"{h['a']} {flecha1} → **{p['c']}** ← {flecha2} {h['b']}")
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

    # feedback del experto (la memoria que persiste)
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

con.close()
