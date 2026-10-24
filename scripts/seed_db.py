"""Re-run only the (idempotent) seeding step. Uses the admin connection."""

import getpass

from app.config import Settings
from app.database import create_admin_engine
from app.seed_data import row_counts, seed

if __name__ == "__main__":
    s = Settings()
    pw = None
    if s.dialect == "mysql":
        pw = s.mysql_admin_password.get_secret_value() or getpass.getpass("MySQL admin password (hidden): ")
    eng = create_admin_engine(s, pw)
    print("Inserted:", seed(eng))
    print("Totals:", row_counts(eng))
