from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.db_schema import customers, order_items, orders, products
from app.seed_data import (
    CUSTOMERS,
    PRODUCTS,
    build_orders,
    row_counts,
    seed,
)


def test_schema_and_seed_counts(admin_engine):
    counts = row_counts(admin_engine)
    o, i = build_orders()
    assert counts == {
        "customers": len(CUSTOMERS),
        "products": len(PRODUCTS),
        "orders": len(o),
        "order_items": len(i),
    }


def test_seed_is_idempotent(admin_engine):
    before = row_counts(admin_engine)
    assert seed(admin_engine) == {"customers": 0, "products": 0, "orders": 0, "order_items": 0}
    assert row_counts(admin_engine) == before


def test_seed_data_is_internally_consistent(admin_engine):
    with admin_engine.connect() as c:
        assert c.execute(text("PRAGMA foreign_key_check")).fetchall() == []
        mismatched = c.execute(
            text(
                "SELECT COUNT(*) FROM order_items oi JOIN products p ON p.product_id=oi.product_id "
                "WHERE oi.unit_price <> p.price"
            )
        ).scalar_one()
        statuses = {r[0] for r in c.execute(text("SELECT DISTINCT status FROM orders"))}
        never = c.execute(
            text(
                "SELECT COUNT(*) FROM customers c WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id=c.customer_id)"
            )
        ).scalar_one()
    assert mismatched == 0
    assert statuses == {"completed", "pending", "cancelled"}
    assert never == 3
    assert all(c[2].endswith("@example.com") for c in CUSTOMERS)


def test_foreign_keys_enforced(admin_engine):
    with pytest.raises(IntegrityError), admin_engine.begin() as c:
        c.execute(insert(orders).values(customer_id=9999, order_date=date(2026, 1, 1), status="pending"))


@pytest.mark.parametrize(
    "stmt",
    [
        lambda: insert(customers).values(
            name="X", email="aarav.nair@example.com", city="Y", signup_date=date(2025, 1, 1)
        ),
        lambda: insert(customers).values(name="X", email=None, city="Y", signup_date=date(2025, 1, 1)),
        lambda: insert(products).values(
            product_name="P", category="C", price=Decimal("-1"), stock_quantity=1
        ),
        lambda: insert(products).values(
            product_name="P", category="C", price=Decimal("1"), stock_quantity=-5
        ),
        lambda: insert(orders).values(customer_id=1, order_date=date(2026, 1, 1), status="shipped"),
        lambda: insert(order_items).values(order_id=1, product_id=1, quantity=0, unit_price=Decimal("1")),
        lambda: insert(order_items).values(order_id=1, product_id=1, quantity=1, unit_price=Decimal("-1")),
    ],
)
def test_invalid_records_rejected(admin_engine, stmt):
    with pytest.raises(IntegrityError), admin_engine.begin() as c:
        c.execute(stmt())


def test_revenue_counts_only_completed_orders(admin_engine):
    o, items = build_orders()
    completed = {r[0] for r in o if r[3] == "completed"}
    expected = sum(q * u for _id, oid, _p, q, u in items if oid in completed)
    with admin_engine.connect() as c:
        got = c.execute(
            text(
                "SELECT SUM(oi.quantity*oi.unit_price) FROM order_items oi JOIN orders o ON o.order_id=oi.order_id "
                "WHERE o.status='completed'"
            )
        ).scalar_one()
    assert Decimal(str(got)) == expected


def test_app_connection_is_read_only(app_engine):
    with pytest.raises(OperationalError), app_engine.begin() as c:
        c.execute(text("DELETE FROM customers"))
