# NEXO

Aplicación de IA que propone hipótesis científicas conectando literatura de áreas
que no se citan entre sí.

Trabajo de fin de ciclo — Inteligencia Artificial Aplicada a Organizaciones (UTN FRBA).

## La idea

Hay hallazgos publicados en un área que podrían resolver problemas de otra, pero nadie
los relaciona porque se publican en revistas separadas. NEXO lee artículos de PubMed,
extrae relaciones (X reduce Y, Y empeora Z) y busca cadenas A→C→B entre dos áreas que
nunca fueron conectadas directamente. Cada hipótesis se muestra con sus fuentes (PMID),
la coherencia de la cadena y un chequeo de novedad contra PubMed.

## Caso de estudio

El sistema se evalúa con un **corte temporal**: se alimenta solo con literatura
**anterior a 2013** de dos dominios — farmacología de inhibidores SGLT2 (diabetes) y
fisiopatología de la insuficiencia cardíaca — y se verifica si redescubre la conexión
entre ambos, que la ciencia confirmó recién en 2015-2019 (EMPA-REG, DAPA-HF).

## Estructura

```
pipeline/   scripts del pipeline offline (corpus → extracción → grafo → hipótesis)
app/        aplicación web (Streamlit)
data/       corpus y grafo de conocimiento (SQLite)
docs/       informe y documentación
```

## Cómo correr

*(se completa a medida que avanza el desarrollo)*

## Enlaces

- **App en vivo:** https://a9katfbde589gdtas3ybxg.streamlit.app
- **Informe:** [docs/informe_final.pdf](docs/informe_final.pdf)
- **Registro de desarrollo (co-work con IA):** [DEVLOG.md](DEVLOG.md)
