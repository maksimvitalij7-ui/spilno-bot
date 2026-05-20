"""
База данных — SQLite для хранения данных сотрудников, отметок и отчётов
"""

import sqlite3
from datetime import date, datetime
from config import ADMIN_IDS

DB_PATH = "staff_bot.db"

class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS employees (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                name        TEXT NOT NULL,
                username    TEXT DEFAULT '',
                is_admin    INTEGER DEFAULT 0,
                created_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS checkins (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id     INTEGER NOT NULL,
                check_date      TEXT NOT NULL,
                checkin_status  TEXT NOT NULL,
                latitude        REAL,
                longitude       REAL,
                distance_m      REAL,
                created_at      TEXT DEFAULT (datetime('now')),
                UNIQUE(telegram_id, check_date)
            );

            CREATE TABLE IF NOT EXISTS reports (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                report_date TEXT NOT NULL,
                report_text TEXT NOT NULL,
                created_at  TEXT DEFAULT (datetime('now')),
                UNIQUE(telegram_id, report_date)
            );
        """)
        self.conn.commit()

    # ── Сотрудники ────────────────────────────────────────────────────────────

    def register_employee(self, telegram_id: int, name: str, username: str):
        is_admin = 1 if telegram_id in ADMIN_IDS else 0
        self.conn.execute("""
            INSERT INTO employees (telegram_id, name, username, is_admin)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                name = excluded.name,
                username = excluded.username
        """, (telegram_id, name, username, is_admin))
        self.conn.commit()

    def get_all_employees(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM employees ORDER BY name"
        ).fetchall()]

    def get_admin_ids(self):
        rows = self.conn.execute(
            "SELECT telegram_id FROM employees WHERE is_admin = 1"
        ).fetchall()
        return [r["telegram_id"] for r in rows] + list(ADMIN_IDS)

    # ── Отметки ───────────────────────────────────────────────────────────────

    def save_checkin(self, telegram_id: int, status: str,
                     lat, lon, distance):
        today = date.today().isoformat()
        self.conn.execute("""
            INSERT INTO checkins (telegram_id, check_date, checkin_status,
                                  latitude, longitude, distance_m)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id, check_date) DO UPDATE SET
                checkin_status = excluded.checkin_status,
                latitude = excluded.latitude,
                longitude = excluded.longitude,
                distance_m = excluded.distance_m
        """, (telegram_id, today, status, lat, lon, distance))
        self.conn.commit()

    def get_today_status(self, telegram_id: int):
        today = date.today().isoformat()
        checkin = self.conn.execute(
            "SELECT * FROM checkins WHERE telegram_id=? AND check_date=?",
            (telegram_id, today)
        ).fetchone()
        report = self.conn.execute(
            "SELECT * FROM reports WHERE telegram_id=? AND report_date=?",
            (telegram_id, today)
        ).fetchone()
        result = dict(checkin) if checkin else {}
        if report:
            result["report_text"] = report["report_text"]
        return result

    def get_no_response_employees(self):
        today = date.today().isoformat()
        return [dict(r) for r in self.conn.execute("""
            SELECT e.telegram_id, e.name FROM employees e
            WHERE e.is_admin = 0
            AND e.telegram_id NOT IN (
                SELECT telegram_id FROM checkins WHERE check_date = ?
            )
        """, (today,)).fetchall()]

    def get_today_locations(self):
        today = date.today().isoformat()
        return [dict(r) for r in self.conn.execute("""
            SELECT e.name, c.latitude, c.longitude, c.distance_m as distance
            FROM employees e
            LEFT JOIN checkins c ON e.telegram_id = c.telegram_id AND c.check_date = ?
            WHERE e.is_admin = 0
            ORDER BY e.name
        """, (today,)).fetchall()]

    # ── Отчёты ────────────────────────────────────────────────────────────────

    def save_report(self, telegram_id: int, text: str):
        today = date.today().isoformat()
        self.conn.execute("""
            INSERT INTO reports (telegram_id, report_date, report_text)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_id, report_date) DO UPDATE SET
                report_text = excluded.report_text
        """, (telegram_id, today, text))
        self.conn.commit()

    def get_today_reports(self):
        today = date.today().isoformat()
        return [dict(r) for r in self.conn.execute("""
            SELECT e.name, r.report_text
            FROM employees e
            LEFT JOIN reports r ON e.telegram_id = r.telegram_id AND r.report_date = ?
            WHERE e.is_admin = 0
            ORDER BY e.name
        """, (today,)).fetchall()]

    # ── Сводка ────────────────────────────────────────────────────────────────

    def get_today_summary(self):
        today = date.today().isoformat()
        total = self.conn.execute("SELECT COUNT(*) as cnt FROM employees WHERE is_admin = 0").fetchone()["cnt"]

        def count_status(statuses):
            placeholders = ",".join("?" * len(statuses))
            return self.conn.execute(f"""
                SELECT COUNT(*) as cnt FROM checkins
                WHERE check_date = ? AND checkin_status IN ({placeholders})
            """, [today] + list(statuses)).fetchone()["cnt"]

        at_office   = count_status(("at_office",))
        remote      = count_status(("remote", "at_work_remote_loc"))
        on_the_way  = count_status(("on_the_way",))
        absent      = count_status(("sick", "day_off"))
        checked_in  = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM checkins WHERE check_date=?", (today,)
        ).fetchone()["cnt"]
        no_response = total - checked_in

        reports_submitted = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM reports WHERE report_date=?", (today,)
        ).fetchone()["cnt"]

        return {
            "at_office": at_office,
            "remote": remote,
            "on_the_way": on_the_way,
            "absent": absent,
            "no_response": no_response,
            "reports_submitted": reports_submitted,
            "reports_missing": total - reports_submitted,
        }

    # ── Данные для экспорта ───────────────────────────────────────────────────

    def get_export_data(self, days=30):
        return [dict(r) for r in self.conn.execute("""
            SELECT
                e.name,
                e.username,
                c.check_date,
                c.checkin_status,
                c.distance_m,
                r.report_text
            FROM employees e
            LEFT JOIN checkins c ON e.telegram_id = c.telegram_id
            LEFT JOIN reports r  ON e.telegram_id = r.telegram_id
                                 AND r.report_date = c.check_date
            WHERE c.check_date >= date('now', '-' || ? || ' days')
            ORDER BY c.check_date DESC, e.name
        """, (days,)).fetchall()]
