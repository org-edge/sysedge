#!/usr/bin/env python3
"""Load SysEdge amygdala rules from a YAML file into the JSON cache.

Run once during setup (or any time rules change):
  python3 amygdala/load_rules.py amygdala/sysedge_rules.yaml

The cache is written to data/amygdala_cache.json (CWD-relative).
guard.py reads the cache on every hook invocation.
"""

import json
import os
import sys
import uuid
from pathlib import Path


def load_from_yaml(yaml_path: str, cache_path: str = None) -> int:
    try:
        import yaml
    except ImportError:
        print("ERROR: pyyaml not installed. Run: pip install pyyaml", file=sys.stderr)
        sys.exit(1)

    if cache_path is None:
        cache_path = os.path.join(os.getcwd(), "data", "amygdala_cache.json")

    with open(yaml_path) as f:
        data = yaml.safe_load(f) or {}

    rules = data.get("rules", [])
    cache = []
    for r in rules:
        cache.append({
            "rule_id":        r.get("id") or f"rule_{uuid.uuid4().hex[:12]}",
            "rule_type":      r.get("type", "warning"),
            "trigger":        r.get("trigger", ""),
            "response":       r.get("response", ""),
            "severity":       r.get("severity", "medium"),
            "source":         "onboarding",
            "context":        r.get("context", ""),
            "check_code":     r.get("check_code", False),
            "project_paths":  r.get("project_paths", []),
            "confirmed_count": 0,
        })

    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(cache, f, indent=2)

    return len(cache)


if __name__ == "__main__":
    yaml_path = sys.argv[1] if len(sys.argv) > 1 else None
    if not yaml_path:
        print(f"Usage: python3 {Path(__file__).name} <rules.yaml>", file=sys.stderr)
        sys.exit(1)

    cache_path = sys.argv[2] if len(sys.argv) > 2 else None
    count = load_from_yaml(yaml_path, cache_path)
    print(f"✓ Loaded {count} amygdala rules → {cache_path or 'data/amygdala_cache.json'}")
