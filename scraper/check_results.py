import json
from pathlib import Path

path = Path("out/manifest.json")
if not path.exists():
    raise SystemExit("No manifest produced")

manifest = json.loads(path.read_text(encoding="utf-8"))
if manifest.get("count", 0) == 0:
    raise SystemExit("No requests were processed")
if manifest.get("failure_count", 0):
    raise SystemExit(f"{manifest['failure_count']} request(s) failed; artifact still uploaded")
print(f"All {manifest['success_count']} request(s) fetched successfully")
