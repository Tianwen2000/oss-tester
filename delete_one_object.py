"""Guarded one-object compatibility helper; no fixed key is used."""
import argparse
from oss_cli import main

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", required=True)
    args = parser.parse_args()
    if not args.key.startswith("oss-test:"):
        parser.error("--key must be under a current oss-test:<run-id>: prefix")
    raise SystemExit(main(["delete", "--key", args.key, "--confirm-risk"]))
