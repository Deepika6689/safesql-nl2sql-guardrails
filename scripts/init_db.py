"""Create the database, a least-privilege application account, the schema and seed data.

MySQL usage (PowerShell, from the project root, venv active):
    python -m scripts.init_db            # prompts for the MySQL ADMIN password (not echoed)
    python -m scripts.init_db --reset --confirm-database safesql   # DROPS the tables first

The admin password is read from MYSQL_ADMIN_PASSWORD or, if unset, from a hidden prompt.
The application account (MYSQL_USER / MYSQL_PASSWORD from .env) receives only
SELECT on the allowlisted tables/columns.
"""

import argparse
import getpass
import re
import sys

from sqlalchemy import create_engine, text

from app.config import Settings
from app.database import create_admin_engine
from app.db_schema import metadata
from app.security.permissions import ALLOWED_COLUMNS
from app.seed_data import row_counts, seed

IDENT = re.compile(r"^[A-Za-z0-9_]{1,32}$")


def _require_ident(value: str, label: str) -> str:
    if not IDENT.match(value):
        sys.exit(f"{label} must match [A-Za-z0-9_]{{1,32}}")
    return value


def provision_mysql(settings: Settings, admin_password: str) -> None:
    """Create DB + restricted user using an administrator connection (MySQL only)."""
    db = _require_ident(settings.mysql_database, "MYSQL_DATABASE")
    app_user = _require_ident(settings.mysql_user, "MYSQL_USER")
    app_pw = settings.mysql_password.get_secret_value()
    if not app_pw:
        sys.exit("MYSQL_PASSWORD (application account) is empty in .env")
    if app_user == settings.mysql_admin_user:
        sys.exit("MYSQL_USER must be a dedicated account, different from the admin account")
    admin = create_admin_engine_no_db(settings, admin_password)
    with admin.begin() as conn:
        conn.execute(text(f"CREATE DATABASE IF NOT EXISTS `{db}` CHARACTER SET utf8mb4"))


def create_admin_engine_no_db(settings: Settings, admin_password: str):
    return create_engine(settings.admin_url(password=admin_password, with_database=False))


def grant_select(settings: Settings, admin_password: str) -> None:
    db = settings.mysql_database
    app_user = settings.mysql_user
    app_pw = settings.mysql_password.get_secret_value()
    admin = create_admin_engine_no_db(settings, admin_password)
    with admin.begin() as conn:
        # Identifiers are validated above; the password is passed as a bound parameter.
        conn.execute(
            text(f"CREATE USER IF NOT EXISTS '{app_user}'@'localhost' IDENTIFIED BY :pw"),
            {"pw": app_pw},
        )
        conn.execute(text(f"ALTER USER '{app_user}'@'localhost' IDENTIFIED BY :pw"), {"pw": app_pw})
        conn.execute(text(f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM '{app_user}'@'localhost'"))
        for table, cols in ALLOWED_COLUMNS.items():
            col_list = ", ".join(f"`{c}`" for c in cols)  # column-level: email is NOT granted
            conn.execute(text(f"GRANT SELECT ({col_list}) ON `{db}`.`{table}` TO '{app_user}'@'localhost'"))
        conn.execute(text("FLUSH PRIVILEGES"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="DROP all SafeSQL tables first")
    parser.add_argument("--confirm-database", help="must equal the database name to allow --reset")
    args = parser.parse_args()

    settings = Settings()
    admin_password: str | None = None
    if settings.dialect == "mysql":
        _require_ident(settings.mysql_database, "MYSQL_DATABASE")
        if not settings.mysql_admin_user:
            sys.exit("Set MYSQL_ADMIN_USER in .env (the administrator account name).")
        admin_password = settings.mysql_admin_password.get_secret_value() or getpass.getpass(
            "MySQL admin password (hidden): "
        )
        provision_mysql(settings, admin_password)

    if args.reset:
        target = settings.mysql_database if settings.dialect == "mysql" else "sqlite-test-db"
        if args.confirm_database != target:
            sys.exit(f"--reset deletes all SafeSQL data. Re-run with --confirm-database {target}")
        engine = create_admin_engine(settings, admin_password)
        metadata.drop_all(engine)
        print("Dropped existing SafeSQL tables.")

    engine = create_admin_engine(settings, admin_password)
    metadata.create_all(engine)  # idempotent: only creates missing tables
    inserted = seed(engine)
    if settings.dialect == "mysql":
        grant_select(settings, admin_password or "")
    print("Rows inserted this run:", inserted)
    print("Total rows:", row_counts(engine))
    print("Done. Verify with: python -m scripts.verify_db")


if __name__ == "__main__":
    main()
