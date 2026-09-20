"""
database.py -- back up and restore the ChucnyServer data folder.

Every file under the server's saves/ folder, plus the main config JSONs
(gyms.json, settings.json, places.json), gets stored in a single `files`
table as a (path, content) pair. Restoring puts them all back on disk.

Two backends, chosen explicitly or auto-detected:

    MariaDB / MySQL   shared, always-on; needs `pip install mariadb`.
    SQLite            zero-install fallback; one local_backup.db file.

Configuration comes from the environment first, then an optional config.py
(`settings.get`, the original interface), then sane defaults:

    DB_BACKEND        "mariadb" | "sqlite"  (default: auto)
    DB_HOST           default "localhost"
    DB_PORT           default 3306
    DB_NAME           default "chucnyserver"
    DB_USER           default "root"
    DB_PASSWORD       default ""
    DB_SQLITE_FILE    default "<server data dir>/local_backup.db"
    CHUCNY_SERVER_DIR default "<repo>/src/server/data"

Run interactively:

    py database.py

or pass a single command:

    py database.py backup_all
"""

import os
import shutil
import sqlite3
import sys
from pathlib import Path

# ============================================================================
# CONFIG
# ============================================================================

def _setting(key, env=None, default=None):
    """Environment first, then config.py's settings.get, then the default.

    The original file imported `from config import settings`, which doesn't
    exist here -- so a missing config.py used to crash on import. Falling back
    to the environment keeps this usable with or without that module.
    """
    if env:
        raw = os.environ.get(env)
        if raw not in (None, ""):
            return raw
    try:
        from config import settings           # optional, old interface
        value = settings.get(key, default)
    except Exception:
        value = default
    return default if value is None else value


# Where the game server keeps its data: the folder holding settings.json,
# places.json and saves/. The server now lives under src/server, and
# windstock.config.paths puts its files in a `data/` folder there -- so that is
# the default. Override with CHUCNY_SERVER_DIR for any other layout (an old
# /chucnyserver checkout, a packaged build, tests, ...).
_HERE = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = _HERE / "src" / "server" / "data"
BASE_DIR = Path(os.environ.get("CHUCNY_SERVER_DIR") or DEFAULT_DATA_DIR)

MAIN_FILES = ("gyms.json", "settings.json", "places.json")
SAVES_DIRNAME = "saves"


def db_config():
    return {
        "host": _setting("host", "DB_HOST", "localhost"),
        "port": int(_setting("port", "DB_PORT", 3306)),
        "name": _setting("name", "DB_NAME", "chucnyserver"),
        "user": _setting("user", "DB_USER", "root"),
        "password": _setting("password", "DB_PASSWORD", ""),
    }


def sqlite_path():
    path = _setting("sqlite_file", "DB_SQLITE_FILE", None)
    return Path(path) if path else (BASE_DIR / "local_backup.db")


# ============================================================================
# BACKENDS
# ============================================================================

class Backend:
    """Common shape for both databases. Subclasses own the SQL dialect."""

    kind = "?"
    UPSERT = ""
    CREATE = ""

    def __init__(self):
        self._conn = None
        self._cursor = None

    # -- connection ----------------------------------------------------------
    def connect(self):
        if self._conn is None:
            self._conn = self._open()
        return self._conn

    def _open(self):
        raise NotImplementedError

    @property
    def cursor(self):
        if self._cursor is None:
            self._cursor = self.connect().cursor()
        return self._cursor

    def close(self):
        # Close the cursor before the connection, and never let a failure here
        # mask the real error the caller is reporting.
        for closer in (getattr(self._cursor, "close", None),
                       getattr(self._conn, "close", None)):
            if closer is not None:
                try:
                    closer()
                except Exception:
                    pass
        self._cursor = self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- schema --------------------------------------------------------------
    def initialize(self):
        self.cursor.execute(self.CREATE)
        self.commit()

    # -- writes / reads ------------------------------------------------------
    def commit(self):
        self.connect().commit()

    def upsert_many(self, rows):
        # executemany + the dialect's upsert keeps this atomic-ish and one round
        # trip per batch instead of one per file.
        if rows:
            self.cursor.executemany(self.UPSERT, rows)
        self.commit()

    def fetch_rows(self):
        self.cursor.execute("SELECT path, content FROM files")
        return self.cursor.fetchall()

    def fetch_one(self, path):
        self.cursor.execute("SELECT content FROM files WHERE path = ?", (path,))
        row = self.cursor.fetchone()
        return row[0] if row else None

    def count(self):
        self.cursor.execute("SELECT COUNT(*) FROM files")
        row = self.cursor.fetchone()
        return row[0] if row else 0


