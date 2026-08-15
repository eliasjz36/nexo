#!/usr/bin/env python3
"""
Paso 1 del pipeline: descarga del corpus desde PubMed (API E-utilities, gratuita).

Dos dominios con corte temporal en 2013:
  A) farmacología de inhibidores SGLT2 (diabetes)
  B) fisiopatología de la insuficiencia cardíaca (manejo de sodio/volumen)

La conexión entre ambos se publicó recién en 2015-2019, así que un corpus cortado
en 2013 permite evaluar si el sistema la "redescubre" (metodología de time-slicing).

Uso:
    python3 01_fetch_corpus.py --probe        # solo contar resultados por consulta
    python3 01_fetch_corpus.py --fetch        # descargar abstracts a data/corpus.db
    python3 01_fetch_corpus.py --fetch --max 800
"""
import argparse
import json
import sqlite3
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
DB = Path(__file__).resolve().parent.parent / "data" / "corpus.db"

# Corte temporal: nada posterior a esta fecha entra al corpus
CORTE = '("1900/01/01"[PDAT] : "2013/12/31"[PDAT])'

# Términos del dominio A: el fármaco y su mecanismo (pre-2013 la literatura usa
# mucho "phlorizin", el inhibidor SGLT clásico estudiado desde hace décadas)
TERMINOS_A = (
    '(SGLT2[tiab] OR "SGLT-2"[tiab] OR "sodium glucose cotransporter 2"[tiab]'
    ' OR "sodium-glucose cotransporter 2"[tiab] OR gliflozin*[tiab]'
    ' OR dapagliflozin[tiab] OR empagliflozin[tiab] OR canagliflozin[tiab]'
    ' OR phlorizin[tiab] OR phlorhizin[tiab])'
)

# Términos del dominio B: insuficiencia cardíaca, enfocada en los mecanismos que
# podrían conectar con el dominio A (sodio, volumen, precarga, diuresis)
TERMINOS_B = (
    '"heart failure"[MeSH Major Topic] AND (natriuresis[tiab]'
    ' OR "sodium retention"[tiab] OR "sodium excretion"[tiab]'
    ' OR "volume overload"[tiab] OR preload[tiab] OR "osmotic diuresis"[tiab]'
    ' OR diuretic*[tiab] OR hemodynamic*[tiab])'
)

DOMINIOS = {
    "sglt2": f"{TERMINOS_A} AND {CORTE}",
    "falla_cardiaca": f"({TERMINOS_B}) AND {CORTE}",
}

# Consulta de contaminación: papers que YA mencionan ambos temas antes del corte
# (p. ej. diseños de ensayos clínicos ~2012-2013). Se mide y se excluye del corpus.
CONTAMINACION = f'{TERMINOS_A} AND "heart failure"[tiab] AND {CORTE}'


def pedir(endpoint: str, params: dict) -> str:
    """Llamada a E-utilities respetando el límite de 3 requests/segundo."""
    r = requests.get(f"{BASE}/{endpoint}", params=params, timeout=30)
    r.raise_for_status()
    time.sleep(0.4)
    return r.text


def contar(consulta: str) -> int:
    xml = pedir("esearch.fcgi", {"db": "pubmed", "term": consulta, "rettype": "count"})
    return int(ET.fromstring(xml).findtext("Count"))


def buscar_ids(consulta: str, maximo: int) -> list[str]:
    xml = pedir("esearch.fcgi", {
        "db": "pubmed", "term": consulta, "retmax": maximo, "sort": "relevance",
    })
    return [e.text for e in ET.fromstring(xml).findall(".//Id")]


def descargar_abstracts(pmids: list[str]) -> list[dict]:
    """Baja los registros en lotes de 200 y devuelve los que tienen abstract."""
    papers = []
    for i in range(0, len(pmids), 200):
        lote = pmids[i:i + 200]
        xml = pedir("efetch.fcgi", {
            "db": "pubmed", "id": ",".join(lote), "retmode": "xml",
        })
        raiz = ET.fromstring(xml)
        for art in raiz.findall(".//PubmedArticle"):
            pmid = art.findtext(".//PMID")
            titulo = art.findtext(".//ArticleTitle") or ""
            trozos = [t.text or "" for t in art.findall(".//Abstract/AbstractText")]
            abstract = " ".join(trozos).strip()
            if not abstract:
                continue  # sin abstract no hay nada que extraer
            # Año: algunos registros usan MedlineDate ("1992 Jul-Aug") en vez de Year
            anio = art.findtext(".//JournalIssue/PubDate/Year") or ""
            if not anio:
                md = art.findtext(".//JournalIssue/PubDate/MedlineDate") or ""
                anio = md[:4] if md[:4].isdigit() else ""
            # Filtro estricto del corte temporal: el filtro [PDAT] de PubMed deja
            # pasar registros con fecha electrónica 2013 pero impresa 2014
            if not anio or int(anio) > 2013:
                continue
            revista = art.findtext(".//Journal/Title") or ""
            mesh = [m.text for m in art.findall(".//MeshHeading/DescriptorName")]
            papers.append({
                "pmid": pmid, "titulo": titulo, "abstract": abstract,
                "anio": anio, "revista": revista, "mesh": mesh,
            })
        print(f"    lote {i // 200 + 1}: {len(papers)} papers con abstract acumulados")
    return papers


def guardar(papers: list[dict], dominio: str):
    DB.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS papers (
        pmid TEXT PRIMARY KEY, dominio TEXT, anio TEXT,
        titulo TEXT, abstract TEXT, revista TEXT, mesh TEXT)""")
    for p in papers:
        con.execute(
            "INSERT OR REPLACE INTO papers VALUES (?,?,?,?,?,?,?)",
            (p["pmid"], dominio, p["anio"], p["titulo"], p["abstract"],
             p["revista"], json.dumps(p["mesh"], ensure_ascii=False)),
        )
    con.commit()
    con.close()


def modo_probe():
    print("Conteos en PubMed (corte 2013/12/31):\n")
    for nombre, consulta in DOMINIOS.items():
        print(f"  {nombre}: {contar(consulta)}")
    n = contar(CONTAMINACION)
    print(f"  contaminación (ambos temas juntos pre-corte): {n}")
    print("\nLos papers 'contaminados' se excluirán del corpus en --fetch.")


def modo_fetch(maximo: int):
    excluidos = set(buscar_ids(CONTAMINACION, 10000))
    print(f"Papers contaminados a excluir: {len(excluidos)}")
    for nombre, consulta in DOMINIOS.items():
        print(f"\nDominio {nombre}:")
        ids = [i for i in buscar_ids(consulta, maximo) if i not in excluidos]
        print(f"  {len(ids)} PMIDs tras excluir contaminados; descargando…")
        papers = descargar_abstracts(ids)
        guardar(papers, nombre)
        print(f"  guardados {len(papers)} papers con abstract en {DB.name}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="solo contar, no descargar")
    ap.add_argument("--fetch", action="store_true", help="descargar a SQLite")
    ap.add_argument("--max", type=int, default=1200, help="máx. PMIDs por dominio")
    args = ap.parse_args()
    if args.probe:
        modo_probe()
    elif args.fetch:
        modo_fetch(args.max)
    else:
        ap.print_help()
