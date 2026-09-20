"""Pack what every CHTC job needs into chtc/bundle/ (gitignored).

    bundle/code.tar.gz          config/paths.py + src/ — the same tree the scripts use here
    bundle/trait_models.tar.gz  every *.json in the trait-model folder
    bundle/flights/<id>.json    flight manifests from the inventory layer
    bundle/neon_token.txt       the NEON API token, one line

Run from the repository root on this machine, then chtc/deploy.sh copies chtc/ to the
access point:

    uv run python chtc/make_bundle.py
    uv run python chtc/make_bundle.py --models /path/to/your/trait_models
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from config.paths import FLIGHTS_DIR, TRAIT_MODEL_DIR  # noqa: E402

try:
    from config.credentials import NEON_API_TOKEN
except ImportError:
    NEON_API_TOKEN = ""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", type=Path, default=TRAIT_MODEL_DIR)
    ap.add_argument("--flights", nargs="*", help="flight ids to include (default: all manifests)")
    ap.add_argument("--out", type=Path, default=REPO / "chtc" / "bundle")
    args = ap.parse_args()

    out = args.out
    if out.exists():
        shutil.rmtree(out)
    (out / "flights").mkdir(parents=True)

    with tarfile.open(out / "code.tar.gz", "w:gz") as tar:
        tar.add(REPO / "config" / "__init__.py", "config/__init__.py")
        tar.add(REPO / "config" / "paths.py", "config/paths.py")
        for py in sorted((REPO / "src").rglob("*.py")):
            if "__pycache__" not in py.parts:
                tar.add(py, py.relative_to(REPO).as_posix())

    models = sorted(Path(args.models).glob("*.json"))
    if not models:
        sys.exit(f"no trait model json in {args.models}")
    with tarfile.open(out / "trait_models.tar.gz", "w:gz") as tar:
        for m in models:
            tar.add(m, m.name)

    manifests = sorted(FLIGHTS_DIR.glob("*.json"))
    if args.flights:
        keep = set(args.flights)
        manifests = [m for m in manifests if m.stem in keep]
    for m in manifests:
        shutil.copy2(m, out / "flights" / m.name)

    if not NEON_API_TOKEN:
        sys.exit("config/credentials.py has no NEON_API_TOKEN")
    (out / "neon_token.txt").write_text(NEON_API_TOKEN + "\n")

    print(f"bundle -> {out}")
    print(f"  code.tar.gz          {(out / 'code.tar.gz').stat().st_size / 1e3:.0f} kB")
    print(f"  trait_models.tar.gz  {len(models)} models from {args.models}")
    print(f"  flights/             {len(manifests)} manifests")


if __name__ == "__main__":
    main()
