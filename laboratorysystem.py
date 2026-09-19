import os
import sys
import csv
import re
import sqlite3
import logging
import psycopg
from psycopg import sql
try:
    import tkinter as tk
    from tkinter import messagebox, ttk
    TKINTER_AVAILABLE = True
except ImportError:
    tk = None
    messagebox = None
    ttk = None
    TKINTER_AVAILABLE = False

if not TKINTER_AVAILABLE:
    class _DummyTk:
        class Frame:
            pass
        class Tk:
            pass

    tk = _DummyTk()

import bcrypt
from pydantic import BaseModel, Field, ValidationError, field_validator
from dotenv import load_dotenv

load_dotenv()

SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL")

# 1. AUDIT LOGGING SETUP

def setup_logger():
    log_dir = "app_logging"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    logging.basicConfig(
        filename=os.path.join(log_dir, "app.log"),
        level=logging.INFO,
        format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    return logging.getLogger("InventoryApp")

logger = setup_logger()

# 2. DATABASE INITIALIZATION

DB_NAME = "hardware_inventory.db"

# Supabase PostgreSQL connection used for automatic SQLite -> Supabase syncing.
SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL", "").strip()

SYNC_TABLES = [
    "users", "hardware", "borrow_requests", "item_requests",
    "item_holds", "borrow_transactions", "password_resets", "return_requests"
]

def sync_sqlite_to_supabase():
    """Mirror committed local SQLite data to Supabase. SQLite remains the source of truth."""
    if not SUPABASE_DB_URL:
        logger.warning("SUPABASE_DB_URL is not set; automatic Supabase sync skipped.")
        return False

    sqlite_conn = None
    pg_conn = None
    try:
        sqlite_conn = sqlite3.connect(DB_NAME)
        sqlite_cur = sqlite_conn.cursor()
        pg_conn = psycopg.connect(SUPABASE_DB_URL, sslmode="require")
        pg_cur = pg_conn.cursor()

        # Child tables first for deletes, so FK relationships are less likely to block cleanup.
        tables_for_sync = [
            "borrow_transactions", "return_requests", "borrow_requests",
            "item_holds", "item_requests", "password_resets", "hardware", "users"
        ]

        for table_name in tables_for_sync:
            sqlite_cur.execute(f"PRAGMA table_info('{table_name}')")
            columns_info = sqlite_cur.fetchall()
            if not columns_info:
                continue

            columns = [c[1] for c in columns_info]
            pk_columns = [c[1] for c in columns_info if c[5]]
            if not pk_columns:
                continue

            pg_cur.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = %s)",
                (table_name,)
            )
            if not pg_cur.fetchone()[0]:
                logger.warning("Supabase table '%s' does not exist; skipping it.", table_name)
                continue

            sqlite_cur.execute(f"SELECT * FROM {table_name}")
            rows = sqlite_cur.fetchall()

            col_sql = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
            placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in columns)
            pk_sql = sql.SQL(", ").join(sql.Identifier(c) for c in pk_columns)
            updates = [c for c in columns if c not in pk_columns]

            if updates:
                update_sql = sql.SQL(", ").join(
                    sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c))
                    for c in updates
                )
                stmt = sql.SQL(
                    "INSERT INTO {t} ({cols}) VALUES ({vals}) "
                    "ON CONFLICT ({pk}) DO UPDATE SET {updates}"
                ).format(t=sql.Identifier(table_name), cols=col_sql, vals=placeholders, pk=pk_sql, updates=update_sql)
            else:
                stmt = sql.SQL(
                    "INSERT INTO {t} ({cols}) VALUES ({vals}) ON CONFLICT ({pk}) DO NOTHING"
                ).format(t=sql.Identifier(table_name), cols=col_sql, vals=placeholders, pk=pk_sql)

            if rows:
                pg_cur.executemany(stmt, rows)

            # Remove rows that no longer exist locally.
            pg_cur.execute(sql.SQL("SELECT {pk} FROM {t}").format(pk=pk_sql, t=sql.Identifier(table_name)))
            remote_keys = {tuple(r) for r in pg_cur.fetchall()}
            local_keys = {tuple(row[columns.index(pk)] for pk in pk_columns) for row in rows}
            stale_keys = remote_keys - local_keys
            if stale_keys:
                where_sql = sql.SQL(" AND ").join(
                    sql.SQL("{c} = {p}").format(c=sql.Identifier(pk), p=sql.Placeholder())
                    for pk in pk_columns
                )
                delete_stmt = sql.SQL("DELETE FROM {t} WHERE {where}").format(
                    t=sql.Identifier(table_name), where=where_sql
                )
                pg_cur.executemany(delete_stmt, list(stale_keys))

        pg_conn.commit()
        logger.info("SQLite changes synced to Supabase successfully.")
        return True
    except Exception as e:
        if pg_conn:
            try: pg_conn.rollback()
            except Exception: pass
        logger.error("Automatic SQLite -> Supabase sync failed: %s", e)
        print("AUTOMATIC SYNC ERROR:", e, flush=True)
        return False
    finally:
        if sqlite_conn: sqlite_conn.close()
        if pg_conn: pg_conn.close()

def restore_sqlite_from_supabase_if_empty():
    """Restore a fresh SQLite database from Supabase before syncing back."""
    if not SUPABASE_DB_URL:
        logger.warning("SUPABASE_DB_URL is not set; SQLite restore skipped.")
        return True

    tables = [
        "users",
        "hardware",
        "borrow_requests",
        "item_requests",
        "item_holds",
        "borrow_transactions",
        "password_resets",
        "return_requests",
    ]

    sqlite_conn = None
    pg_conn = None

    try:
        sqlite_conn = sqlite3.connect(DB_NAME)
        sqlite_cur = sqlite_conn.cursor()

      # Restore when the local hardware inventory is empty.
        sqlite_cur.execute('SELECT COUNT(*) FROM "hardware"')
        local_hardware_count = sqlite_cur.fetchone()[0]

        if local_hardware_count > 0:
            logger.info(
                "Local SQLite already contains hardware; Supabase restore skipped."
            )
            return True

        logger.info(
            "Fresh SQLite database detected; restoring data from Supabase."
        )

        pg_conn = psycopg.connect(
            SUPABASE_DB_URL,
            sslmode="require"
        )
        pg_cur = pg_conn.cursor()

        for table_name in tables:
            # Check whether the table exists in Supabase.
            pg_cur.execute("""
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name = %s
                )
            """, (table_name,))

            if not pg_cur.fetchone()[0]:
                logger.warning(
                    "Supabase table '%s' does not exist; skipping.",
                    table_name
                )
                continue

            # Get local SQLite columns.
            sqlite_cur.execute(
                f'PRAGMA table_info("{table_name}")'
            )
            local_info = sqlite_cur.fetchall()

            local_columns = [row[1] for row in local_info]

            # Get Supabase columns.
            pg_cur.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = %s
                ORDER BY ordinal_position
            """, (table_name,))

            remote_columns = [
                row[0]
                for row in pg_cur.fetchall()
            ]

            # Use only columns that exist in both databases.
            columns = [
                column
                for column in local_columns
                if column in remote_columns
            ]

            if not columns:
                logger.warning(
                    "No matching columns found for table '%s'; skipping.",
                    table_name
                )
                continue

            # Make sure required SQLite columns are available.
            missing_required = []

            for row in local_info:
                column_name = row[1]
                not_null = row[3]
                default_value = row[4]
                primary_key = row[5]

                if (
                    not primary_key
                    and not_null
                    and default_value is None
                    and column_name not in remote_columns
                ):
                    missing_required.append(column_name)

            if missing_required:
                raise RuntimeError(
                    f"Cannot restore table '{table_name}'. "
                    f"Missing required columns: {missing_required}"
                )

            select_columns = sql.SQL(", ").join(
                sql.Identifier(column)
                for column in columns
            )

            pg_cur.execute(
                sql.SQL("SELECT {} FROM {}").format(
                    select_columns,
                    sql.Identifier(table_name)
                )
            )

            rows = pg_cur.fetchall()

            if not rows:
                continue

            quoted_columns = ", ".join(
                f'"{column}"'
                for column in columns
            )

            placeholders = ", ".join(
                "?"
                for _ in columns
            )

            insert_sql = (
                f'INSERT OR IGNORE INTO "{table_name}" '
                f'({quoted_columns}) '
                f'VALUES ({placeholders})'
            )

            sqlite_cur.executemany(
                insert_sql,
                rows
            )

            logger.info(
                "Restored %s row(s) into '%s'.",
                len(rows),
                table_name
            )

        sqlite_conn.commit()

        logger.info(
            "Supabase -> SQLite restore completed successfully."
        )

        return True

    except Exception as e:
        if sqlite_conn:
            sqlite_conn.rollback()

        logger.error(
            "Supabase -> SQLite restore failed: %s",
            e
        )

        return False

    finally:
        if sqlite_conn:
            sqlite_conn.close()

        if pg_conn:
            pg_conn.close()

def init_db():
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                hold_id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'User',
                failed_attempts INTEGER DEFAULT 0,
                is_locked INTEGER DEFAULT 0
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS password_resets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                email TEXT NOT NULL,
                status TEXT DEFAULT 'Pending'
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS hardware (
                item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_name TEXT UNIQUE NOT NULL,
                category TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                unit_price REAL NOT NULL,
                status TEXT NOT NULL
            )
        """)

        cursor.execute("PRAGMA table_info(hardware)")
        hardware_columns = [row[1] for row in cursor.fetchall()]

        if "initial_quantity" not in hardware_columns:
            cursor.execute(
                "ALTER TABLE hardware ADD COLUMN initial_quantity INTEGER"
            )

            cursor.execute("""
                UPDATE hardware
                SET initial_quantity = quantity
                WHERE initial_quantity IS NULL
            """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS borrow_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                item_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                status TEXT DEFAULT 'Pending',
                borrowed_at TEXT,
                return_status TEXT,
                return_quantity INTEGER,
                returned_at TEXT,
                FOREIGN KEY (item_id) REFERENCES hardware(item_id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS item_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                item_name TEXT NOT NULL,
                category TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                status TEXT DEFAULT 'Pending',
                requested_at TEXT NOT NULL
            )
        """)

        cursor.execute("PRAGMA table_info(item_requests)")
        item_request_columns = [row[1] for row in cursor.fetchall()]

        if "quantity" not in item_request_columns:
            cursor.execute(
                "ALTER TABLE item_requests ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1"
        )
        if "requested_at" not in item_request_columns:
            cursor.execute(
                "ALTER TABLE item_requests ADD COLUMN requested_at TEXT"
    )
        if "unit_price" not in item_request_columns:
            cursor.execute(
                "ALTER TABLE item_requests ADD COLUMN unit_price REAL DEFAULT 0"
    )
        
        cursor.execute("PRAGMA table_info(borrow_requests)")
        borrow_columns = [column[1] for column in cursor.fetchall()]

        if "return_status" not in borrow_columns:
            cursor.execute(
                "ALTER TABLE borrow_requests ADD COLUMN return_status TEXT"
            )

        if "return_quantity" not in borrow_columns:
            cursor.execute(
                "ALTER TABLE borrow_requests ADD COLUMN return_quantity INTEGER"
            )

        if "due_date" not in borrow_columns:
            cursor.execute(
                "ALTER TABLE borrow_requests ADD COLUMN due_date TEXT"
            )

        if "renewal_count" not in borrow_columns:
            cursor.execute(
                "ALTER TABLE borrow_requests ADD COLUMN renewal_count INTEGER DEFAULT 0"
            )

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS return_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                borrow_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                item_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                status TEXT DEFAULT 'Pending',
                requested_at TEXT,
                approved_at TEXT,
                FOREIGN KEY (borrow_id) REFERENCES borrow_requests(id),
                FOREIGN KEY (item_id) REFERENCES hardware(item_id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS item_holds (
                hold_id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                item_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                status TEXT DEFAULT 'Active',
                hold_date TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (item_id) REFERENCES hardware(item_id)
            )
        """)

        cursor.execute("PRAGMA table_info(item_holds)")
        hold_columns = [row[1] for row in cursor.fetchall()]

        if "quantity" not in hold_columns:
            cursor.execute(
                "ALTER TABLE item_holds ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1"
            )

        cursor.execute("PRAGMA table_info(users)")
        user_columns = [row[1] for row in cursor.fetchall()]

        if "email" not in user_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN email TEXT")

        if "role" not in user_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'User'")

        if "failed_attempts" not in user_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN failed_attempts INTEGER DEFAULT 0")

        if "is_locked" not in user_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN is_locked INTEGER DEFAULT 0")

        if "full_name" not in user_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN full_name TEXT")

        if "student_number" not in user_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN student_number TEXT")

        if "year_level" not in user_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN year_level TEXT")

        if "program" not in user_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN program TEXT")

        if "employee_id" not in user_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN employee_id TEXT")

        if "building" not in user_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN building TEXT")

        if "laboratory_room" not in user_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN laboratory_room TEXT")

        cursor.execute("PRAGMA table_info(password_resets)")
        reset_columns = [row[1] for row in cursor.fetchall()]
        if "status" not in reset_columns:
            cursor.execute("ALTER TABLE password_resets ADD COLUMN status TEXT DEFAULT 'Pending'")

        conn.commit()
        conn.close()

        if restore_sqlite_from_supabase_if_empty():
            sync_sqlite_to_supabase()
        else:
            logger.error(
                "SQLite restore failed; Supabase sync was skipped for safety."
            )
        logger.info("Hardware Inventory database initialized successfully.")
    except sqlite3.Error as e:
        logger.error(f"Database initialization error: {e}")

