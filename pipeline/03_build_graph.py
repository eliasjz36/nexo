#!/usr/bin/env python3
"""
Paso 3 del pipeline: normalización de entidades y construcción del grafo.

El problema real de este paso: la misma cosa aparece escrita distinto
("sglt2 inhibitors", "sglt2 inhibition", "inhibición de sglt2") y si no se
unifica, el grafo queda desconectado y el paso 4 no encuentra caminos.

Antes de normalizar hay una capa 0 de INTEGRIDAD DE EVIDENCIA: solo entran al
grafo las relaciones cuya frase de respaldo existe textualmente en el abstract.
Motivo (encontrado en los datos, no teórico): el LLM extractor a veces inyecta
conocimiento de su pre-entrenamiento como si fuera del paper — p. ej. inventó
"sglt2 inhibition trata heart failure" en papers de química sintética que jamás
mencionan la enfermedad, porque el modelo YA SABE (de literatura post-2015) que
esa conexión existe. En un experimento con corte temporal eso es contaminación
del futuro. La verificación textual elimina también paráfrasis y citas
traducidas (~10% del total): se pierde algo de recall a cambio de que TODA
arista del grafo sea verificable contra su fuente.

Después, normalización en cuatro capas, de la más segura a la más discrecional:
  1. Entidades que quedaron en español → inglés (LLM local, son pocas)
  2. Plural → singular, solo si la forma singular ya existe en los datos
  3. Tabla de alias curada a mano para los conceptos centrales (decisión
     metodológica declarada — ver ALIAS)
  4. Marca informativa de qué nodos coinciden con un descriptor MeSH

Genera las tablas `relaciones_norm` y `nodos` en data/corpus.db.

Uso:  python3 03_build_graph.py          (requiere el servidor LLM para la capa 1)
      python3 03_build_graph.py --sin-llm  (salta la capa 1)
"""
import argparse
import json
import re
import sqlite3
from pathlib import Path

import requests

DB = Path(__file__).resolve().parent.parent / "data" / "corpus.db"
SERVER = "http://127.0.0.1:8090"

# Signos por tipo de relación (redundante con 02, pero este script debe poder
# correr solo)
SIGNOS = {"aumenta": +1, "causa": +1, "reduce": -1, "previene": -1,
          "trata": -1, "inhibe": -1, "se_asocia": 0, "no_afecta": 0}

# Capa 3: alias curados a mano. Es una decisión metodológica declarada:
# unificamos variantes del MISMO concepto (no conceptos parecidos).
ALIAS = {
    # el fármaco / mecanismo central del dominio A
    "sglt2 inhibitor": "sglt2 inhibition",
    "sglt2 inhibitors": "sglt2 inhibition",
    "sodium glucose cotransporter 2 inhibition": "sglt2 inhibition",
    "sodium glucose cotransporter 2 inhibitor": "sglt2 inhibition",
    "sodium-glucose cotransporter 2 inhibition": "sglt2 inhibition",
    "sglt-2": "sglt2",
    "sodium glucose cotransporter 2": "sglt2",
    "sodium-glucose cotransporter 2": "sglt2",
    "sodium glucose co-transporter 2": "sglt2",
    # la enfermedad central del dominio B (variantes clínicas unificadas
    # a costa de granularidad; se documenta en el informe)
    "congestive heart failure": "heart failure",
    "chronic heart failure": "heart failure",
    # conceptos fisiológicos puente
    "urinary sodium excretion": "natriuresis",
    "sodium excretion": "natriuresis",
    "urinary glucose excretion": "glycosuria",
    "glucosuria": "glycosuria",
    "plasma glucose": "blood glucose",
    "blood sugar": "blood glucose",
    "glycemia": "blood glucose",
    "arterial pressure": "blood pressure",
    "arterial blood pressure": "blood pressure",
    "mean arterial pressure": "blood pressure",
}

PATRON_ESP = re.compile(r"[áéíóúñ]|\b(de|del|la|el|en|con|por)\b|ción")


def _norm_texto(t: str) -> str:
    t = t.lower().replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", t).strip()


def relaciones_validas(con) -> list[tuple]:
    """Capa 0: integridad de evidencia, en dos controles.

    0a) La frase de respaldo debe existir textualmente en el abstract.
    0b) Sujeto y objeto deben estar ANCLADOS en el texto: cada token largo de
        la entidad tiene que aparecer (por prefijo) en el abstract. Esto caza
        el engaño más fino: citar una frase real pero atribuirle una relación
        con entidades que el paper jamás menciona (p. ej. "heart failure" en
        papers de química sintética de 2010, o "sglt2" en un paper de 1997).
    """
    abstracts = {p: _norm_texto(f"{a} {t}") for p, a, t in
                 con.execute("SELECT pmid, abstract, titulo FROM papers")}
    palabras = {p: set(re.findall(r"[a-z0-9]+", txt))
                for p, txt in abstracts.items()}

    def anclada(entidad: str, pmid: str) -> bool:
        toks = [t for t in re.findall(r"[a-z0-9]+", entidad) if len(t) >= 4]
        if not toks:
            return True  # siglas cortas ("gh", "bnp"): no evaluables así
        ws = palabras.get(pmid, set())
        return all(any(t[:5] in w for w in ws) for t in toks)

    validas, sin_frase, sin_ancla = [], 0, 0
    for fila in con.execute(
            "SELECT pmid, sujeto, relacion, objeto, frase FROM relaciones"):
        pmid, s, _, o, frase = fila
        if _norm_texto(frase) not in abstracts.get(pmid, ""):
            sin_frase += 1
        elif not (anclada(s, pmid) and anclada(o, pmid)):
            sin_ancla += 1
        else:
            validas.append(fila)
    print(f"Capa 0 — evidencia verificada: {len(validas)} relaciones entran, "
          f"{sin_frase} sin frase en el abstract, {sin_ancla} con entidades "
          f"no ancladas al texto")
    return validas


