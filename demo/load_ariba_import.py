"""Demo only: act as the Ariba admin who loads the CIG import files and re-exports the realm.

    .\\.venv\\Scripts\\python demo\\load_ariba_import.py ariba-dev

Merges demo/outbox/<instance>/*.import.csv into the instance's export folder
(demo/state/ariba/<env>) by natural key, then moves the import files to outbox/loaded/.
In a real project this is the upload in the CIG portal / Ariba Administrator.
"""

from __future__ import annotations

import csv
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from p2p_agents.paths import natural_key  # noqa: E402
from p2p_agents.settings import load_instance, load_platform  # noqa: E402


def main(instance_name: str) -> None:
    inst = load_instance(instance_name, ROOT / "demo" / "config")
    platform = load_platform(inst.platform, ROOT / "config")
    outbox, bundle = ROOT / inst.outbox_dir, ROOT / inst.bundle_dir
    files = sorted(outbox.glob("*.import.csv"))
    if not files:
        print(f"nothing to load in {outbox}")
        return
    for imp in files:
        cfg = platform.objects[imp.name.split(".")[0]]
        export = bundle / cfg.file
        with export.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            columns, rows = list(reader.fieldnames or []), list(reader)
        with imp.open(newline="", encoding="utf-8-sig") as f:
            changes = [{k: v for k, v in r.items() if k != "_action"} for r in csv.DictReader(f)]
        by_key = {natural_key(r, cfg.key): r for r in rows}
        for change in changes:
            columns += [c for c in change if c not in columns]
            by_key[natural_key(change, cfg.key)] = {**by_key.get(natural_key(change, cfg.key), {}), **change}
        with export.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            writer.writerows(by_key.values())
        (outbox / "loaded").mkdir(exist_ok=True)
        shutil.move(str(imp), outbox / "loaded" / imp.name)
        print(f"loaded {len(changes)} row(s) from {imp.name} into {export.relative_to(ROOT)}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: load_ariba_import.py <instance>  (e.g. ariba-dev)")
    main(sys.argv[1])
