import base64
from pathlib import Path

cwd = Path.cwd()
html_files = sorted(p for p in cwd.iterdir() if p.is_file() and p.suffix.lower() == '.html')

for html_path in html_files:
    txt_path = html_path.parent / (html_path.stem + '_base64.txt')
    with html_path.open('rb') as f:
        content = f.read()
    b64 = base64.b64encode(content).decode('ascii')
    with txt_path.open('w', encoding='ascii') as f:
        f.write(b64)