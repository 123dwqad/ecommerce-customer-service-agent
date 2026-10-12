"""Create schema and seed fictional data. Never deletes or resets existing records."""
import argparse
import json
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path
from sqlalchemy.engine import make_url
from app.commerce.config import Settings
from app.commerce.store import Store
from app.commerce.seed import BATCH, seed_scenarios, table_counts
from app.commerce.db_models import SeedBatch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users", type=int, default=10)
    parser.add_argument("--credentials", default="data/demo_accounts.json")
    args = parser.parse_args()
    if not 1 <= args.users <= 100:
        parser.error("--users must be between 1 and 100")
    settings = Settings()
    url = make_url(settings.database_url)
    if url.get_backend_name() == "sqlite" and url.database and Path(url.database).is_file():
        backup = Path("data/backups") / f"commerce-before-seed-{datetime.now():%Y%m%d-%H%M%S-%f}.db"
        backup.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(url.database) as source, sqlite3.connect(backup) as dest:
            source.backup(dest)
        print(f"SQLite backup: {backup.resolve()}")
    store = Store(settings.database_url)
    try:
        store.initialize()
        with store.session() as db:
            done = db.get(SeedBatch, BATCH) is not None
        if not done:
            output = Path(args.credentials)
            output.parent.mkdir(parents=True, exist_ok=True)
            if output.exists():
                payload = json.loads(output.read_text(encoding="utf-8"))
                if payload.get("batch") != BATCH or len(payload.get("accounts", [])) != args.users:
                    raise ValueError("Existing credentials file belongs to another batch/size; choose a new --credentials path")
                accounts = payload["accounts"]
            else:
                accounts = [{"user_id": f"demo_buyer_{n + 1:02}", "password": secrets.token_urlsafe(18)} for n in range(args.users)]
                # Persist before the transaction so a failure/retry never loses login credentials.
                with output.open("x", encoding="utf-8") as file:
                    json.dump({"batch": BATCH, "accounts": accounts}, file, ensure_ascii=False, indent=2)
            seed_scenarios(store, accounts)
            print(f"Demo credentials (keep local): {output.resolve()}")
        else:
            print("Scenario batch already exists; no duplicate records created.")
        print(json.dumps(table_counts(store), ensure_ascii=False, indent=2))
    finally:
        store.engine.dispose()


if __name__ == "__main__":
    main()