# 3. INPUT VALIDATION SCHEMAS (PYDANTIC)

class UserRegisterSchema(BaseModel):
    full_name: str
    username: str = Field(..., min_length=3, max_length=20)
    email: str
    student_number: str
    year_level: str
    program: str
    password: str = Field(..., min_length=8)
    role: str = Field(default="User")

    @field_validator('username')
    def username_alphanumeric(cls, v):
        if len(v) < 3:
            raise ValueError('Username must be at least 3 characters long.')
        if not re.match(r"^[a-zA-Z0-9_]+$", v):
            raise ValueError('Username must contain only letters, numbers, and underscores.')
        return v

    @field_validator('email')
    def email_domain_check(cls, v):
        pattern = r"^[\w\.-]+@(gmail\.com|yahoo\.com|outlook\.com|hotmail\.com|[a-zA-Z0-9-]+\.[a-zA-Z0-9-\.]+)$"
        if not re.match(pattern, v, re.IGNORECASE):
            raise ValueError('Please enter a valid email address.')
        return v

    @field_validator('password')
    def password_complexity(cls, v):
        if not re.search(r"[A-Z]", v):
            raise ValueError('Password must contain at least 1 uppercase letter.')
        if not re.search(r"[0-9]", v):
            raise ValueError('Password must contain at least 1 number.')
        if not re.search(r"[!@#$%^&*(),.?\":{}|<>]", v):
            raise ValueError('Password must contain at least 1 special character.')
        return v

    @field_validator('role')
    def validate_role(cls, v):
        allowed_roles = ["User", "Administrator"]
        if v not in allowed_roles:
            raise ValueError('Role must be either User or Administrator.')
        return v
class LabTechRegisterSchema(BaseModel):
    full_name: str
    username: str = Field(..., min_length=3, max_length=20)
    email: str
    employee_id: str
    building: str
    laboratory_room: str
    password: str = Field(..., min_length=8)
    role: str = Field(default="Admin")

class HardwareItemSchema(BaseModel):
    item_name: str = Field(..., min_length=2, max_length=100)
    category: str = Field(..., min_length=2, max_length=50)
    quantity: int = Field(..., ge=0)
    unit_price: float = Field(..., ge=0.0)

# 4. AUTHENTICATION & BUSINESS LOGIC

