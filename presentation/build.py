#!/usr/bin/env python3
"""Build the PDF and a self-contained HTML deck (requires Node.js and Chrome)."""
import base64
import mimetypes
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent
CLI = ["npx", "--yes", "@marp-team/marp-cli@4.5.0", str(ROOT / "deck.md"), "--html"]
subprocess.run(CLI + ["--pdf", "--allow-local-files", "-o", str(ROOT / "deck.pdf")], check=True)
subprocess.run(CLI + ["-o", str(ROOT / "deck.html")], check=True)


def embed(match):
    path = (ROOT / match.group(1)).resolve()
    path.relative_to(ROOT)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f'src="data:{mime};base64,{data}"'


html_path = ROOT / "deck.html"
html = re.sub(r'src="(\./assets/[^"<>]+)"', embed, html_path.read_text())
html_path.write_text(html)
