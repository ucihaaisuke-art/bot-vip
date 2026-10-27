import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def main():
    migrations = {
        -5160399926: -1004434516169,
        -5495735553: -1004328410781,
    }

    path = Path(os.environ["VIP_DB_PATH"])

    if not path.is_file():
        raise SystemExit(
            "Database VIP tidak ditemukan; migrasi dibatalkan."
        )

    db = sqlite3.connect(path)

    try:
        needs_migration = any(
            db.execute(
                "SELECT 1 FROM orders WHERE group_id = ? "
                "UNION SELECT 1 FROM access "
                "WHERE group_id = ? LIMIT 1",
                (old_id, old_id),
            ).fetchone() is not None
            for old_id in migrations
        )

        if not needs_migration:
            print(
                "Migrasi ID grup sudah selesai atau tidak diperlukan.",
                flush=True,
            )
            return

        stamp = datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S%fZ"
        )

        backup = path.with_name(
            path.name + ".before-group-migration-" + stamp + ".bak"
        )

        backup_db = sqlite3.connect(backup)
        try:
            db.backup(backup_db)
        finally:
            backup_db.close()

        with db:
            db.execute("BEGIN IMMEDIATE")

            for old_id, new_id in migrations.items():
                db.execute(
                    "UPDATE orders SET group_id = ? "
                    "WHERE group_id = ?",
                    (new_id, old_id),
                )

                db.execute(
                    "INSERT OR IGNORE INTO access "
                    "(user_id, group_id, order_id) "
                    "SELECT user_id, ?, order_id FROM access "
                    "WHERE group_id = ?",
                    (new_id, old_id),
                )

                db.execute(
                    "DELETE FROM access WHERE group_id = ?",
                    (old_id,),
                )

        print(
            "Migrasi ID grup selesai. "
            "Status pembayaran tetap tersimpan.",
            flush=True,
        )

    finally:
        db.close()


if __name__ == "__main__":
    main()
