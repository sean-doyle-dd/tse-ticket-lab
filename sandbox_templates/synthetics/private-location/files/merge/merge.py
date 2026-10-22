"""Merge config/overrides.json on top of the private location config the TSE downloaded."""
import json
import sys

base_path, overrides_path, out_path = "/config/worker-config.json", "/config/overrides.json", "/out/synthetics-check-runner.json"
try:
    with open(base_path) as f:
        base = json.load(f)
except FileNotFoundError:
    sys.exit("config/worker-config.json not found. Download your private location config from your "
             "sandbox org and save it there (see config/README.txt).")
with open(overrides_path) as f:
    overrides = json.load(f)
base.update(overrides)
with open(out_path, "w") as f:
    json.dump(base, f, indent=2)
print("merged overrides:", ", ".join(f"{k}={v}" for k, v in overrides.items()))