class MariaDB(Backend):
    kind = "MariaDB"
    CREATE = (
        "CREATE TABLE IF NOT EXISTS files ("
        " path VARCHAR(512) PRIMARY KEY,"
        " content LONGTEXT"
        ") "
        "CHARACTER SET utf8mb4"
    )
    UPSERT = (
        "INSERT INTO files (path, content) VALUES (?, ?) "
        "ON DUPLICATE KEY UPDATE content = VALUES(content)"
    )

    def _open(self):
        try:
            import mariadb
        except ImportError as e:
            raise RuntimeError(
                "the 'mariadb' package is not installed "
                "(pip install mariadb), or use the SQLite backend"
            ) from e
        cfg = db_config()
        return mariadb.connect(
            host=cfg["host"],
            port=cfg["port"],
            user=cfg["user"],
            password=cfg["password"],
            database=cfg["name"],
        )


class SQLite(Backend):
    kind = "SQLite"
    CREATE = (
        "CREATE TABLE IF NOT EXISTS files ("
        " path TEXT PRIMARY KEY,"
        " content TEXT"
        ")"
    )
    UPSERT = (
        "INSERT INTO files (path, content) VALUES (?, ?) "
        "ON CONFLICT(path) DO UPDATE SET content = excluded.content"
    )

    def __init__(self, path=None):
        super().__init__()
        self.path = Path(path) if path else sqlite_path()

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(str(self.path))


# ============================================================================
# BACKEND SELECTION
# ============================================================================

_backend = None


def make_backend(kind):
    kind = (kind or "").strip().lower()
    if kind in ("", "auto"):
        return None
    if kind in ("sqlite", "sqlite3", "local"):
        return SQLite()
    if kind in ("mariadb", "mysql"):
        return MariaDB()
    raise ValueError(f"unknown backend {kind!r} (use 'mariadb' or 'sqlite')")


def get_backend():
    """The active backend. Auto-detects once: MariaDB if reachable, else SQLite."""
    global _backend
    if _backend is not None:
        return _backend
    requested = os.environ.get("DB_BACKEND", "").strip()
    if requested:
        _backend = make_backend(requested)
        print(f"Using {_backend.kind} (from DB_BACKEND).")
        return _backend
    # Auto: prefer MariaDB, but only if it actually connects.
    try:
        candidate = MariaDB()
        candidate.initialize()
        _backend = candidate
        print("Connected to MariaDB.")
        return _backend
    except Exception as e:
        print(f"MariaDB unavailable ({e}); falling back to SQLite.")
        _backend = SQLite()
        return _backend


def use_backend(kind):
    """Switch backend at runtime (the /use command)."""
    global _backend
    if _backend is not None:
        _backend.close()
    _backend = make_backend(kind)
    _backend.initialize()
    print(f"Switched to {_backend.kind}.")
    return _backend


# ============================================================================
# PATH HELPERS
# ============================================================================

def _store_path(path):
    """Path written to the table: POSIX, relative to BASE_DIR."""
    return path.relative_to(BASE_DIR).as_posix()


def _normalize_db_path(db_path):
    """Accept both our relative keys and the old absolute paths.

    Older builds wrote '/chucnyserver/gyms.json' (or a Windows equivalent) as
    the key; restore has to keep understanding those or old backups come back
    empty-handed.
    """
    p = str(db_path).replace("\\", "/")
    for prefix in (BASE_DIR.as_posix().rstrip("/") + "/", "/chucnyserver/"):
        if p.startswith(prefix):
            return p[len(prefix):]
    return p.lstrip("/")


def _read_text(path):
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Not every file in saves/ is guaranteed to be UTF-8 text; skip rather
        # than abort the whole backup.
        print(f"  skipping {path} (not valid UTF-8)")
        return None


def _iter_backup_files():
    """Yield (relative_path, content) for saves/** plus the main JSON files."""
    saves = BASE_DIR / SAVES_DIRNAME
    if saves.is_dir():
        for path in sorted(saves.rglob("*")):
            if path.is_file():
                content = _read_text(path)
                if content is not None:
                    yield _store_path(path), content
    for name in MAIN_FILES:
        path = BASE_DIR / name
        if path.is_file():
            content = _read_text(path)
            if content is not None:
                yield name, content


