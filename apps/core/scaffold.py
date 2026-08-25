"""
Shared helpers for the code-generation management commands
(`newapp` and `setup_model`).

The generators work by inserting snippets at named ANCHOR comments in each
target file. Anchors make generation idempotent and let you keep hand-written
code in the same files without the generator clobbering it.
"""
from pathlib import Path

# Anchor markers.
LOCAL_APPS_ANCHOR = "# <scaffold:local-apps>"
SERIALIZERS_ANCHOR = "# <scaffold:serializers>"
VIEWSETS_ANCHOR = "# <scaffold:viewsets>"
ROUTES_ANCHOR = "# <scaffold:routes>"
ADMIN_ANCHOR = "# <scaffold:admin>"

# File headers written when a target file is missing (or lacks its anchor).
SERIALIZERS_HEADER = (
    "from rest_framework import serializers\n"
    "from . import models\n\n"
    f"{SERIALIZERS_ANCHOR}\n"
)
VIEWS_HEADER = (
    "from rest_framework import permissions, viewsets\n"
    "from . import models, serializers\n\n"
    f"{VIEWSETS_ANCHOR}\n"
)
URLS_HEADER = (
    "from rest_framework.routers import DefaultRouter\n"
    "from . import views\n\n"
    "router = DefaultRouter()\n"
    f"{ROUTES_ANCHOR}\n"
    "urlpatterns = router.urls\n"
)
ADMIN_HEADER = (
    "from django.contrib import admin\n"
    "from . import models\n\n"
    f"{ADMIN_ANCHOR}\n"
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def ensure_anchor(path: Path, header: str) -> str:
    """Guarantee the file exists and contains its anchor. Returns the content."""
    content = read(path)
    anchor = header.strip().splitlines()[-1]
    if not content:
        write(path, header)
        return header
    if anchor not in content:
        content = content.rstrip("\n") + "\n\n" + header
        write(path, content)
    return content


def insert_before_anchor(content: str, anchor: str, snippet_lines: list[str]) -> str:
    """Insert snippet_lines just before the anchor line (matching its indent)."""
    out: list[str] = []
    inserted = False
    for line in content.splitlines():
        if not inserted and anchor in line:
            indent = line[: len(line) - len(line.lstrip())]
            for s in snippet_lines:
                out.append(f"{indent}{s}" if s.strip() else "")
            inserted = True
        out.append(line)
    if not inserted:  # anchor vanished somehow; append at end
        out.extend(snippet_lines)
    return _collapse_blanks("\n".join(out) + "\n")


def _collapse_blanks(text: str) -> str:
    """Collapse runs of 3+ blank lines down to 2 (keeps generated code PEP8-ish)."""
    lines = text.splitlines()
    result: list[str] = []
    blanks = 0
    for line in lines:
        if line.strip() == "":
            blanks += 1
            if blanks <= 2:
                result.append("")
        else:
            blanks = 0
            result.append(line)
    return "\n".join(result) + "\n"
