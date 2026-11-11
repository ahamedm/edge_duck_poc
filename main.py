"""Edge Duck PoC entrypoint: seed on first run, then serve."""
from __future__ import annotations

import os

from app.db import DB_PATH, db_exists, init_schema
from app.seed import run_seed


def main() -> None:
    if not db_exists():
        print(f"No database at {DB_PATH} - seeding synthetic data...")
        init_schema()
        run_seed()
    else:
        init_schema()

    from app import create_app

    app = create_app()
    port = int(os.environ.get("PORT", 5001))
    print(f"edge_duck running on http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
