"""Every SerializerMethodField must have the method it names.

This exists because of a real outage. A `beans_value = SerializerMethodField()`
was added to SaleSerializer while its `get_beans_value()` landed, through a
mis-anchored edit, in DebtSerializer twelve hundred lines away. Both files
imported cleanly, every unit test passed, and the code shipped — because
DRF resolves the method by NAME, at render time, on the instance. The failure
surfaced as `AttributeError: 'SaleSerializer' object has no attribute
'get_beans_value'` on `POST /api/v1/sales/`: a 500 on every single sale, i.e.
the till down, discovered by a barista rather than by CI.

The check is trivial and static, which is the point — it costs nothing and it
closes the whole class, not the one instance.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _serializer_modules():
    for path in sorted(ROOT.glob("apps/*/serializers.py")):
        yield path
    for path in sorted(ROOT.glob("apps/*/serializers/*.py")):
        yield path


def _declared_method_fields(cls: ast.ClassDef) -> set[str]:
    out: set[str] = set()
    for node in cls.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        name = getattr(func, "attr", getattr(func, "id", ""))
        if name != "SerializerMethodField":
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                out.add(target.id)
        # `field = SerializerMethodField(method_name="x")` names its own method.
        for kw in node.value.keywords:
            if kw.arg == "method_name" and isinstance(kw.value, ast.Constant):
                out.discard(target.id if isinstance(target, ast.Name) else "")
    return out


def _methods(cls: ast.ClassDef) -> set[str]:
    return {
        node.name
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


@pytest.mark.parametrize(
    "path", list(_serializer_modules()), ids=lambda p: str(p.relative_to(ROOT))
)
def test_every_method_field_has_its_method(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    missing: list[str] = []

    # Only classes defined at module level; a serializer nested in a function
    # is a test fixture, not shipped code.
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        have = _methods(cls)
        for field in sorted(_declared_method_fields(cls)):
            if f"get_{field}" not in have:
                missing.append(f"{cls.name}.{field} → get_{field}() not defined")

    assert not missing, (
        f"{path.relative_to(ROOT)}: SerializerMethodField without its method — "
        "this is a 500 at render time, not an import error:\n  "
        + "\n  ".join(missing)
    )