# ============================================================================
# BACKUP
# ============================================================================

def backup_saves_folder(backend=None):
    backend = backend or get_backend()
    backend.initialize()
    rows = [(p, c) for p, c in _iter_backup_files()
            if p.startswith(SAVES_DIRNAME + "/")]
    backend.upsert_many(rows)
    print(f"Saves folder backed up to {backend.kind} ({len(rows)} files).")


def backup_main_files(backend=None):
    backend = backend or get_backend()
    backend.initialize()
    rows = [(name, content) for name, content in _iter_backup_files()
            if name in MAIN_FILES]
    backend.upsert_many(rows)
    print(f"Main JSON files backed up to {backend.kind} ({len(rows)} files).")


def backup_all(backend=None):
    backend = backend or get_backend()
    backend.initialize()
    rows = list(_iter_backup_files())
    backend.upsert_many(rows)
    print(f"Everything backed up to {backend.kind} ({len(rows)} files).")


# ============================================================================
# RESTORE
# ============================================================================

def _write_file(relative_path, content):
    target = BASE_DIR / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def restore_all(backend=None):
    backend = backend or get_backend()
    rows = backend.fetch_rows()

    saves_folder = BASE_DIR / SAVES_DIRNAME
    # Wipe saves/ FIRST so a file deleted since the backup doesn't linger.
    if saves_folder.exists():
        shutil.rmtree(saves_folder)
    saves_folder.mkdir(parents=True, exist_ok=True)

    saves = 0
    for db_path, content in rows:
        relative = _normalize_db_path(db_path)
        if not relative.startswith(SAVES_DIRNAME + "/"):
            continue
        _write_file(relative, content)
        saves += 1

    for name in MAIN_FILES:
        content = backend.fetch_one(name)
        if content is None:
            # Legacy key (absolute path) from an older backup.
            content = backend.fetch_one(str(BASE_DIR / name))
        if content is None:
            print(f"{name} was not found in {backend.kind} -- skipped.")
            continue
        _write_file(name, content)
        print(f"{name} imported!")

    print(f"Everything imported from {backend.kind} ({saves} save files).")


# ============================================================================
# INTERACTIVE INTERFACE
# ============================================================================

HELP_MESSAGE = """Commands:
  /help                       show this message
  /init                       create the files table on the active backend
  /use mariadb | sqlite       switch backend

  MariaDB / MySQL (shared):
    /backup_all               back up saves/ + the main JSONs
    /backup_main_files        back up only gyms/settings/places
    /import                   restore from the active backend

  SQLite (local_backup.db):
    /backup_all_sqlite        back up everything to SQLite
    /backup_main_files_sqlite back up only the main JSONs to SQLite
    /import_sqlite            restore from SQLite

  /quit                       leave"""


def command(line):
    """Run one command. Returns False when the caller should exit."""
    cmd = (line or "").strip()
    if not cmd:
        return True
    try:
        if cmd in ("/quit", "/exit", "/q"):
            return False
        if cmd == "/help":
            print(HELP_MESSAGE)
        elif cmd == "/init":
            get_backend().initialize()
            print(f"{get_backend().kind} ready.")
        elif cmd.startswith("/use"):
            target = cmd[4:].strip()
            if not target:
                print("Usage: /use mariadb | sqlite")
            else:
                use_backend(target)
        elif cmd == "/import":
            restore_all()
        elif cmd == "/backup_all":
            backup_all()
        elif cmd == "/backup_main_files":
            backup_main_files()
        elif cmd == "/import_sqlite":
            restore_all(SQLite())
        elif cmd == "/backup_all_sqlite":
            backup_all(SQLite())
        elif cmd == "/backup_main_files_sqlite":
            backup_main_files(SQLite())
        else:
            print("Unknown command, type /help for help")
    except Exception as e:
        print(f"Error occurred: {e}")
    return True


def db_interface():
    print(f"ChucnyServer backup console. Data dir: {BASE_DIR}")
    print("Type /help for commands, /quit to leave.")
    running = True
    while running:
        try:
            running = command(input("ChucnyServer> "))
        except (EOFError, KeyboardInterrupt):
            print()
            break


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        # Non-interactive: `py database.py backup_all`
        command(argv[0])
        return
    db_interface()


if __name__ == "__main__":
    main()
