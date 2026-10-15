"""pytest plugin (loaded with -p depexit_warnings) that records every warning as JSON."""
import json
import os

_records = []


def pytest_warning_recorded(warning_message, when, nodeid, location):
    _records.append({
        "category": warning_message.category.__name__,
        "message": str(warning_message.message)[:500],
        "filename": warning_message.filename,
        "lineno": warning_message.lineno,
        "nodeid": nodeid,
        "when": when,
    })


def pytest_sessionfinish(session, exitstatus):
    out = os.environ.get("DEPEXIT_WARNINGS_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(_records, fh, indent=1)
