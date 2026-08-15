#!/usr/bin/env python3
"""
Paso 2 del pipeline: extracción de relaciones tipadas desde los abstracts.

Usa un LLM LOCAL servido por llama-server (llama.cpp) con API compatible OpenAI
y salida forzada por JSON schema — el modelo no puede responder otra cosa que el
formato pedido. Sin costo de API y sin enviar datos afuera.

Requiere el servidor corriendo (ver servidor_llm.sh). Uso:
    python3 02_extract.py --sample 50          # muestra de prueba (25 por dominio)
    python3 02_extract.py --all --workers 4    # corpus completo, 4 pedidos en paralelo
"""
import argparse
import json
import random
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

DB = Path(__file__).resolve().parent.parent / "data" / "corpus.db"
SERVER = "http://127.0.0.1:8080"
MODELO = "qwen2.5-14b-instruct-q4km"

# Tipos de relación y su signo para la composición de cadenas (paso 4).
#   +1: el sujeto sube/provoca el objeto    -1: lo baja/bloquea    0: sin dirección
RELACIONES = {
    "aumenta": +1, "causa": +1,
    "reduce": -1, "previene": -1, "trata": -1, "inhibe": -1,
    "se_asocia": 0, "no_afecta": 0,
}

# El modelo solo puede responder este formato (llama-server lo convierte a gramática)
SCHEMA = {
    "type": "object",
    "properties": {
        "relaciones": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "sujeto": {"type": "string"},
                    "relacion": {"type": "string", "enum": list(RELACIONES)},
                    "objeto": {"type": "string"},
                    "frase": {"type": "string"},
                },
                "required": ["sujeto", "relacion", "objeto", "frase"],
            },
        }
    },
    "required": ["relaciones"],
}

# El prompt va en inglés porque los abstracts están en inglés y los términos deben
# salir canónicos en inglés (para poder normalizarlos contra MeSH). Ver DEVLOG.
PROMPT = """You are a biomedical relation extraction system.

Extract the explicit relations stated in the abstract below.

Rules:
- Only extract relations EXPLICITLY stated in the text. Never use your own \
background knowledge.
- "sujeto" and "objeto": short canonical biomedical terms in English, lowercase, \
singular, abbreviations expanded ("heart failure", not "HF"; "sglt2 inhibition", \
not "SGLT2i"). 1 to 4 words. No dosages, no percentages.
- "relacion" meanings: aumenta = subject increases/raises object; reduce = \
decreases/lowers; causa = causes/leads to/induces; previene = prevents; trata = \
treats or improves a disease/condition; inhibe = inhibits/blocks; se_asocia = \
associated, direction unclear; no_afecta = the study found NO effect of subject \
on object (negative finding — do extract these, they matter).
- "frase": the exact text fragment (verbatim) that supports the relation.
- 0 to 8 relations. Quality over quantity: skip speculative statements \
("may", "could") unless the results support them.

Abstract:
"""


def extraer(pmid: str, abstract: str) -> list[dict]:
    r = requests.post(f"{SERVER}/v1/chat/completions", json={
        "messages": [{"role": "user", "content": PROMPT + abstract}],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "relaciones", "schema": SCHEMA}},
        "temperature": 0.1,
        "max_tokens": 1200,
    }, timeout=300)
    r.raise_for_status()
    contenido = r.json()["choices"][0]["message"]["content"]
    filas = []
    for rel in json.loads(contenido)["relaciones"]:
        if rel["relacion"] in RELACIONES and rel["sujeto"] and rel["objeto"]:
            filas.append((pmid, rel["sujeto"].strip().lower(),
                          rel["relacion"], rel["objeto"].strip().lower(),
                          rel["frase"].strip(), MODELO))
    return filas


def elegir_papers(con, cuantos_por_dominio: int | None) -> list[tuple[str, str]]:
    """Muestra estratificada y reproducible, o todo el corpus pendiente."""
    hechos = {p for (p,) in con.execute("SELECT DISTINCT pmid FROM relaciones")}
    papers = []
    for dominio in ("sglt2", "falla_cardiaca"):
        filas = con.execute(
            "SELECT pmid, abstract FROM papers WHERE dominio=? ORDER BY pmid",
            (dominio,)).fetchall()
        filas = [f for f in filas if f[0] not in hechos]
        if cuantos_por_dominio:
            filas = random.Random(42).sample(filas, min(cuantos_por_dominio, len(filas)))
        papers += filas
    return papers


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, help="N abstracts por dominio (prueba)")
    ap.add_argument("--all", action="store_true", help="todo el corpus pendiente")
    ap.add_argument("--workers", type=int, default=1)
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS relaciones (
        id INTEGER PRIMARY KEY, pmid TEXT, sujeto TEXT, relacion TEXT,
        objeto TEXT, frase TEXT, modelo TEXT)""")
    con.commit()

    papers = elegir_papers(con, args.sample if not args.all else None)
    print(f"A procesar: {len(papers)} abstracts (workers={args.workers})")

    ok = errores = total_rel = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futuros = {pool.submit(extraer, p, a): p for p, a in papers}
        for fut in as_completed(futuros):
            pmid = futuros[fut]
            try:
                filas = fut.result()
                con.executemany(
                    "INSERT INTO relaciones (pmid,sujeto,relacion,objeto,frase,modelo)"
                    " VALUES (?,?,?,?,?,?)", filas)
                con.commit()
                ok += 1
                total_rel += len(filas)
                if ok % 10 == 0:
                    print(f"  {ok}/{len(papers)} papers · {total_rel} relaciones")
            except Exception as e:
                errores += 1
                print(f"  ERROR pmid {pmid}: {type(e).__name__}: {e}")

    print(f"\nListo: {ok} papers procesados, {total_rel} relaciones, {errores} errores")


if __name__ == "__main__":
    main()
