"""Copy the tracked guide notebooks referenced by MkDocs without executing them."""

from pathlib import Path
import shutil

root = Path(__file__).resolve().parents[2]
for number in range(101, 107):
    matches = list((root / "examples").glob(f"{number}_guide_*.ipynb"))
    if len(matches) != 1:
        raise SystemExit(f"Expected exactly one tracked guide notebook {number}")
    destination = root / "docs" / "notebooks" / matches[0].name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(matches[0], destination)
