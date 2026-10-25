# Bot VIP standalone: save as bot_vip.py and run: python bot_vip.py bot
import hashlib
import hmac
import sqlite3
import threading
import uuid
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
import httpx

class PaymentError(Exception):
    """Safe diagnostic that contains no credentials or raw provider response."""

def rupiah(value):
    if isinstance(value, bool):
        raise PaymentError('Nominal pembayaran tidak valid.')
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount != amount.to_integral_value():
            raise PaymentError('Nominal harus berupa rupiah utuh dan lebih dari nol.')
        return int(amount)
    except (InvalidOperation, ValueError, TypeError):
        raise PaymentError('Nominal pembayaran tidak valid.') from None

class Store:

    def __init__(self, path):
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.connection:
            self.connection.executescript("\n                CREATE TABLE IF NOT EXISTS orders (\n                    order_id TEXT PRIMARY KEY,\n                    user_id INTEGER NOT NULL,\n                    group_id INTEGER NOT NULL,\n                    amount INTEGER NOT NULL,\n                    state TEXT NOT NULL DEFAULT 'pending',\n                    charge_id TEXT UNIQUE\n                );\n                CREATE TABLE IF NOT EXISTS access (\n                    user_id INTEGER NOT NULL,\n                    group_id INTEGER NOT NULL,\n                    order_id TEXT NOT NULL,\n                    PRIMARY KEY (user_id, group_id)\n                );\n                CREATE TABLE IF NOT EXISTS invites (\n                    link TEXT PRIMARY KEY,\n                    order_id TEXT NOT NULL,\n                    expires_at INTEGER NOT NULL\n                );\n            ")
            columns = {row[1] for row in self.connection.execute('PRAGMA table_info(orders)')}
            if 'notification_attempted' not in columns:
                self.connection.execute('ALTER TABLE orders ADD COLUMN notification_attempted INTEGER NOT NULL DEFAULT 0')

    def close(self):
        self.connection.close()

    def create_order(self, user_id, group_id, amount):
        if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
            raise PaymentError('ID pengguna Telegram tidak valid.')
        if not isinstance(group_id, int) or isinstance(group_id, bool) or group_id >= 0:
            raise PaymentError('ID grup Telegram harus berupa angka negatif.')
        amount = rupiah(amount)
        order_id = 'vip-' + uuid.uuid4().hex
        with self.lock, self.connection:
            self.connection.execute('INSERT INTO orders (order_id, user_id, group_id, amount) VALUES (?, ?, ?, ?)', (order_id, user_id, group_id, amount))
        return self.order(order_id)

    def order(self, order_id):
        with self.lock:
            row = self.connection.execute('SELECT * FROM orders WHERE order_id = ?', (order_id,)).fetchone()
            return dict(row) if row else None

    def has_access(self, user_id, group_id):
        with self.lock:
            return self.connection.execute('SELECT 1 FROM access WHERE user_id = ? AND group_id = ?', (user_id, group_id)).fetchone() is not None

    def access_orders(self, user_id):
        with self.lock:
            return [dict(row) for row in self.connection.execute('SELECT orders.* FROM access JOIN orders USING (order_id) WHERE access.user_id = ?', (user_id,))]

    def claim_notification(self, order_id):
        with self.lock, self.connection:
            return self.connection.execute("UPDATE orders SET notification_attempted = 1 WHERE order_id = ? AND state = 'paid' AND notification_attempted = 0", (order_id,)).rowcount == 1

    def add_invite(self, order_id, link, expires_at):
        with self.lock, self.connection:
            order = self.order(order_id)
            if order is None or order['state'] != 'paid':
                raise PaymentError('Pesanan belum dibayar.')
            self.connection.execute('INSERT INTO invites (link, order_id, expires_at) VALUES (?, ?, ?)', (link, order_id, expires_at))

    def invite_order(self, link):
        with self.lock:
            row = self.connection.execute('SELECT orders.*, invites.expires_at FROM invites JOIN orders USING (order_id) WHERE invites.link = ?', (link,)).fetchone()
            return dict(row) if row else None

    def _verified_status(self, order_id, state, charge_id):
        with self.lock, self.connection:
            order = self.order(order_id)
            if order is None:
                raise PaymentError('Pesanan tidak ditemukan.')
            if order['state'] == 'paid':
                if state == 'paid' and order['charge_id'] != charge_id:
                    raise PaymentError('ID transaksi tidak cocok dengan pembayaran tersimpan.')
                return self.order(order_id)
            if state == 'paid':
                try:
                    self.connection.execute("UPDATE orders SET state = 'paid', charge_id = ? WHERE order_id = ?", (charge_id, order_id))
                except sqlite3.IntegrityError:
                    raise PaymentError('ID transaksi sudah digunakan pesanan lain.') from None
                self.connection.execute('INSERT OR IGNORE INTO access (user_id, group_id, order_id) VALUES (?, ?, ?)', (order['user_id'], order['group_id'], order_id))
            else:
                self.connection.execute('UPDATE orders SET state = ? WHERE order_id = ?', (state, order_id))
