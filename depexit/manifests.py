"""Read every dependency declaration in a repo and edit them for a migration.

Everything here is *observed*: it comes straight from files in the repository.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

REQ_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\s*(\[[^\]]*\])?\s*(==|>=|<=|~=|!=|>|<|===)")


@dataclass
class Declared:
    name: str            # canonical distribution name
    raw: str             # requirement string as written
    specifier: str
    file: str            # path relative to repo root
    line: int | None
    group: str           # runtime | dev | build | optional:<extra>
    commented: bool = False

    def to_dict(self):
        return asdict(self)


def _group_for_requirements_file(name: str) -> str:
    return "dev" if re.search(r"dev|test|lint|doc", name, re.I) else "runtime"


def requirement_files(root: Path) -> list[Path]:
    files = sorted(set(root.glob("requirements*.txt")) | set(root.glob("requirements/*.txt")))
    return [f for f in files if f.is_file()]


def parse_requirements_file(path: Path, root: Path) -> list[Declared]:
    out: list[Declared] = []
    rel = path.relative_to(root).as_posix()
    group = _group_for_requirements_file(path.name)
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        s = line.strip()
        if not s:
            continue
        commented = False
        if s.startswith("#"):
            body = s.lstrip("#").strip()
            if not REQ_NAME_RE.match(body):
                continue
            s, commented = body, True
        if s.startswith("-"):
            continue
        s = re.split(r"\s+#", s, maxsplit=1)[0].strip()
        try:
            req = Requirement(s)
        except InvalidRequirement:
            continue
        out.append(Declared(canonicalize_name(req.name), s, str(req.specifier), rel, i, group,
                            commented))
    return out


def _find_line(text: str, needle: str) -> int | None:
    for i, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return i
    return None


def parse_pyproject(root: Path) -> list[Declared]:
    path = root / "pyproject.toml"
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    out: list[Declared] = []

    def add(items, group):
        for raw in items or []:
            try:
                req = Requirement(raw)
            except InvalidRequirement:
                continue
            out.append(Declared(canonicalize_name(req.name), raw, str(req.specifier),
                                "pyproject.toml", _find_line(text, f'"{raw}"'), group))

    project = data.get("project", {})
    add(project.get("dependencies"), "runtime")
    for extra, items in (project.get("optional-dependencies") or {}).items():
        add(items, f"optional:{extra}")
    for grp, items in (data.get("dependency-groups") or {}).items():
        add([i for i in items if isinstance(i, str)], "dev")
    add(data.get("build-system", {}).get("requires"), "build")
    return out


def collect(root: Path) -> list[Declared]:
    decls: list[Declared] = []
    for f in requirement_files(root):
        decls.extend(parse_requirements_file(f, root))
    decls.extend(parse_pyproject(root))
    return decls


def pinned_version(specifier: str) -> str | None:
    m = re.fullmatch(r"===?([^,\s]+)", specifier.strip())
    return m.group(1) if m else None


def find_drift(decls: list[Declared]) -> dict[str, dict]:
    """Same distribution declared differently in different files (runtime groups only)."""
    by_name: dict[str, list[Declared]] = {}
    for d in decls:
        if d.commented or d.group == "build":
            continue
        by_name.setdefault(d.name, []).append(d)
    drift = {}
    for name, items in by_name.items():
        specs = {d.specifier for d in items}
        if len(items) < 2 or len(specs) < 2:
            continue
        conflict = False
        for a in items:
            pin = pinned_version(a.specifier)
            if not pin:
                continue
            for b in items:
                if b is a or not b.specifier:
                    continue
                try:
                    if not SpecifierSet(b.specifier).contains(Version(pin), prereleases=True):
                        conflict = True
                except InvalidVersion:
                    pass
        drift[name] = {"conflict": conflict,
                       "declarations": [{"file": d.file, "line": d.line, "spec": d.raw} for d in items]}
    return drift


# --------------------------------------------------------------------------- editing

def _edit_requirements_file(path: Path, name: str, new_req: str | None, add_if_missing: bool) -> bool:
    lines = path.read_text(encoding="utf-8").splitlines()
    changed, found = False, False
    out = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith(("#", "-")):
            try:
                req = Requirement(re.split(r"\s+#", s, maxsplit=1)[0].strip())
                if canonicalize_name(req.name) == name:
                    found = True
                    changed = True
                    if new_req:
                        out.append(new_req)
                    continue
            except InvalidRequirement:
                pass
        out.append(line)
    if not found and add_if_missing and new_req:
        out.append(new_req)
        changed = True
    if changed:
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return changed


def _edit_pyproject(path: Path, name: str, new_req: str | None, add_if_missing: bool) -> bool:
    text = path.read_text(encoding="utf-8")
    m = re.search(r"(?ms)^(dependencies\s*=\s*\[)(.*?)(^\s*\])", text)
    if not m:
        m = re.search(r"(?ms)^(dependencies\s*=\s*\[)(.*?)(\])", text)
        if not m:
            return False
    items = re.findall(r"""["']([^"']+)["']""", m.group(2))
    new_items, found = [], False
    for raw in items:
        try:
            same = canonicalize_name(Requirement(raw).name) == name
        except InvalidRequirement:
            same = False
        if same:
            found = True
            if new_req:
                new_items.append(new_req)
        else:
            new_items.append(raw)
    if not found and not (add_if_missing and new_req):
        return False
    if not found:
        new_items.append(new_req)
    body = "\n" + "".join(f'    "{i}",\n' for i in new_items)
    text = text[:m.start(2)] + body + text[m.start(3):]
    path.write_text(text, encoding="utf-8")
    return True


def apply_dependency_change(root: Path, name: str, new_req: str | None,
                            declared_in: list[Declared], group: str = "runtime") -> list[str]:
    """Replace/remove `name` in every manifest that declares it, or add `new_req`.

    new_req=None removes the dependency. Returns the list of edited files.
    """
    edited: list[str] = []
    files = sorted({d.file for d in declared_in if not d.commented})
    add_targets: list[str] = []
    if not files and new_req:
        # declare a new dependency in the main runtime manifests
        for f in requirement_files(root):
            if _group_for_requirements_file(f.name) == ("dev" if group == "dev" else "runtime"):
                add_targets.append(f.relative_to(root).as_posix())
        if (root / "pyproject.toml").exists() and group != "dev":
            add_targets.append("pyproject.toml")
    for rel in files + add_targets:
        path = root / rel
        add = rel in add_targets
        if rel == "pyproject.toml":
            ok = _edit_pyproject(path, name, new_req, add)
        else:
            ok = _edit_requirements_file(path, name, new_req, add)
        if ok:
            edited.append(rel)
    return edited
