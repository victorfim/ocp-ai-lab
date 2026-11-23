"""Cria um vector store no OGX e carrega os runbooks de rag/runbooks.

Usa as APIs Files e Vector Stores do OGX (compatíveis com OpenAI): o próprio OGX
divide os documentos em trechos (chunks), gera os embeddings e indexa.

Isto é para o caminho do cliente (ask_cluster.py --vector-store). No Gen AI Playground,
o jeito mais simples é subir os PDFs de rag/pdf na aba Knowledge (veja rag/README.md).

Exemplos:
    export OGX_URL=http://<servico-ogx>.<projeto>.svc.cluster.local:8321
    python ingest_runbooks.py                      # cria o vector store "ocp-runbooks"
    python ingest_runbooks.py --list-models        # mostra os modelos de embedding
    python ingest_runbooks.py --embedding-model ibm-granite/granite-embedding-125m-english \
        --embedding-dimension 768 --provider-id pgvector
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from openai import OpenAI

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "rag" / "runbooks"


def make_client(ogx_url: str) -> OpenAI:
    headers = {}
    if os.environ.get("MAAS_TOKEN"):
        headers["X-OGX-Provider-Data"] = json.dumps({"vllm_api_token": os.environ["MAAS_TOKEN"]})
    return OpenAI(
        base_url=ogx_url.rstrip("/") + "/v1",
        api_key=os.environ.get("OGX_API_KEY", "none"),
        default_headers=headers,
    )


def embedding_models(client: OpenAI) -> list[tuple[str, dict]]:
    """Modelos de embedding registrados no OGX, com metadados (dimensão, quando houver)."""
    found = []
    for m in client.models.list():
        extra = m.model_extra or {}
        model_type = extra.get("model_type") or (extra.get("custom_metadata") or {}).get("model_type", "")
        if model_type == "embedding" or "embed" in m.id.lower():
            found.append((m.id, extra))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ogx-url", default=os.environ.get("OGX_URL", "http://localhost:8321"),
                        help="URL base do servidor OGX (sem /v1). Padrão: $OGX_URL")
    parser.add_argument("--name", default="ocp-runbooks", help="Nome do vector store")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="Pasta com os .md")
    parser.add_argument("--embedding-model", help="ID do modelo de embedding. Padrão: o primeiro do OGX")
    parser.add_argument("--embedding-dimension", type=int, help="Dimensão do embedding (ex.: 768)")
    parser.add_argument("--provider-id", help="Provider de vector_io do OGX (ex.: pgvector). Padrão: o do servidor")
    parser.add_argument("--chunk-tokens", type=int, default=400, help="Tamanho máximo do trecho, em tokens")
    parser.add_argument("--chunk-overlap", type=int, default=80, help="Sobreposição entre trechos, em tokens")
    parser.add_argument("--list-models", action="store_true", help="Só lista os modelos de embedding")
    args = parser.parse_args()

    client = make_client(args.ogx_url)

    models = embedding_models(client)
    if args.list_models:
        for model_id, extra in models:
            meta = extra.get("custom_metadata") or extra.get("metadata") or {}
            print(model_id, json.dumps(meta, ensure_ascii=False) if meta else "")
        return 0

    files = sorted(args.dir.glob("*.md"))
    if not files:
        print(f"Nenhum .md em {args.dir}", file=sys.stderr)
        return 1

    extra_body: dict = {}
    embedding_model = args.embedding_model or (models[0][0] if models else None)
    if embedding_model:
        extra_body["embedding_model"] = embedding_model
    if args.embedding_dimension:
        extra_body["embedding_dimension"] = args.embedding_dimension
    if args.provider_id:
        extra_body["provider_id"] = args.provider_id

    store = client.vector_stores.create(name=args.name, extra_body=extra_body or None)
    print(f"[vector store] {store.id} ({args.name}), embedding: {embedding_model or 'padrão do servidor'}")

    chunking = {
        "type": "static",
        "static": {"max_chunk_size_tokens": args.chunk_tokens, "chunk_overlap_tokens": args.chunk_overlap},
    }
    for path in files:
        with path.open("rb") as fh:
            uploaded = client.files.create(file=(path.name, fh, "text/markdown"), purpose="assistants")
        client.vector_stores.files.create(
            vector_store_id=store.id,
            file_id=uploaded.id,
            chunking_strategy=chunking,
            attributes={"runbook": "-".join(path.stem.split("-")[:2])},
        )
        print(f"[arquivo] {path.name} -> {uploaded.id}")

    # Espera a indexação terminar.
    for _ in range(60):
        status = client.vector_stores.retrieve(store.id).file_counts
        if status.in_progress == 0:
            break
        time.sleep(2)
    print(f"[pronto] concluídos={status.completed} falhas={status.failed} em andamento={status.in_progress}")

    hits = client.vector_stores.search(vector_store_id=store.id, query="Qual a janela de mudança do cluster?")
    if hits.data:
        top = hits.data[0]
        print(f"[teste de busca] melhor trecho: {top.filename} (score {top.score:.3f})")

    print(f"\nUse com: python ask_cluster.py --vector-store {store.id} \"<pergunta>\"")
    return 0 if status.failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
