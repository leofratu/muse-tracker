from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RPG_PATH = ROOT / "repo_plan" / "rpg.json"
VALID_STATUSES = {"planned", "implemented", "tested"}



def main() -> None:
    payload = json.loads(RPG_PATH.read_text())
    node_ids = {node["id"] for node in payload.get("nodes", [])}
    if len(node_ids) != len(payload.get("nodes", [])):
        raise SystemExit("Duplicate node ids found in RPG")
    for item in payload.get("work_plan", []):
        if item.get("id") not in node_ids:
            raise SystemExit(f"Unknown work_plan node: {item.get('id')}")
        if item.get("status") not in VALID_STATUSES:
            raise SystemExit(f"Invalid work_plan status: {item.get('status')}")
    print("RPG VALIDATION OK")


if __name__ == "__main__":
    main()
