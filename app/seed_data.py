"""Deterministic, fictional seed data and idempotent loading.

All people, e-mail addresses (example.com) and products are fictional. Orders are produced by
a tiny linear-congruential generator so the dataset is identical on every machine/Python version.
"""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import Engine, func, insert, select

from app.db_schema import customers, metadata, order_items, orders, products

CUSTOMERS = [
    (1, "Aarav Nair", "aarav.nair@example.com", "Mysuru", date(2025, 1, 12)),
    (2, "Diya Menon", "diya.menon@example.com", "Bengaluru", date(2025, 1, 25)),
    (3, "Kabir Rao", "kabir.rao@example.com", "Bengaluru", date(2025, 2, 3)),
    (4, "Isha Kulkarni", "isha.kulkarni@example.com", "Pune", date(2025, 2, 19)),
    (5, "Rohan Iyer", "rohan.iyer@example.com", "Chennai", date(2025, 3, 7)),
    (6, "Meera Shah", "meera.shah@example.com", "Mumbai", date(2025, 3, 21)),
    (7, "Vihaan Reddy", "vihaan.reddy@example.com", "Hyderabad", date(2025, 4, 2)),
    (8, "Anaya Das", "anaya.das@example.com", "Kolkata", date(2025, 4, 18)),
    (9, "Arjun Verma", "arjun.verma@example.com", "Bengaluru", date(2025, 5, 9)),
    (10, "Saanvi Joshi", "saanvi.joshi@example.com", "Pune", date(2025, 5, 30)),
    (11, "Reyansh Pillai", "reyansh.pillai@example.com", "Mysuru", date(2025, 6, 14)),
    (12, "Kiara Bhat", "kiara.bhat@example.com", "Chennai", date(2025, 7, 1)),
    (13, "Advait Singh", "advait.singh@example.com", "Delhi", date(2025, 7, 22)),
    # The next three never place an order (used by the "never ordered" question).
    (14, "Navya Gowda", "navya.gowda@example.com", "Mysuru", date(2025, 8, 11)),
    (15, "Tanish Kapoor", "tanish.kapoor@example.com", "Mumbai", date(2025, 9, 5)),
    (16, "Pari Chatterjee", "pari.chatterjee@example.com", "Kolkata", date(2025, 9, 28)),
]
CUSTOMERS_WITH_ORDERS = 13  # ids 1..13

PRODUCTS = [
    (1, "Wireless Mouse", "Electronics", Decimal("799.00"), 120),
    (2, "Mechanical Keyboard", "Electronics", Decimal("3499.00"), 45),
    (3, "USB-C Hub", "Electronics", Decimal("1599.00"), 8),
    (4, "Noise-Cancelling Headphones", "Electronics", Decimal("5999.00"), 6),
    (5, "Laptop Stand", "Accessories", Decimal("1299.00"), 60),
    (6, "Phone Case", "Accessories", Decimal("299.00"), 250),
    (7, "Webcam Cover (3-pack)", "Accessories", Decimal("149.00"), 9),
    (8, "A5 Notebook", "Stationery", Decimal("120.00"), 400),
    (9, "Gel Pen Set", "Stationery", Decimal("199.00"), 180),
    (10, "Desk Lamp", "Home", Decimal("1099.00"), 35),
    (11, "Ceramic Mug", "Home", Decimal("349.00"), 5),
    (12, "Wall Clock", "Home", Decimal("899.00"), 22),
]


def build_orders() -> tuple[list[tuple], list[tuple]]:
    """Return (orders, order_items) rows, deterministically."""
    state = 2026

    def rnd(n: int) -> int:
        nonlocal state
        state = (state * 1103515245 + 12345) % (2**31)
        return (state >> 8) % n

    price = {p[0]: p[3] for p in PRODUCTS}
    start = date(2026, 1, 5)
    order_rows: list[tuple] = []
    item_rows: list[tuple] = []
    item_id = 1
    for order_id in range(1, 61):
        customer_id = 1 + rnd(CUSTOMERS_WITH_ORDERS)
        order_date = start + timedelta(days=rnd(176))  # 2026-01-05 .. 2026-06-29
        roll = rnd(100)
        status = "completed" if roll < 70 else ("pending" if roll < 85 else "cancelled")
        order_rows.append((order_id, customer_id, order_date, status))
        chosen: list[int] = []
        for _ in range(1 + rnd(3)):
            product_id = 1 + rnd(len(PRODUCTS))
            if product_id in chosen:
                continue
            chosen.append(product_id)
            item_rows.append((item_id, order_id, product_id, 1 + rnd(4), price[product_id]))
            item_id += 1
    return order_rows, item_rows


def _insert_missing(conn, table, pk_col: str, rows: list[dict]) -> int:
    existing = set(conn.execute(select(table.c[pk_col])).scalars())
    new_rows = [r for r in rows if r[pk_col] not in existing]
    if new_rows:
        conn.execute(insert(table), new_rows)  # parameterized bulk insert
    return len(new_rows)


def seed(engine: Engine) -> dict[str, int]:
    """Insert any missing seed rows. Safe to run repeatedly; returns rows inserted per table."""
    order_rows, item_rows = build_orders()
    inserted = {}
    with engine.begin() as conn:
        inserted["customers"] = _insert_missing(
            conn,
            customers,
            "customer_id",
            [dict(customer_id=i, name=n, email=e, city=c, signup_date=d) for i, n, e, c, d in CUSTOMERS],
        )
        inserted["products"] = _insert_missing(
            conn,
            products,
            "product_id",
            [
                dict(product_id=i, product_name=n, category=c, price=p, stock_quantity=s)
                for i, n, c, p, s in PRODUCTS
            ],
        )
        inserted["orders"] = _insert_missing(
            conn,
            orders,
            "order_id",
            [dict(order_id=i, customer_id=c, order_date=d, status=s) for i, c, d, s in order_rows],
        )
        inserted["order_items"] = _insert_missing(
            conn,
            order_items,
            "item_id",
            [
                dict(item_id=i, order_id=o, product_id=p, quantity=q, unit_price=u)
                for i, o, p, q, u in item_rows
            ],
        )
    return inserted


def row_counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            name: conn.execute(select(func.count()).select_from(t)).scalar_one()
            for name, t in metadata.tables.items()
        }
