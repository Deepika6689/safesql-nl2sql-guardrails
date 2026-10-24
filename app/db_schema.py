"""Physical schema (SQLAlchemy Core). Single source of truth for both MySQL and SQLite."""

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
)

ORDER_STATUSES = ("completed", "pending", "cancelled")

metadata = MetaData()

customers = Table(
    "customers",
    metadata,
    Column("customer_id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(100), nullable=False),
    Column("email", String(255), nullable=False, unique=True),
    Column("city", String(100), nullable=False),
    Column("signup_date", Date, nullable=False),
    Index("ix_customers_city", "city"),
    mysql_engine="InnoDB",
)

products = Table(
    "products",
    metadata,
    Column("product_id", Integer, primary_key=True, autoincrement=True),
    Column("product_name", String(150), nullable=False),
    Column("category", String(100), nullable=False),
    Column("price", Numeric(10, 2), nullable=False),
    Column("stock_quantity", Integer, nullable=False),
    CheckConstraint("price >= 0", name="ck_products_price_nonneg"),
    CheckConstraint("stock_quantity >= 0", name="ck_products_stock_nonneg"),
    Index("ix_products_category", "category"),
    mysql_engine="InnoDB",
)

orders = Table(
    "orders",
    metadata,
    Column("order_id", Integer, primary_key=True, autoincrement=True),
    Column("customer_id", Integer, ForeignKey("customers.customer_id"), nullable=False),
    Column("order_date", Date, nullable=False),
    Column("status", String(20), nullable=False),
    CheckConstraint("status IN ('" + "','".join(ORDER_STATUSES) + "')", name="ck_orders_status_valid"),
    Index("ix_orders_customer", "customer_id"),
    Index("ix_orders_date", "order_date"),
    Index("ix_orders_status", "status"),
    mysql_engine="InnoDB",
)

order_items = Table(
    "order_items",
    metadata,
    Column("item_id", Integer, primary_key=True, autoincrement=True),
    Column("order_id", Integer, ForeignKey("orders.order_id"), nullable=False),
    Column("product_id", Integer, ForeignKey("products.product_id"), nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("unit_price", Numeric(10, 2), nullable=False),
    CheckConstraint("quantity > 0", name="ck_items_quantity_positive"),
    CheckConstraint("unit_price >= 0", name="ck_items_price_nonneg"),
    Index("ix_items_order", "order_id"),
    Index("ix_items_product", "product_id"),
    mysql_engine="InnoDB",
)
