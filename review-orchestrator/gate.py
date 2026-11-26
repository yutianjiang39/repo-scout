"""Gate controller CLI (manual operations). Normal flow: orchestrate.py.

  python gate.py --db <task>/review.sqlite <command> [args...]
"""

import argparse
import json
from pathlib import Path
import sqlite3
import subprocess

from ro import config
from ro.gates import COMMANDS, execute
from ro.util import Refused

MODELS = {r: config.model(r) for r in config.roles()}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("command", choices=COMMANDS)
    p.add_argument("args", nargs=argparse.REMAINDER)
    a = p.parse_args()
    a.db.parent.mkdir(parents=True, exist_ok=True)
    try:
        print(json.dumps(execute(a.db, a.command, *a.args), ensure_ascii=False, indent=2))
    except (Refused, ValueError, KeyError, IndexError, OSError, sqlite3.Error, subprocess.CalledProcessError) as e:
        p.exit(1, f"Gate refused: {e}\n")


if __name__ == "__main__":
    main()
