#!/usr/bin/env bash
# Gera os PDFs dos runbooks (o upload do Playground aceita PDF, DOC ou CSV, não Markdown).
# Requer pandoc e libreoffice. Uso: ./rag/build_pdfs.sh
set -euo pipefail
cd "$(dirname "$0")"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p pdf
for md in runbooks/*.md; do
  name=$(basename "$md" .md)
  pandoc "$md" -f gfm -o "$tmp/$name.docx"
  libreoffice --headless --convert-to pdf --outdir pdf "$tmp/$name.docx" >/dev/null
  echo "pdf/$name.pdf"
done
