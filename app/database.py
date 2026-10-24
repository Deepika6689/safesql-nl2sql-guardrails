"""Engine factories. The API only ever uses the restricted application engine."""

import sqlite3

from sqlalchemy import Engine, create_engine, event

from app.config import Settings


def create_app_engine(settings: Settings) -> Engine:
    """Engine for the SELECT-only application account (read-only session where supported)."""
    url = settings.app_url()
    if settings.dialect == "sqlite":
        engine = create_engine(url)

        @event.listens_for(engine, "connect")
        def _sqlite_readonly(dbapi_conn: sqlite3.Connection, _rec) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA query_only=ON")  # defence in depth: refuse writes
            cur.close()

        return engine

    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={"connect_timeout": 5, "read_timeout": 15, "write_timeout": 15},
    )

    @event.listens_for(engine, "connect")
    def _mysql_readonly(dbapi_conn, _rec) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("SET SESSION TRANSACTION READ ONLY")
        cur.close()

    return engine


def create_admin_engine(settings: Settings, password: str | None = None) -> Engine:
    """Privileged engine. Used ONLY by scripts/init_db.py and scripts/seed_db.py."""
    url = settings.admin_url(password=password)
    engine = create_engine(url)
    if settings.dialect == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_fk(dbapi_conn, _rec) -> None:
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine
