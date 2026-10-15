"""Static usage map: where and how each third-party module is used.

Resolves aliases (`from requests import Session as S`), attribute chains,
monkeypatches (`requests.Session.request = ...`), subclassing and private APIs.
Also finds tool usage (console scripts, `python -m x`) in Makefile/Dockerfile/CI files.
"""
from __future__ import annotations

import ast
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .util import SKIP_DIRS

TOOL_FILES = ["Makefile", "makefile", "GNUmakefile", "Dockerfile", "tox.ini", "noxfile.py",
              "setup.cfg", "Justfile", "justfile", ".pre-commit-config.yaml"]


@dataclass
class UsageSite:
    module: str        # top-level import name, e.g. "requests"
    symbol: str        # fully qualified symbol, e.g. "requests.Session.get"
    kind: str          # import | call | attribute | reference | subclass | monkeypatch | tooling
    file: str
    line: int
    code: str
    private: bool = False
    alias: str | None = None

    def to_dict(self):
        return asdict(self)


def python_files(root: Path) -> list[Path]:
    out = []
    for p in root.rglob("*.py"):
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        out.append(p)
    return sorted(out)


def local_top_level_names(root: Path) -> set[str]:
    names = set()
    for p in root.iterdir():
        if p.name in SKIP_DIRS:
            continue
        if p.is_dir() and ((p / "__init__.py").exists() or p.name in {"tests", "test"}):
            names.add(p.name)
        elif p.suffix == ".py":
            names.add(p.stem)
    src = root / "src"
    if src.is_dir():
        names |= {p.name for p in src.iterdir() if p.is_dir()}
    names |= {"conftest"}
    return names


def _is_private(symbol: str) -> bool:
    parts = symbol.split(".")[1:]
    return any(p.startswith("_") and not (p.startswith("__") and p.endswith("__")) for p in parts)


class _Visitor(ast.NodeVisitor):
    def __init__(self, rel: str, lines: list[str]):
        self.rel = rel
        self.lines = lines
        self.aliases: dict[str, str] = {}
        self.alias_names: dict[str, str] = {}
        self.sites: list[UsageSite] = []

    def add(self, node, symbol: str, kind: str, alias: str | None = None):
        line = getattr(node, "lineno", 0)
        code = self.lines[line - 1].strip() if 0 < line <= len(self.lines) else ""
        self.sites.append(UsageSite(symbol.split(".")[0], symbol, kind, self.rel, line, code,
                                    _is_private(symbol), alias))

    # imports -------------------------------------------------------------
    def visit_Import(self, node):
        for a in node.names:
            if a.asname:
                self.aliases[a.asname] = a.name
                alias = a.asname
            else:
                top = a.name.split(".")[0]
                self.aliases[top] = top
                alias = None
            self.add(node, a.name, "import", alias)

    def visit_ImportFrom(self, node):
        if node.level or not node.module:
            return
        for a in node.names:
            if a.name == "*":
                self.add(node, f"{node.module}.*", "import")
                continue
            q = f"{node.module}.{a.name}"
            self.aliases[a.asname or a.name] = q
            self.add(node, q, "import", a.asname if a.asname and a.asname != a.name else None)

    # resolution ----------------------------------------------------------
    def resolve(self, node) -> str | None:
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name) and node.id in self.aliases:
            return ".".join([self.aliases[node.id], *reversed(parts)])
        return None

    def visit_Attribute(self, node):
        q = self.resolve(node)
        if q:
            self.add(node, q, "attribute")
            return
        self.generic_visit(node)

    def visit_Name(self, node):
        if node.id in self.aliases and isinstance(node.ctx, ast.Load):
            self.add(node, self.aliases[node.id], "reference")

    def visit_Call(self, node):
        q = self.resolve(node.func)
        if q:
            self.add(node, q, "call")
        else:
            self.visit(node.func)
        for a in node.args:
            self.visit(a)
        for k in node.keywords:
            self.visit(k.value)

    def visit_Assign(self, node):
        for t in node.targets:
            q = self.resolve(t) if isinstance(t, ast.Attribute) else None
            if q:
                self.add(t, q, "monkeypatch")
        self.visit(node.value)

    def visit_ClassDef(self, node):
        for b in node.bases:
            q = self.resolve(b)
            if q:
                self.add(b, q, "subclass")
            else:
                self.visit(b)
        for k in node.keywords:
            self.visit(k.value)
        for d in node.decorator_list:
            self.visit(d)
        for s in node.body:
            self.visit(s)


def scan_python(root: Path) -> tuple[list[UsageSite], list[dict]]:
    """Return usage sites for all non-stdlib, non-local modules, plus parse errors."""
    local = local_top_level_names(root)
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    sites: list[UsageSite] = []
    errors = []
    for path in python_files(root):
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=rel)
        except (SyntaxError, UnicodeDecodeError) as exc:
            errors.append({"file": rel, "error": str(exc)})
            continue
        v = _Visitor(rel, text.splitlines())
        v.visit(tree)
        for s in v.sites:
            if s.module in stdlib or s.module in local:
                continue
            sites.append(s)
    return sites, errors


def scan_tooling(root: Path, scripts_by_dist: dict[str, list[str]],
                 modules_by_dist: dict[str, list[str]]) -> list[tuple[str, UsageSite]]:
    """Find console-script / `python -m` usage of distributions in build & CI files."""
    files = [root / f for f in TOOL_FILES if (root / f).is_file()]
    wf = root / ".github" / "workflows"
    if wf.is_dir():
        files += sorted(wf.glob("*.y*ml"))
    hits: list[tuple[str, UsageSite]] = []
    for f in files:
        rel = f.relative_to(root).as_posix()
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        for dist in set(scripts_by_dist) | set(modules_by_dist):
            pats = [re.escape(s) for s in scripts_by_dist.get(dist, [])]
            pats += [r"-m\s+" + re.escape(m) for m in modules_by_dist.get(dist, [])]
            if not pats:
                continue
            rx = re.compile(r"(?<![\w.-])(" + "|".join(pats) + r")(?![\w-])")
            for i, line in enumerate(lines, 1):
                m = rx.search(line)
                if m and not line.lstrip().startswith("#"):
                    hits.append((dist, UsageSite(dist, m.group(1), "tooling", rel, i, line.strip())))
    return hits
