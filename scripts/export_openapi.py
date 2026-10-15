"""Genera openapi.json (contrato con el cliente móvil).

uv run python scripts/export_openapi.py          # escribe openapi.json
uv run python scripts/export_openapi.py --check  # falla si el commiteado está viejo
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "openapi.json"

# La app se construye sin conectarse a nada; solo hacen falta settings válidos.
os.environ.setdefault("DATABASE_URL", "postgresql://unused:unused@localhost:1/unused")
os.environ.setdefault("JWT_SECRET", "unused")
os.environ.setdefault("REFRESH_DERIVATION_KEY", "unused-refresh")
# Sin providers externos: no depender del .env local (que puede pedir más config).
os.environ["FOOD_DATA_PROVIDERS"] = ""
sys.path.insert(0, str(ROOT))


def render() -> str:
    from app.main import create_app

    schema = create_app().openapi()
    return json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    rendered = render()
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != rendered:
            print(
                "openapi.json está desactualizado. Corre: uv run python scripts/export_openapi.py"
            )
            return 1
        print("openapi.json al día")
        return 0

    OUTPUT.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"escrito {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
