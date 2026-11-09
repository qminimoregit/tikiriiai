from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_settings

ROOT = Path(__file__).resolve().parent.parent

_url = get_settings().db_url
if _url.startswith("sqlite"):
    engine = create_engine(_url, connect_args={"check_same_thread": False})
else:
    # MySQL / MariaDB. Connections go stale when the server restarts or times them out. READ COMMITTED (not the
    # default REPEATABLE READ): the app opens several short sessions per call and each must see what the others
    # committed, e.g. a meeting booked a moment ago, as it does on SQLite.
    engine = create_engine(_url, pool_pre_ping=True, pool_recycle=1800, isolation_level="READ COMMITTED")
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db(url: str | None = None) -> None:
    """Bring the schema up to date with Alembic (also creates a fresh database). `url`: another database than DB_URL."""
    from alembic import command
    from alembic.config import Config

    from . import models  # noqa: F401  (register tables)

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url or _url)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
