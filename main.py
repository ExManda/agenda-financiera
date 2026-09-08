from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from typing import Optional, List
import sqlite3
import os
import shutil
import calendar
from datetime import datetime, timedelta
from pathlib import Path

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None

app = FastAPI(title="Agenda Financiera Personal")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_NAME = "agenda_financiera.db"
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path("/tmp") / DB_NAME if os.getenv("VERCEL") else BASE_DIR / DB_NAME
DB_SEED_MARKER = DB_PATH.with_suffix(".seed")
DB_SEED_VERSION = os.getenv("VERCEL_GIT_COMMIT_SHA", "initial")
POSTGRES_URLS = [value for value in (
    os.getenv("POSTGRES_URL"),
    os.getenv("POSTGRES_URL_NON_POOLING"),
    os.getenv("POSTGRES_PRISMA_URL"),
) if value]
POSTGRES_URL = POSTGRES_URLS[0] if POSTGRES_URLS else None
USING_POSTGRES = bool(POSTGRES_URLS and psycopg)


class DatabaseConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, query, params=()):
        if USING_POSTGRES:
            query = query.replace("?", "%s")
        return self.connection.execute(query, params)

    def commit(self):
        self.connection.commit()

    def close(self):
        self.connection.close()


@app.get("/", include_in_schema=False)
def serve_frontend():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/api/health")
def health():
    postgres_connection = False
    postgres_error = None
    if POSTGRES_URLS and psycopg:
        for database_url in POSTGRES_URLS:
            try:
                connection = psycopg.connect(database_url, connect_timeout=5)
                connection.close()
                postgres_connection = True
                break
            except Exception as error:
                postgres_error = type(error).__name__
    return {
        "status": "ok",
        "storage": "postgres" if postgres_connection else "sqlite",
        "persistent_storage_configured": bool(POSTGRES_URL),
        "postgres_connection": postgres_connection,
        "postgres_error_type": postgres_error,
    }


def get_db():
    global USING_POSTGRES
    if os.getenv("VERCEL") and not POSTGRES_URL:
        raise RuntimeError("POSTGRES_URL no está configurada en Vercel")
    if USING_POSTGRES:
        for database_url in POSTGRES_URLS:
            try:
                return DatabaseConnection(psycopg.connect(database_url, row_factory=dict_row, connect_timeout=5))
            except psycopg.Error:
                continue
        USING_POSTGRES = False
        if os.getenv("VERCEL"):
            raise RuntimeError("No se pudo conectar con Supabase PostgreSQL")
    needs_seed = not DB_PATH.exists() or not DB_SEED_MARKER.exists()
    if not needs_seed:
        needs_seed = DB_SEED_MARKER.read_text() != DB_SEED_VERSION
    if os.getenv("VERCEL") and needs_seed:
        shutil.copyfile(BASE_DIR / DB_NAME, DB_PATH)
        DB_SEED_MARKER.write_text(DB_SEED_VERSION)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return DatabaseConnection(conn)


def execute(conn, query, params=()):
    if USING_POSTGRES:
        query = query.replace("?", "%s")
    return conn.execute(query, params)


