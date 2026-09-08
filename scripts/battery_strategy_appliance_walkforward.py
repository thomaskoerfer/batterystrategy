#!/usr/bin/env python3
"""Evaluate a cyclic appliance from a copied Battery Strategy feature store."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


def main() -> int:
    from custom_components.battery_strategy.appliance_evaluation import (
        evaluate_cyclic_appliance,
    )
    from custom_components.battery_strategy.feature_store import CompressedFeatureStore

    parser = argparse.ArgumentParser()
    parser.add_argument("feature_store", type=Path)
    parser.add_argument("--component-key", required=True)
    args = parser.parse_args()

    store = CompressedFeatureStore(args.feature_store)
    store.initialize()
    if store.last_error:
        parser.error(store.last_error)
    report = evaluate_cyclic_appliance(store.load(0, 2**63 - 1), args.component_key)
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
