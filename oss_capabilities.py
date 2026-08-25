#!/usr/bin/env python3
"""Compatibility wrapper for the former capability demonstration script.

The old implementation silently swallowed failures and deleted its own bucket.
It now delegates to the single safe runner. ``--category bucket`` maps to the
guarded control-plane suite; no bucket is created or deleted automatically.
"""

from __future__ import annotations

import argparse

from oss_test import main as runner_main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OSS capability compatibility wrapper; use oss_test.py")
    parser.add_argument("--category", choices=["all", "bucket", "object", "multipart"], default="all")
    parser.add_argument("--cleanup", action="store_true", help="clean only this run prefix after delegated tests")
    parser.add_argument("--skip-create-bucket", action="store_true", help="retained for compatibility; buckets are never created")
    parser.add_argument("--endpoint"); parser.add_argument("--region"); parser.add_argument("--bucket")
    parser.add_argument("--confirm-control-plane", action="store_true")
    parser.add_argument("--confirm-bucket", action="store_true")
    parser.add_argument("--confirm-risk", action="store_true")
    args, remainder = parser.parse_known_args(argv)
    if args.category == "bucket": suites = "network,authentication,control-plane"
    elif args.category == "object": suites = "network,authentication,data"
    elif args.category == "multipart": suites = "network,authentication,multipart"
    else: suites = "network,authentication,data,multipart"
    delegated = ["--suites", suites]
    for option, value in (("--endpoint", args.endpoint), ("--region", args.region), ("--bucket", args.bucket)):
        if value: delegated.extend([option, value])
    if args.cleanup: delegated.extend(["--cleanup", "always"])
    if args.confirm_control_plane: delegated.append("--confirm-control-plane")
    if args.confirm_bucket: delegated.append("--confirm-bucket")
    if args.confirm_risk: delegated.append("--confirm-risk")
    delegated.extend(remainder)
    return runner_main(delegated)


if __name__ == "__main__": raise SystemExit(main())
