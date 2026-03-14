"""
database.py — SQLite structured data storage skill for TrinityClaw.
Databases are stored in /app/memory/databases/*.db (persisted via volume mount).
All writes use parameterized queries — no SQL injection risk.
"""
import sqlite3
import json
import re
from pathlib import Path

NAME = "database"
DOC = (
    "SQLite structured data storage. Databases live in /app/memory/databases/. "
    "Functions: "
    "create_table(db, table, columns)→define a table; "
    "insert(db, table, data_json)→add a row; "
    "upsert(db, table, data_json, key_col)→insert or replace; "
    "select(db, sql, params_json?)→run SELECT query; "
    "update(db, table, set_json, where_json)→update rows; "
    "delete_rows(db, table, where_json)→remove rows; "
    "count(db, table, where_json?)→count rows; "
    "list_databases()→show all .db files; "
    "list_tables(db)→show tables in a db; "
    "describe(db, table)→show column schema; "
    "drop_table(db, table)→delete a table; "
    "export_csv(db, table)→dump table as CSV."
)

_DB_DIR = Path("/app/memory/databases")
_IDENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _safe_ident(name):
    """Validate a table or column name — letters, digits, underscores only."""
    if not _IDENT_RE.match(name):
        raise ValueError(
            f"Invalid identifier '{name}': only letters, digits, and underscores are allowed."
        )
    return name


def _db_path(db):
    """Resolve database file path; always inside _DB_DIR."""
    _DB_DIR.mkdir(parents=True, exist_ok=True)
    name = Path(db).name  # strip any directory components
    if not name.endswith(".db"):
        name += ".db"
    return _DB_DIR / name


def _connect(db):
    """Open a SQLite connection with WAL mode and dict-like rows."""
    conn = sqlite3.connect(str(_db_path(db)))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _parse(s):
    """Parse a JSON string, or pass through if already a dict/list."""
    if s is None:
        return None
    if isinstance(s, (dict, list)):
        return s
    return json.loads(s)


# ── DDL ───────────────────────────────────────────────────────────────────────

def create_table(db, table, columns):
    """
    Create a table if it doesn't already exist.

    db      — database name (e.g. "budget")
    table   — table name (e.g. "expenses")
    columns — SQL column definitions, comma-separated:
              "id INTEGER PRIMARY KEY AUTOINCREMENT, item TEXT NOT NULL, amount REAL, date TEXT"

    Example:
      create_table("budget", "expenses",
                   "id INTEGER PRIMARY KEY AUTOINCREMENT, item TEXT, amount REAL, date TEXT")
    """
    try:
        _safe_ident(table)
        sql = f"CREATE TABLE IF NOT EXISTS {table} ({columns})"
        with _connect(db) as conn:
            conn.execute(sql)
        return f"✅ Table '{table}' ready in {db}.db"
    except Exception as e:
        return f"❌ Error: {e}"


def drop_table(db, table):
    """Drop a table and all its data permanently."""
    try:
        _safe_ident(table)
        with _connect(db) as conn:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        return f"✅ Table '{table}' dropped from {db}.db"
    except Exception as e:
        return f"❌ Error: {e}"


# ── WRITE OPS ─────────────────────────────────────────────────────────────────

def insert(db, table, data_json):
    """
    Insert one row into a table.

    data_json — JSON object mapping column names to values:
                '{"item": "Coffee", "amount": 4.50, "date": "2026-03-13"}'

    Returns the new row's id.
    """
    try:
        _safe_ident(table)
        data = _parse(data_json)
        if not isinstance(data, dict):
            return "❌ data_json must be a JSON object {column: value, ...}"
        cols = [_safe_ident(k) for k in data]
        placeholders = ",".join("?" * len(cols))
        sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
        with _connect(db) as conn:
            cur = conn.execute(sql, list(data.values()))
            return f"✅ Inserted row id={cur.lastrowid} into '{table}'"
    except Exception as e:
        return f"❌ Error: {e}"