def init_db():
    conn = get_db()
    if USING_POSTGRES:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS monthly_service_overrides (
                service_id BIGINT NOT NULL,
                month TEXT NOT NULL,
                kind TEXT NOT NULL,
                owner_user_id BIGINT NOT NULL,
                account_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                amount NUMERIC(12,2) NOT NULL,
                frequency TEXT NOT NULL,
                due_date DATE NOT NULL,
                is_shared BOOLEAN NOT NULL DEFAULT FALSE,
                status TEXT NOT NULL DEFAULT 'pending',
                reference TEXT,
                notes TEXT,
                PRIMARY KEY (service_id, month)
            )
        """)
        for column, definition in (("kind", "TEXT NOT NULL DEFAULT 'service'"), ("owner_user_id", "BIGINT NOT NULL DEFAULT 1"), ("account_id", "BIGINT NOT NULL DEFAULT 1")):
            try:
                conn.execute(f"ALTER TABLE monthly_service_overrides ADD COLUMN {column} {definition}")
            except Exception:
                pass
        conn.commit()
        conn.close()
        return
    else:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT,
            role TEXT DEFAULT 'user'
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            owner_user_id INTEGER
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            kind TEXT NOT NULL,
            owner_user_id INTEGER,
            account_id INTEGER,
            amount REAL NOT NULL,
            frequency TEXT NOT NULL,
            due_date TEXT NOT NULL,
            is_shared INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            reference TEXT,
            notes TEXT
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            payment_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'paid'
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS monthly_service_overrides (
            service_id INTEGER NOT NULL,
            month TEXT NOT NULL,
            kind TEXT NOT NULL,
            owner_user_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            frequency TEXT NOT NULL,
            due_date TEXT NOT NULL,
            is_shared INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            reference TEXT,
            notes TEXT,
            PRIMARY KEY (service_id, month)
        )
        """)
        for column, definition in (("kind", "TEXT NOT NULL DEFAULT 'service'"), ("owner_user_id", "INTEGER NOT NULL DEFAULT 1"), ("account_id", "INTEGER NOT NULL DEFAULT 1")):
            try:
                conn.execute(f"ALTER TABLE monthly_service_overrides ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError:
                pass

    if USING_POSTGRES:
        seed_postgres(conn)
        conn.commit()
        conn.close()
        return

    existing_users = conn.execute("SELECT COUNT(*) as count FROM users").fetchone()["count"]
    if existing_users == 0:
        conn.execute("INSERT INTO users (name, email, role) VALUES (?, ?, ?)", ("Exequiel", "exequiel@test.com", "owner"))
        conn.execute("INSERT INTO users (name, email, role) VALUES (?, ?, ?)", ("Cecilia", "cecilia@test.com", "owner"))

    existing_accounts = conn.execute("SELECT COUNT(*) as count FROM accounts").fetchone()["count"]
    if existing_accounts == 0:
        conn.execute("INSERT INTO accounts (name, type, owner_user_id) VALUES (?, ?, ?)", ("Exequiel", "personal", 1))
        conn.execute("INSERT INTO accounts (name, type, owner_user_id) VALUES (?, ?, ?)", ("Cecilia", "personal", 2))
        conn.execute("INSERT INTO accounts (name, type, owner_user_id) VALUES (?, ?, ?)", ("Hogar", "shared", None))

    existing_services = conn.execute("SELECT COUNT(*) as count FROM services").fetchone()["count"]
    if existing_services == 0:
        conn.execute("""
            INSERT INTO services (
                name, category, kind, owner_user_id, account_id, amount, frequency,
                due_date, is_shared, status, reference, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "Naturgy",
            "Gas",
            "service",
            1,
            3,
            31436.48,
            "mensual",
            "2026-09-10",
            1,
            "pending",
            "N 6535511",
            "Servicio compartido del hogar"
        ))

    conn.commit()
    conn.close()


def seed_postgres(conn):
    if execute(conn, "SELECT COUNT(*) AS count FROM users").fetchone()["count"]:
        return
    seed = sqlite3.connect(BASE_DIR / DB_NAME)
    seed.row_factory = sqlite3.Row
    for table in ("users", "accounts", "services", "payments"):
        rows = seed.execute(f"SELECT * FROM {table}").fetchall()
        if not rows:
            continue
        columns = rows[0].keys()
        names = ", ".join(columns)
        placeholders = ", ".join(["%s"] * len(columns))
        for row in rows:
            execute(conn, f"INSERT INTO {table} ({names}) VALUES ({placeholders})", tuple(row))
        execute(conn, f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE((SELECT MAX(id) FROM {table}), 1), true)")
    seed.close()


class ServiceCreate(BaseModel):
    name: str
    category: str
    kind: str = "service"
    owner_user_id: int
    account_id: int
    amount: float
    frequency: str
    due_date: str
    is_shared: bool = False
    reference: Optional[str] = None
    notes: Optional[str] = None
    status: str = "pending"


class MonthlyStatus(BaseModel):
    month: str
    paid: bool


@app.on_event("startup")
def startup_event():
    if not (os.getenv("VERCEL") and not POSTGRES_URL):
        try:
            init_db()
        except RuntimeError:
            pass


@app.get("/api/users")
def get_users():
    conn = get_db()
    rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/accounts")
def get_accounts():
    conn = get_db()
    rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def month_services(conn, month):
    try:
        selected_month = datetime.strptime(month, "%Y-%m")
    except ValueError:
        raise HTTPException(status_code=400, detail="El mes debe tener formato YYYY-MM")

    month_start = selected_month.strftime("%Y-%m")
    payment_month = "to_char(p.payment_date, 'YYYY-MM')" if USING_POSTGRES else "strftime('%Y-%m', p.payment_date)"
    rows = conn.execute(f"""
        SELECT s.*, u.name as owner_name, a.name as account_name,
               EXISTS(
                   SELECT 1 FROM payments p
                   WHERE p.service_id = s.id
                     AND {payment_month} = ?
                     AND p.status = 'paid'
               ) as month_paid
        FROM services s
        LEFT JOIN users u ON u.id = s.owner_user_id
        LEFT JOIN accounts a ON a.id = s.account_id
        ORDER BY s.due_date
    """, (month_start,)).fetchall()

    result = []
    last_day = calendar.monthrange(selected_month.year, selected_month.month)[1]
    for row in rows:
        item = dict(row)
        override = conn.execute("SELECT kind, owner_user_id, account_id, name, category, amount, frequency, due_date, is_shared, status, reference, notes FROM monthly_service_overrides WHERE service_id = ? AND month = ?", (item["id"], month_start)).fetchone()
        if override:
            item.update(dict(override))
        due_date = item["due_date"]
        if hasattr(due_date, "strftime"):
            original_due = due_date
        else:
            original_due = datetime.strptime(due_date, "%Y-%m-%d")
        if item["frequency"] == "anual" and original_due.month != selected_month.month:
            continue
        if item["frequency"] == "unico" and original_due.strftime("%Y-%m") != month_start:
            continue
        due_day = min(original_due.day, last_day)
        item["due_date"] = f"{selected_month.year:04d}-{selected_month.month:02d}-{due_day:02d}"
        item["due_weekday"] = original_due.weekday()
        item["status"] = "paid" if item["month_paid"] or item["status"] == "paid" else "pending"
        item["month"] = month_start
        result.append(item)
    return result


@app.get("/api/services")
def get_services(month: Optional[str] = None):
    conn = get_db()
    if month:
        result = month_services(conn, month)
        conn.close()
        return result
    rows = conn.execute("SELECT s.*, u.name as owner_name, a.name as account_name FROM services s LEFT JOIN users u ON u.id = s.owner_user_id LEFT JOIN accounts a ON a.id = s.account_id ORDER BY s.due_date").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def ics_escape(value):
    return str(value or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold_ics_line(line):
    return [line[index:index + 72] if index == 0 else " " + line[index:index + 71] for index in range(0, len(line), 72)]


@app.get("/api/calendar.ics")
def download_calendar(month: Optional[str] = None):
    selected_month = month or datetime.now().strftime("%Y-%m")
    conn = get_db()
    events = []
    for service in month_services(conn, selected_month):
            due = datetime.strptime(service["due_date"], "%Y-%m-%d")
            description = f"Vencimiento: {service['category']} - {money_text(service['amount'])}"
            dates = [due]
            if service["frequency"] == "semanal":
                year, month = due.year, due.month
                dates = [
                    datetime(year, month, day_number)
                    for day_number in range(1, calendar.monthrange(year, month)[1] + 1)
                    if datetime(year, month, day_number).weekday() == service["due_weekday"]
                ]
            for occurrence in dates:
                events.extend([
                    "BEGIN:VEVENT",
                    f"UID:{service['id']}-{occurrence:%Y%m%d}@agenda-financiera",
                    f"DTSTAMP:{datetime.utcnow():%Y%m%dT%H%M%SZ}",
                    f"DTSTART;VALUE=DATE:{occurrence:%Y%m%d}",
                    f"DTEND;VALUE=DATE:{(occurrence + timedelta(days=1)):%Y%m%d}",
                    f"SUMMARY:{ics_escape(service['name'])}",
                    f"DESCRIPTION:{ics_escape(description)}",
                    "BEGIN:VALARM",
                    "TRIGGER:-P1D",
                    "ACTION:DISPLAY",
                    f"DESCRIPTION:Vence mañana: {ics_escape(service['name'])}",
                    "END:VALARM",
                    "END:VEVENT",
                ])
    conn.close()
    calendar_lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Agenda Financiera//ES",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:Agenda Financiera",
        "X-WR-CALDESC:Vencimientos de gastos fijos", "X-WR-TIMEZONE:America/Argentina/Buenos_Aires",
        "REFRESH-INTERVAL;VALUE=DURATION:P1D", "X-PUBLISHED-TTL:P1D", *events, "END:VCALENDAR", ""
    ]
    calendar_feed = "\r\n".join(line for item in calendar_lines for line in fold_ics_line(item))
    filename = f"agenda-{selected_month}.ics"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    headers["Cache-Control"] = "no-store, max-age=0"
    return Response(content=calendar_feed, media_type="text/calendar", headers=headers)


def money_text(value):
    return f"$ {float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


@app.put("/api/services/{service_id}/monthly-status")
def update_monthly_status(service_id: int, item: MonthlyStatus):
    try:
        datetime.strptime(item.month, "%Y-%m")
    except ValueError:
        raise HTTPException(status_code=400, detail="El mes debe tener formato YYYY-MM")
    conn = get_db()
    if conn.execute("SELECT id FROM services WHERE id = ?", (service_id,)).fetchone() is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    payment_month = "to_char(payment_date, 'YYYY-MM')" if USING_POSTGRES else "strftime('%Y-%m', payment_date)"
    conn.execute(f"DELETE FROM payments WHERE service_id = ? AND {payment_month} = ?", (service_id, item.month))
    if item.paid:
        conn.execute("INSERT INTO payments (service_id, user_id, amount, payment_date, status) SELECT id, owner_user_id, amount, ?, 'paid' FROM services WHERE id = ?", (f"{item.month}-01", service_id))
    conn.commit()
    conn.close()
    return {"message": "Estado mensual actualizado"}


@app.post("/api/services")
def create_service(item: ServiceCreate):
    conn = get_db()
    conn.execute("""
        INSERT INTO services (
            name, category, kind, owner_user_id, account_id, amount, frequency,
            due_date, is_shared, status, reference, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        item.name,
        item.category,
        item.kind,
        item.owner_user_id,
        item.account_id,
        item.amount,
        item.frequency,
        item.due_date,
        1 if item.is_shared else 0,
        item.status,
        item.reference,
        item.notes
    ))
    conn.commit()
    last_id_query = "SELECT lastval() as id" if USING_POSTGRES else "SELECT last_insert_rowid() as id"
    last_id = conn.execute(last_id_query).fetchone()["id"]
    conn.close()
    return {"id": last_id, "message": "Servicio creado"}


@app.put("/api/services/{service_id}")
def update_service(service_id: int, item: ServiceCreate, month: Optional[str] = None):
    conn = get_db()
    existing = conn.execute("SELECT id FROM services WHERE id = ?", (service_id,)).fetchone()
    if existing is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Servicio no encontrado")

    if month:
        try:
            datetime.strptime(month, "%Y-%m")
        except ValueError:
            conn.close()
            raise HTTPException(status_code=400, detail="El mes debe tener formato YYYY-MM")
        conn.execute("DELETE FROM monthly_service_overrides WHERE service_id = ? AND month = ?", (service_id, month))
        conn.execute("""
            INSERT INTO monthly_service_overrides (
                service_id, month, kind, owner_user_id, account_id, name, category, amount, frequency, due_date,
                is_shared, status, reference, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            service_id, month, item.kind, item.owner_user_id, item.account_id, item.name, item.category, item.amount, item.frequency,
            item.due_date, 1 if item.is_shared else 0, item.status, item.reference, item.notes
        ))
    else:
        conn.execute("""
        UPDATE services SET
            name = ?, category = ?, kind = ?, owner_user_id = ?, account_id = ?,
            amount = ?, frequency = ?, due_date = ?, is_shared = ?, status = ?,
            reference = ?, notes = ?
        WHERE id = ?
        """, (
            item.name, item.category, item.kind, item.owner_user_id, item.account_id,
            item.amount, item.frequency, item.due_date, 1 if item.is_shared else 0,
            item.status, item.reference, item.notes, service_id
        ))
    conn.commit()
    conn.close()
    return {"id": service_id, "message": "Servicio actualizado"}


@app.get("/api/dashboard")
def get_dashboard(month: Optional[str] = None):
    conn = get_db()
    if month:
        services = month_services(conn, month)
        conn.close()
        next_due = sorted((item for item in services if item["status"] == "pending"), key=lambda item: item["due_date"])[:5]
        return {
            "month": month,
            "total_services": len(services),
            "total_amount": round(sum(float(item["amount"]) for item in services), 2),
            "shared_total": round(sum(float(item["amount"]) for item in services if item["is_shared"]), 2),
            "personal_total": round(sum(float(item["amount"]) for item in services if not item["is_shared"]), 2),
            "paid_services": sum(1 for item in services if item["status"] == "paid"),
            "next_due": next_due
        }
    total_services = conn.execute("SELECT COUNT(*) as total FROM services").fetchone()["total"]
    total_amount = conn.execute("SELECT COALESCE(SUM(amount), 0) as total FROM services").fetchone()["total"]

    shared_total = conn.execute("""
        SELECT COALESCE(SUM(amount), 0) as total
        FROM services
        WHERE is_shared = 1
    """).fetchone()["total"]

    personal_total = conn.execute("""
        SELECT COALESCE(SUM(amount), 0) as total
        FROM services
        WHERE is_shared = 0
    """).fetchone()["total"]

    next_due = conn.execute("""
        SELECT name, due_date, amount
        FROM services
        WHERE status = 'pending'
        ORDER BY due_date ASC
        LIMIT 5
    """).fetchall()

    conn.close()

    return {
        "total_services": total_services,
        "total_amount": round(float(total_amount), 2),
        "shared_total": round(float(shared_total), 2),
        "personal_total": round(float(personal_total), 2),
        "next_due": [dict(r) for r in next_due]
    }
