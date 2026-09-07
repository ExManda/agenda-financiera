from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List
import sqlite3
import os
from datetime import datetime
from pathlib import Path

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


@app.get("/", include_in_schema=False)
def serve_frontend():
    return FileResponse(BASE_DIR / "index.html")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
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


@app.get("/api/services")
def get_services():
    conn = get_db()
    rows = conn.execute("""
        SELECT s.*, u.name as owner_name, a.name as account_name
        FROM services s
        LEFT JOIN users u ON u.id = s.owner_user_id
        LEFT JOIN accounts a ON a.id = s.account_id
        ORDER BY s.due_date
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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
    last_id = conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
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
def get_dashboard():
    conn = get_db()
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