class AuthController:
    def register_user(self, full_name, username, email, student_number, year_level, program, password, role="User"):
        try:
            validated = UserRegisterSchema(
                full_name=full_name,
                username=username,
                email=email,
                student_number=student_number,
                year_level=year_level,
                program=program,
                password=password,
                role=role
            )
        except ValidationError as e:
            msg = e.errors()[0]['msg']
            if msg.lower().startswith("value error, "):
                msg = msg[13:]
            if "String should" in msg:
                msg = msg.replace("String", "Username")
            return False, msg

        try:
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            
            cursor.execute("SELECT 1 FROM users WHERE LOWER(username) = LOWER(?)", (validated.username,))
            username_exists = cursor.fetchone() is not None

            cursor.execute("SELECT 1 FROM users WHERE LOWER(email) = LOWER(?)", (validated.email,))
            email_exists = cursor.fetchone() is not None

            conn.close()

            if username_exists and email_exists:
                return False, "Username and email are already registered."
            elif username_exists:
                return False, "Username is already registered."
            elif email_exists:
                return False, "Email is already registered."

            hashed_pw = bcrypt.hashpw(validated.password.encode('utf-8'), bcrypt.gensalt())

            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO users (
                    full_name,
                    username,
                    email,
                    student_number,
                    year_level,
                    program,
                    password_hash,
                    role
                )   
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    validated.full_name,
                    validated.username,
                    validated.email,
                    validated.student_number,
                    validated.year_level,
                    validated.program,
                    hashed_pw.decode('utf-8'),
                    validated.role
                )
            )
            conn.commit()
            sync_sqlite_to_supabase()
            conn.close()
            return True, "Registration successful! You may now log in."
        except sqlite3.IntegrityError:
            return False, "Username or email is already registered."

    def register_lab_technician(
        self,
        full_name,
        username,
        email,
        employee_id,
        building,
        laboratory_room,
        password
    ):
        try:
            validated = LabTechRegisterSchema(
                full_name=full_name,
                username=username,
                email=email,
                employee_id=employee_id,
                building=building,
                laboratory_room=laboratory_room,
                password=password,
                role="Admin"
            )
        except ValidationError as e:
            msg = e.errors()[0]["msg"]
            return False, msg

        try:
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT id FROM users
                WHERE username = ? OR email = ? OR employee_id = ?
                """,
                (validated.username, validated.email, validated.employee_id)
            )

            existing_user = cursor.fetchone()

            if existing_user:
                conn.close()
                return False, "Username, email, or employee ID already exists."

            password_hash = bcrypt.hashpw(
                validated.password.encode("utf-8"),
                bcrypt.gensalt()
            ).decode("utf-8")

            cursor.execute(
                """
                INSERT INTO users (
                    full_name,
                    username,
                    email,
                    employee_id,
                    building,
                    laboratory_room,
                    password_hash,
                    role
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    validated.full_name,
                    validated.username,
                    validated.email,
                    validated.employee_id,
                    validated.building,
                    validated.laboratory_room,
                    password_hash,
                    "Admin"
                )
            )

            conn.commit()
            sync_sqlite_to_supabase()
            conn.close()

            return True, "Lab Technician account created successfully."

        except sqlite3.IntegrityError:
            return False, "Username, email, or employee ID already exists."

        except Exception as e:
            return False, str(e)
    
    def login_user(self, username, password):
        if not username or not password:
            return False, "Please fill in all fields."

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT id, password_hash, failed_attempts, is_locked FROM users WHERE username = ?", (username,))
        row = cursor.fetchone()

        if not row:
            conn.close()
            return False, "Invalid username or password."

        user_id, stored_hash, failed_attempts, is_locked = row

        if is_locked:
            conn.close()
            return False, "ACCOUNT_LOCKED"

        if bcrypt.checkpw(password.encode('utf-8'), stored_hash.encode('utf-8')):
            cursor.execute("UPDATE users SET failed_attempts = 0 WHERE id = ?", (user_id,))
            conn.commit()
            sync_sqlite_to_supabase()
            conn.close()
            return True, "Login successful!"
        else:
            failed_attempts += 1
            if failed_attempts >= 3:
                cursor.execute("UPDATE users SET failed_attempts = ?, is_locked = 1 WHERE id = ?", (failed_attempts, user_id))
                conn.commit()
                sync_sqlite_to_supabase()
                conn.close()
                return False, "ACCOUNT_LOCKED"
            else:
                cursor.execute("UPDATE users SET failed_attempts = ? WHERE id = ?", (failed_attempts, user_id))
                conn.commit()
                sync_sqlite_to_supabase()
                conn.close()
                remaining_tries = 3 - failed_attempts
                return False, f"Invalid username or password. {remaining_tries} attempt(s) remaining before lockout."

    def request_password_reset(self, username, email):

        username = (username or "").strip()
        email = (email or "").strip()

        if not username or not email:
            return False, "Username and email are required."

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        try:

            # Check username and registered email
            cursor.execute("""
                SELECT id, email
                FROM users
                WHERE username = ?
            """, (username,))

            user = cursor.fetchone()

            if not user:
                return False, "Username not found."

            registered_email = user[1] or ""

            if registered_email.lower() != email.lower():
                return False, "Username and registered email do not match."

            # Prevent duplicate pending requests
            cursor.execute("""
                SELECT id
                FROM password_resets
                WHERE username = ?
                AND status = 'Pending'
            """, (username,))

            if cursor.fetchone():
                return False, "You already have a pending reset request."

            # Create reset request
            cursor.execute("""
                INSERT INTO password_resets
                (
                    username,
                    email,
                    status
                )
                VALUES
                (
                    ?,
                    ?,
                    'Pending'
                )
            """, (
                username,
                email
            ))

            conn.commit()
            sync_sqlite_to_supabase()

            return (
                True,
                "Password reset request submitted successfully. "
                "Please wait for administrator approval."
            )

        except sqlite3.Error:

            conn.rollback()

            return False, "Database error occurred while submitting the reset request."

        finally:

            conn.close()

    def approve_password_reset(self, request_id):
        import secrets
        import string

        try:
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()

            # Get pending password reset request
            cursor.execute("""
                SELECT username
                FROM password_resets
                WHERE id = ?
                  AND status = 'Pending'
            """, (request_id,))

            row = cursor.fetchone()

            if not row:
                conn.close()
                return (
                    False,
                    "Password reset request not found or already processed.",
                    None
                )

            username = row[0]

            # Character sets
            uppercase = string.ascii_uppercase
            lowercase = string.ascii_lowercase
            numbers = "01"
            special = "!>~@#$%^&*()|"

            # Guarantee all required character types
            password_characters = [
                secrets.choice(uppercase),
                secrets.choice(lowercase),
                secrets.choice(numbers),
                secrets.choice(special)
            ]

            # Fill remaining characters
            all_characters = (
                uppercase +
                lowercase +
                numbers +
                special
            )

            while len(password_characters) < 10:
                password_characters.append(
                    secrets.choice(all_characters)
                )

            # Shuffle so the required characters are not always first
            secrets.SystemRandom().shuffle(password_characters)

            temporary_password = "".join(password_characters)

            # Hash temporary password
            new_hashed_password = bcrypt.hashpw(
                temporary_password.encode("utf-8"),
                bcrypt.gensalt()
            ).decode("utf-8")

            # Update user password and unlock account
            cursor.execute("""
                UPDATE users
                SET password_hash = ?,
                    failed_attempts = 0,
                    is_locked = 0
                WHERE username = ?
            """, (
                new_hashed_password,
                username
            ))

            if cursor.rowcount == 0:
                conn.rollback()
                conn.close()
                return (
                    False,
                    "User account was not found.",
                    None
                )

            # Mark reset request as Completed
            cursor.execute("""
                UPDATE password_resets
                SET status = 'Completed'
                WHERE id = ?
                  AND status = 'Pending'
            """, (request_id,))

            if cursor.rowcount == 0:
                conn.rollback()
                conn.close()
                return (
                    False,
                    "Password reset request could not be completed.",
                    None
                )

            conn.commit()
            sync_sqlite_to_supabase()
            conn.close()

            return (
                True,
                "Password reset approved successfully.",
                temporary_password
            )

        except sqlite3.Error as e:
            try:
                conn.rollback()
                conn.close()
            except Exception:
                pass

            return (
                False,
                f"Database error: {e}",
                None
            )


    def reject_password_reset(self, request_id):
        try:
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE password_resets
                SET status = 'Declined'
                WHERE id = ?
                  AND status = 'Pending'
            """, (request_id,))

            if cursor.rowcount == 0:
                conn.close()
                return (
                    False,
                    "Password reset request not found or already processed."
                )

            conn.commit()
            sync_sqlite_to_supabase()
            conn.close()

            return (
                True,
                "Password reset request declined."
            )

        except sqlite3.Error as e:
            try:
                conn.rollback()
                conn.close()
            except Exception:
                pass

            return (
                False,
                f"Database error: {e}"
            )
        
    def reject_password_reset(self, request_id):
        try:
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE password_resets
                SET status = 'Declined'
                WHERE id = ?
                  AND status = 'Pending'
            """, (request_id,))

            if cursor.rowcount == 0:
                conn.close()
                return (
                    False,
                    "Password reset request not found or already processed."
                )

            conn.commit()
            sync_sqlite_to_supabase()
            conn.close()

            return (
                True,
                "Password reset request declined."
            )

        except sqlite3.Error as e:
            try:
                conn.rollback()
                conn.close()
            except Exception:
                pass

            return (
                False,
                f"Database error: {e}"
            )

    def get_user_profile(self, username):
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT username, email, role FROM users WHERE username = ?", (username,))
        row = cursor.fetchone()
        conn.close()
        return row

    def update_password(self, username, current_pass, new_pass):
        if not current_pass or not new_pass:
            return False, "Please fill in all password fields."

        try:
            if len(new_pass) < 8:
                raise ValueError('Password must be at least 8 characters long.')
            if not re.search(r"[A-Z]", new_pass):
                raise ValueError('Password must contain at least 1 uppercase letter.')
            if not re.search(r"[0-9]", new_pass):
                raise ValueError('Password must contain at least 1 number.')
            if not re.search(r"[!@#$%^&*(),.?\":{}|<>]", new_pass):
                raise ValueError('Password must contain at least 1 special character.')
        except ValueError as e:
            return False, str(e)

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT id, password_hash FROM users WHERE username = ?", (username,))
        row = cursor.fetchone()

        if not row:
            conn.close()
            return False, "User not found."

        user_id, stored_hash = row

        if not bcrypt.checkpw(current_pass.encode('utf-8'), stored_hash.encode('utf-8')):
            conn.close()
            return False, "Current password is incorrect."

        new_hashed_pw = bcrypt.hashpw(new_pass.encode('utf-8'), bcrypt.gensalt())
        cursor.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hashed_pw.decode('utf-8'), user_id))
        conn.commit()
        sync_sqlite_to_supabase()
        conn.close()
        return True, "Password updated successfully! Please log in with your new password."

