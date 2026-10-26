import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def main():
    old_id = -5160399926
    new_id = -1004434516169
    path = Path(os.environ["VIP_DB_PATH"])

    if not path.is_file():
        raise SystemExit("Database VIP tidak ditemukan; migrasi dibatalkan.")

    db = sqlite3.connect(path)
    try:
        old_orders = db.execute(
            "SELECT COUNT(*) FROM orders WHERE group_id = ?", (old_id,)
        ).fetchone()[0]
        old_access = db.execute(
            "SELECT COUNT(*) FROM access WHERE group_id = ?", (old_id,)
        ).fetchone()[0]

        if not old_orders and not old_access:
            print("Migrasi INDO PREMIUM sudah selesai atau tidak diperlukan.", flush=True)
            return

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(path.name + ".before-indo-" + stamp + ".bak")
        backup_db = sqlite3.connect(backup)
        try:
            db.backup(backup_db)
        finally:
            backup_db.close()

        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE orders SET group_id = ? WHERE group_id = ?",
                (new_id, old_id),
            )
            db.execute(
                "INSERT OR IGNORE INTO access (user_id, group_id, order_id) "
                "SELECT user_id, ?, order_id FROM access WHERE group_id = ?",
                (new_id, old_id),
            )
            db.execute("DELETE FROM access WHERE group_id = ?", (old_id,))

        print("Migrasi INDO PREMIUM selesai. Status pembayaran tetap tersimpan.", flush=True)
    finally:
        db.close()


if __name__ == "__main__":
    main()
