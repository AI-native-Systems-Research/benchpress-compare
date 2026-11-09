"""Execute one immutable observation/preparation specification."""

import argparse
import json
from pathlib import Path

from . import run_spec


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    args = parser.parse_args()
    receipt = run_spec(json.loads(args.spec.read_text()))
    return 0 if receipt["status"] == "SUCCEEDED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
