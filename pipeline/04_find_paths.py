#!/usr/bin/env python3
"""
Paso 4 del pipeline: descubrimiento de hipótesis (el patrón A→C→B de Swanson).

Busca pares (A, B) donde:
  - A pertenece al dominio SGLT2 y B al dominio insuficiencia cardíaca
  - A y B NUNCA aparecen juntos en un mismo paper del corpus
  - existe al menos un concepto puente C con relaciones firmadas de ambos lados

La composición de signos decide el tipo de hipótesis:
  A -(s1)-> C  y  C -(s2)-> B   (o bien  B -(s2)-> C)
  s1*s2 = -1  →  A se opone al proceso de B  →  hipótesis de BENEFICIO
  s1*s2 = +1  →  A empuja en la dirección de B  →  hipótesis de RIESGO

El ranking premia la CONVERGENCIA: cuantos más puentes C independientes
sostienen el mismo par (A,B), más fuerte el candidato (criterio de Swanson).

Uso:
    python3 04_find_paths.py --generar          # construir tabla de hipótesis
    python3 04_find_paths.py --novedad --top 30 # chequear novedad en PubMed
"""
import argparse
import sqlite3
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import requests

DB = Path(__file__).resolve().parent.parent / "data" / "corpus.db"

# Umbrales para filtrar ruido de extracción (nodos que aparecen una sola vez)
MIN_MENCIONES_EXTREMO = 3   # A y B deben ser conceptos consolidados
MIN_MENCIONES_PUENTE = 2
PUREZA_DOMINIO = 0.8        # A: ≥80% de sus menciones en papers del dominio A


def perfiles_de_dominio(con):
    """Cuenta menciones de cada nodo según el dominio del paper de origen."""
    perfil = defaultdict(lambda: {"sglt2": 0, "falla_cardiaca": 0})
    for nodo, dominio, n in con.execute("""
        SELECT e.nombre, p.dominio, COUNT(*)
        FROM (SELECT sujeto AS nombre, pmid FROM relaciones_norm
              UNION ALL SELECT objeto, pmid FROM relaciones_norm) e
        JOIN papers p ON p.pmid = e.pmid
        GROUP BY e.nombre, p.dominio"""):
        perfil[nodo][dominio] = n
    return perfil


def cargar_aristas(con):
    """Agrega las relaciones firmadas por par (origen, destino)."""
    aristas = defaultdict(list)   # (origen, destino) -> [(signo, pmid, relacion, frase)]
    for s, rel, signo, o, pmid, frase in con.execute(
            "SELECT sujeto, relacion, signo, objeto, pmid, frase "
            "FROM relaciones_norm WHERE signo != 0"):
        aristas[(s, o)].append((signo, pmid, rel, frase))
    return aristas


def co_menciones(con):
    """Pares de nodos que aparecen en un mismo paper (para excluir conexiones ya hechas)."""
    por_paper = defaultdict(set)
    for nombre, pmid in con.execute(
            "SELECT sujeto, pmid FROM relaciones_norm "
            "UNION SELECT objeto, pmid FROM relaciones_norm"):
        por_paper[pmid].add(nombre)
    pares = set()
    for nodos in por_paper.values():
        lista = sorted(nodos)
        for i, a in enumerate(lista):
            for b in lista[i + 1:]:
                pares.add((a, b))
    return pares


def signo_dominante(evidencia):
    """Signo mayoritario de una arista; None si hay empate (contradicción)."""
    suma = sum(s for s, *_ in evidencia)
    if suma > 0:
        return +1
    if suma < 0:
        return -1
    return None


