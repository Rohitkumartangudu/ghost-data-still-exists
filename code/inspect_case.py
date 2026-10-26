"""Developer utility: print the dossier (or any tool result) for a case.
Usage: python code/inspect_case.py <case_id> [tool_name json_args]"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

from ghost.tools.registry import dispatch  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) > 2:
        print(json.dumps(dispatch(sys.argv[2], json.loads(sys.argv[3])), indent=1, ensure_ascii=False, default=str))
    else:
        print(json.dumps(dispatch("get_case_dossier", {"case_id": sys.argv[1]}), indent=1, ensure_ascii=False, default=str))
