"""Smart Counter - backend (Flask + SQLite)

Flow:  CiRA CORE (DeepDetect -> Python+) --POST /api/detect--> this server
       web page: live detection -> [scan] -> cart -> [checkout] -> PromptPay QR
       -> [confirm paid] -> stock is deducted.

Stock model (simple reservation):
  available = stock - reserved
  checkout  -> reserved += qty         (order status: pending)
  confirm   -> stock -= qty, reserved -= qty   (status: paid)
  cancel / timeout -> reserved -= qty  (status: cancelled / expired)
"""
import io
import os
import re
import sqlite3
import time
from contextlib import closing

import qrcode
from flask import Flask, abort, jsonify, render_template, request, send_file

# ----------------------------- config -----------------------------
PROMPTPAY_ID = os.environ.get("PROMPTPAY_ID", "0612656498")  # phone number
PENDING_TTL = int(os.environ.get("PENDING_TTL", "300"))      # seconds before an unpaid order expires
LIVE_FRESH = float(os.environ.get("LIVE_FRESH", "3"))        # seconds without data before the camera pill turns "stale"
LIVE_KEEP = float(os.environ.get("LIVE_KEEP", "60"))         # seconds the last detection stays on screen / can be scanned
HOLD_EMPTY = float(os.environ.get("HOLD_EMPTY", "1.5"))      # ignore an empty frame right after items were seen (flicker)
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "smart_counter.db"))

# sku = class name exactly as the DeepDetect model outputs it.
# !! price / stock below are placeholders - edit them (or use the stock page).
PRODUCTS = [
    # sku,                        display name,                 price(baht), stock
    ("By365UVSPF50-60g",          "By365 UV SPF50 60g",         199, 20),
    ("AuraWater0-5ml",            "Aura Water 0-5ml",           159, 20),
    ("NiveaDremaControl50ml",     "Nivea Derma Control 50ml",   189, 20),
    ("Kocobabywatwipes20p",       "Koco Baby Wet Wipes 20p",     49, 20),
]

app = Flask(__name__)

# in-memory state (single counter, single cart)
LIVE = {"items": {}, "ts": 0.0, "unknown": {}, "nonempty_ts": 0.0}
CART = {}  # sku -> qty


# ----------------------------- database -----------------------------
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS products(
            sku TEXT PRIMARY KEY, name TEXT NOT NULL, price INTEGER NOT NULL,
            stock INTEGER NOT NULL, reserved INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL,
            total INTEGER NOT NULL, created_at REAL NOT NULL, paid_at REAL);
        CREATE TABLE IF NOT EXISTS order_items(
            order_id INTEGER NOT NULL, sku TEXT NOT NULL, name TEXT NOT NULL,
            price INTEGER NOT NULL, qty INTEGER NOT NULL);
        """
    )
    if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
        conn.executemany("INSERT INTO products(sku,name,price,stock) VALUES(?,?,?,?)", PRODUCTS)
    conn.close()


def expire_pending(conn):
    """Release reservations of unpaid orders older than PENDING_TTL. Call inside a transaction."""
    cutoff = time.time() - PENDING_TTL
    for o in conn.execute("SELECT id FROM orders WHERE status='pending' AND created_at<?", (cutoff,)).fetchall():
        release(conn, o["id"])
        conn.execute("UPDATE orders SET status='expired' WHERE id=?", (o["id"],))


def release(conn, order_id):
    for it in conn.execute("SELECT sku, qty FROM order_items WHERE order_id=?", (order_id,)).fetchall():
        conn.execute("UPDATE products SET reserved = reserved - ? WHERE sku=?", (it["qty"], it["sku"]))


class tx:
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK, so stock checks and updates are atomic."""
    def __enter__(self):
        self.conn = db()
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        self.conn.close()
        return False


# ----------------------------- PromptPay QR -----------------------------
def _tlv(tag, value):
    return f"{tag}{len(value):02d}{value}"


def crc16_ccitt(text):
    crc = 0xFFFF
    for b in text.encode("ascii"):
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return f"{crc:04X}"


