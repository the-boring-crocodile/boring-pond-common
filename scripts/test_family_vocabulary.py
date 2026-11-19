"""Literal Refused/Failed causes in src; dynamic calls have short reviewed exceptions."""
import ast
from pathlib import Path
from boring_outcomes import OUR_CODES

EXTRA_CODES = {
    "write_failed": "a local save write failed after the service answered",
    "foreground_interrupted": "the execution owner's foreground operation was interrupted",
}
DYNAMIC_CODES = {
    ("packages/boring-worker-kit/src/boring_worker_kit/outcomes.py", "code"):
        "failure_data chooses shared cleanup/interruption/timeout causes or the caller's fallback",
    ("packages/boring-worker-kit/src/boring_worker_kit/outcomes.py", "cast(str, data.get('code'))"):
        "forward the received ServiceFailure through the wire validator",
}


def test_local_cause_vocabulary():
    root = Path(__file__).resolve().parents[1]
    assert not (root / "packages/boring-outcomes/src/boring_outcomes/vocabulary.py").exists()
    paths = sorted(root.glob("packages/*/src/**/*.py"))
    assert paths and not OUR_CODES.keys() & EXTRA_CODES.keys()
    allowed, reviewed = OUR_CODES.keys() | EXTRA_CODES.keys(), set()
    for path in paths:
        for call in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if not isinstance(call, ast.Call):
                continue
            name = (call.func.id if isinstance(call.func, ast.Name) else
                    call.func.attr if isinstance(call.func, ast.Attribute) else "")
            if name not in ("Refused", "Failed"):
                continue
            code = next((kw.value for kw in call.keywords if kw.arg == "code"),
                        call.args[0] if call.args else None)
            where = f"{path.relative_to(root)}:{call.lineno}"
            if isinstance(code, ast.Constant):
                assert isinstance(code.value, str) and code.value in allowed, where
            else:
                key = (str(path.relative_to(root)), ast.unparse(code))
                assert key in DYNAMIC_CODES and DYNAMIC_CODES[key], where
                reviewed.add(key)
    assert reviewed == DYNAMIC_CODES.keys()
