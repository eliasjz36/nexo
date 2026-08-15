#!/usr/bin/env python3
"""
Prueba de humo de la app: ejecuta el script completo de Streamlit sin navegador
(framework oficial AppTest) y verifica que no lance excepciones y que muestre
los elementos centrales. Corre también contra una base parcial: la app debe
ser robusta al estado del pipeline.

Uso:  .venv/bin/python tests/test_app.py
"""
import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "app" / "streamlit_app.py")


def main():
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()

    errores = []
    if at.exception:
        errores.append(f"excepción en la app: {at.exception}")
    if not at.title:
        errores.append("falta el título")
    if not at.warning:
        errores.append("falta el disclaimer sanitario")
    if not at.sidebar.checkbox:
        errores.append("faltan los filtros de la barra lateral")

    # si hay hipótesis, elegir la primera debe renderizar el detalle sin romper
    if at.radio:
        at.radio[0].set_value(at.radio[0].options[0])
        at.run()
        if at.exception:
            errores.append(f"excepción al abrir el detalle: {at.exception}")

    if errores:
        print("FALLÓ:")
        for e in errores:
            print(f"  - {e}")
        sys.exit(1)
    n_hip = len(at.radio[0].options) if at.radio else 0
    print(f"OK — la app corre sin excepciones ({n_hip} hipótesis listadas)")


if __name__ == "__main__":
    main()
