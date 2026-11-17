"""Build gate: 0 passes, 1 law violations, 2 setup or incomplete checking."""

import argparse
import json
import math
from pathlib import Path
import sys

from .checker import Checker
from .contracts import discover_contracts, load_contract
from .errors import LawbridgeError
from .runner import new_report, read_replay, run_contract


def parser():
    result = argparse.ArgumentParser(prog="lawbridge", description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="check actual Python executions against Lean laws")
    check.add_argument("paths", nargs="*", help="contract files or directories")
    check.add_argument("--report", type=Path, help="write complete JSON evidence")
    check.add_argument("--timeout", type=float, default=10.0, help="per Python/Lean response deadline, seconds")
    check.add_argument("--replay", type=Path, help="replay the first saved law failure from a report")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    report = new_report()
    exit_code = 2
    try:
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise LawbridgeError("--timeout must be a positive finite number")
        if args.replay:
            if args.paths:
                raise LawbridgeError("--replay cannot be combined with contract paths")
            contract, replay = read_replay(args.replay)
            work = [(contract, replay)]
        else:
            if not args.paths:
                raise LawbridgeError("provide contract paths or --replay")
            work = [(load_contract(path), None) for path in discover_contracts(args.paths)]
        with Checker(timeout=args.timeout) as checker:
            report["checker"] = checker.metadata()
            for contract, replay in work:
                try:
                    item = run_contract(contract, checker, args.timeout, replay=replay)
                except (LawbridgeError, OSError, ValueError) as exc:
                    report["contracts"].append({"name": contract.name, "contract": str(contract.path),
                                                "status": "error", "error": str(exc),
                                                "implementation_proved": False})
                    raise
                report["contracts"].append(item)
                print(f"{item['status'].upper()} {item['name']}: {item['cases_checked']} checks "
                      f"({item['eligible_cases']} eligible, {item['ineligible_cases']} rejection cases); {item['evidence']}")
                for failure in item["failures"]:
                    print(f"  {failure['id']}: {'; '.join(failure['failures'])}")
            report["status"] = "failed" if any(x["status"] == "failed" for x in report["contracts"]) else "passed"
            exit_code = 1 if report["status"] == "failed" else 0
    except (LawbridgeError, OSError, ValueError) as exc:
        report["status"] = "error"
        report["errors"].append(str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        exit_code = 2
    finally:
        report["cases_checked"] = sum(item.get("cases_checked", 0) for item in report["contracts"])
        if args.report:
            try:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
            except (OSError, ValueError, UnicodeError) as exc:
                print(f"ERROR: cannot write report: {exc}", file=sys.stderr)
                exit_code = 2
    return exit_code