def upsert(db, table, data_json, key_col):
    """
    Insert a row or replace it if the key already exists (INSERT OR REPLACE).

    key_col must be a PRIMARY KEY or UNIQUE column in the schema.

    Example:
      upsert("settings", "prefs", '{"key": "theme", "value": "dark"}', "key")
    """
    try:
        _safe_ident(table)
        _safe_ident(key_col)
        data = _parse(data_json)
        if not isinstance(data, dict):
            return "❌ data_json must be a JSON object"
        cols = [_safe_ident(k) for k in data]
        placeholders = ",".join("?" * len(cols))
        sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
        with _connect(db) as conn:
            cur = conn.execute(sql, list(data.values()))
            return f"✅ Upserted row id={cur.lastrowid} in '{table}'"
    except Exception as e:
        return f"❌ Error: {e}"


def update(db, table, set_json, where_json):
    """
    Update rows in a table.

    set_json   — columns and new values: '{"amount": 5.00, "item": "Latte"}'
    where_json — filter conditions (AND'd): '{"id": 3}'

    Example:
      update("budget", "expenses", '{"amount": 5.00}', '{"id": 3}')
    """
    try:
        _safe_ident(table)
        set_data = _parse(set_json)
        where_data = _parse(where_json)
        if not isinstance(set_data, dict) or not isinstance(where_data, dict):
            return "❌ Both set_json and where_json must be JSON objects"
        if not where_data:
            return "❌ where_json cannot be empty — this would update every row. Add a condition."
        set_clause = ", ".join(f"{_safe_ident(k)}=?" for k in set_data)
        where_clause = " AND ".join(f"{_safe_ident(k)}=?" for k in where_data)
        sql = f"UPDATE {table} SET {set_clause} WHERE {where_clause}"
        params = list(set_data.values()) + list(where_data.values())
        with _connect(db) as conn:
            cur = conn.execute(sql, params)
            return f"✅ Updated {cur.rowcount} row(s) in '{table}'"
    except Exception as e:
        return f"❌ Error: {e}"


def delete_rows(db, table, where_json):
    """
    Delete rows matching the given conditions.

    where_json — filter conditions (AND'd): '{"id": 3}'
                 Use '{"_all": 1}' only if you intentionally want to wipe all rows.

    Example:
      delete_rows("budget", "expenses", '{"id": 5}')
    """
    try:
        _safe_ident(table)
        where_data = _parse(where_json)
        if not isinstance(where_data, dict):
            return "❌ where_json must be a JSON object"
        # Safety: require explicit intent to delete all rows
        if not where_data:
            return "❌ where_json cannot be empty. To delete all rows pass '{\"_all\": 1}'."
        if "_all" in where_data:
            sql = f"DELETE FROM {table}"
            params = []
        else:
            where_clause = " AND ".join(f"{_safe_ident(k)}=?" for k in where_data)
            sql = f"DELETE FROM {table} WHERE {where_clause}"
            params = list(where_data.values())
        with _connect(db) as conn:
            cur = conn.execute(sql, params)
            return f"✅ Deleted {cur.rowcount} row(s) from '{table}'"
    except Exception as e:
        return f"❌ Error: {e}"


# ── READ OPS ──────────────────────────────────────────────────────────────────

