"""Reescreve os metadados dos PDFs gerados pelo LibreOffice.

O LibreOffice grava a data como D:20261123174907Z' (com um apóstrofo sobrando), que o
leitor de PDF do OGX (pypdf) não aceita: o upload no Playground falha com
"Can not convert date". Este script troca as datas por um formato válido.
Uso: python3 fix_pdf_metadata.py pdf/*.pdf   (requer pypdf)
"""

import sys
from datetime import datetime, timezone

from pypdf import PdfReader, PdfWriter

now = datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%S+00'00'")
for path in sys.argv[1:]:
    reader = PdfReader(path)
    writer = PdfWriter(clone_from=reader)
    info = {k: v for k, v in (reader.metadata or {}).items() if k not in ("/CreationDate", "/ModDate")}
    info.update({"/CreationDate": now, "/ModDate": now, "/Producer": "ocp-ai-lab rag/build_pdfs.sh"})
    writer.metadata = None
    writer.add_metadata(info)
    with open(path, "wb") as fh:
        writer.write(fh)
    PdfReader(path).metadata.creation_date  # falha aqui se a data ainda for inválida
    print(f"{path}: metadados corrigidos")