def promptpay_payload(phone, amount):
    """EMV QR payload for PromptPay (phone number target) with a fixed amount."""
    digits = re.sub(r"\D", "", phone)
    if not (len(digits) == 10 and digits.startswith("0")):
        raise ValueError("PROMPTPAY_ID must be a 10-digit Thai phone number")
    target = "0066" + digits[1:]
    merchant = _tlv("00", "A000000677010111") + _tlv("01", target)
    body = (
        _tlv("00", "01")
        + _tlv("01", "12")            # 12 = dynamic QR (has amount)
        + _tlv("29", merchant)
        + _tlv("53", "764")           # THB
        + _tlv("54", f"{amount:.2f}")
        + _tlv("58", "TH")
        + "6304"
    )
    return body + crc16_ccitt(body)


# ----------------------------- helpers -----------------------------
def products_map(conn):
    return {r["sku"]: dict(r) for r in conn.execute("SELECT * FROM products")}


def lines_for(items, prods):
    lines, total = [], 0
    for sku, qty in items.items():
        p = prods.get(sku)
        if not p or qty <= 0:
            continue
        sub = p["price"] * qty
        total += sub
        lines.append({"sku": sku, "name": p["name"], "price": p["price"], "qty": qty,
                      "subtotal": sub, "available": p["stock"] - p["reserved"]})
    return lines, total


def order_json(conn, order_id):
    o = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not o:
        return None
    items = [dict(r) for r in conn.execute(
        "SELECT sku,name,price,qty FROM order_items WHERE order_id=?", (order_id,))]
    return {"id": o["id"], "status": o["status"], "total": o["total"], "items": items,
            "created_at": o["created_at"], "paid_at": o["paid_at"],
            "expires_at": o["created_at"] + PENDING_TTL, "now": time.time()}


# ----------------------------- pages / API -----------------------------
@app.get("/")
def index():
    return render_template("index.html", ttl=PENDING_TTL)


@app.post("/api/detect")
def api_detect():
    """Called by the CiRA CORE Python+ box. Body: {"items": {"<class name>": count, ...}}"""
    data = request.get_json(silent=True) or {}
    raw = data.get("items") or {}
    known = {}
    unknown = {}
    with closing(db()) as conn:
        skus = {r["sku"] for r in conn.execute("SELECT sku FROM products")}
    for name, cnt in raw.items():
        try:
            cnt = int(cnt)
        except (TypeError, ValueError):
            continue
        if cnt <= 0:
            continue
        (known if name in skus else unknown)[name] = cnt
    now = time.time()
    if not known and not unknown and (now - LIVE["nonempty_ts"]) < HOLD_EMPTY:
        LIVE["ts"] = now              # one empty frame (flicker): keep the previous items
    else:
        LIVE.update(items=known, unknown=unknown, ts=now)
        if known or unknown:
            LIVE["nonempty_ts"] = now
    return jsonify(ok=True)


@app.get("/api/live")
def api_live():
    with closing(db()) as conn:
        prods = products_map(conn)
    age = time.time() - LIVE["ts"] if LIVE["ts"] else None
    fresh = age is not None and age <= LIVE_FRESH
    keep = age is not None and age <= LIVE_KEEP     # last result stays visible even if data stops
    lines, total = lines_for(LIVE["items"] if keep else {}, prods)
    return jsonify(fresh=fresh, connected=LIVE["ts"] > 0, age=age, lines=lines, total=total,
                   unknown=LIVE["unknown"] if keep else {})


@app.post("/api/cart/scan")
def api_scan():
    if not LIVE["items"]:
        return jsonify(error="ไม่พบสินค้าบนเคาน์เตอร์"), 409
    if (time.time() - LIVE["ts"]) > LIVE_KEEP:
        return jsonify(error="ผลตรวจจับเก่าเกินไป กรุณาตรวจสอบว่ากล้อง/flow ยังทำงานอยู่"), 409
    CART.clear()
    CART.update(LIVE["items"])
    return api_cart()


@app.post("/api/cart/clear")
def api_cart_clear():
    CART.clear()
    return api_cart()


@app.get("/api/cart")
def api_cart():
    with closing(db()) as conn:
        prods = products_map(conn)
    lines, total = lines_for(CART, prods)
    ok = all(l["qty"] <= l["available"] for l in lines)
    return jsonify(lines=lines, total=total, stock_ok=ok)


