"""
Layer 2 (context delivery) of the harness — assembles what each role sees
about the target project. Deliberately shallow for v1: a file listing and
package.json, not full file contents (keeps prompts small and cheap).
"""
import json
import os

IGNORE_DIRS = {"node_modules", ".git", "dist", "build", ".claude"}


def project_snapshot(project_root: str, max_files: int = 200):
    files = []
    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        for fn in filenames:
            rel = os.path.relpath(os.path.join(dirpath, fn), project_root)
            files.append(rel)
            if len(files) >= max_files:
                return files
    return files


def read_package_json(project_root: str):
    path = os.path.join(project_root, "package.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return None
    return None
