#!/usr/bin/env python3
"""Run live GLM prompt-behaviour checks for watch_mention/watch_regex."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from prompt_behavior_glm import DEFAULT_CASES_FILE, run_cases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases-file",
        type=Path,
        default=DEFAULT_CASES_FILE,
        help="Prompt behaviour case fixture JSON.",
    )
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="Run only a specific case id. Can be repeated.",
    )
    parser.add_argument("--model", default="", help="Override model name.")
    parser.add_argument("--provider", default="", help="Override provider name.")
    parser.add_argument("--api-mode", default="", help="Override API transport mode.")
    parser.add_argument("--json", action="store_true", help="Emit JSON lines only.")
    parser.add_argument("--quiet", action="store_true", help="Suppress passing final text.")
    args = parser.parse_args(argv)

    runtime, results = run_cases(
        cases_file=args.cases_file,
        case_ids=set(args.case) or None,
        model_override=args.model,
        provider_override=args.provider,
        api_mode_override=args.api_mode,
    )
    ok = all(result.passed for result in results)
    if args.json:
        print(json.dumps({"runtime": runtime}, ensure_ascii=False), flush=True)
        for result in results:
            print(json.dumps(result.as_dict(), ensure_ascii=False), flush=True)
        return 0 if ok else 1

    print(f"[prompt-behavior] runtime={json.dumps(runtime, ensure_ascii=False)}")
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        detail = result.as_dict()
        if args.quiet and result.passed:
            detail["final"] = ""
        print(f"[prompt-behavior] {status} {json.dumps(detail, ensure_ascii=False)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
