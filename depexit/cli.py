"""Command line: `depexit analyze <repo-or-url>`."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .llm import LLMConfig
from .pipeline import Pipeline


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="depexit", description="Dependency Exit Capsules, verified by execution, assisted by Gemma 4.")
    ap.add_argument("--version", action="version", version=f"depexit {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze", help="analyse a repository and produce Exit Capsules")
    a.add_argument("target", help="path to a local repo or a git URL")
    a.add_argument("-o", "--out", default="depexit-out", help="output directory (default: depexit-out)")
    a.add_argument("-p", "--python", action="append", dest="pythons",
                   help="Python version to verify on; repeatable (default: 3.12 and 3.13)")
    a.add_argument("--only", action="append", help="only process this dependency; repeatable")
    a.add_argument("--no-llm", action="store_true", help="run without Gemma (rule-based proposals only)")
    a.add_argument("--model", help="model name (default $DEPEXIT_LLM_MODEL or gemma4:26b)")
    a.add_argument("--base-url", help="OpenAI-compatible base URL (default $DEPEXIT_LLM_BASE_URL or Ollama)")
    a.add_argument("--jobs", type=int, default=4, help="dependencies processed in parallel")
    a.add_argument("--max-fix-attempts", type=int, default=2, help="Gemma fix-loop iterations after a failed verification")
    a.add_argument("--include-low", action="store_true", help="also build capsules for low/info-risk dependencies")
    args = ap.parse_args(argv)

    if args.cmd == "analyze":
        cfg = None
        if not args.no_llm:
            cfg = LLMConfig()
            if args.model:
                cfg.model = args.model
            if args.base_url:
                cfg.base_url = args.base_url
        pythons = args.pythons or ["3.12", "3.13"]
        out = Pipeline(args.target, Path(args.out), pythons, cfg, only=args.only, jobs=args.jobs,
                       max_fix_attempts=args.max_fix_attempts, include_low=args.include_low).execute()
        print(out / "REPORT.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
