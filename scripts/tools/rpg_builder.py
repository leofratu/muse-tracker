from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RPG_PATH = ROOT / "repo_plan" / "rpg.json"
FILE_INDEX_PATH = ROOT / "repo_plan" / "file_index.json"
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv"}



def collect_files() -> list[str]:
    files: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if relative.name == ".DS_Store":
            continue
        if relative.parts[:2] == ("repo_plan", "runs"):
            continue
        files.append(str(relative))
    return sorted(files)



def build_file_index() -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": collect_files(),
    }



def ensure_rpg() -> dict:
    if RPG_PATH.exists():
        return json.loads(RPG_PATH.read_text())
    return {
        "version": 1,
        "task": "Initialize Repository Planning Graph",
        "nodes": [],
        "edges": [],
        "work_plan": [],
        "localization": {"nodes": [], "files": []},
    }



def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh the file index and preserve the current RPG.")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--rpg-mode", default="minimal")
    parser.add_argument("--dep-depth", default="1")
    parser.add_argument("--include-tests", action="store_true")
    args = parser.parse_args()

    rpg = ensure_rpg()
    file_index = build_file_index()

    if args.write:
        RPG_PATH.parent.mkdir(parents=True, exist_ok=True)
        FILE_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        RPG_PATH.write_text(json.dumps(rpg, indent=2) + "\n")
        FILE_INDEX_PATH.write_text(json.dumps(file_index, indent=2) + "\n")

    print(json.dumps({"rpg": str(RPG_PATH), "file_index": str(FILE_INDEX_PATH), "count": len(file_index["files"])}))


if __name__ == "__main__":
    main()
