"""Verify schema, foreign keys, row counts and the revenue definition using the APP account."""

import sys

from sqlalchemy import inspect, text

from app.config import Settings
from app.database import create_app_engine
from app.db_schema import metadata

REVENUE_SQL = """
SELECT ROUND(SUM(oi.quantity * oi.unit_price), 2)
FROM order_items oi JOIN orders o ON o.order_id = oi.order_id
WHERE o.status = 'completed'
"""


def main() -> int:
    engine = create_app_engine(Settings())
    problems = []
    insp = inspect(engine)
    existing = set(insp.get_table_names())
    for name in metadata.tables:
        if name not in existing:
            problems.append(f"missing table: {name}")
    with engine.connect() as conn:
        for table, cols in [
            ("customers", "customer_id"),
            ("products", "product_id"),
            ("orders", "order_id"),
            ("order_items", "item_id"),
        ]:
            if table in existing:
                n = conn.execute(text(f"SELECT COUNT({cols}) FROM {table}")).scalar_one()
                print(f"{table:12s} rows={n}")
                if n == 0:
                    problems.append(f"{table} is empty")
        orphan = conn.execute(
            text(
                "SELECT COUNT(*) FROM orders o LEFT JOIN customers c ON c.customer_id=o.customer_id "
                "WHERE c.customer_id IS NULL"
            )
        ).scalar_one()
        if orphan:
            problems.append(f"{orphan} orders reference missing customers")
        print("completed-order revenue:", conn.execute(text(REVENUE_SQL)).scalar_one())
    for p in problems:
        print("PROBLEM:", p)
    print("VERIFY:", "FAILED" if problems else "OK")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
