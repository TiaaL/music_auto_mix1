#!/usr/bin/env python3
"""Print vocal/accomp bus gain in dB from a resolved mix plan.

Output format: `<vocal_db> <accomp_db>` on stdout, single line.
Returns 0.0 0.0 if no reference overrides are present.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 2:
        print("0.0 0.0")
        return
    path = Path(sys.argv[1])
    try:
        plan = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        print("0.0 0.0")
        return
    bus = (plan.get("reference") or {}).get("overrides", {}).get("bus_balance", {})
    vocal = float(bus.get("vocal_bus_gain_db") or 0.0)
    accomp = float(bus.get("accomp_bus_gain_db") or 0.0)
    print(f"{vocal:.3f} {accomp:.3f}")


if __name__ == "__main__":
    main()
