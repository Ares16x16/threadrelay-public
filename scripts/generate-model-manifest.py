"""Bundle the model catalog exactly; CI/tests reject drift from models/manifest.json."""
import argparse
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
source = root / "models/manifest.json"
target = root / "worker/src/threadrelay/inference/manifest.json"
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--check", action="store_true")
args = parser.parse_args()
content = source.read_bytes()
if args.check:
    if not target.is_file() or target.read_bytes() != content:
        sys.exit("Model catalog drift; run python scripts/generate-model-manifest.py")
else:
    target.write_bytes(content)