class InventoryController:
    @staticmethod
    def calculate_status(quantity: int) -> str:
        if quantity > 5:
            return "In Stock"
        elif 1 <= quantity <= 5:
            return "Low Stock"
        else:
            return "Out of Stock"

    def check_item_exists(self, item_name):
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT item_id, quantity, unit_price, category FROM hardware WHERE LOWER(item_name) = LOWER(?)", (item_name,))
        row = cursor.fetchone()
        conn.close()
        return row

    def add_item(self, item_name, category, quantity_str, unit_price_str):
        try:
            qty = int(quantity_str)
            price = float(unit_price_str)

            validated = HardwareItemSchema(
                item_name=item_name,
                category=category,
                quantity=qty,
                unit_price=price
            )

        except ValueError:
            return "error", "Quantity must be an integer, and Price must be numeric."

        except ValidationError as e:
            msg = e.errors()[0]['msg']

            if msg.lower().startswith("value error, "):
                msg = msg[13:]

            return "error", msg

        existing_item = self.check_item_exists(validated.item_name)

        if existing_item:
            return "exists", f"Item '{validated.item_name}' already exists."

        status = self.calculate_status(validated.quantity)

        try:
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()

            cursor.execute(
                """
                INSERT INTO hardware
                (
                    item_name,
                    category,
                    initial_quantity,
                    quantity,
                    unit_price,
                    status
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    validated.item_name,
                    validated.category,
                    validated.quantity,
                    validated.quantity,
                    validated.unit_price,
                    status
                )
            )

            conn.commit()
            sync_sqlite_to_supabase()
            conn.close()

            return "added", "New hardware component added successfully!"

        except sqlite3.Error:
            return "error", "Database error occurred."

    def update_item_record(self, item_id, item_name, category, quantity_str, unit_price_str):
        try:
            qty = int(quantity_str)
            price = float(unit_price_str)

            validated = HardwareItemSchema(
                item_name=item_name,
                category=category,
                quantity=qty,
                unit_price=price
            )

        except ValueError:
            return "error", "Quantity must be an integer, and Price must be numeric."

        except ValidationError as e:
            msg = e.errors()[0]['msg']

            if msg.lower().startswith("value error, "):
                msg = msg[13:]

            return "error", msg

        status = self.calculate_status(validated.quantity)

        try:
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT item_id
                FROM hardware
                WHERE LOWER(item_name) = LOWER(?)
                AND item_id != ?
                """,
                (validated.item_name, item_id)
            )

            if cursor.fetchone():
                conn.close()
                return "error", (
                    f"Another item named '{validated.item_name}' already exists."
                )

            cursor.execute(
                """
                UPDATE hardware
                SET item_name = ?,
                    category = ?,
                    quantity = ?,
                    unit_price = ?,
                    status = ?
                WHERE item_id = ?
                """,
                (
                    validated.item_name,
                    validated.category,
                    validated.quantity,
                    validated.unit_price,
                    status,
                    item_id
                )
            )

            conn.commit()
            sync_sqlite_to_supabase()
            conn.close()

            return "updated", "Hardware record updated successfully!"

        except sqlite3.Error:
            return "error", "Database error occurred."

    def delete_items_batch(self, item_ids):
        try:
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.executemany("DELETE FROM hardware WHERE item_id = ?", [(i,) for i in item_ids])
            conn.commit()
            sync_sqlite_to_supabase()
            conn.close()
            return True, f"Successfully deleted {len(item_ids)} item(s)."
        except sqlite3.Error:
            return False, "Failed to delete items from database."

    def fetch_all_items(self, search_query="", category_filter="All"):
        try:
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()

            query = """
                SELECT
                    item_id,
                    item_name,
                    category,
                    initial_quantity,
                    quantity,
                    unit_price,
                    status
                FROM hardware
                WHERE 1=1
            """

            params = []

            if search_query:
                query += " AND (LOWER(item_name) LIKE ? OR LOWER(category) LIKE ?)"
                params.extend([
                    f"%{search_query.lower()}%",
                    f"%{search_query.lower()}%"
                ])

            if category_filter and category_filter != "All":
                query += " AND category = ?"
                params.append(category_filter)

            cursor.execute(query, params)

            rows = cursor.fetchall()

            conn.close()

            return rows

        except sqlite3.Error:
            return []
    def get_all_loans_history(self):
        try:
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT
                    br.id,
                    br.username,
                    h.item_name,
                    br.quantity,
                    br.borrowed_at,
                    br.due_date,
                    br.status,
                    COALESCE(br.renewal_count, 0),
                    br.return_status,
                    br.returned_at
                FROM borrow_requests br
                JOIN hardware h
                    ON br.item_id = h.item_id
                ORDER BY br.id DESC
            """)

            rows = cursor.fetchall()
            conn.close()

            return rows

        except sqlite3.Error:
            return []
    
    def get_categories(self):
        try:
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT category FROM hardware")
            categories = [row[0] for row in cursor.fetchall()]
            conn.close()
            return categories
        except sqlite3.Error:
            return []

    def export_to_csv(self, selected_items=None, filename="hardware_inventory_report.csv"):
        rows = selected_items if selected_items else self.fetch_all_items()
        try:
            with open(filename, mode="w", newline="", encoding="utf-8") as file:
                writer = csv.writer(file)
                writer.writerow(["Item ID", "Item Name", "Category", "Quantity", "Unit Price (PHP)", "Status"])
                for r in rows:
                    writer.writerow([r[0], r[1], r[2], r[3], r[4], r[5]])
            return True, f"Inventory exported to '{filename}' successfully!"
        except Exception:
            return False, "Failed to generate CSV report."
    def request_borrow(self, username, item_id, quantity):
        try:
            quantity = int(quantity)
        except (ValueError, TypeError):
            return False, "Quantity must be a whole number."

        if quantity < 1:
            return False, "Quantity must be at least 1."

        try:
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()

            # Check available stock
            cursor.execute("""
                SELECT item_name, quantity
                FROM hardware
                WHERE item_id = ?
            """, (item_id,))

            item = cursor.fetchone()

            if not item:
                conn.close()
                return False, "Hardware item not found."

            item_name, available_quantity = item

            # Check requested quantity against available stock
            if quantity > available_quantity:
                conn.close()
                return False, (
                    f"Only {available_quantity} unit(s) of "
                    f"'{item_name}' are available."
                )

            # Prevent duplicate pending request
            cursor.execute("""
                SELECT id
                FROM borrow_requests
                WHERE username = ?
                AND item_id = ?
                AND status = 'Pending'
            """, (username, item_id))

            if cursor.fetchone():
                conn.close()
                return False, (
                    "You already have a pending borrow request "
                    "for this item."
                )

            # ------------------------------------------
            # STEP 1: DEDUCT STOCK IMMEDIATELY
            # ------------------------------------------
            new_quantity = available_quantity - quantity
            new_status = self.calculate_status(new_quantity)

            cursor.execute("""
                UPDATE hardware
                SET quantity = ?,
                    status = ?
                WHERE item_id = ?
            """, (
                new_quantity,
                new_status,
                item_id
            ))

            # ------------------------------------------
            # CREATE PENDING BORROW RECORD
            # ------------------------------------------
            cursor.execute("""
                INSERT INTO borrow_requests
                (
                    username,
                    item_id,
                    quantity,
                    status,
                    borrowed_at,
                    due_date
                )
                VALUES (
                    ?,
                    ?,
                    ?,
                    'Pending',
                    datetime('now', 'localtime'),
                    datetime('now', 'localtime', '+3 days')
                )
            """, (
                username,
                item_id,
                quantity
            ))

            conn.commit()
            sync_sqlite_to_supabase()
            conn.close()

            return True, (
                f"Borrow request for {quantity} unit(s) "
                f"of '{item_name}' submitted."
            )

        except sqlite3.Error:
            return False, (
                "Database error occurred while submitting "
                "the borrow request."
            )
    def place_hold(self, username, item_id, quantity):
        try:
            quantity = int(quantity)
        except (ValueError, TypeError):
            return False, "Quantity must be a whole number."

        if quantity < 1:
            return False, "Quantity must be at least 1."

        try:
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT item_name, quantity
                FROM hardware
                WHERE item_id = ?
            """, (item_id,))

            item = cursor.fetchone()

            if not item:
                conn.close()
                return False, "Hardware item not found."

            item_name, available_quantity = item

            if quantity > available_quantity:
                conn.close()
                return False, (
                    f"Only {available_quantity} unit(s) of "
                    f"'{item_name}' are available."
                )

            cursor.execute("""
                SELECT rowid
                FROM item_holds
                WHERE username = ?
                    AND item_id = ?
                    AND status = 'Active'
            """, (username, item_id))

            if cursor.fetchone():
                conn.close()
                return False, (
                    "You already have an active hold "
                    "for this item."
                )

            cursor.execute("""
                INSERT INTO item_holds
                (
                    username,
                    item_id,
                    quantity,
                    status,
                    hold_date
                )
                VALUES (?, ?, ?, 'Active',datetime('now','localtime'))
            """, (
                username,
                item_id,
                quantity
            ))

            conn.commit()
            sync_sqlite_to_supabase()
            conn.close()

            return True, (
                f"Item hold for {quantity} unit(s) of "
                f"'{item_name}' placed successfully."
            )

        except sqlite3.Error:
            return False, (
                "Database error occurred while "
                "placing the item hold."
            )
    def get_item_request(self, request_id):
        try:
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT
                    rowid AS id,
                    username,
                    item_name,
                    category,
                    quantity,
                    status,
                    requested_at,
                    unit_price
                FROM item_requests
                WHERE rowid = ?
            """, (request_id,))

            row = cursor.fetchone()
            conn.close()

            return row

        except sqlite3.Error:
            return None


    def approve_item_request(self, request_id, quantity, unit_price):
        try:
            quantity = int(quantity)
            unit_price = float(unit_price)
        except (ValueError, TypeError):
            return False, "Quantity must be a whole number and price must be numeric."

        if quantity < 1:
            return False, "Quantity must be at least 1."

        if unit_price < 0:
            return False, "Unit price cannot be negative."

        request_data = self.get_item_request(request_id)

        if not request_data:
            return False, "Item request not found."

        item_name = request_data[2]
        category = request_data[3]
        status = request_data[5]

        if status != "Pending":
            return False, "Only pending item requests can be approved."

        result, message = self.add_item(
            item_name,
            category,
            str(quantity),
            str(unit_price)
        )

        if result != "added":
            return False, message

        try:
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE item_requests
                SET quantity = ?,
                    unit_price = ?,
                    status = 'Approved'
                WHERE rowid = ?
                  AND status = 'Pending'
            """, (
                quantity,
                unit_price,
                request_id
            ))

            conn.commit()
            sync_sqlite_to_supabase()
            conn.close()

            return True, "Item request approved and added to inventory."

        except sqlite3.Error:
            return False, "Item was added, but the request status could not be updated."


    def reject_item_request(self, request_id):
        try:
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE item_requests
                SET status = 'Rejected'
                WHERE rowid = ?
                  AND status = 'Pending'
            """, (request_id,))

            if cursor.rowcount == 0:
                conn.close()
                return False, "Only pending item requests can be rejected."

            conn.commit()
            sync_sqlite_to_supabase()
            conn.close()

            return True, "Item request rejected."

        except sqlite3.Error:
            return False, "Database error occurred while rejecting the item request."
        
    def approve_borrow(self, request_id):
        try:
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT item_id, quantity, status
                FROM borrow_requests
                WHERE id = ?
            """, (request_id,))

            borrow_request = cursor.fetchone()

            if not borrow_request:
                conn.close()
                return False, "Borrow request not found."

            item_id, requested_quantity, request_status = borrow_request

            if request_status != "Pending":
                conn.close()
                return False, "This borrow request has already been processed."

            cursor.execute("""
                SELECT item_name, quantity
                FROM hardware
                WHERE item_id = ?
            """, (item_id,))

            item = cursor.fetchone()

            if not item:
                conn.close()
                return False, "Hardware item not found."

            item_name, available_quantity = item

            if requested_quantity > available_quantity:
                conn.close()
                return False, (
                    f"Not enough stock to approve this request. "
                    f"Only {available_quantity} unit(s) available."
                )

            new_quantity = available_quantity - requested_quantity
            new_status = self.calculate_status(new_quantity)

            cursor.execute("""
                UPDATE hardware
                SET quantity = ?, status = ?
                WHERE item_id = ?
            """, (new_quantity, new_status, item_id))

            cursor.execute("""
                UPDATE borrow_requests
                SET status = 'Approved',
                    borrowed_at = datetime('now'),
                    due_date = datetime('now', '+7 days'),
                    renewal_count = 0
                WHERE id = ?
            """, (request_id,))

            conn.commit()
            sync_sqlite_to_supabase()
            conn.close()

            return True, (
                f"Borrow request approved. "
                f"{requested_quantity} unit(s) of '{item_name}' deducted from stock."
            )

        except sqlite3.Error:
            return False, "Database error occurred while approving the borrow request."

        def reject_borrow(self, request_id):
            try:
                conn = sqlite3.connect(DB_NAME)
                cursor = conn.cursor()

                cursor.execute("""
                    UPDATE borrow_requests
                    SET status = 'Rejected'
                    WHERE id = ?
                     AND status = 'Pending'
                """, (request_id,))

                if cursor.rowcount == 0:
                    conn.close()
                    return False, "Borrow request not found or already processed."

                conn.commit()
                sync_sqlite_to_supabase()
                conn.close()

                return True, "Borrow request rejected."

            except sqlite3.Error:
                return False, "Database error occurred while rejecting the borrow request."

        def request_return(self, username, item_id, quantity):
            try:
                quantity = int(quantity)
            except (ValueError, TypeError):
                return False, "Quantity must be a whole number."

            if quantity < 1:
                return False, "Quantity must be at least 1."

            try:
                conn = sqlite3.connect(DB_NAME)
                cursor = conn.cursor()

                cursor.execute("""
                    SELECT id
                    FROM borrow_requests
                    WHERE username = ?
                    AND item_id = ?
                    AND status = 'Approved'
                    AND return_status IS NULL
                    AND quantity >= ?
                """, (username, item_id, quantity))

                borrow_request = cursor.fetchone()

                if not borrow_request:
                    conn.close()
                    return False, "No matching borrowed item found."

                borrow_request_id = borrow_request[0]

                cursor.execute("""
                    UPDATE borrow_requests
                    SET return_status = 'Pending',
                    return_quantity = ?
                    WHERE id = ?
                """, (quantity, borrow_request_id))

                conn.commit()
                sync_sqlite_to_supabase()
                conn.close()

                return True, "Return request submitted successfully."

            except sqlite3.Error:
                return False, "Database error occurred while submitting the return request."

    def approve_return(self, request_id):
        try:
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT item_id, return_quantity, return_status
                FROM borrow_requests
                WHERE id = ?
            """, (request_id,))

            return_request = cursor.fetchone()

            if not return_request:
                conn.close()
                return False, "Return request not found."

            item_id, return_quantity, return_status = return_request

            if return_status != "Pending":
                conn.close()
                return False, "This return request has already been processed."

            cursor.execute("""
                SELECT item_name, quantity
                FROM hardware
                WHERE item_id = ?
            """, (item_id,))

            item = cursor.fetchone()

            if not item:
                conn.close()
                return False, "Hardware item not found."

            item_name, current_quantity = item
            new_quantity = current_quantity + return_quantity
            new_status = self.calculate_status(new_quantity)

            cursor.execute("""
                UPDATE hardware
                SET quantity = ?, status = ?
                WHERE item_id = ?
            """, (new_quantity, new_status, item_id))

            cursor.execute("""
                UPDATE borrow_requests
                SET return_status = 'Approved',
                    returned_at = datetime('now')
                WHERE id = ?
            """, (request_id,))

            conn.commit()
            sync_sqlite_to_supabase()
            conn.close()

            return True, (
                f"Return approved. "
                f"{return_quantity} unit(s) of '{item_name}' added back to stock."
            )

        except sqlite3.Error:
            return False, "Database error occurred while approving the return request."                           

    def reject_return(self, request_id):
        try:
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE borrow_requests
                SET return_status = 'Rejected'
                WHERE id = ?
                  AND return_status = 'Pending'
            """, (request_id,))

            if cursor.rowcount == 0:
                conn.close()
                return False, "Return request not found or already processed."

            conn.commit()
            sync_sqlite_to_supabase()
            conn.close()

            return True, "Return request rejected."

        except sqlite3.Error:
            return False, "Database error occurred while rejecting the return request."
        
