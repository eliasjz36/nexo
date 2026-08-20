#!/usr/bin/env python3
"""
Paso 5 del pipeline: el agente escéptico (falsador).

Para cada hipótesis top, el LLM local recibe la cadena A→C→B con sus frases de
evidencia textuales y su tarea NO es opinar si "suena bien": es intentar
refutarla. Recibe además las contradicciones que existan en el propio grafo
(aristas con signos opuestos o hallazgos negativos entre los mismos nodos).

Una hipótesis solo "sobrevive" si el escéptico no encuentra cómo tumbarla.
Los veredictos quedan guardados (la app los muestra; no se recalculan en vivo).

Uso:  python3 05_skeptic.py --top 20
"""
import argparse
import json
import sqlite3
from pathlib import Path

import requests

DB = Path(__file__).resolve().parent.parent / "data" / "corpus.db"
SERVER = "http://127.0.0.1:8090"
MODELO = "qwen2.5-14b-instruct-q4km"

SCHEMA = {
    "type": "object",
    "properties": {
        "veredicto": {"type": "string", "enum": ["sobrevive", "refutada", "dudosa"]},
        "mecanismo": {"type": "string"},
        "a_favor": {"type": "string"},
        "en_contra": {"type": "string"},
        "como_probar": {"type": "string"},
    },
    "required": ["veredicto", "mecanismo", "a_favor", "en_contra", "como_probar"],
}

PROMPT = """Sos un revisor científico escéptico. Tu trabajo es intentar REFUTAR la \
siguiente hipótesis, no confirmarla. Solo si no encontrás forma sólida de tumbarla, \
declarala "sobrevive".

HIPÓTESIS: «{a}» {tipo_desc} «{b}».
(Los dos conceptos nunca fueron estudiados juntos en el corpus; la conexión surge de \
los puentes de abajo.)

EVIDENCIA (extraída de papers reales, con la frase textual):
{evidencia}

CONTRADICCIONES DETECTADAS EN EL GRAFO (si las hay):
{contradicciones}

Evaluá tres cosas, en este orden:
1. ¿La cadena mecanística es coherente? (¿los efectos realmente componen en esa \
dirección, o hay un salto lógico?)
2. ¿La evidencia citada de verdad sostiene cada eslabón, o las frases son débiles, \
tangenciales o de contextos no comparables (in vitro vs clínico, especies distintas, \
dosis)?
3. ¿Hay contradicciones que debiliten algún eslabón?

Respondé en español, conciso:
- "mecanismo": la cadena causal propuesta en UNA frase.
- "a_favor": el argumento más fuerte a favor (máx 2 frases).
- "en_contra": el ataque más fuerte que encontraste (máx 2 frases). Si no hay, decilo.
- "veredicto": "refutada" si el ataque destruye la cadena, "dudosa" si la debilita \
seriamente, "sobrevive" solo si resiste.
- "como_probar": el estudio o experimento más directo que confirmaría o refutaría \
esta hipótesis (1-2 frases concretas: tipo de estudio, qué se mediría).
"""


def evidencia_de(con, hid: int) -> tuple[str, str]:
    """Arma el texto de evidencia y contradicciones para una hipótesis."""
    lineas, contras = [], []
    for c, s1, s2, orientacion, pa, pb in con.execute(
            "SELECT c, s1, s2, orientacion, pmids_lado_a, pmids_lado_b "
            "FROM puentes WHERE hipotesis_id=? LIMIT 5", (hid,)):
        a, b = con.execute("SELECT a, b FROM hipotesis WHERE id=?", (hid,)).fetchone()
        # frases de respaldo de cada lado del puente (una por lado alcanza)
        for lado, origen, destino, pmids in (("A→C", a, c, pa), (orientacion, c, b, pb)):
            pmid = pmids.split(",")[0]
            fila = con.execute(
                "SELECT relacion, frase FROM relaciones_norm WHERE pmid=? AND "
                "((sujeto=? AND objeto=?) OR (sujeto=? AND objeto=?)) LIMIT 1",
                (pmid, origen, destino, destino, origen)).fetchone()
            if fila:
                rel, frase = fila
                lineas.append(f'- [{lado}] «{origen}» {rel} «{destino}» '
                              f'(PMID {pmid}): "{frase}"')
        # contradicciones: aristas con signo opuesto o no_afecta en el puente
        for x, y in ((a, c), (c, b), (b, c)):
            for rel, frase, pmid in con.execute(
                    "SELECT relacion, frase, pmid FROM relaciones_norm "
                    "WHERE sujeto=? AND objeto=? AND (relacion='no_afecta' OR signo=?)",
                    (x, y, -s1 if (x, y) == (a, c) else -s2)):
                contras.append(f'- «{x}» {rel} «{y}» (PMID {pmid}): "{frase}"')
    return "\n".join(dict.fromkeys(lineas)), ("\n".join(dict.fromkeys(contras)) or "ninguna")


def evaluar(con, hid: int) -> dict:
    a, b, tipo = con.execute(
        "SELECT a, b, tipo FROM hipotesis WHERE id=?", (hid,)).fetchone()
    evidencia, contradicciones = evidencia_de(con, hid)
    tipo_desc = ("actuaría en sentido OPUESTO al proceso o efecto de"
                 if tipo == "opuesto" else
                 "actuaría en el MISMO sentido que")
    r = requests.post(f"{SERVER}/v1/chat/completions", json={
        "messages": [{"role": "user", "content": PROMPT.format(
            a=a, b=b, tipo_desc=tipo_desc, evidencia=evidencia,
            contradicciones=contradicciones)}],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "v", "schema": SCHEMA}},
        "temperature": 0.2, "max_tokens": 700,
    }, timeout=300)
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS veredictos (
        hipotesis_id INTEGER PRIMARY KEY, veredicto TEXT, mecanismo TEXT,
        a_favor TEXT, en_contra TEXT, como_probar TEXT, modelo TEXT)""")
    # migración: si la tabla vieja no tiene la columna, agregarla
    cols = [c[1] for c in con.execute("PRAGMA table_info(veredictos)")]
    if "como_probar" not in cols:
        con.execute("ALTER TABLE veredictos ADD COLUMN como_probar TEXT")

    pendientes = con.execute("""
        SELECT id FROM hipotesis WHERE id NOT IN
        (SELECT hipotesis_id FROM veredictos) ORDER BY score DESC LIMIT ?""",
        (args.top,)).fetchall()
    print(f"Evaluando {len(pendientes)} hipótesis…\n")
    for (hid,) in pendientes:
        a, b = con.execute("SELECT a, b FROM hipotesis WHERE id=?", (hid,)).fetchone()
        try:
            v = evaluar(con, hid)
            con.execute(
                "INSERT OR REPLACE INTO veredictos "
                "(hipotesis_id, veredicto, mecanismo, a_favor, en_contra, "
                "como_probar, modelo) VALUES (?,?,?,?,?,?,?)",
                (hid, v["veredicto"], v["mecanismo"], v["a_favor"],
                 v["en_contra"], v["como_probar"], MODELO))
            con.commit()
            print(f"  [{v['veredicto']:9s}] {a} ⇒ {b}")
        except Exception as e:
            print(f"  ERROR en {a} ⇒ {b}: {e}")


if __name__ == "__main__":
    main()