def traducir(entidad: str) -> str:
    """Capa 1: pasa a inglés canónico una entidad que salió en español."""
    r = requests.post(f"{SERVER}/v1/chat/completions", json={
        "messages": [{"role": "user", "content":
            "Translate this biomedical term to canonical English: lowercase, "
            "singular, 1-4 plain words, no explanations.\n"
            f"Term: {entidad}"}],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "t", "schema": {"type": "object", "properties": {
                "english": {"type": "string"}}, "required": ["english"]}}},
        "temperature": 0.0, "max_tokens": 60,
    }, timeout=120)
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"])["english"].strip().lower()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sin-llm", action="store_true",
                    help="saltar la traducción de entidades en español")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    filas_validas = relaciones_validas(con)
    entidades = {e for f in filas_validas for e in (f[1], f[3])}
    print(f"Entidades únicas crudas: {len(entidades)}")

    mapa = {}

    # Capa 1: español → inglés
    if not args.sin_llm:
        en_espanol = sorted(e for e in entidades if PATRON_ESP.search(e))
        print(f"Capa 1 — en español: {len(en_espanol)}")
        for e in en_espanol:
            try:
                mapa[e] = traducir(e)
                print(f"    «{e}» → «{mapa[e]}»")
            except Exception as ex:
                print(f"    ERROR con «{e}»: {ex}")

    def canonica(e: str) -> str:
        return mapa.get(e, e)

    # Capa 2: plural → singular solo si el singular existe
    actuales = {canonica(e) for e in entidades}
    plurales = 0
    for e in sorted(actuales):
        if e.endswith("s") and not e.endswith("ss") and e[:-1] in actuales:
            for orig in entidades:
                if canonica(orig) == e:
                    mapa[orig] = e[:-1]
            plurales += 1
    print(f"Capa 2 — plurales unificados: {plurales}")

    # Capa 3: alias curados (se aplican sobre el resultado de las capas 1-2)
    aplicados = 0
    for orig in entidades:
        c = canonica(orig)
        if c in ALIAS:
            mapa[orig] = ALIAS[c]
            aplicados += 1
    print(f"Capa 3 — alias aplicados: {aplicados}")

    # Capa 4: ¿el nombre canónico es un descriptor MeSH del corpus?
    mesh = set()
    for (m,) in con.execute("SELECT mesh FROM papers"):
        mesh.update(t.lower() for t in json.loads(m))

    # Materializar tablas normalizadas
    con.executescript("""
        DROP TABLE IF EXISTS relaciones_norm;
        CREATE TABLE relaciones_norm (
            id INTEGER PRIMARY KEY, pmid TEXT, sujeto TEXT, relacion TEXT,
            signo INTEGER, objeto TEXT, frase TEXT);
        DROP TABLE IF EXISTS nodos;
        CREATE TABLE nodos (nombre TEXT PRIMARY KEY, menciones INTEGER,
            es_mesh INTEGER);
    """)
    for pmid, s, rel, o, frase in filas_validas:
        cs, co = canonica(s), canonica(o)
        if cs == co:
            continue  # relación consigo misma tras normalizar: ruido
        con.execute(
            "INSERT INTO relaciones_norm (pmid,sujeto,relacion,signo,objeto,frase)"
            " VALUES (?,?,?,?,?,?)", (pmid, cs, rel, SIGNOS[rel], co, frase))
    con.execute("""
        INSERT INTO nodos
        SELECT nombre, COUNT(*), 0 FROM (
            SELECT sujeto AS nombre FROM relaciones_norm
            UNION ALL SELECT objeto FROM relaciones_norm)
        GROUP BY nombre""")
    con.execute("UPDATE nodos SET es_mesh=1 WHERE lower(nombre) IN (%s)" %
                ",".join("?" * len(mesh)), list(mesh))
    con.commit()

    n_rel = con.execute("SELECT COUNT(*) FROM relaciones_norm").fetchone()[0]
    n_nod = con.execute("SELECT COUNT(*) FROM nodos").fetchone()[0]
    n_mesh = con.execute("SELECT COUNT(*) FROM nodos WHERE es_mesh=1").fetchone()[0]
    print(f"\nGrafo: {n_nod} nodos ({n_mesh} coinciden con MeSH), {n_rel} aristas")
    print("\nNodos más conectados:")
    for nombre, m in con.execute(
            "SELECT nombre, menciones FROM nodos ORDER BY menciones DESC LIMIT 15"):
        print(f"  {m:4d}  {nombre}")


if __name__ == "__main__":
    main()
