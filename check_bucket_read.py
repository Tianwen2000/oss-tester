"""Compatibility helper: run the unified data-plane suite with a fresh prefix."""
from oss_test import main

if __name__ == "__main__":
    raise SystemExit(main(["--suites", "network,authentication,data", "--cleanup", "never", "--prefix", "oss-read-check"]))
