"""Dependency metadata from public sources (PyPI JSON API, GitHub repo API).

Observed evidence: every fact records the URL it came from.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from .util import HttpCache

PYPI = "https://pypi.org/pypi"


def _parse_time(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _release_time(files: list[dict]):
    times = [_parse_time(f.get("upload_time_iso_8601")) for f in files or []]
    times = [t for t in times if t]
    return min(times) if times else None


def _stable_versions(releases: dict) -> list[Version]:
    out = []
    for v, files in releases.items():
        try:
            pv = Version(v)
        except InvalidVersion:
            continue
        if pv.is_prerelease or not files or all(f.get("yanked") for f in files):
            continue
        out.append(pv)
    return sorted(out)


def _python_ok(requires_python: str | None, python: str) -> bool:
    if not requires_python:
        return True
    try:
        return SpecifierSet(requires_python).contains(Version(python))
    except (InvalidSpecifier, InvalidVersion):
        return True


def latest_compatible(releases: dict, pythons: list[str]) -> str | None:
    for v in reversed(_stable_versions(releases)):
        files = releases.get(str(v)) or []
        req = next((f.get("requires_python") for f in files if f.get("requires_python")), None)
        if all(_python_ok(req, p) for p in pythons):
            return str(v)
    return None


def pypi_info(http: HttpCache, name: str, version: str | None, pythons: list[str]) -> dict:
    url = f"{PYPI}/{name}/json"
    status, body = http.get_json(url)
    if status != 200 or not body:
        return {"found": False, "source": url, "status": status}
    info = body.get("info", {})
    releases = body.get("releases", {})
    stable = _stable_versions(releases)
    latest = str(stable[-1]) if stable else info.get("version")
    now = datetime.now(timezone.utc)
    latest_time = _release_time(releases.get(latest, []))
    out = {
        "found": True,
        "source": url,
        "summary": info.get("summary"),
        "latest_version": latest,
        "latest_compatible": latest_compatible(releases, pythons),
        "latest_release_date": latest_time.date().isoformat() if latest_time else None,
        "days_since_latest_release": (now - latest_time).days if latest_time else None,
        "release_count_last_2y": sum(
            1 for v in stable
            if (t := _release_time(releases.get(str(v), []))) and (now - t).days <= 730),
        "classifiers_inactive": [c for c in info.get("classifiers", [])
                                 if "Inactive" in c or "7 - " in c],
        "requires_python_latest": info.get("requires_python"),
        "project_urls": info.get("project_urls") or {},
        "home_page": info.get("home_page"),
    }
    if version:
        vurl = f"{PYPI}/{name}/{version}/json"
        vstatus, vbody = http.get_json(vurl)
        if vstatus == 200 and vbody:
            vinfo = vbody.get("info", {})
            vt = _release_time(vbody.get("urls", []))
            out["installed"] = {
                "version": version,
                "source": vurl,
                "release_date": vt.date().isoformat() if vt else None,
                "yanked": bool(vinfo.get("yanked")),
                "requires_python": vinfo.get("requires_python"),
                "vulnerabilities": [
                    {"id": v.get("id"), "aliases": v.get("aliases", []),
                     "fixed_in": v.get("fixed_in", []),
                     "summary": (v.get("summary") or v.get("details") or "")[:300]}
                    for v in vbody.get("vulnerabilities", []) if not v.get("withdrawn")
                ],
            }
            try:
                out["installed"]["major_behind"] = Version(latest).major - Version(version).major
            except InvalidVersion:
                out["installed"]["major_behind"] = None
    return out


GH_RE = re.compile(r"github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/|$)")


def github_repo(info: dict) -> str | None:
    urls = list((info.get("project_urls") or {}).values()) + [info.get("home_page") or ""]
    for u in urls:
        m = GH_RE.search(u or "")
        if m:
            return f"{m.group(1)}/{m.group(2)}"
    return None


def github_info(http: HttpCache, repo: str) -> dict:
    url = f"https://api.github.com/repos/{repo}"
    status, body = http.get_json(url, headers={"Accept": "application/vnd.github+json"})
    if status != 200 or not isinstance(body, dict) or "archived" not in body:
        return {"repo": repo, "source": url, "available": False, "status": status}
    pushed = _parse_time(body.get("pushed_at"))
    return {"repo": repo, "source": url, "available": True, "archived": body.get("archived"),
            "pushed_at": body.get("pushed_at"),
            "days_since_push": (datetime.now(timezone.utc) - pushed).days if pushed else None,
            "open_issues": body.get("open_issues_count")}


def package_exists(http: HttpCache, name: str, specifier: str | None, pythons: list[str]) -> dict:
    """Hallucination guard for AI proposals: does the package/version really exist?"""
    url = f"{PYPI}/{name}/json"
    status, body = http.get_json(url)
    if status != 200 or not body:
        return {"exists": False, "source": url}
    releases = body.get("releases", {})
    matching = []
    try:
        spec = SpecifierSet(specifier or "")
    except InvalidSpecifier:
        return {"exists": True, "source": url, "specifier_valid": False}
    for v in _stable_versions(releases):
        if spec.contains(v):
            files = releases.get(str(v)) or []
            req = next((f.get("requires_python") for f in files if f.get("requires_python")), None)
            if all(_python_ok(req, p) for p in pythons):
                matching.append(str(v))
    return {"exists": True, "source": url, "specifier_valid": True,
            "satisfying_versions": len(matching), "best": matching[-1] if matching else None}
