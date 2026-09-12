"""Build local Apple Vision OCR locator. No models, client operations or restart."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
import config
if sys.platform!='darwin':raise SystemExit('This locator requires macOS Vision; Linux deployment is not supported by this adapter.')
source=root/'tools/native/recognize_text.swift'
out=Path(config.WORK_DIR)/'native-adapter';out.mkdir(parents=True,exist_ok=True)
temporary=out/'recognize-text.new';binary=out/'recognize-text'
subprocess.run(['swiftc',str(source),'-o',str(temporary)],check=True)
os.replace(temporary,binary)
(out/'build.json').write_text(json.dumps(dict(source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),platform=sys.platform),indent=2))
print('Built local OCR locator. No service restarted; no WeChat messages sent.')
