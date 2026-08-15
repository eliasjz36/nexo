#!/bin/bash
# Levanta el LLM local para la extracción (llama.cpp con CUDA, RTX 5070 Ti).
#   -ngl 99   : todas las capas en GPU
#   -c 16384  : contexto total, repartido entre slots
#   -np 4     : 4 pedidos en paralelo (los abstracts son cortos)
exec /home/elias/llama.cpp/build-cuda/bin/llama-server \
  -m /home/elias/models/Qwen2.5-14B-Instruct-Q4_K_M.gguf \
  -ngl 99 -c 16384 -np 4 --port 8080 --host 127.0.0.1
