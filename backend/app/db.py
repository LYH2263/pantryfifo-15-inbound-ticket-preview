import os, sqlite3
from contextlib import contextmanager
from pathlib import Path

def db_path() -> Path:
    d = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "pantryfifo.db"

def connect():
    c = sqlite3.connect(db_path())
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=5000")
    return c

@contextmanager
def write_tx(c):
    """One generation per write.

    BEGIN IMMEDIATE takes the DB write lock up front, so a confirm and an
    expire-sweep/consume running at the same time serialize instead of
    interleaving: each writer sees (and publishes) a complete generation,
    never half of the other's.
    """
    c.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        c.execute("ROLLBACK")
        raise
    c.execute("COMMIT")
