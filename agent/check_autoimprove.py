"""Lightweight regression checks for the autoimprove skill.

This script avoids running real improvement loops. It verifies import/export
contracts, schedule time normalization, and the interactive runtime-cap path.
"""

import ast
import importlib.util
import sys
import tempfile
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUTO_PATH = ROOT / "agent" / "skills" / "core" / "autoimprove.py"
SCHED_PATH = ROOT / "agent" / "skills" / "core" / "scheduler.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    source = AUTO_PATH.read_text(encoding="utf-8-sig")
    tree = ast.parse(source, filename=str(AUTO_PATH))

    skill_timeout_assigns = [
        node for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id == "SKILL_TIMEOUT"
    ]
    assert len(skill_timeout_assigns) == 1, "SKILL_TIMEOUT should be declared once"

    auto = load_module("autoimprove_check_subject", AUTO_PATH)
    missing_exports = [name for name in auto.__all__ if not hasattr(auto, name)]
    assert not missing_exports, f"Missing __all__ exports: {missing_exports}"

    assert "missing_docstring" not in auto.AUTO_FIXABLE
    assert '"missing_docstring"' in source, "missing_docstring should remain suggestion-visible"

    sys.modules.setdefault("requests", types.SimpleNamespace())
    scheduler = load_module("scheduler_check_subject", SCHED_PATH)
    for spec in ("every day at 12am", "every day at 12pm", "every day at 2am"):
        parsed = scheduler._parse_recurring_spec(spec)
        assert parsed["kind"] == "calendar", f"{spec} should parse as calendar"

    calls = []

    class FakeScheduler:
        def schedule_recurring(self, name, every, prompt):
            calls.append((name, every, prompt))
            return f"scheduled {name} {every}"

    def fake_import_skill(name, reload=False):
        if name == "scheduler":
            return FakeScheduler()
        raise ImportError(name)

    auto._import_skill = fake_import_skill
    result = auto.schedule_nightly("midnight")
    assert calls and calls[0][1] == "every day at 12am"
    assert "every 24h from now" not in result
    assert "exact start time is not enforced" not in result

    with tempfile.TemporaryDirectory() as tmp:
        auto.IMPROVE_LOG = Path(tmp) / "improvement_log.jsonl"
        capped = auto.run_all(max_experiments=1, max_runtime_seconds=0)
        assert "runtime cap reached" in capped
        assert "run_all complete" in capped

    print("autoimprove checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
