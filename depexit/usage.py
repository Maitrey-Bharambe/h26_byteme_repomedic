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
    function: str | None = None   # enclosing def as a __qualname__, e.g. "Cache.path" / "f.<locals>.g"

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
        self.scope: list[tuple[str, str]] = []   # (kind, name) of enclosing class/def

    def qualname(self) -> str | None:
        """__qualname__ of the innermost enclosing def; None at module or class-body level."""
        if not self.scope or self.scope[-1][0] != "def":
            return None
        parts = []
        for i, (kind, name) in enumerate(self.scope):
            parts.append(name)
            if kind == "def" and i < len(self.scope) - 1:
                parts.append("<locals>")
        return ".".join(parts)

    def add(self, node, symbol: str, kind: str, alias: str | None = None):
        line = getattr(node, "lineno", 0)
        code = self.lines[line - 1].strip() if 0 < line <= len(self.lines) else ""
        self.sites.append(UsageSite(symbol.split(".")[0], symbol, kind, self.rel, line, code,
                                    _is_private(symbol), alias, self.qualname()))

    def _visit_def(self, node):
        for d in node.decorator_list:
            self.visit(d)
        self.scope.append(("def", node.name))
        for s in node.body:
            self.visit(s)
        self.scope.pop()

    visit_FunctionDef = _visit_def
    visit_AsyncFunctionDef = _visit_def

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
        self.scope.append(("class", node.name))
        for s in node.body:
            self.visit(s)
        self.scope.pop()


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


# --------------------------------------------------------------------------- replay targets

# Entry points that open connections. Pure helpers of the same libraries (e.g.
# requests.utils.*, requests.exceptions.*) are not network I/O.
NETWORK_PREFIXES = (
    "requests.get", "requests.post", "requests.put", "requests.patch", "requests.delete",
    "requests.head", "requests.options", "requests.request", "requests.Session", "requests.session",
    "requests.api", "requests.sessions", "urllib.request", "urllib3", "socket", "http.client",
    "httpx", "aiohttp", "ftplib", "smtplib", "websocket", "websockets", "grpc", "boto3",
    "botocore", "paramiko",
)


def _is_network(symbol: str) -> bool:
    return any(symbol == p or symbol.startswith(p + ".") for p in NETWORK_PREFIXES)


def module_name(rel: str) -> str:
    mod = rel[:-3] if rel.endswith(".py") else rel
    if mod.startswith("src/"):
        mod = mod[4:]
    mod = mod.replace("/", ".")
    return mod[:-9] if mod.endswith(".__init__") else mod


def _is_test_file(rel: str) -> bool:
    name = rel.split("/")[-1]
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"         or "/tests/" in "/" + rel or "/test/" in "/" + rel


def _find_def(tree, qualname: str):
    node, parts = tree, qualname.split(".")
    for part in parts:
        node = next((n for n in getattr(node, "body", [])
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                     and n.name == part), None)
        if node is None:
            return None
    return node


def _local_calls(node, rel: str, aliases: dict[str, str], imports_rel: dict[str, str]) -> set[str]:
    """Fully qualified repo-local functions called inside `node`."""
    mod = module_name(rel)
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            name = n.func.id
            out.add(imports_rel.get(name) or aliases.get(name) or f"{mod}.{name}")
    return out


def network_functions(root: Path) -> dict[str, str]:
    """Repo functions that (transitively, through repo-local calls) perform network I/O."""
    defs: dict[str, tuple] = {}
    for path in python_files(root):
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text)
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        v = _Visitor(rel, text.splitlines())
        v.visit(tree)
        pkg = module_name(rel).rsplit(".", 1)[0] if "." in module_name(rel) else ""
        imports_rel = {}
        for n in tree.body:
            if isinstance(n, ast.ImportFrom) and n.level:
                base = pkg.rsplit(".", n.level - 1)[0] if n.level > 1 else pkg
                for a in n.names:
                    imports_rel[a.asname or a.name] = ".".join(x for x in (base, n.module, a.name) if x)
        for n in tree.body:
            nodes = [n] + ([m for m in n.body] if isinstance(n, ast.ClassDef) else [])
            for d in nodes:
                if isinstance(d, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qual = d.name if d is n else f"{n.name}.{d.name}"
                    net = sorted({q for x in ast.walk(d) if isinstance(x, (ast.Name, ast.Attribute))
                                  for q in [v.resolve(x) if isinstance(x, ast.Attribute) else v.aliases.get(x.id)]
                                  if q and _is_network(q)})
                    defs[f"{module_name(rel)}.{qual}"] = (net, _local_calls(d, rel, v.aliases, imports_rel))
    reason = {f: f"performs network I/O via {', '.join(n)}" for f, (n, _) in defs.items() if n}
    changed = True
    while changed:  # propagate through repo-local calls
        changed = False
        for f, (_, calls) in defs.items():
            if f not in reason:
                hit = next((c for c in calls if c in reason), None)
                if hit:
                    reason[f] = f"calls {hit}, which {reason[hit].split(' (')[0]}"
                    changed = True
    return reason


def replay_targets(root: Path, sites: list[dict]) -> list[dict]:
    """The repository's own functions that contain usage of a dependency.

    Their names and signatures survive a migration, so the same recorded inputs can be
    replayed against the migrated code. Each target says whether it is safe to replay.
    """
    by_fn: dict[tuple[str, str], list[int]] = {}
    for s in sites:
        f, fn = s.get("file", ""), s.get("function")
        if s.get("kind") == "tooling" or not fn or not f.endswith(".py") or _is_test_file(f):
            continue
        by_fn.setdefault((f, fn), []).append(s["line"])
    out = []
    network = network_functions(root) if by_fn else {}
    for (rel, qual), lines in sorted(by_fn.items()):
        t = {"module": module_name(rel), "qualname": qual, "file": rel, "lines": sorted(set(lines)),
             "function": f"{module_name(rel)}.{qual}", "replayable": True, "skip": None}
        try:
            text = (root / rel).read_text(encoding="utf-8")
            tree = ast.parse(text)
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            t.update(replayable=False, skip=f"unparseable: {exc}")
            out.append(t)
            continue
        node = None if "<locals>" in qual else _find_def(tree, qual)
        if node is None:
            t.update(replayable=False, skip="nested or dynamically defined function")
        elif isinstance(node, ast.AsyncFunctionDef):
            t.update(replayable=False, skip="async function")
        elif any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(node)):
            t.update(replayable=False, skip="generator function")
        elif node.decorator_list:
            t.update(replayable=False, skip="decorated function (module attribute is not the plain function)")
        elif t["function"] in network:
            t.update(replayable=False, skip=f"side_effect_skipped: {network[t['function']]}")
        out.append(t)
    return out