# 5. GUI VIEWS & WHIMSICAL PASTEL STYLING 

def apply_global_styles():
    style = ttk.Style()
    style.theme_use("clam")
    
    style.configure(
        "Treeview",
        background="#F9DDD8",
        foreground="#35522B",
        rowheight=34,
        fieldbackground="#F9DDD8",
        font=("Segoe UI", 10),
        borderwidth=0
    )
    style.configure(
        "Treeview.Heading",
        background="#A7B59E",
        foreground="#FFFFFF",
        font=("Segoe UI", 10, "bold"),
        relief="flat"
    )
    style.map("Treeview", background=[('selected', '#F8D0C8')], foreground=[('selected', '#35522B')])
    
    style.configure("TCombobox", fieldbackground="#F9DDD8", background="#A7B59E", bordercolor="#F8D0C8")

class LoginFrame(tk.Frame):
    def __init__(self, parent, on_login_success, on_register, on_reset_request):
        super().__init__(parent, bg="#F9DDD8")
        self.parent = parent
        self.on_login_success = on_login_success
        self.on_register = on_register
        self.on_reset_request = on_reset_request
        self.auth = AuthController()

        card = tk.Frame(self, bg="#F8D0C8", padx=40, pady=40, highlightbackground="#F3BABA", highlightthickness=1)
        card.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(card, text="Campus Hardware Inventory", font=("Segoe UI", 18, "bold"), bg="#F8D0C8", fg="#35522B").pack(anchor="w", pady=(0, 4))
        tk.Label(card, text="Sign in with your credentials to access system", font=("Segoe UI", 10), bg="#F8D0C8", fg="#5B744B").pack(anchor="w", pady=(0, 25))

        tk.Label(card, text="Username", font=("Segoe UI", 9, "bold"), bg="#F8D0C8", fg="#35522B").pack(anchor="w")
        self.entry_user = tk.Entry(card, width=35, font=("Segoe UI", 11), bg="#F9DDD8", fg="#35522B", insertbackground="#35522B", bd=0, relief="flat")
        self.entry_user.pack(anchor="w", pady=(4, 15), ipady=6)

        tk.Label(card, text="Password", font=("Segoe UI", 9, "bold"), bg="#F8D0C8", fg="#35522B").pack(anchor="w")
        self.entry_pass = tk.Entry(card, show="*", width=35, font=("Segoe UI", 11), bg="#F9DDD8", fg="#35522B", insertbackground="#35522B", bd=0, relief="flat")
        self.entry_pass.pack(anchor="w", pady=(4, 8), ipady=6)

        self.show_pass_var = tk.BooleanVar()
        chk = tk.Checkbutton(card, text="Show Password", variable=self.show_pass_var, command=self.toggle_password, font=("Segoe UI", 9), bg="#F8D0C8", fg="#5B744B", selectcolor="#F9DDD8", activebackground="#F8D0C8", activeforeground="#35522B")
        chk.pack(anchor="w", pady=(0, 20))

        btn_login = tk.Button(card, text="Sign In", command=self.handle_login, bg="#799567", fg="#FFFFFF", font=("Segoe UI", 10, "bold"), bd=0, cursor="hand2", width=33, pady=10, activebackground="#5B744B", activeforeground="#FFFFFF")
        btn_login.pack(pady=(0, 10))

        links_frame = tk.Frame(card, bg="#F8D0C8")
        links_frame.pack(fill="x", pady=(5, 0))

        tk.Button(links_frame, text="Forgot Password?", command=self.on_reset_request, bg="#F8D0C8", fg="#35522B", font=("Segoe UI", 9), bd=0, cursor="hand2", activebackground="#F8D0C8", activeforeground="#F3BABA").pack(side="left")
        tk.Button(links_frame, text="Create Account", command=self.on_register, bg="#F8D0C8", fg="#5B744B", font=("Segoe UI", 9), bd=0, cursor="hand2", activebackground="#F8D0C8", activeforeground="#799567").pack(side="right")

    def toggle_password(self):
        if self.show_pass_var.get():
            self.entry_pass.config(show="")
        else:
            self.entry_pass.config(show="*")

    def handle_login(self):
        user = self.entry_user.get().strip()
        pwd = self.entry_pass.get().strip()
        success, msg = self.auth.login_user(user, pwd)
        if success:
            messagebox.showinfo("Access Granted", msg)
            self.on_login_success(user)
        else:
            if msg == "ACCOUNT_LOCKED":
                messagebox.showerror("Account Locked", "Your account has been locked due to 3 failed attempts. Please proceed to reset your password.")
                self.on_reset_request()
            else:
                messagebox.showerror("Access Denied", msg)


class ResetRequestFrame(tk.Frame):
    def __init__(self, parent, on_back):
        super().__init__(parent, bg="#F9DDD8")
        self.parent = parent
        self.on_back = on_back
        self.auth = AuthController()

        card = tk.Frame(self, bg="#F8D0C8", padx=40, pady=40, highlightbackground="#F3BABA", highlightthickness=1)
        card.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(card, text="Password Recovery", font=("Segoe UI", 18, "bold"), bg="#F8D0C8", fg="#35522B").pack(anchor="w", pady=(0, 4))
        tk.Label(card, text="Submit a request for administrator approval", font=("Segoe UI", 10), bg="#F8D0C8", fg="#5B744B").pack(anchor="w", pady=(0, 25))

        tk.Label(card, text="Username", font=("Segoe UI", 9, "bold"), bg="#F8D0C8", fg="#35522B").pack(anchor="w")
        self.entry_user = tk.Entry(card, width=35, font=("Segoe UI", 11), bg="#F9DDD8", fg="#35522B", insertbackground="#35522B", bd=0, relief="flat")
        self.entry_user.pack(anchor="w", pady=(4, 15), ipady=6)

        tk.Label(card, text="Registered Email", font=("Segoe UI", 9, "bold"), bg="#F8D0C8", fg="#35522B").pack(anchor="w")
        self.entry_email = tk.Entry(card, width=35, font=("Segoe UI", 11), bg="#F9DDD8", fg="#35522B", insertbackground="#35522B", bd=0, relief="flat")
        self.entry_email.pack(anchor="w", pady=(4, 25), ipady=6)

        tk.Button(card, text="Submit Request", command=self.handle_submit, bg="#A7B59E", fg="#FFFFFF", font=("Segoe UI", 10, "bold"), bd=0, cursor="hand2", width=33, pady=10, activebackground="#799567", activeforeground="#FFFFFF").pack(pady=(0, 10))
        tk.Button(card, text="Back to Login", command=self.on_back, bg="#F9DDD8", fg="#35522B", font=("Segoe UI", 9, "bold"), bd=0, cursor="hand2", width=37, pady=8, activebackground="#F3BABA", activeforeground="#35522B").pack()

    def handle_submit(self):
        user = self.entry_user.get().strip()
        email = self.entry_email.get().strip()
        if not user or not email:
            messagebox.showwarning("Validation Error", "Please fill in all fields.")
            return

        success, msg = self.auth.request_password_reset(user, email)
        if success:
            messagebox.showinfo("Success", msg)
            self.on_back()
        else:
            messagebox.showerror("Error", msg)


