"""Explicit allowlist of tables and columns the query engine may touch.

Anything not listed here is invisible to the LLM prompt, the /schema endpoint, the SQL
validator, and (on MySQL) is not granted to the application account.

`customers.email` is deliberately NOT exposed: it is personal data the demo questions
never need, so it shows the column-level allowlist working end to end.
"""

ALLOWED_COLUMNS: dict[str, dict[str, str]] = {
    "customers": {
        "customer_id": "INT, primary key",
        "name": "VARCHAR, customer full name",
        "city": "VARCHAR, city of residence",
        "signup_date": "DATE, when the customer signed up",
    },
    "products": {
        "product_id": "INT, primary key",
        "product_name": "VARCHAR",
        "category": "VARCHAR, product category",
        "price": "DECIMAL(10,2), current list price",
        "stock_quantity": "INT, units in stock",
    },
    "orders": {
        "order_id": "INT, primary key",
        "customer_id": "INT, foreign key -> customers.customer_id",
        "order_date": "DATE",
        "status": "VARCHAR, one of 'completed', 'pending', 'cancelled'",
    },
    "order_items": {
        "item_id": "INT, primary key",
        "order_id": "INT, foreign key -> orders.order_id",
        "product_id": "INT, foreign key -> products.product_id",
        "quantity": "INT, units ordered (> 0)",
        "unit_price": "DECIMAL(10,2), price per unit at time of order",
    },
}

TABLE_DESCRIPTIONS: dict[str, str] = {
    "customers": "People who buy things.",
    "products": "Items for sale, with stock levels.",
    "orders": "One row per order placed by a customer.",
    "order_items": "Line items: which products (and how many) belong to each order.",
}

BUSINESS_RULES = (
    "Revenue = SUM(order_items.quantity * order_items.unit_price) over orders whose "
    "status = 'completed', unless the question explicitly asks for another status.",
    "Orders link to customers via orders.customer_id; products link to orders only via order_items.",
)


def allowed_schema_for_validator() -> dict[str, dict[str, str]]:
    """Schema in the shape sqlglot's qualifier expects: {table: {column: type}}."""
    return {t: {c: "TEXT" for c in cols} for t, cols in ALLOWED_COLUMNS.items()}
