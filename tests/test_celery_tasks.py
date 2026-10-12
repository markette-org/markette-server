"""Every task the code queues must be known to the worker (Celery only auto-discovers tasks.py)."""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def shared_tasks():
    for path in (ROOT / "apps").rglob("*.py"):
        tree = ast.parse(path.read_text())
        module = ".".join(path.relative_to(ROOT).with_suffix("").parts)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and any(
                (isinstance(d, ast.Name) and d.id == "shared_task")
                or (isinstance(d, ast.Call) and getattr(d.func, "id", "") == "shared_task")
                for d in node.decorator_list
            ):
                yield f"{module}.{node.name}"


# What a freshly started worker knows (other tests may already have imported every module here).
WORKER = """
import json, django
django.setup()
from markette.celery import app
app.loader.import_default_modules()
print(json.dumps(sorted(app.tasks)))
"""


def test_every_shared_task_is_registered_with_the_worker():
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": os.environ.get("DJANGO_SETTINGS_MODULE", "markette.settings")}
    out = subprocess.run([sys.executable, "-c", WORKER], cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    registered = set(json.loads(out.stdout.strip().splitlines()[-1]))
    names = list(shared_tasks())
    assert "apps.analytics.history.import_history" in names
    missing = [n for n in names if n not in registered]
    assert not missing, f"Not imported by any tasks.py, so the worker cannot run them: {missing}"