class AdminApprovalsFrame(tk.Frame):
    def __init__(self, parent, on_back):
        super().__init__(parent, bg="#F9DDD8")
        self.parent = parent
        self.on_back = on_back

        container = tk.Frame(self, bg="#F9DDD8")
        container.pack(fill="both", expand=True, padx=30, pady=30)

        header_frame = tk.Frame(container, bg="#F9DDD8")
        header_frame.pack(fill="x", pady=(0, 20))
        tk.Label(header_frame, text="Security & Access Approvals", font=("Segoe UI", 18, "bold"), bg="#F9DDD8", fg="#35522B").pack(side="left")
        tk.Button(header_frame, text="← Back to Catalog", command=self.on_back, bg="#F8D0C8", fg="#35522B", font=("Segoe UI", 9, "bold"), bd=0, padx=14, pady=8, cursor="hand2", activebackground="#F3BABA").pack(side="right")

        table_container = tk.Frame(container, bg="#F8D0C8", bd=1, relief="solid", highlightbackground="#F3BABA")
        table_container.pack(fill="both", expand=True, pady=(0, 20))

        self.tree = ttk.Treeview(table_container, columns=("ID", "Username", "Email", "Status"), show="headings")
        self.tree.heading("ID", text="ID")
        self.tree.heading("Username", text="Username")
        self.tree.heading("Email", text="Email Address")
        self.tree.heading("Status", text="Status")
        self.tree.column("ID", width=60, anchor="center")
        self.tree.column("Username", width=200)
        self.tree.column("Email", width=320)
        self.tree.column("Status", width=140, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=1, pady=1)

        btn_frame = tk.Frame(container, bg="#F9DDD8")
        btn_frame.pack(fill="x")
        tk.Button(btn_frame, text="Approve & Unlock", command=self.approve_request, bg="#799567", fg="#FFFFFF", font=("Segoe UI", 9, "bold"), bd=0, padx=16, pady=9, cursor="hand2", activebackground="#5B744B").pack(side="left", padx=(0, 10))
        tk.Button(btn_frame, text="Reject Request", command=self.reject_request, bg="#F3BABA", fg="#35522B", font=("Segoe UI", 9, "bold"), bd=0, padx=16, pady=9, cursor="hand2", activebackground="#F8D0C8").pack(side="left")

        self.load_requests()

    def load_requests(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT id, username, email, status FROM password_resets WHERE status = 'Pending'")
        for row in cursor.fetchall():
            self.tree.insert("", tk.END, values=row)
        conn.close()

    def approve_request(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("Selection Required", "Please select a request to approve.")
            return
        values = self.tree.item(selected[0], "values")
        req_id, username = values[0], values[1]

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        default_hash = bcrypt.hashpw("Password123!".encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        cursor.execute("UPDATE users SET is_locked = 0, failed_attempts = 0, password_hash = ? WHERE username = ?", (default_hash, username))
        cursor.execute("UPDATE password_resets SET status = 'Approved' WHERE id = ?", (req_id,))
        conn.commit()
        sync_sqlite_to_supabase()
        conn.close()

        messagebox.showinfo("Success", f"Request approved for '{username}'. Account has been unlocked with temporary password: Password123!")
        self.load_requests()

    def reject_request(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("Selection Required", "Please select a request to reject.")
            return
        values = self.tree.item(selected[0], "values")
        req_id = values[0]

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("UPDATE password_resets SET status = 'Rejected' WHERE id = ?", (req_id,))
        conn.commit()
        sync_sqlite_to_supabase()
        conn.close()

        messagebox.showinfo("Success", "Password reset request rejected.")
        self.load_requests()


class RegisterFrame(tk.Frame):
    def __init__(self, parent, on_registered, on_back):
        super().__init__(parent, bg="#F9DDD8")
        self.parent = parent
        self.on_registered = on_registered
        self.on_back = on_back
        self.auth = AuthController()

        card = tk.Frame(self, bg="#F8D0C8", padx=40, pady=35, highlightbackground="#F3BABA", highlightthickness=1)
        card.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(card, text="Create Account", font=("Segoe UI", 18, "bold"), bg="#F8D0C8", fg="#35522B").pack(anchor="w", pady=(0, 4))
        tk.Label(card, text="Register to start managing hardware inventory", font=("Segoe UI", 10), bg="#F8D0C8", fg="#5B744B").pack(anchor="w", pady=(0, 20))

        tk.Label(card, text="Role", font=("Segoe UI", 9, "bold"), bg="#F8D0C8", fg="#35522B").pack(anchor="w")
        self.role_combobox = ttk.Combobox(card, values=["User", "Administrator"], width=33, state="readonly", font=("Segoe UI", 10))
        self.role_combobox.set("User")
        self.role_combobox.pack(anchor="w", pady=(4, 12))

        tk.Label(card, text="Username", font=("Segoe UI", 9, "bold"), bg="#F8D0C8", fg="#35522B").pack(anchor="w")
        self.entry_user = tk.Entry(card, width=35, font=("Segoe UI", 10), bg="#F9DDD8", fg="#35522B", insertbackground="#35522B", bd=0, relief="flat")
        self.entry_user.pack(anchor="w", pady=(4, 12), ipady=5)

        tk.Label(card, text="Email Address", font=("Segoe UI", 9, "bold"), bg="#F8D0C8", fg="#35522B").pack(anchor="w")
        self.entry_email = tk.Entry(card, width=35, font=("Segoe UI", 10), bg="#F9DDD8", fg="#35522B", insertbackground="#35522B", bd=0, relief="flat")
        self.entry_email.pack(anchor="w", pady=(4, 12), ipady=5)

        tk.Label(card, text="Password", font=("Segoe UI", 9, "bold"), bg="#F8D0C8", fg="#35522B").pack(anchor="w")
        self.entry_pass = tk.Entry(card, show="*", width=35, font=("Segoe UI", 10), bg="#F9DDD8", fg="#35522B", insertbackground="#35522B", bd=0, relief="flat")
        self.entry_pass.pack(anchor="w", pady=(4, 2), ipady=5)

        tk.Label(card, text="Min. 8 chars, 1 uppercase, 1 number, 1 special character", font=("Segoe UI", 8), bg="#F8D0C8", fg="#5B744B").pack(anchor="w", pady=(0, 10))

        self.show_pass_var = tk.BooleanVar()
        tk.Checkbutton(card, text="Show Password", variable=self.show_pass_var, command=self.toggle_password, font=("Segoe UI", 9), bg="#F8D0C8", fg="#5B744B", selectcolor="#F9DDD8", activebackground="#F8D0C8", activeforeground="#35522B").pack(anchor="w", pady=(0, 15))

        tk.Button(card, text="Complete Registration", command=self.handle_register, bg="#799567", fg="#FFFFFF", font=("Segoe UI", 10, "bold"), bd=0, cursor="hand2", width=33, pady=10, activebackground="#5B744B", activeforeground="#FFFFFF").pack(pady=(0, 10))
        tk.Button(card, text="Back to Login", command=self.on_back, bg="#F9DDD8", fg="#35522B", font=("Segoe UI", 9, "bold"), bd=0, cursor="hand2", width=37, pady=8, activebackground="#F3BABA", activeforeground="#35522B").pack()

    def toggle_password(self):
        if self.show_pass_var.get():
            self.entry_pass.config(show="")
        else:
            self.entry_pass.config(show="*")

    def handle_register(self):
        user = self.entry_user.get().strip()
        email = self.entry_email.get().strip()
        role = self.role_combobox.get().strip()
        pwd = self.entry_pass.get()

        success, msg = self.auth.register_user(user, email, pwd, role)
        if success:
            messagebox.showinfo("Success", msg)
            self.on_registered()
        else:
            messagebox.showwarning("Registration Failed", msg)


class UserProfileFrame(tk.Frame):
    def __init__(self, parent, current_user, on_back):
        super().__init__(parent, bg="#F9DDD8")
        self.parent = parent
        self.current_user = current_user
        self.on_back = on_back
        self.auth = AuthController()

        container = tk.Frame(self, bg="#F9DDD8")
        container.place(relx=0.5, rely=0.5, anchor="center", width=500, height=520)

        card = tk.Frame(container, bg="#F8D0C8", bd=1, relief="solid", highlightbackground="#F3BABA", padx=30, pady=30)
        card.pack(fill="both", expand=True)

        tk.Label(card, text="Account & Security", font=("Segoe UI", 16, "bold"), bg="#F8D0C8", fg="#35522B").pack(anchor="w", pady=(0, 2))
        tk.Label(card, text=f"Profile settings for @{current_user}", font=("Segoe UI", 9), bg="#F8D0C8", fg="#5B744B").pack(anchor="w", pady=(0, 20))

        profile = self.auth.get_user_profile(current_user)
        uname, uemail, urole = profile if profile else (current_user, "N/A", "N/A")

        info_box = tk.Frame(card, bg="#F9DDD8", padx=15, pady=12)
        info_box.pack(fill="x", pady=(0, 20))
        tk.Label(info_box, text=f"Username: {uname}", font=("Segoe UI", 9), bg="#F9DDD8", fg="#35522B").pack(anchor="w", pady=1)
        tk.Label(info_box, text=f"Email: {uemail}", font=("Segoe UI", 9), bg="#F9DDD8", fg="#35522B").pack(anchor="w", pady=1)
        tk.Label(info_box, text=f"Role: {urole}", font=("Segoe UI", 9, "bold"), bg="#F9DDD8", fg="#5B744B").pack(anchor="w", pady=1)

        tk.Label(card, text="Current Password", font=("Segoe UI", 9, "bold"), bg="#F8D0C8", fg="#35522B").pack(anchor="w")
        self.entry_curr = tk.Entry(card, show="*", width=38, font=("Segoe UI", 10), bg="#F9DDD8", fg="#35522B", insertbackground="#35522B", bd=0, relief="flat")
        self.entry_curr.pack(anchor="w", pady=(4, 10), ipady=5)

        tk.Label(card, text="New Password", font=("Segoe UI", 9, "bold"), bg="#F8D0C8", fg="#35522B").pack(anchor="w")
        self.entry_new = tk.Entry(card, show="*", width=38, font=("Segoe UI", 10), bg="#F9DDD8", fg="#35522B", insertbackground="#35522B", bd=0, relief="flat")
        self.entry_new.pack(anchor="w", pady=(4, 2), ipady=5)
        tk.Label(card, text="Min. 8 chars, 1 uppercase, 1 number, 1 special character", font=("Segoe UI", 7), bg="#F8D0C8", fg="#5B744B").pack(anchor="w", pady=(0, 10))

        self.show_pass_var = tk.BooleanVar()
        tk.Checkbutton(card, text="Show Passwords", variable=self.show_pass_var, command=self.toggle_password, font=("Segoe UI", 8), bg="#F8D0C8", fg="#5B744B", selectcolor="#F9DDD8", activebackground="#F8D0C8", activeforeground="#35522B").pack(anchor="w", pady=(0, 15))

        btn_row = tk.Frame(card, bg="#F8D0C8")
        btn_row.pack(fill="x")
        tk.Button(btn_row, text="Update Password", command=self.handle_password_change, bg="#799567", fg="#FFFFFF", font=("Segoe UI", 9, "bold"), bd=0, padx=15, pady=8, cursor="hand2", activebackground="#5B744B").pack(side="left")
        tk.Button(btn_row, text="Back to Catalog", command=self.on_back, bg="#F9DDD8", fg="#35522B", font=("Segoe UI", 9, "bold"), bd=0, padx=15, pady=8, cursor="hand2", activebackground="#F3BABA").pack(side="right")

    def toggle_password(self):
        show_char = "" if self.show_pass_var.get() else "*"
        self.entry_curr.config(show=show_char)
        self.entry_new.config(show=show_char)

    def handle_password_change(self):
        curr_p = self.entry_curr.get()
        new_p = self.entry_new.get()
        success, msg = self.auth.update_password(self.current_user, curr_p, new_p)
        if success:
            messagebox.showinfo("Success", msg)
            self.on_back()
        else:
            messagebox.showerror("Failed", msg)


class HardwareCatalogFrame(tk.Frame):
    def __init__(self, parent, current_user, on_logout, show_admin_approvals, show_profile):
        super().__init__(parent, bg="#F9DDD8")
        self.parent = parent
        self.current_user = current_user
        self.on_logout = on_logout
        self.show_admin_approvals = show_admin_approvals
        self.show_profile = show_profile
        self.controller = InventoryController()
        self.selected_item_id = None

        header_frame = tk.Frame(self, bg="#F8D0C8", padx=24, pady=14)
        header_frame.pack(fill="x")

        tk.Label(header_frame, text="⚡ Campus Hardware Inventory System", fg="#35522B", bg="#F8D0C8", font=("Segoe UI", 14, "bold")).pack(side="left")

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT role FROM users WHERE username = ?", (current_user,))
        user_row = cursor.fetchone()
        conn.close()
        user_role = user_row[0] if user_row else "User"

        btn_logout = tk.Button(header_frame, text="Logout", command=self.handle_logout, bg="#F3BABA", fg="#35522B", font=("Segoe UI", 9, "bold"), bd=0, padx=14, pady=6, cursor="hand2", activebackground="#F8D0C8")
        btn_logout.pack(side="right")

        if user_role == "Administrator":
            btn_approvals = tk.Button(header_frame, text="Admin Approvals", command=self.show_admin_approvals, bg="#A7B59E", fg="#FFFFFF", font=("Segoe UI", 9, "bold"), bd=0, padx=14, pady=6, cursor="hand2", activebackground="#799567")
            btn_approvals.pack(side="right", padx=8)

        if user_role != "Administrator":
            btn_profile = tk.Button(header_frame, text="Profile & Security", command=self.show_profile, bg="#799567", fg="#FFFFFF", font=("Segoe UI", 9, "bold"), bd=0, padx=14, pady=6, cursor="hand2", activebackground="#5B744B")
            btn_profile.pack(side="right", padx=8)

        workspace = tk.Frame(self, bg="#F9DDD8")
        workspace.pack(fill="both", expand=True, padx=20, pady=16)

        if user_role != "User":
            sidebar = tk.Frame(workspace, bg="#F8D0C8", bd=1, relief="solid", highlightbackground="#F3BABA", padx=16, pady=16)
            sidebar.pack(side="left", fill="y", padx=(0, 16))

            tk.Label(sidebar, text="Item Management", font=("Segoe UI", 12, "bold"), bg="#F8D0C8", fg="#35522B").pack(anchor="w", pady=(0, 14))

            tk.Label(sidebar, text="Item Name", font=("Segoe UI", 9, "bold"), bg="#F8D0C8", fg="#35522B").pack(anchor="w")
            self.entry_name = tk.Entry(sidebar, width=28, font=("Segoe UI", 10), bg="#F9DDD8", fg="#35522B", insertbackground="#35522B", bd=0, relief="flat")
            self.entry_name.pack(anchor="w", pady=(4, 12), ipady=5)

            tk.Label(sidebar, text="Category", font=("Segoe UI", 9, "bold"), bg="#F8D0C8", fg="#35522B").pack(anchor="w")
            self.entry_cat = tk.Entry(sidebar, width=28, font=("Segoe UI", 10), bg="#F9DDD8", fg="#35522B", insertbackground="#35522B", bd=0, relief="flat")
            self.entry_cat.pack(anchor="w", pady=(4, 12), ipady=5)

            tk.Label(sidebar, text="Quantity", font=("Segoe UI", 9, "bold"), bg="#F8D0C8", fg="#35522B").pack(anchor="w")
            self.entry_qty = tk.Entry(sidebar, width=28, font=("Segoe UI", 10), bg="#F9DDD8", fg="#35522B", insertbackground="#35522B", bd=0, relief="flat")
            self.entry_qty.pack(anchor="w", pady=(4, 12), ipady=5)

            tk.Label(sidebar, text="Unit Price (₱)", font=("Segoe UI", 9, "bold"), bg="#F8D0C8", fg="#35522B").pack(anchor="w")
            self.entry_price = tk.Entry(sidebar, width=28, font=("Segoe UI", 10), bg="#F9DDD8", fg="#35522B", insertbackground="#35522B", bd=0, relief="flat")
            self.entry_price.pack(anchor="w", pady=(4, 20), ipady=5)

            tk.Button(sidebar, text="Add New Item", command=self.add_item, bg="#799567", fg="#FFFFFF", font=("Segoe UI", 9, "bold"), bd=0, width=26, pady=8, cursor="hand2", activebackground="#5B744B").pack(anchor="w", pady=(0, 8))
            tk.Button(sidebar, text="Save Updates", command=self.update_record, bg="#A7B59E", fg="#FFFFFF", font=("Segoe UI", 9, "bold"), bd=0, width=26, pady=8, cursor="hand2", activebackground="#799567").pack(anchor="w", pady=(0, 8))
            tk.Button(sidebar, text="Clear Fields / Selection", command=self.clear_selection, bg="#F3BABA", fg="#35522B", font=("Segoe UI", 9, "bold"), bd=0, width=26, pady=8, cursor="hand2", activebackground="#F8D0C8").pack(anchor="w")

        main_panel = tk.Frame(workspace, bg="#F9DDD8")
        main_panel.pack(side="right", fill="both", expand=True)

        filter_card = tk.Frame(main_panel, bg="#F8D0C8", bd=1, relief="solid", highlightbackground="#F3BABA", padx=16, pady=12)
        filter_card.pack(fill="x", pady=(0, 12))

        tk.Label(filter_card, text="Search:", font=("Segoe UI", 9, "bold"), bg="#F8D0C8", fg="#35522B").pack(side="left", padx=(0, 6))
        self.entry_search = tk.Entry(filter_card, width=24, font=("Segoe UI", 9), bg="#F9DDD8", fg="#35522B", insertbackground="#35522B", bd=0, relief="flat")
        self.entry_search.pack(side="left", padx=(0, 20), ipady=3)
        self.entry_search.bind("<KeyRelease>", lambda e: self.refresh_grid())

        tk.Label(filter_card, text="Category Filter:", font=("Segoe UI", 9, "bold"), bg="#F8D0C8", fg="#35522B").pack(side="left", padx=(0, 6))
        self.cat_filter_combo = ttk.Combobox(filter_card, values=["All"], width=16, state="readonly", font=("Segoe UI", 9))
        self.cat_filter_combo.set("All")
        self.cat_filter_combo.pack(side="left", padx=(0, 20))
        self.cat_filter_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_grid())

        tk.Button(filter_card, text="Reset Filters", command=self.reset_filters, font=("Segoe UI", 9), bg="#F9DDD8", fg="#35522B", bd=0, padx=12, pady=5, cursor="hand2", activebackground="#F3BABA").pack(side="left")

        grid_card = tk.Frame(main_panel, bg="#F8D0C8", bd=1, relief="solid", highlightbackground="#F3BABA")
        grid_card.pack(fill="both", expand=True, pady=(0, 12))

        scroll_y = tk.Scrollbar(grid_card, orient=tk.VERTICAL)
        self.tree = ttk.Treeview(
            grid_card,
            columns=("Check", "ID", "Name", "Category", "Qty", "Price", "Status"),
            show="headings",
            yscrollcommand=scroll_y.set
        )
        scroll_y.config(command=self.tree.yview)
        scroll_y.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.heading("Check", text="☑")
        self.tree.heading("ID", text="ID")
        self.tree.heading("Name", text="Item Name")
        self.tree.heading("Category", text="Category")
        self.tree.heading("Qty", text="Qty")
        self.tree.heading("Price", text="Unit Price (₱)")
        self.tree.heading("Status", text="Status")

        self.tree.column("Check", width=45, anchor="center")
        self.tree.column("ID", width=55, anchor="center")
        self.tree.column("Name", width=220)
        self.tree.column("Category", width=150)
        self.tree.column("Qty", width=70, anchor="center")
        self.tree.column("Price", width=110, anchor="e")
        self.tree.column("Status", width=110, anchor="center")

        self.tree.pack(fill="both", expand=True, padx=1, pady=1)
        self.tree.bind("<Button-1>", self.on_tree_click)

        action_bar = tk.Frame(main_panel, bg="#F9DDD8")
        action_bar.pack(fill="x")

        if user_role != "User":
            tk.Button(action_bar, text="Deselect All", command=self.deselect_all, font=("Segoe UI", 9, "bold"), bg="#F8D0C8", fg="#35522B", bd=0, padx=14, pady=8, cursor="hand2", activebackground="#F3BABA").pack(side="left", padx=(0, 8))
            tk.Button(action_bar, text="Delete Checked Items", command=self.delete_selected_items_batch, font=("Segoe UI", 9, "bold"), bg="#F3BABA", fg="#35522B", bd=0, padx=14, pady=8, cursor="hand2", activebackground="#F8D0C8").pack(side="left")

        tk.Button(action_bar, text="Export Filtered/Selected to CSV", command=self.export_csv, font=("Segoe UI", 9, "bold"), bg="#A7B59E", fg="#FFFFFF", bd=0, padx=16, pady=8, cursor="hand2", activebackground="#799567").pack(side="right")

        self.update_category_dropdown()
        self.refresh_grid()

    def update_category_dropdown(self):
        cats = ["All"] + self.controller.get_categories()
        self.cat_filter_combo.config(values=cats)

    def reset_filters(self):
        self.entry_search.delete(0, tk.END)
        self.cat_filter_combo.set("All")
        self.refresh_grid()

    def deselect_all(self):
        self.tree.selection_remove(self.tree.selection())
        self.selected_item_id = None
        for item in self.tree.get_children():
            vals = list(self.tree.item(item, "values"))
            vals[0] = "☐"
            self.tree.item(item, values=vals)
        self.clear_entries()

    def clear_selection(self):
        self.deselect_all()

    def on_tree_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        if region == "cell":
            column = self.tree.identify_column(event.x)
            item_id = self.tree.identify_row(event.y)
            if column == "#1" and item_id:
                vals = list(self.tree.item(item_id, "values"))
                if vals[0] == "☑":
                    vals[0] = "☐"
                else:
                    vals[0] = "☑"
                    self.selected_item_id = vals[1]
                    if hasattr(self, 'entry_name'):
                        self.entry_name.delete(0, tk.END)
                        self.entry_name.insert(0, vals[2])
                        self.entry_cat.delete(0, tk.END)
                        self.entry_cat.insert(0, vals[3])
                        self.entry_qty.delete(0, tk.END)
                        self.entry_qty.insert(0, vals[4])
                        clean_price = vals[5].replace("₱", "").replace(",", "").strip()
                        self.entry_price.delete(0, tk.END)
                        self.entry_price.insert(0, clean_price)
                self.tree.item(item_id, values=vals)

    def add_item(self):
        name = self.entry_name.get().strip()
        cat = self.entry_cat.get().strip()
        qty = self.entry_qty.get().strip()
        price = self.entry_price.get().strip()

        res_type, msg = self.controller.add_item(name, cat, qty, price)

        if res_type == "added":
            messagebox.showinfo("Success", msg)
            self.clear_entries()
            self.update_category_dropdown()
            self.refresh_grid()
        elif res_type == "exists":
            messagebox.showwarning("Item Exists", msg)
        else:
            messagebox.showerror("Error", msg)

    def update_record(self):
        if not self.selected_item_id:
            messagebox.showwarning("Selection Required", "Please check/select an existing hardware record from the table to modify.")
            return

        name = self.entry_name.get().strip()
        cat = self.entry_cat.get().strip()
        qty = self.entry_qty.get().strip()
        price = self.entry_price.get().strip()

        res_type, msg = self.controller.update_item_record(self.selected_item_id, name, cat, qty, price)

        if res_type == "updated":
            messagebox.showinfo("Success", msg)
            self.clear_entries()
            self.selected_item_id = None
            self.update_category_dropdown()
            self.refresh_grid()
        else:
            messagebox.showerror("Error", msg)

    def delete_selected_items_batch(self):
        checked_ids = []
        for item in self.tree.get_children():
            vals = self.tree.item(item, "values")
            if vals[0] == "☑":
                checked_ids.append(vals[1])

        if not checked_ids:
            messagebox.showwarning("Selection Required", "Please check at least one item from the table to delete.")
            return

        confirm = messagebox.askyesno("Confirm Batch Deletion", f"Are you sure you want to delete {len(checked_ids)} selected item(s)?")
        if confirm:
            success, msg = self.controller.delete_items_batch(checked_ids)
            if success:
                messagebox.showinfo("Deleted", msg)
                self.selected_item_id = None
                self.clear_entries()
                self.update_category_dropdown()
                self.refresh_grid()
            else:
                messagebox.showerror("Error", msg)

    def clear_entries(self):
        if hasattr(self, 'entry_name'):
            self.entry_name.delete(0, tk.END)
        if hasattr(self, 'entry_cat'):
            self.entry_cat.delete(0, tk.END)
        if hasattr(self, 'entry_qty'):
            self.entry_qty.delete(0, tk.END)
        if hasattr(self, 'entry_price'):
            self.entry_price.delete(0, tk.END)

    def refresh_grid(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

        search_query = self.entry_search.get().strip()
        category_filter = self.cat_filter_combo.get()

        items = self.controller.fetch_all_items(search_query, category_filter)
        for row in items:
            item_id, name, cat, qty, price, status = row
            formatted_price = f"₱{price:,.2f}"
            self.tree.insert("", tk.END, values=("☐", item_id, name, cat, qty, formatted_price, status))

    def export_csv(self):
        checked_rows = []
        for item in self.tree.get_children():
            vals = self.tree.item(item, "values")
            if vals[0] == "☑":
                raw_price = vals[5].replace("₱", "").replace(",", "").strip()
                checked_rows.append((vals[1], vals[2], vals[3], int(vals[4]), float(raw_price), vals[6]))

        if checked_rows:
            success, msg = self.controller.export_to_csv(selected_items=checked_rows)
        else:
            success, msg = self.controller.export_to_csv()
            if success:
                msg = "No items checked. Exported all items successfully!"

        if success:
            messagebox.showinfo("Export Successful", msg)
        else:
            messagebox.showerror("Export Failed", msg)

    def handle_logout(self):
        self.on_logout()

# 6. APPLICATION CONTAINER & ENTRY POINT

class AppContainer(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Campus Hardware Inventory System")
        self.geometry("1080x740")

        try:
            self.iconbitmap("")
        except Exception:
            pass

        apply_global_styles()

        self.current_frame = None
        self.show_login()

    def show_login(self):
        if self.current_frame:
            self.current_frame.destroy()
        self.current_frame = LoginFrame(
            self,
            on_login_success=self.show_catalog,
            on_register=self.show_register,
            on_reset_request=self.show_reset_request
        )
        self.current_frame.pack(fill="both", expand=True)

    def show_register(self):
        if self.current_frame:
            self.current_frame.destroy()
        self.current_frame = RegisterFrame(
            self,
            on_registered=self.show_login,
            on_back=self.show_login
        )
        self.current_frame.pack(fill="both", expand=True)

    def show_reset_request(self):
        if self.current_frame:
            self.current_frame.destroy()
        self.current_frame = ResetRequestFrame(
            self,
            on_back=self.show_login
        )
        self.current_frame.pack(fill="both", expand=True)

    def show_admin_approvals(self, username):
        if self.current_frame:
            self.current_frame.destroy()
        self.current_frame = AdminApprovalsFrame(
            self,
            on_back=lambda: self.show_catalog(username)
        )
        self.current_frame.pack(fill="both", expand=True)

    def show_profile(self, username):
        if self.current_frame:
            self.current_frame.destroy()
        self.current_frame = UserProfileFrame(
            self,
            current_user=username,
            on_back=lambda: self.show_catalog(username)
        )
        self.current_frame.pack(fill="both", expand=True)

    def show_catalog(self, username):
        if self.current_frame:
            self.current_frame.destroy()
        self.current_frame = HardwareCatalogFrame(
            self, 
            current_user=username, 
            on_logout=self.show_login,
            show_admin_approvals=lambda: self.show_admin_approvals(username),
            show_profile=lambda: self.show_profile(username)
        )
        self.current_frame.pack(fill="both", expand=True)

if __name__ == "__main__":
    init_db()
    app = AppContainer()
    app.mainloop()