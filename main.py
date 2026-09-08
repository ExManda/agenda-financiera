from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List
import sqlite3
import os
import shutil
import calendar
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

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
POSTGRES_URL = os.getenv("POSTGRES_URL") or os.getenv("POSTGRES_PRISMA_URL")
USING_POSTGRES = bool(POSTGRES_URL)


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


def get_db():
    global USING_POSTGRES
    if USING_POSTGRES:
        try:
            return DatabaseConnection(psycopg.connect(POSTGRES_URL, row_factory=dict_row, connect_timeout=5))
        except psycopg.Error:
            USING_POSTGRES = False
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
    init_db()


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
def update_service(service_id: int, item: ServiceCreate):
    conn = get_db()
    existing = conn.execute("SELECT id FROM services WHERE id = ?", (service_id,)).fetchone()
    if existing is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Servicio no encontrado")

    conn.execute("""
        UPDATE services SET
            name = ?, category = ?, kind = ?, owner_user_id = ?, account_id = ?,
            amount = ?, frequency = ?, due_date = ?, is_shared = ?, status = ?,
            reference = ?, notes = ?
        WHERE id = ?
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
        item.notes,
        service_id
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
