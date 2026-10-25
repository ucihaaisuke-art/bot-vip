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
            return self.order(order_id)

class Payments:

    def __init__(self, store, server_key, *, production=False, merchant_id=None, transport=None):
        if not server_key:
            raise PaymentError('MIDTRANS_SERVER_KEY belum diatur.')
        self.store = store
        self.server_key = server_key
        self.merchant_id = merchant_id
        self.base_url = 'https://api.midtrans.com' if production else 'https://api.sandbox.midtrans.com'
        self.client = httpx.Client(auth=(server_key, ''), timeout=30, transport=transport, headers={'Accept': 'application/json'})

    def close(self):
        self.client.close()

    def _request(self, method, path, **kwargs):
        try:
            response = self.client.request(method, self.base_url + path, **kwargs)
            if response.status_code not in (200, 201):
                raise PaymentError(f'Midtrans memberikan HTTP {response.status_code}.')
            data = response.json()
            if not isinstance(data, dict) or str(data.get('status_code')) not in ('200', '201'):
                raise PaymentError('Permintaan belum diterima Midtrans.')
            return data
        except (httpx.HTTPError, ValueError) as error:
            raise PaymentError(f'Permintaan Midtrans gagal ({type(error).__name__}).') from None

    def create_qris(self, order_id):
        order = self.store.order(order_id)
        if order is None or order['state'] != 'pending':
            raise PaymentError('Pesanan tidak tersedia untuk pembayaran baru.')
        data = self._request('POST', '/v2/charge', json={'payment_type': 'qris', 'transaction_details': {'order_id': order_id, 'gross_amount': order['amount']}, 'qris': {'acquirer': 'gopay'}})
        self._validate_order(data, order)
        qr_url = next((action.get('url') for action in data.get('actions', []) if action.get('name') == 'generate-qr-code'), None)
        parsed = urlsplit(qr_url or '')
        if parsed.scheme != 'https' or parsed.hostname != urlsplit(self.base_url).hostname or parsed.username or parsed.password or (parsed.port not in (None, 443)):
            raise PaymentError('Alamat QRIS tidak sesuai dengan server Midtrans.')
        return {'order_id': order_id, 'amount': order['amount'], 'qr_url': qr_url}

    def _validate_order(self, data, order):
        if data.get('order_id') != order['order_id'] or rupiah(data.get('gross_amount')) != order['amount'] or data.get('currency') != 'IDR' or (data.get('payment_type') != 'qris'):
            raise PaymentError('Detail transaksi tidak cocok dengan pesanan.')
        if self.merchant_id and data.get('merchant_id') != self.merchant_id:
            raise PaymentError('Merchant pembayaran tidak cocok.')

    def qr_image(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname != urlsplit(self.base_url).hostname or parsed.username or parsed.password or (parsed.port not in (None, 443)):
            raise PaymentError('Alamat gambar QRIS tidak valid.')
        try:
            with self.client.stream('GET', url) as response:
                if response.status_code != 200 or not response.headers.get('content-type', '').startswith('image/'):
                    raise PaymentError('Gambar QRIS belum tersedia.')
                result = bytearray()
                for chunk in response.iter_bytes():
                    result.extend(chunk)
                    if len(result) > 2 * 1024 * 1024:
                        raise PaymentError('Gambar QRIS terlalu besar.')
                if not result:
                    raise PaymentError('Gambar QRIS kosong.')
                return bytes(result)
        except httpx.HTTPError as error:
            raise PaymentError(f'Pengambilan gambar QRIS gagal ({type(error).__name__}).') from None

    def check(self, order_id, *, user_id=None):
        order = self.store.order(order_id)
        if order is None or (user_id is not None and order['user_id'] != user_id):
            raise PaymentError('Pesanan tidak ditemukan untuk pengguna ini.')
        data = self._request('GET', f'/v2/{order_id}/status')
        self._validate_order(data, order)
        state = data.get('transaction_status')
        if state == 'settlement' and data.get('fraud_status', 'accept') == 'accept':
            charge_id = data.get('transaction_id')
            if not isinstance(charge_id, str) or not charge_id:
                raise PaymentError('ID transaksi pembayaran tidak tersedia.')
            return self.store._verified_status(order_id, 'paid', charge_id)
        if state in ('cancel', 'deny', 'expire', 'failure'):
            return self.store._verified_status(order_id, 'failed', None)
        return self.store._verified_status(order_id, 'pending', None)

    def notification(self, data):
        if not isinstance(data, dict):
            raise PaymentError('Notifikasi pembayaran tidak valid.')
        fields = ('order_id', 'status_code', 'gross_amount', 'signature_key')
        if any((not isinstance(data.get(field), str) or not data[field] for field in fields)):
            raise PaymentError('Notifikasi pembayaran tidak lengkap.')
        expected = hashlib.sha512((data['order_id'] + data['status_code'] + data['gross_amount'] + self.server_key).encode()).hexdigest()
        if not hmac.compare_digest(expected, data['signature_key']):
            raise PaymentError('Tanda tangan notifikasi pembayaran tidak valid.')
        return self.check(data['order_id'])

from dataclasses import dataclass

@dataclass(frozen=True)
class Plan:
    name: str
    amount: int

PLANS = {
    'indo_premium': Plan('INDO PREMIUM', 30000),
    'gay_lesby_acak': Plan('GAY/LESBY ACAK', 50000),
    'chil_vip': Plan('CHIL VIP', 75000),
    'full_paket': Plan('FULL PAKET', 95000),
}

def create_plan_order(store, user_id, plan_id, group_ids):
    """Use a server-owned price and group mapping, never a buyer's amount."""
    plan = PLANS.get(plan_id)
    if plan is None:
        raise PaymentError('Paket VIP tidak ditemukan.')
    if plan_id not in group_ids:
        raise PaymentError('Grup Telegram untuk paket ini belum dikonfigurasi.')
    return store.create_order(user_id, group_ids[plan_id], plan.amount)

import json
import logging
from http.server import BaseHTTPRequestHandler

logger = logging.getLogger(__name__)

def notification_handler(payments, environment, on_paid=None):

    class Handler(BaseHTTPRequestHandler):

        def setup(self):
            super().setup()
            self.connection.settimeout(20)

        def log_message(self, *args):
            pass

        def reply(self, status, data):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.reply(200 if self.path == '/health' else 404, {'status': 'running' if self.path == '/health' else 'not_found', 'environment': environment})

        def do_POST(self):
            if self.path != '/midtrans/notifications':
                return self.reply(404, {'error': 'not_found'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 65536:
                    return self.reply(400, {'error': 'invalid_body_size'})
                result = payments.notification(json.loads(self.rfile.read(length)))
            except (PaymentError, ValueError, UnicodeError) as error:
                status = 503 if isinstance(error, PaymentError) else 400
                return self.reply(status, {'error': 'notification_not_verified'})
            if result['state'] == 'paid' and on_paid is not None:
                try:
                    on_paid(result)
                except Exception as error:
                    logger.error('Penjadwalan akses gagal: jenis=%s', type(error).__name__)
            self.reply(200, {'order_id': result['order_id'], 'state': result['state']})
    return Handler

import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, ChatJoinRequestHandler, CommandHandler

logger = logging.getLogger(__name__)

def group_settings(raw):
    try:
        groups = json.loads(raw)
    except (ValueError, TypeError):
        raise PaymentError('VIP_GROUP_IDS harus berupa objek JSON.') from None
    if not isinstance(groups, dict):
        raise PaymentError('VIP_GROUP_IDS harus berupa objek JSON.')
    if any((plan not in PLANS or not isinstance(group, int) or isinstance(group, bool) or (group >= 0) for plan, group in groups.items())):
        raise PaymentError('VIP_GROUP_IDS berisi paket atau ID grup yang tidak valid.')
    if len(set(groups.values())) != len(groups):
        raise PaymentError('Setiap paket harus mempunyai grup yang berbeda.')
    return groups

class VipBot:

    def __init__(self, payments, store, groups, environment):
        self.payments = payments
        self.store = store
        self.groups = groups
        self.environment = environment
        self.bot = None
        self.delivery_locks = {}

    async def start(self, update, context):
        message = update.effective_message
        if update.effective_chat.type != 'private':
            await message.reply_text('Buka chat pribadi dengan bot untuk memilih paket VIP.')
            return
        buttons = [[InlineKeyboardButton(f'{plan.name} — Rp{plan.amount:,}'.replace(',', '.'), callback_data=f'vip:buy:{plan_id}')] for plan_id, plan in PLANS.items()]
        prefix = 'MODE UJI COBA — jangan kirim uang sungguhan.\n\n' if self.environment == 'sandbox' else ''
        await message.reply_text(prefix + 'Pilih grup VIP. Semua paket permanen, cukup bayar sekali.\nGunakan /akses untuk mengambil akses yang sudah dibayar.', reply_markup=InlineKeyboardMarkup(buttons))

    async def group_id(self, update, context):
        await update.effective_message.reply_text(f'ID chat: {update.effective_chat.id}')

    async def ready_group(self, group_id):
        chat = await self.bot.get_chat(group_id)
        member = await self.bot.get_chat_member(group_id, self.bot.id)
        if chat.type not in ('group', 'supergroup') or chat.username:
            raise PaymentError('Grup VIP harus berupa grup privat.')
        if member.status != 'administrator' or not member.can_invite_users:
            raise PaymentError('Bot harus menjadi admin dengan izin mengundang anggota.')

    async def callback(self, update, context):
        query = update.callback_query
        await query.answer()
        if query.message is None or query.message.chat.type != 'private':
            return
        user_id = query.from_user.id
        parts = query.data.split(':', 2)
        if len(parts) != 3:
            return
        _, action, reference = parts
        try:
            if action == 'buy':
                if reference not in PLANS or reference not in self.groups:
                    await query.message.reply_text('Paket ini belum siap. Hubungi admin.')
                    return
                group_id = self.groups[reference]
                await self.ready_group(group_id)
                existing = next((order for order in self.store.access_orders(user_id) if order['group_id'] == group_id), None)
                if existing is not None:
                    await self.deliver(existing, explicit=True)
                    return
                order = create_plan_order(self.store, user_id, reference, self.groups)
                await query.message.reply_text(f"Menyiapkan tagihan. Nomor pesanan: {order['order_id']}")
                invoice = await asyncio.to_thread(self.payments.create_qris, order['order_id'])
                qr = await asyncio.to_thread(self.payments.qr_image, invoice['qr_url'])
                warning = 'UJI COBA SANDBOX — jangan transfer uang sungguhan.\n' if self.environment == 'sandbox' else ''
                amount = f"Rp{order['amount']:,}".replace(',', '.')
                await query.message.reply_photo(photo=qr, caption=warning + f"{PLANS[reference].name}\n{amount}\nPesanan: {order['order_id']}\nAkses permanen diberikan setelah pembayaran terverifikasi.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Cek Pembayaran', callback_data=f"vip:check:{order['order_id']}")]]))
            elif action == 'check':
                order = await asyncio.to_thread(self.payments.check, reference, user_id=user_id)
                if order['state'] == 'paid':
                    await self.deliver(order, explicit=True)
                else:
                    text = 'Pembayaran belum terkonfirmasi.' if order['state'] == 'pending' else 'Pembayaran gagal atau kedaluwarsa.'
                    await query.message.reply_text(text)
        except (PaymentError, TelegramError) as error:
            logger.error('Permintaan VIP gagal: jenis=%s', type(error).__name__)
            await query.message.reply_text('Permintaan belum berhasil. Jika sudah membayar, gunakan /akses atau /cek diikuti nomor pesanan. Jangan membayar ulang sebelum statusnya diperiksa.')

    async def check_order(self, update, context):
        if update.effective_chat.type != 'private':
            return
        if len(context.args) != 1:
            await update.effective_message.reply_text('Gunakan /cek NOMOR_PESANAN.')
            return
        try:
            order = await asyncio.to_thread(self.payments.check, context.args[0], user_id=update.effective_user.id)
            if order['state'] == 'paid':
                await self.deliver(order, explicit=True)
            else:
                await update.effective_message.reply_text('Status pembayaran: ' + order['state'])
        except (PaymentError, TelegramError) as error:
            logger.error('Pemeriksaan VIP gagal: jenis=%s', type(error).__name__)
            await update.effective_message.reply_text('Pembayaran belum dapat diverifikasi. Periksa nomor pesanan atau hubungi admin.')

    async def access(self, update, context):
        if update.effective_chat.type != 'private':
            return
        orders = self.store.access_orders(update.effective_user.id)
        if not orders:
            await update.effective_message.reply_text('Belum ada akses VIP terverifikasi. Pilih paket melalui /start.')
        for order in orders:
            await self.deliver(order, explicit=True)

    async def deliver(self, order, *, explicit=False):
        lock = self.delivery_locks.setdefault(order['order_id'], asyncio.Lock())
        async with lock:
            order = self.store.order(order['order_id'])
            if order is None or order['state'] != 'paid':
                return
            if not explicit and (not self.store.claim_notification(order['order_id'])):
                return
            try:
                await self.ready_group(order['group_id'])
                expires_at = int(time.time()) + 600
                invite = await self.bot.create_chat_invite_link(chat_id=order['group_id'], creates_join_request=True, expire_date=datetime.fromtimestamp(expires_at, timezone.utc))
                self.store.add_invite(order['order_id'], invite.invite_link, expires_at)
                await self.bot.send_message(chat_id=order['user_id'], text='Pembayaran terverifikasi. Akses VIP kamu permanen.\nKlik link lalu ajukan bergabung menggunakan akun ini.\nLink berlaku 10 menit; gunakan /akses untuk meminta link baru.\n' + invite.invite_link)
            except (PaymentError, TelegramError) as error:
                logger.error('Pengiriman akses tertunda: jenis=%s', type(error).__name__)

    async def join(self, update, context):
        request = update.chat_join_request
        if request.invite_link is None:
            return
        order = self.store.invite_order(request.invite_link.invite_link)
        if order is None:
            return
        if order['state'] == 'paid' and order['user_id'] == request.from_user.id and (order['group_id'] == request.chat.id) and (order['expires_at'] > time.time()) and self.store.has_access(request.from_user.id, request.chat.id):
            await self.bot.approve_chat_join_request(request.chat.id, request.from_user.id)
            await self.bot.revoke_chat_invite_link(request.chat.id, request.invite_link.invite_link)
        else:
            await self.bot.decline_chat_join_request(request.chat.id, request.from_user.id)

def run(payments, store, environment):
    token = os.environ.get('VIP_BOT_TOKEN')
    if not token:
        raise PaymentError('VIP_BOT_TOKEN belum diatur. Gunakan bot baru, terpisah dari bot ZIP.')
    groups = group_settings(os.environ.get('VIP_GROUP_IDS', '{}'))
    controller = VipBot(payments, store, groups, environment)
    server = None

    async def post_init(application):
        nonlocal server
        controller.bot = application.bot
        for group_id in groups.values():
            await controller.ready_group(group_id)
        loop = asyncio.get_running_loop()

        def on_paid(order):

            def schedule():
                application.create_task(controller.deliver(order))
            loop.call_soon_threadsafe(schedule)

        server = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8080'))), notification_handler(payments, environment, on_paid))
        threading.Thread(target=server.serve_forever, daemon=True).start()

    async def post_stop(application):
        if server is not None:
            await asyncio.to_thread(server.shutdown)
            await asyncio.to_thread(server.server_close)

    async def error_handler(update, context):
        logger.error('Handler VIP gagal: jenis=%s', type(context.error).__name__)

    app = Application.builder().token(token).post_init(post_init).post_stop(post_stop).build()
    app.add_handler(CommandHandler('start', controller.start))
    app.add_handler(CommandHandler('akses', controller.access))
    app.add_handler(CommandHandler('cek', controller.check_order))
    app.add_handler(CommandHandler('id', controller.group_id))
    app.add_handler(CallbackQueryHandler(controller.callback, pattern='^vip:(buy|check):'))
    app.add_handler(ChatJoinRequestHandler(controller.join))
    app.add_error_handler(error_handler)
    app.run_polling(allowed_updates=['message', 'callback_query', 'chat_join_request'])

import argparse
import json
import os
from http.server import ThreadingHTTPServer
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description='Midtrans QRIS sandbox integration')
    parser.add_argument('command', choices=('create', 'check', 'serve', 'bot'))
    parser.add_argument('--user-id', type=int)
    parser.add_argument('--group-id', type=int)
    parser.add_argument('--plan', choices=tuple(PLANS))
    parser.add_argument('--order-id')
    args = parser.parse_args()

    key = os.environ.get('MIDTRANS_SERVER_KEY')
    if not key:
        parser.exit(1, 'MIDTRANS_SERVER_KEY belum diatur. Masukkan secara aman di Variables.\n')

    environment = os.environ.get('MIDTRANS_ENVIRONMENT', 'sandbox')
    if environment not in ('sandbox', 'production'):
        parser.error('MIDTRANS_ENVIRONMENT harus sandbox atau production.')

    path = Path(os.environ.get('VIP_DB_PATH', 'data/payments.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(path)
    payments = Payments(store, key, production=environment == 'production', merchant_id=os.environ.get('MIDTRANS_MERCHANT_ID'))

    try:
        if args.command == 'create':
            if None in (args.user_id, args.group_id, args.plan):
                parser.error('create memerlukan --user-id, --group-id, dan --plan.')
            order = create_plan_order(store, args.user_id, args.plan, {args.plan: args.group_id})
            print(json.dumps({'order_id': order['order_id'], 'state': 'pending'}), flush=True)
            print(json.dumps(payments.create_qris(order['order_id'])))
        elif args.command == 'check':
            if args.order_id is None or args.user_id is None:
                parser.error('check memerlukan --order-id dan --user-id pemilik pesanan.')
            print(json.dumps(payments.check(args.order_id, user_id=args.user_id)))
        elif args.command == 'bot':
            run(payments, store, environment)
        else:
            server = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8080'))), notification_handler(payments, environment))
            try:
                server.serve_forever()
            finally:
                server.server_close()
    except PaymentError as error:
        parser.exit(1, str(error) + '\n')
    finally:
        payments.close()
        store.close()

if __name__ == '__main__':
    main()
