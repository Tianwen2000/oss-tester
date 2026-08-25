"""Read-only bucket listing compatibility entry point."""
from oss_cli import main

if __name__ == "__main__":
    raise SystemExit(main(["buckets"]))