def select(db, sql, params_json=None):
    """
    Run a SELECT query and return results as a formatted table.

    sql        — a SELECT statement; use ? placeholders for parameters
    params_json — optional JSON array of values: '[3.0, "coffee"]'

    Example:
      select("budget", "SELECT * FROM expenses WHERE amount > ? ORDER BY date", "[3.0]")
      select("budget", "SELECT item, SUM(amount) as total FROM expenses GROUP BY item")
    """
    try:
        if not sql.strip().upper().startswith("SELECT"):
            return "❌ Only SELECT queries allowed here. Use insert/update/delete_rows for writes."
        params = _parse(params_json) or []
        if not isinstance(params, list):
            params = [params]
        with _connect(db) as conn:
            cur = conn.execute(sql, params)
            rows = cur.fetchmany(500)
            if not rows:
                return "📭 No rows returned"
            cols = [d[0] for d in cur.description]
            rows_as_dicts = [dict(zip(cols, row)) for row in rows]
            # Build aligned text table
            col_widths = {
                c: max(len(c), max(len(str(r[c])) for r in rows_as_dicts))
                for c in cols
            }
            header = " | ".join(c.ljust(col_widths[c]) for c in cols)
            sep    = "-+-".join("-" * col_widths[c] for c in cols)
            body   = "\n".join(
                " | ".join(str(r[c]).ljust(col_widths[c]) for c in cols)
                for r in rows_as_dicts
            )
            note = f"\n({len(rows)} row(s)" + (", limited to 500)" if len(rows) == 500 else ")")
            return f"{header}\n{sep}\n{body}{note}"
    except Exception as e:
        return f"❌ Error: {e}"


def count(db, table, where_json=None):
    """
    Count rows in a table, with optional filter.

    where_json — optional JSON conditions: '{"status": "active"}'

    Example:
      count("budget", "expenses")
      count("budget", "expenses", '{"date": "2026-03-13"}')
    """
    try:
        _safe_ident(table)
        where_data = _parse(where_json) if where_json else {}
        if where_data:
            where_clause = " AND ".join(f"{_safe_ident(k)}=?" for k in where_data)
            sql = f"SELECT COUNT(*) FROM {table} WHERE {where_clause}"
            params = list(where_data.values())
        else:
            sql = f"SELECT COUNT(*) FROM {table}"
            params = []
        with _connect(db) as conn:
            n = conn.execute(sql, params).fetchone()[0]
        suffix = f" where {where_json}" if where_data else ""
        return f"{n} row(s) in '{table}'{suffix}"
    except Exception as e:
        return f"❌ Error: {e}"


# ── META ──────────────────────────────────────────────────────────────────────

def list_databases():
    """List all SQLite databases stored in /app/memory/databases/."""
    _DB_DIR.mkdir(parents=True, exist_ok=True)
    dbs = sorted(_DB_DIR.glob("*.db"))
    if not dbs:
        return "📭 No databases yet. Use create_table('mydb', 'mytable', '...') to create one."
    lines = ["📦 Databases:"]
    for db in dbs:
        size_kb = db.stat().st_size / 1024
        lines.append(f"  {db.stem}.db  —  {size_kb:.1f} KB")
    return "\n".join(lines)


def list_tables(db):
    """List all tables in a database."""
    try:
        with _connect(db) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        if not rows:
            return f"📭 No tables in {db}.db yet"
        return f"Tables in {db}.db: " + ", ".join(r[0] for r in rows)
    except Exception as e:
        return f"❌ Error: {e}"


def describe(db, table):
    """Show the column schema of a table (names, types, constraints)."""
    try:
        _safe_ident(table)
        with _connect(db) as conn:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not rows:
            return f"❌ Table '{table}' not found in {db}.db"
        lines = [f"Schema of '{table}' in {db}.db:"]
        for r in rows:
            pk      = " PRIMARY KEY" if r["pk"] else ""
            notnull = " NOT NULL"    if r["notnull"] else ""
            default = f" DEFAULT {r['dflt_value']}" if r["dflt_value"] else ""
            lines.append(f"  {r['name']}  {r['type']}{pk}{notnull}{default}")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Error: {e}"


def export_csv(db, table):
    """Export a table's contents as a CSV string (max 1000 rows)."""
    try:
        _safe_ident(table)
        with _connect(db) as conn:
            cur = conn.execute(f"SELECT * FROM {table} LIMIT 1000")
            rows = cur.fetchall()
        if not rows:
            return f"📭 Table '{table}' is empty"
        cols = [d[0] for d in cur.description]
        lines = [",".join(cols)]
        for row in rows:
            lines.append(",".join(
                f'"{str(v)}"' if ("," in str(v) or "\n" in str(v)) else str(v)
                for v in row
            ))
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Error: {e}"
