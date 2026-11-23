"""Pergunta ao OGX sobre o cluster, deixando o modelo usar o servidor MCP.

Usa a Responses API do OGX (compatível com OpenAI): o OGX chama as ferramentas
MCP do lado do servidor e devolve a resposta final.

Exemplos:
    export OGX_URL=http://<servico-ogx>.<projeto>.svc.cluster.local:8321
    export MAAS_TOKEN=<token>   # só para modelos do Models as a Service
    python ask_cluster.py "Quais pods estão com problema no cluster?"
    python ask_cluster.py --model llama32-3b "Qual a versão do OpenShift?"
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from openai import OpenAI

DEFAULT_MCP_URL = "http://ocp-cluster-mcp.ocp-ai-lab.svc.cluster.local:8000/mcp"

INSTRUCTIONS = (
    "Você é um assistente de operações de OpenShift. Para responder sobre o cluster, "
    "use SEMPRE as ferramentas do servidor MCP 'ocp-cluster' em vez de supor. "
    "Responda em português, de forma objetiva, citando nomes de recursos."
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("question", help="Pergunta sobre o cluster")
    parser.add_argument("--ogx-url", default=os.environ.get("OGX_URL", "http://localhost:8321"),
                        help="URL base do servidor OGX (sem /v1). Padrão: $OGX_URL")
    parser.add_argument("--mcp-url", default=os.environ.get("MCP_URL", DEFAULT_MCP_URL),
                        help="URL do servidor MCP, vista pelo OGX. Padrão: $MCP_URL")
    parser.add_argument("--model", default=os.environ.get("OGX_MODEL"),
                        help="ID do modelo no OGX. Padrão: $OGX_MODEL ou o primeiro LLM listado")
    parser.add_argument("--verbose", "-v", action="store_true", help="Mostra as chamadas de ferramenta")
    args = parser.parse_args()

    # Modelos do Models as a Service exigem o token da subscription. O Playground o envia
    # no header X-OGX-Provider-Data; fora dele, precisamos enviar.
    headers = {}
    if os.environ.get("MAAS_TOKEN"):
        headers["X-OGX-Provider-Data"] = json.dumps({"vllm_api_token": os.environ["MAAS_TOKEN"]})
    client = OpenAI(
        base_url=args.ogx_url.rstrip("/") + "/v1",
        api_key=os.environ.get("OGX_API_KEY", "none"),
        default_headers=headers,
    )

    model = args.model
    if not model:
        models = [m.id for m in client.models.list()]
        llms = [m for m in models if "embed" not in m.lower()]
        if not llms:
            print("Nenhum modelo encontrado no OGX. Use --model.", file=sys.stderr)
            return 1
        model = llms[0]
        if args.verbose:
            print(f"[modelo] {model}  (disponíveis: {', '.join(models)})", file=sys.stderr)

    response = client.responses.create(
        model=model,
        instructions=INSTRUCTIONS,
        input=args.question,
        tools=[
            {
                "type": "mcp",
                "server_label": "ocp-cluster",
                "server_url": args.mcp_url,
                "require_approval": "never",
            }
        ],
    )

    if args.verbose:
        for item in response.output:
            if item.type == "mcp_call":
                print(f"[ferramenta] {item.name}({item.arguments})", file=sys.stderr)
            elif item.type == "mcp_list_tools":
                print(f"[mcp] {len(item.tools)} ferramentas disponíveis", file=sys.stderr)

    print(response.output_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
