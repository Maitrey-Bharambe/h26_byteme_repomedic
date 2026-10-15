"""Runs INSIDE the target venv. Prints installed distributions, import names, scripts."""
import importlib.metadata as md
import json
import re
import sys


def canon(name):
    return re.sub(r"[-_.]+", "-", name).lower()


modules = {}
for mod, dists in md.packages_distributions().items():
    if mod.startswith("_") and mod != "_yaml":
        continue
    modules[mod] = sorted({canon(d) for d in dists})
versions, scripts, requires = {}, {}, {}
for d in md.distributions():
    name = canon(d.metadata["Name"] or "")
    if not name:
        continue
    versions[name] = d.version
    eps = [e.name for e in d.entry_points if e.group == "console_scripts"]
    if eps:
        scripts[name] = sorted(eps)
    requires[name] = d.requires or []
print(json.dumps({"python": sys.version.split()[0], "stdlib": sorted(sys.stdlib_module_names),
                  "modules": modules, "versions": versions, "scripts": scripts,
                  "requires": requires}))