def generar(con):
    perfil = perfiles_de_dominio(con)
    aristas = cargar_aristas(con)
    ya_conectados = co_menciones(con)
    menciones = dict(con.execute("SELECT nombre, menciones FROM nodos"))

    def es_de(nodo, dominio):
        p = perfil[nodo]
        total = p["sglt2"] + p["falla_cardiaca"]
        return total > 0 and p[dominio] / total >= PUREZA_DOMINIO

    nodos_a = {n for n in menciones if menciones[n] >= MIN_MENCIONES_EXTREMO
               and es_de(n, "sglt2")}
    nodos_b = {n for n in menciones if menciones[n] >= MIN_MENCIONES_EXTREMO
               and es_de(n, "falla_cardiaca")}
    print(f"Candidatos A (dominio sglt2): {len(nodos_a)}")
    print(f"Candidatos B (dominio falla cardíaca): {len(nodos_b)}")

    # indexar aristas por origen y por destino
    salientes = defaultdict(list)   # nodo -> [(vecino, evidencia)]
    entrantes = defaultdict(list)
    for (s, o), ev in aristas.items():
        salientes[s].append((o, ev))
        entrantes[o].append((s, ev))

    # buscar puentes: A -s1-> C, y del lado B: C -s2-> B  o  B -s2-> C
    candidatos = defaultdict(list)  # (a, b) -> [puente_info]
    for a in nodos_a:
        for c, ev1 in salientes[a]:
            if menciones.get(c, 0) < MIN_MENCIONES_PUENTE:
                continue
            s1 = signo_dominante(ev1)
            if s1 is None:
                continue
            lados_b = [(b, ev2, "C→B") for b, ev2 in salientes[c] if b in nodos_b]
            lados_b += [(b, ev2, "B→C") for b, ev2 in entrantes[c] if b in nodos_b]
            for b, ev2, orientacion in lados_b:
                s2 = signo_dominante(ev2)
                if s2 is None:
                    continue
                if (min(a, b), max(a, b)) in ya_conectados:
                    continue
                candidatos[(a, b)].append({
                    "c": c, "s1": s1, "s2": s2, "orientacion": orientacion,
                    "producto": s1 * s2,
                    "pmids_1": sorted({p for _, p, *_ in ev1}),
                    "pmids_2": sorted({p for _, p, *_ in ev2}),
                })

    # materializar tablas
    con.executescript("""
        DROP TABLE IF EXISTS hipotesis;
        CREATE TABLE hipotesis (
            id INTEGER PRIMARY KEY, a TEXT, b TEXT, tipo TEXT,
            n_puentes INTEGER, n_papers INTEGER, score REAL,
            novedad_corpus INTEGER DEFAULT 1,
            pubmed_pre2014 INTEGER, pubmed_total INTEGER);
        DROP TABLE IF EXISTS puentes;
        CREATE TABLE puentes (
            id INTEGER PRIMARY KEY, hipotesis_id INTEGER, c TEXT,
            s1 INTEGER, s2 INTEGER, orientacion TEXT, producto INTEGER,
            pmids_lado_a TEXT, pmids_lado_b TEXT);
    """)
    filas = 0
    for (a, b), puentes in candidatos.items():
        # el tipo lo decide el producto mayoritario entre los puentes
        prod = sum(p["producto"] for p in puentes)
        if prod == 0:
            continue
        tipo = "beneficio" if prod < 0 else "riesgo"
        papers = set()
        for p in puentes:
            papers.update(p["pmids_1"])
            papers.update(p["pmids_2"])
        # score: convergencia de puentes con el signo mayoritario + evidencia total
        coherentes = [p for p in puentes if (p["producto"] < 0) == (tipo == "beneficio")]
        score = len(coherentes) * 10 + len(papers)
        cur = con.execute(
            "INSERT INTO hipotesis (a,b,tipo,n_puentes,n_papers,score)"
            " VALUES (?,?,?,?,?,?)",
            (a, b, tipo, len(coherentes), len(papers), score))
        hid = cur.lastrowid
        for p in puentes:
            con.execute(
                "INSERT INTO puentes (hipotesis_id,c,s1,s2,orientacion,producto,"
                "pmids_lado_a,pmids_lado_b) VALUES (?,?,?,?,?,?,?,?)",
                (hid, p["c"], p["s1"], p["s2"], p["orientacion"], p["producto"],
                 ",".join(p["pmids_1"]), ",".join(p["pmids_2"])))
        filas += 1
    con.commit()
    print(f"\nHipótesis generadas: {filas}")
    print("\nTop 15 por score:")
    for a, b, tipo, np_, npap, sc in con.execute(
            "SELECT a,b,tipo,n_puentes,n_papers,score FROM hipotesis "
            "ORDER BY score DESC LIMIT 15"):
        print(f"  [{tipo:9s}] {a}  ⇒  {b}   (puentes={np_}, papers={npap}, score={sc:.0f})")


def contar_pubmed(term_a: str, term_b: str, hasta_2013: bool) -> int:
    consulta = f'"{term_a}"[tiab] AND "{term_b}"[tiab]'
    if hasta_2013:
        consulta += ' AND ("1900/01/01"[PDAT] : "2013/12/31"[PDAT])'
    r = requests.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                     params={"db": "pubmed", "term": consulta, "rettype": "count"},
                     timeout=30)
    r.raise_for_status()
    time.sleep(0.4)
    return int(ET.fromstring(r.text).findtext("Count"))


def novedad(con, top: int):
    """Verifica los top-N contra PubMed: ¿cuántos papers los co-mencionan?"""
    filas = con.execute(
        "SELECT id, a, b FROM hipotesis ORDER BY score DESC LIMIT ?", (top,)).fetchall()
    print(f"Chequeando novedad de {len(filas)} hipótesis contra PubMed…\n")
    for hid, a, b in filas:
        try:
            pre = contar_pubmed(a, b, hasta_2013=True)
            total = contar_pubmed(a, b, hasta_2013=False)
            con.execute("UPDATE hipotesis SET pubmed_pre2014=?, pubmed_total=? "
                        "WHERE id=?", (pre, total, hid))
            con.commit()
            marca = "★ REDESCUBRIMIENTO" if pre == 0 and total > 0 else ""
            print(f"  {a} + {b}: pre-2014={pre}, hoy={total}  {marca}")
        except Exception as e:
            print(f"  ERROR {a}+{b}: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--generar", action="store_true")
    ap.add_argument("--novedad", action="store_true")
    ap.add_argument("--top", type=int, default=30)
    args = ap.parse_args()
    con = sqlite3.connect(DB)
    if args.generar:
        generar(con)
    if args.novedad:
        novedad(con, args.top)
    if not (args.generar or args.novedad):
        print("Usar --generar y/o --novedad")