@app.get("/api/stock")
def api_stock():
    with tx() as conn:
        expire_pending(conn)
        rows = [dict(r, available=r["stock"] - r["reserved"]) for r in conn.execute("SELECT * FROM products")]
    return jsonify(products=rows)


@app.post("/api/stock/<sku>")
def api_set_stock(sku):
    data = request.get_json(silent=True) or {}
    try:
        stock = int(data["stock"])
        if stock < 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return jsonify(error="stock ต้องเป็นจำนวนเต็ม >= 0"), 400
    with tx() as conn:
        cur = conn.execute("UPDATE products SET stock=? WHERE sku=?", (stock, sku))
        if cur.rowcount == 0:
            abort(404)
    return jsonify(ok=True)


@app.post("/api/checkout")
def api_checkout():
    if not CART:
        return jsonify(error="ตะกร้าว่าง"), 400
    with tx() as conn:
        expire_pending(conn)
        prods = products_map(conn)
        lines, total = lines_for(CART, prods)
        if not lines:
            return jsonify(error="ตะกร้าว่าง"), 400
        for l in lines:   # atomic check + reserve
            cur = conn.execute(
                "UPDATE products SET reserved = reserved + ? WHERE sku=? AND stock - reserved >= ?",
                (l["qty"], l["sku"], l["qty"]))
            if cur.rowcount == 0:
                raise OutOfStock(l["name"], l["available"])
        cur = conn.execute("INSERT INTO orders(status,total,created_at) VALUES('pending',?,?)", (total, time.time()))
        oid = cur.lastrowid
        conn.executemany("INSERT INTO order_items VALUES(?,?,?,?,?)",
                         [(oid, l["sku"], l["name"], l["price"], l["qty"]) for l in lines])
        result = order_json(conn, oid)
    CART.clear()
    return jsonify(result)


class OutOfStock(Exception):
    def __init__(self, name, available):
        self.name, self.available = name, available


@app.errorhandler(OutOfStock)
def handle_oos(e):
    return jsonify(error=f"สินค้าไม่พอ: {e.name} (เหลือ {e.available})"), 409


@app.get("/api/orders/<int:oid>")
def api_order(oid):
    with tx() as conn:
        expire_pending(conn)
        o = order_json(conn, oid)
    if not o:
        abort(404)
    return jsonify(o)


@app.get("/api/orders")
def api_orders():
    with tx() as conn:
        expire_pending(conn)
        ids = [r["id"] for r in conn.execute("SELECT id FROM orders ORDER BY id DESC LIMIT 20")]
        orders = [order_json(conn, i) for i in ids]
    return jsonify(orders=orders)


@app.get("/api/orders/<int:oid>/qr.png")
def api_qr(oid):
    with tx() as conn:
        expire_pending(conn)
        o = order_json(conn, oid)
    if not o:
        abort(404)
    img = qrcode.make(promptpay_payload(PROMPTPAY_ID, o["total"]), box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", max_age=0)


@app.post("/api/orders/<int:oid>/confirm")
def api_confirm(oid):
    """Cashier / owner confirms the money arrived (no bank API in this prototype)."""
    with tx() as conn:
        expire_pending(conn)
        o = conn.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()
        if not o:
            abort(404)
        if o["status"] != "pending":
            return jsonify(error=f"ออเดอร์นี้สถานะ {o['status']} แล้ว"), 409
        for it in conn.execute("SELECT sku, qty FROM order_items WHERE order_id=?", (oid,)).fetchall():
            conn.execute("UPDATE products SET stock = stock - ?, reserved = reserved - ? WHERE sku=?",
                         (it["qty"], it["qty"], it["sku"]))
        conn.execute("UPDATE orders SET status='paid', paid_at=? WHERE id=?", (time.time(), oid))
        result = order_json(conn, oid)
    return jsonify(result)


@app.post("/api/orders/<int:oid>/cancel")
def api_cancel(oid):
    with tx() as conn:
        expire_pending(conn)
        o = conn.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()
        if not o:
            abort(404)
        if o["status"] != "pending":
            return jsonify(error=f"ออเดอร์นี้สถานะ {o['status']} แล้ว"), 409
        release(conn, oid)
        conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (oid,))
        result = order_json(conn, oid)
    return jsonify(result)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
