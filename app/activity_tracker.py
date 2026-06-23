#!/usr/bin/env python
from __future__ import annotations

import csv
import ctypes
import html
import json
import os
import re
import sqlite3
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, X, Y, Button, Canvas, Entry, Frame, Label, StringVar, Text, Tk, filedialog, font as tkfont, ttk
from urllib.parse import urlparse

import pystray
from PIL import Image, ImageDraw, ImageFont

APP_NAME = "TrackTivity"
HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8765
INSTANCE_MUTEX_NAME = "Local\\TrackTivityDesktopApp"
PH_TZ = timezone(timedelta(hours=8), name="PHT")
FOCUS_POLL_SECONDS = 1
SESSION_SAVE_INTERVAL_SECONDS = 5
BROWSER_PROCESSES = {
    "chrome.exe": "Chrome",
    "msedge.exe": "Edge",
    "firefox.exe": "Firefox",
}
IDLE_TIMEOUT_SECONDS = 180
DASHBOARD_RANGES = {
    "Today": "today",
    "This week": "week",
    "This month": "month",
    "All time": "all",
}
DASHBOARD_COLORS = ("#2563eb", "#16a34a", "#f97316", "#9333ea", "#0f766e")


def now_utc_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def to_ph_time(value: str) -> datetime:
    return parse_iso(value).astimezone(PH_TZ)


def format_ph_datetime(value: str) -> str:
    return to_ph_time(value).strftime("%Y-%m-%d %H:%M:%S")


def format_ph_date(value: str) -> str:
    return to_ph_time(value).strftime("%Y-%m-%d")


def display_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def today_bounds() -> tuple[str, str]:
    return ph_date_bounds(datetime.now(PH_TZ).date())


def ph_date_bounds(day: date) -> tuple[str, str]:
    ph_start = datetime(day.year, day.month, day.day, tzinfo=PH_TZ)
    ph_end = ph_start + timedelta(days=1)
    start = ph_start.astimezone(UTC)
    end = ph_end.astimezone(UTC)
    return start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")


def ph_range_bounds(start_day: date, end_day: date) -> tuple[str, str]:
    start, _ = ph_date_bounds(start_day)
    _, end = ph_date_bounds(end_day)
    return start, end


def this_week_bounds() -> tuple[str, str]:
    ph_today = datetime.now(PH_TZ).date()
    week_start = ph_today - timedelta(days=ph_today.weekday())
    return ph_range_bounds(week_start, ph_today)


def this_month_bounds() -> tuple[str, str]:
    ph_today = datetime.now(PH_TZ).date()
    month_start = ph_today.replace(day=1)
    return ph_range_bounds(month_start, ph_today)


def dashboard_range_bounds(range_key: str) -> tuple[str | None, str | None]:
    if range_key == "week":
        return this_week_bounds()
    if range_key == "month":
        return this_month_bounds()
    if range_key == "all":
        return None, None
    return today_bounds()


def dashboard_range_label(range_key: str) -> str:
    labels = {
        "today": "today",
        "week": "this week",
        "month": "this month",
        "all": "of all time",
    }
    return labels.get(range_key, "today")


def parse_date_entry(value: str) -> date | None:
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def current_ph_date_text() -> str:
    return datetime.now(PH_TZ).strftime("%Y-%m-%d")


def local_data_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "data"

    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "TrackTivity"
    return Path.cwd() / "data"


def already_running() -> bool:
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW(None, False, INSTANCE_MUTEX_NAME)
    return kernel32.GetLastError() == 183


@dataclass(frozen=True)
class ActivityState:
    activity_type: str
    app_name: str
    process_name: str
    window_title: str
    domain: str
    page_title: str
    category: str = "uncategorized"

    def identity(self) -> tuple[str, str, str, str]:
        return (self.activity_type, self.app_name, self.process_name, self.domain or self.window_title)


class ActivityDatabase:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.migrate()

    def migrate(self) -> None:
        with self.lock:
            self.conn.executescript(
                """
                create table if not exists activity_sessions (
                    id integer primary key autoincrement,
                    started_at text not null,
                    ended_at text not null,
                    duration_seconds integer not null default 0,
                    activity_type text not null,
                    app_name text not null,
                    process_name text not null,
                    window_title text not null default '',
                    domain text not null default '',
                    page_title text not null default '',
                    category text not null default 'uncategorized',
                    source text not null default 'windows'
                );

                create index if not exists idx_activity_sessions_started_at
                    on activity_sessions(started_at);

                create table if not exists settings (
                    key text primary key,
                    value text not null
                );
                """
            )
            self.conn.commit()
            self.ensure_default_settings()
            self.repair_inferred_browser_sessions()

    def ensure_default_settings(self) -> None:
        row = self.conn.execute(
            "select value from settings where key = 'idle_timeout_seconds'"
        ).fetchone()
        if not row or row["value"] == "300":
            self.conn.execute(
                "insert into settings(key, value) values('idle_timeout_seconds', ?) "
                "on conflict(key) do update set value = excluded.value",
                (str(IDLE_TIMEOUT_SECONDS),),
            )
            self.conn.commit()

    def get_setting(self, key: str, default: str) -> str:
        with self.lock:
            row = self.conn.execute("select value from settings where key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def repair_inferred_browser_sessions(self) -> None:
        rows = list(
            self.conn.execute(
                """
                select id, process_name, window_title, page_title
                from activity_sessions
                where activity_type = 'application'
                  and domain = ''
                  and process_name in ('chrome.exe', 'msedge.exe', 'firefox.exe')
                """
            )
        )
        for row in rows:
            inferred = infer_browser_page_from_title(row["page_title"] or row["window_title"])
            if not inferred:
                continue
            domain, title = inferred
            self.conn.execute(
                """
                update activity_sessions
                set activity_type = 'website',
                    app_name = ?,
                    domain = ?,
                    page_title = ?
                where id = ?
                """,
                (BROWSER_PROCESSES.get(row["process_name"], "Browser"), domain, title, row["id"]),
            )
        if rows:
            self.conn.commit()

    def set_setting(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute(
                "insert into settings(key, value) values(?, ?) "
                "on conflict(key) do update set value = excluded.value",
                (key, value),
            )
            self.conn.commit()

    def start_session(self, state: ActivityState, started_at: str) -> int:
        with self.lock:
            cursor = self.conn.execute(
                """
                insert into activity_sessions (
                    started_at, ended_at, duration_seconds, activity_type, app_name,
                    process_name, window_title, domain, page_title, category, source
                ) values (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, 'windows')
                """,
                (
                    started_at,
                    started_at,
                    state.activity_type,
                    state.app_name,
                    state.process_name,
                    state.window_title,
                    state.domain,
                    state.page_title,
                    state.category,
                ),
            )
            self.conn.commit()
            return int(cursor.lastrowid)

    def update_session(self, session_id: int, ended_at: str) -> None:
        with self.lock:
            row = self.conn.execute(
                "select started_at from activity_sessions where id = ?", (session_id,)
            ).fetchone()
            if not row:
                return
            duration = int((parse_iso(ended_at) - parse_iso(row["started_at"])).total_seconds())
            self.conn.execute(
                "update activity_sessions set ended_at = ?, duration_seconds = ? where id = ?",
                (ended_at, max(0, duration), session_id),
            )
            self.conn.commit()

    def sessions_between(self, start: str, end: str) -> list[sqlite3.Row]:
        with self.lock:
            return list(
                self.conn.execute(
                    """
                    select * from activity_sessions
                    where started_at < ? and ended_at >= ?
                    order by started_at asc
                    """,
                    (end, start),
                )
            )

    def available_days_between(self, start: str | None = None, end: str | None = None) -> list[sqlite3.Row]:
        clauses = []
        params: list[str] = []
        if start:
            clauses.append("started_at >= ?")
            params.append(start)
        if end:
            clauses.append("started_at < ?")
            params.append(end)
        where = "where " + " and ".join(clauses) if clauses else ""
        with self.lock:
            return list(
                self.conn.execute(
                    f"""
                    select date(started_at, '+8 hours') as day,
                           count(*) as session_count,
                           coalesce(sum(case when activity_type != 'idle' then duration_seconds else 0 end), 0) as active_total
                    from activity_sessions
                    {where}
                    group by day
                    order by day desc
                    """,
                    tuple(params),
                )
            )

    def aggregate_dashboard(self, range_key: str) -> tuple[list[sqlite3.Row], list[sqlite3.Row], int, int]:
        start, end = dashboard_range_bounds(range_key)
        clauses = []
        params: list[str] = []
        if start:
            clauses.append("started_at >= ?")
            params.append(start)
        if end:
            clauses.append("started_at < ?")
            params.append(end)
        bounds = " and " + " and ".join(clauses) if clauses else ""
        with self.lock:
            apps = list(
                self.conn.execute(
                    f"""
                    select app_name, sum(duration_seconds) as total
                    from activity_sessions
                    where activity_type != 'idle'{bounds}
                    group by app_name
                    order by total desc
                    """,
                    tuple(params),
                )
            )
            domains = list(
                self.conn.execute(
                    f"""
                    select domain, sum(duration_seconds) as total
                    from activity_sessions
                    where domain != ''{bounds}
                    group by domain
                    order by total desc
                    """,
                    tuple(params),
                )
            )
            total_row = self.conn.execute(
                f"""
                select
                    coalesce(sum(case when activity_type != 'idle' then duration_seconds else 0 end), 0) as active_total,
                    coalesce(sum(case when activity_type = 'idle' then duration_seconds else 0 end), 0) as idle_total
                from activity_sessions
                where 1 = 1{bounds}
                """,
                tuple(params),
            ).fetchone()
            active_total = int(total_row["active_total"] if total_row else 0)
            idle_total = int(total_row["idle_total"] if total_row else 0)
            return apps, domains, active_total, idle_total


class WindowsForegroundReader:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def __init__(self) -> None:
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32

    def foreground(self) -> tuple[str, str, str]:
        hwnd = self.user32.GetForegroundWindow()
        if not hwnd:
            return "Unknown", "", ""

        title_length = self.user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(title_length + 1)
        self.user32.GetWindowTextW(hwnd, buffer, title_length + 1)
        window_title = buffer.value

        pid = ctypes.c_ulong()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process_name = self._process_name(pid.value)
        app_name = self._friendly_app_name(process_name)
        return app_name, process_name, window_title

    def idle_seconds(self) -> int:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        self.user32.GetLastInputInfo(ctypes.byref(info))
        millis = self.kernel32.GetTickCount() - info.dwTime
        return int(millis / 1000)

    def _process_name(self, pid: int) -> str:
        handle = self.kernel32.OpenProcess(self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = ctypes.c_ulong(260)
            buffer = ctypes.create_unicode_buffer(size.value)
            ok = self.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size))
            if ok:
                return Path(buffer.value).name.lower()
            return ""
        finally:
            self.kernel32.CloseHandle(handle)

    def _friendly_app_name(self, process_name: str) -> str:
        known = {
            "chrome.exe": "Google Chrome",
            "msedge.exe": "Microsoft Edge",
            "firefox.exe": "Firefox",
            "explorer.exe": "File Explorer",
            "notepad.exe": "Notepad",
            "code.exe": "Visual Studio Code",
        }
        if process_name in known:
            return known[process_name]
        if process_name:
            return Path(process_name).stem.title()
        return "Unknown"


class BrowserState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.payload: dict[str, object] = {}
        self.updated_at = 0.0

    def update(self, payload: dict[str, object]) -> None:
        url = str(payload.get("url", ""))
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        clean = {
            "browser": str(payload.get("browser", ""))[:80],
            "domain": domain[:255],
            "title": str(payload.get("title", ""))[:500],
            "windowFocused": bool(payload.get("windowFocused", True)),
        }
        with self.lock:
            self.payload = clean
            self.updated_at = time.time()

    def current(self, max_age_seconds: int = 3) -> dict[str, object] | None:
        with self.lock:
            if not self.payload or time.time() - self.updated_at > max_age_seconds:
                return None
            if not self.payload.get("windowFocused", True):
                return None
            return dict(self.payload)

    def matching_window(self, browser_name: str, window_title: str, max_age_seconds: int = 300) -> dict[str, object] | None:
        with self.lock:
            if not self.payload or time.time() - self.updated_at > max_age_seconds:
                return None
            payload = dict(self.payload)

        if payload.get("browser") and str(payload["browser"]).lower() != browser_name.lower():
            return None

        page_title = normalize_title(str(payload.get("title", "")))
        active_window = normalize_title(window_title)
        if not page_title or not active_window:
            return None
        if page_title in active_window or active_window in page_title:
            return payload
        return None


def normalize_title(value: str) -> str:
    value = value.lower()
    value = re.sub(r"\s+and\s+\d+\s+more\s+pages?.*$", "", value)
    value = re.sub(r"\s+-\s+(microsoft|google)\s+edge.*$", "", value)
    value = re.sub(r"\s+-\s+google chrome.*$", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def infer_browser_page_from_title(window_title: str) -> tuple[str, str] | None:
    title = window_title.lower()
    if "facebook" in title:
        return "facebook.com", "Facebook"
    if "youtube" in title:
        return "youtube.com", "YouTube"
    if "google search" in title or " - google" in title:
        return "google.com", "Google"
    if "new tab" in title:
        return "newtab", "New tab"
    if "extensions" in title:
        return "extensions", "Extensions"
    return None


class ExtensionServer:
    def __init__(self, browser_state: BrowserState) -> None:
        self.browser_state = browser_state
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        browser_state = self.browser_state

        class Handler(BaseHTTPRequestHandler):
            def do_OPTIONS(self) -> None:
                self._send_headers(204)

            def do_POST(self) -> None:
                if self.path != "/active-tab":
                    self._send_headers(404)
                    self.wfile.write(b"not found")
                    return
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(min(length, 8192))
                try:
                    payload = json.loads(body.decode("utf-8"))
                    if isinstance(payload, dict):
                        browser_state.update(payload)
                    self._send_headers(204)
                except Exception:
                    self._send_headers(400)
                    self.wfile.write(b"bad request")

            def log_message(self, format: str, *args: object) -> None:
                return

            def _send_headers(self, status: int) -> None:
                self.send_response(status)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "content-type")
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                self.end_headers()

        self.server = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()


class ActivityRecorder:
    def __init__(self, db: ActivityDatabase, browser_state: BrowserState) -> None:
        self.db = db
        self.browser_state = browser_state
        self.reader = WindowsForegroundReader()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.current_state: ActivityState | None = None
        self.current_session_id: int | None = None
        self.last_session_save_at = 0.0
        self.paused = False
        self.status = "Starting"

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        if paused:
            self._close_current(now_utc_iso())
            self.status = "Paused"

    def _run(self) -> None:
        while not self.stop_event.is_set():
            tick = now_utc_iso()
            try:
                self._sample(tick)
                self.status = "Paused" if self.paused else "Tracking"
            except Exception as exc:
                self.status = f"Tracker error: {exc}"
            self.stop_event.wait(FOCUS_POLL_SECONDS)
        self._close_current(now_utc_iso())

    def _sample(self, tick: str) -> None:
        if self.paused:
            return

        idle_limit = int(self.db.get_setting("idle_timeout_seconds", str(IDLE_TIMEOUT_SECONDS)))
        if self.reader.idle_seconds() >= idle_limit:
            state = ActivityState("idle", "Idle", "", "", "", "Idle")
        else:
            app_name, process_name, window_title = self.reader.foreground()
            state = ActivityState("application", app_name, process_name, window_title, "", window_title)
            browser_name = BROWSER_PROCESSES.get(process_name)
            if browser_name:
                browser = self.browser_state.current()
                if not browser:
                    browser = self.browser_state.matching_window(browser_name, window_title)

                if browser and browser.get("domain"):
                    domain = str(browser.get("domain", ""))
                    title = str(browser.get("title", "")) or window_title
                    state = ActivityState(
                        "website",
                        browser_name,
                        process_name,
                        window_title,
                        domain,
                        title,
                    )
                else:
                    inferred = infer_browser_page_from_title(window_title)
                    if inferred:
                        domain, title = inferred
                        state = ActivityState(
                            "website",
                            browser_name,
                            process_name,
                            window_title,
                            domain,
                            title,
                        )

        if self.current_state and self.current_state.identity() == state.identity():
            if (
                self.current_session_id
                and time.monotonic() - self.last_session_save_at >= SESSION_SAVE_INTERVAL_SECONDS
            ):
                self.db.update_session(self.current_session_id, tick)
                self.last_session_save_at = time.monotonic()
            return

        self._close_current(tick)
        self.current_state = state
        self.current_session_id = self.db.start_session(state, tick)
        self.last_session_save_at = time.monotonic()

    def _close_current(self, tick: str) -> None:
        if self.current_session_id:
            self.db.update_session(self.current_session_id, tick)
        self.current_state = None
        self.current_session_id = None
        self.last_session_save_at = 0.0


class TrayController:
    def __init__(self, root: Tk, open_dashboard: callable, quit_app: callable) -> None:
        self.root = root
        self.open_dashboard = open_dashboard
        self.quit_app = quit_app
        self.icon = pystray.Icon(
            APP_NAME,
            self._create_icon_image(),
            APP_NAME,
            pystray.Menu(
                pystray.MenuItem("Open Dashboard", self._open_dashboard, default=True),
                pystray.MenuItem("Quit", self._quit_app),
            ),
        )
        self.thread = threading.Thread(target=self.icon.run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.icon.stop()

    def _open_dashboard(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        self.root.after(0, self.open_dashboard)

    def _quit_app(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        self.root.after(0, self.quit_app)

    def _create_icon_image(self) -> Image.Image:
        image = Image.new("RGBA", (64, 64), "#2563eb")
        draw = ImageDraw.Draw(image)
        draw.rectangle((6, 6, 58, 58), outline="#ffffff", width=3)
        try:
            font = ImageFont.truetype("arial.ttf", 24)
        except OSError:
            font = ImageFont.load_default()
        text = "TT"
        bbox = draw.textbbox((0, 0), text, font=font)
        x = (64 - (bbox[2] - bbox[0])) / 2
        y = (64 - (bbox[3] - bbox[1])) / 2 - 1
        draw.text((x, y), text, fill="#ffffff", font=font)
        return image


class DashboardApp:
    def __init__(self, root: Tk, db: ActivityDatabase, recorder: ActivityRecorder, server: ExtensionServer) -> None:
        self.root = root
        self.db = db
        self.recorder = recorder
        self.server = server
        self.tray: TrayController | None = None
        self.refresh_after_id: str | None = None
        self.is_quitting = False
        self.status_var = StringVar(value="Starting")
        self.dashboard_range_var = StringVar(value="Today")
        self.dashboard_title_var = StringVar(value="Today")
        self.total_var = StringVar(value="Active: 0s")
        self.idle_var = StringVar(value="Idle: 0s")
        self.apps_title_var = StringVar(value="Top apps today")
        self.domains_title_var = StringVar(value="Top websites today")
        self.pause_var = StringVar(value="Pause")
        self.timeline_domain_filter_var = StringVar(value="All rows")
        self.download_range_var = StringVar(value="All available")
        self.download_start_var = StringVar(value=current_ph_date_text())
        self.download_end_var = StringVar(value=current_ph_date_text())
        self.root.title(APP_NAME)
        self.root.geometry("980x660")
        self._build()
        self.refresh()
        self.tray = TrayController(self.root, self.open_dashboard, self.quit_application)
        self.tray.start()

    def _build(self) -> None:
        top = Frame(self.root, padx=12, pady=10)
        top.pack(fill=X)
        Label(top, text=APP_NAME, font=("Segoe UI", 16, "bold")).pack(side=LEFT)
        Label(top, textvariable=self.status_var, padx=16).pack(side=LEFT)
        Button(top, textvariable=self.pause_var, command=self.toggle_pause, width=10).pack(side=RIGHT)

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=BOTH, expand=True, padx=12, pady=(0, 12))

        self.dashboard_tab = Frame(notebook, padx=12, pady=12)
        self.timeline_tab = Frame(notebook, padx=12, pady=12)
        self.download_tab = Frame(notebook, padx=12, pady=12)
        self.guide_tab = Frame(notebook, padx=12, pady=12)
        self.settings_tab = Frame(notebook, padx=12, pady=12)
        notebook.add(self.dashboard_tab, text="Dashboard")
        notebook.add(self.timeline_tab, text="Timeline")
        notebook.add(self.download_tab, text="Download Data")
        notebook.add(self.guide_tab, text="Guide")
        notebook.add(self.settings_tab, text="Settings")

        self._build_dashboard()
        self._build_timeline()
        self._build_download()
        self._build_guide()
        self._build_settings()

    def _build_dashboard(self) -> None:
        self.dashboard_canvas = Canvas(self.dashboard_tab, highlightthickness=0, background="#f3f4f6")
        dashboard_scrollbar = ttk.Scrollbar(self.dashboard_tab, orient="vertical", command=self.dashboard_canvas.yview)
        self.dashboard_canvas.configure(yscrollcommand=dashboard_scrollbar.set)
        dashboard_scrollbar.pack(side=RIGHT, fill=Y)
        self.dashboard_canvas.pack(side=LEFT, fill=BOTH, expand=True)

        self.dashboard_content = Frame(self.dashboard_canvas, background="#f3f4f6")
        self.dashboard_content_window = self.dashboard_canvas.create_window(
            (0, 0),
            window=self.dashboard_content,
            anchor="nw",
        )
        self.dashboard_content.bind(
            "<Configure>",
            lambda _event: self.dashboard_canvas.configure(scrollregion=self.dashboard_canvas.bbox("all")),
        )
        self.dashboard_canvas.bind(
            "<Configure>",
            lambda event: self.dashboard_canvas.itemconfigure(self.dashboard_content_window, width=event.width),
        )
        self.dashboard_canvas.bind("<Enter>", self._bind_dashboard_mousewheel)
        self.dashboard_canvas.bind("<Leave>", self._unbind_dashboard_mousewheel)

        header = Frame(self.dashboard_content, background="#f3f4f6")
        header.pack(fill=X)
        summary = Frame(header, background="#f3f4f6")
        summary.pack(side=LEFT, fill=X, expand=True)
        Label(summary, textvariable=self.dashboard_title_var, font=("Segoe UI", 18, "bold"), background="#f3f4f6").pack(anchor="w")
        metrics = Frame(summary, background="#f3f4f6")
        metrics.pack(anchor="w", pady=(6, 0))
        Label(metrics, textvariable=self.total_var, font=("Segoe UI", 11, "bold"), foreground="#1f2937", background="#f3f4f6").pack(side=LEFT, padx=(0, 18))
        Label(metrics, textvariable=self.idle_var, font=("Segoe UI", 11), foreground="#4b5563", background="#f3f4f6").pack(side=LEFT)

        range_picker = ttk.Combobox(
            header,
            textvariable=self.dashboard_range_var,
            values=tuple(DASHBOARD_RANGES.keys()),
            state="readonly",
            width=14,
        )
        range_picker.pack(side=RIGHT, pady=(4, 0))
        range_picker.bind("<<ComboboxSelected>>", lambda _event: self.refresh())

        charts = Frame(self.dashboard_content, background="#f3f4f6")
        charts.pack(fill=BOTH, expand=True, pady=(16, 0))

        left = Frame(charts, background="#f3f4f6")
        left.pack(side=LEFT, fill=BOTH, expand=True, padx=(0, 8))
        Label(left, textvariable=self.apps_title_var, font=("Segoe UI", 11, "bold"), background="#f3f4f6").pack(anchor="w")
        apps_list = Frame(left, background="#f3f4f6")
        apps_list.pack(fill=X, pady=(6, 10))
        self.apps_canvas = Canvas(apps_list, height=320, background="#fbfcfe", highlightthickness=1, highlightbackground="#d9dde5")
        apps_scrollbar = ttk.Scrollbar(apps_list, orient="vertical", command=self.apps_canvas.yview)
        self.apps_canvas.configure(yscrollcommand=apps_scrollbar.set)
        self.apps_canvas.bind("<MouseWheel>", lambda event: self._on_list_mousewheel(event, self.apps_canvas))
        self.apps_canvas.pack(side=LEFT, fill=X, expand=True)
        apps_scrollbar.pack(side=RIGHT, fill=Y)
        self.apps_pie_canvas = Canvas(left, height=210, background="#fbfcfe", highlightthickness=1, highlightbackground="#d9dde5")
        self.apps_pie_canvas.pack(fill=BOTH, expand=True)

        right = Frame(charts, background="#f3f4f6")
        right.pack(side=RIGHT, fill=BOTH, expand=True, padx=(8, 0))
        Label(right, textvariable=self.domains_title_var, font=("Segoe UI", 11, "bold"), background="#f3f4f6").pack(anchor="w")
        domains_list = Frame(right, background="#f3f4f6")
        domains_list.pack(fill=X, pady=(6, 10))
        self.domains_canvas = Canvas(domains_list, height=320, background="#fbfcfe", highlightthickness=1, highlightbackground="#d9dde5")
        domains_scrollbar = ttk.Scrollbar(domains_list, orient="vertical", command=self.domains_canvas.yview)
        self.domains_canvas.configure(yscrollcommand=domains_scrollbar.set)
        self.domains_canvas.bind("<MouseWheel>", lambda event: self._on_list_mousewheel(event, self.domains_canvas))
        self.domains_canvas.pack(side=LEFT, fill=X, expand=True)
        domains_scrollbar.pack(side=RIGHT, fill=Y)
        self.domains_pie_canvas = Canvas(right, height=210, background="#fbfcfe", highlightthickness=1, highlightbackground="#d9dde5")
        self.domains_pie_canvas.pack(fill=BOTH, expand=True)

    def _build_timeline(self) -> None:
        controls = Frame(self.timeline_tab)
        controls.pack(fill=X, pady=(0, 8))
        Label(controls, text="Domain filter").pack(side=LEFT, padx=(0, 8))
        domain_filter = ttk.Combobox(
            controls,
            textvariable=self.timeline_domain_filter_var,
            values=("All rows", "Only rows with domains", "Only rows without domains"),
            state="readonly",
            width=26,
        )
        domain_filter.pack(side=LEFT)
        domain_filter.bind("<<ComboboxSelected>>", lambda _event: self._refresh_timeline())

        columns = ("start", "end", "duration", "type", "app", "domain", "title")
        self.timeline = ttk.Treeview(self.timeline_tab, columns=columns, show="headings")
        for column in columns:
            self.timeline.heading(column, text=column.title())
        self.timeline.column("start", width=145)
        self.timeline.column("end", width=145)
        self.timeline.column("duration", width=90)
        self.timeline.column("type", width=90)
        self.timeline.column("app", width=130)
        self.timeline.column("domain", width=150)
        self.timeline.column("title", width=260)
        self.timeline.tag_configure("plain", background="")
        self.timeline.tag_configure("yellow", background="#fff2b8")
        self.timeline.pack(fill=BOTH, expand=True)

    def _build_download(self) -> None:
        Label(self.download_tab, text="Download activity by date", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        Label(
            self.download_tab,
            text="Choose one or more days that have recorded activity, then export those sessions.",
        ).pack(anchor="w", pady=(4, 12))

        controls = Frame(self.download_tab)
        controls.pack(fill=X, pady=(0, 8))
        Label(controls, text="Show").pack(side=LEFT, padx=(0, 8))
        range_picker = ttk.Combobox(
            controls,
            textvariable=self.download_range_var,
            values=("All available", "This week", "This month", "Calendar range"),
            state="readonly",
            width=18,
        )
        range_picker.pack(side=LEFT, padx=(0, 12))
        range_picker.bind("<<ComboboxSelected>>", lambda _event: self._refresh_download_dates())
        Label(controls, text="From").pack(side=LEFT)
        Entry(controls, textvariable=self.download_start_var, width=12).pack(side=LEFT, padx=(4, 10))
        Label(controls, text="To").pack(side=LEFT)
        Entry(controls, textvariable=self.download_end_var, width=12).pack(side=LEFT, padx=(4, 10))
        Button(controls, text="Apply", command=self._refresh_download_dates).pack(side=LEFT)

        columns = ("date", "sessions", "active")
        self.download_days = ttk.Treeview(
            self.download_tab,
            columns=columns,
            show="headings",
            height=12,
            selectmode="extended",
        )
        self.download_days.heading("date", text="Date")
        self.download_days.heading("sessions", text="Sessions")
        self.download_days.heading("active", text="Active time")
        self.download_days.column("date", width=160)
        self.download_days.column("sessions", width=100, anchor="center")
        self.download_days.column("active", width=140)
        self.download_days.tag_configure("plain", background="")
        self.download_days.tag_configure("yellow", background="#fff2b8")
        self.download_days.pack(fill=BOTH, expand=True, pady=(0, 10))

        actions = Frame(self.download_tab)
        actions.pack(fill=X)
        Button(actions, text="Select all shown", command=self.select_all_download_days).pack(side=LEFT, padx=(0, 8))
        Button(actions, text="Export selected CSV", command=lambda: self.export_selected_days("csv")).pack(side=LEFT, padx=(0, 8))
        Button(actions, text="Export selected XLSX", command=lambda: self.export_selected_days("xlsx")).pack(side=LEFT, padx=(0, 16))
        Button(actions, text="Export today CSV", command=lambda: self.export_today("csv")).pack(side=LEFT, padx=(0, 8))
        Button(actions, text="Export today XLSX", command=lambda: self.export_today("xlsx")).pack(side=LEFT)

    def _build_guide(self) -> None:
        guide = Text(self.guide_tab, wrap="word", height=24, padx=10, pady=10, background="#fbfcfe", relief="solid", borderwidth=1)
        guide.pack(fill=BOTH, expand=True)
        guide.tag_configure("h1", font=("Segoe UI", 17, "bold"), spacing3=8)
        guide.tag_configure("h2", font=("Segoe UI", 12, "bold"), spacing1=10, spacing3=4)
        guide.tag_configure("body", font=("Segoe UI", 10), spacing3=4)
        guide.tag_configure("bullet", font=("Segoe UI", 10), lmargin1=18, lmargin2=34, spacing3=3)

        sections = [
            ("h1", "TrackTivity Guide\n"),
            ("body", "TrackTivity is an offline Windows app that records the app or website you are actively using, summarizes your day, and lets you export your history for review.\n"),
            ("h2", "\nMain Features\n"),
            ("bullet", "- Dashboard: shows active time, idle time, top apps, top websites, and charts for today, this week, this month, or all time.\n"),
            ("bullet", "- Timeline: lists recent sessions with start time, end time, duration, app, domain, and title.\n"),
            ("bullet", "- Domain filtering: show all timeline rows, only rows with domains, or only rows without domains.\n"),
            ("bullet", "- Download Data: export one day, several selected days, all shown days, or today's data as CSV or XLSX.\n"),
            ("bullet", "- Date filtering: narrow downloadable dates to all available days, this week, this month, or a custom calendar range.\n"),
            ("bullet", "- Browser tracking: the companion extension sends the active browser tab domain and title to the local desktop app.\n"),
            ("bullet", "- Idle detection: idle time is recorded separately so active totals stay cleaner.\n"),
            ("bullet", "- Pause and resume: temporarily stop recording without closing the app.\n"),
            ("h2", "\nActivating the Browser Extension\n"),
            ("bullet", "- Keep the TrackTivity desktop app running first.\n"),
            ("bullet", "- In Chrome, open chrome://extensions. In Edge, open edge://extensions.\n"),
            ("bullet", "- Turn on Developer mode.\n"),
            ("bullet", "- Click Load unpacked and select this project's browser-extension folder.\n"),
            ("bullet", "- Leave the extension enabled. It sends the focused tab's browser name, domain, and title to http://127.0.0.1:8765/active-tab.\n"),
            ("h2", "\nHow Data Is Stored\n"),
            ("body", "The app stores activity locally in SQLite. Times are saved in UTC internally and displayed/exported in Philippines time (UTC+08:00). No cloud login, telemetry, or external API is used.\n"),
            ("h2", "\nExporting Data\n"),
            ("body", "Use Download Data for all exports. Only days with available activity appear in the list, and the Select all shown button selects every day currently visible after filtering.\n"),
            ("h2", "\nTips\n"),
            ("bullet", "- Keep the desktop app running while you work.\n"),
            ("bullet", "- Install the browser extension for better website domain and page-title tracking.\n"),
            ("bullet", "- Rows without domains usually mean normal desktop apps, idle time, or browser rows that could not be matched to tab data.\n"),
        ]
        for tag, text in sections:
            guide.insert(END, text, tag)
        guide.configure(state="disabled")

    def _build_settings(self) -> None:
        Label(self.settings_tab, text="Local data", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        Label(self.settings_tab, text=str(self.db.db_path)).pack(anchor="w", pady=(4, 16))
        Label(self.settings_tab, text="Browser extension endpoint", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        Label(self.settings_tab, text=f"http://{HTTP_HOST}:{HTTP_PORT}/active-tab").pack(anchor="w", pady=(4, 16))
        Label(self.settings_tab, text="Display timezone: Philippines time (UTC+08:00)").pack(anchor="w")
        Label(self.settings_tab, text="Focus check: every 1 second").pack(anchor="w")
        Label(self.settings_tab, text="SQLite save interval: every 5 seconds while a session is unchanged").pack(anchor="w")
        Label(self.settings_tab, text="Idle timeout: 3 minutes by default; idle is recorded, not paused").pack(anchor="w")
        Label(self.settings_tab, text="App control", font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(18, 4))
        Label(self.settings_tab, text="Closing the window keeps TrackTivity running in the system tray.").pack(anchor="w")
        Button(self.settings_tab, text="Quit TrackTivity", command=self.quit_application).pack(anchor="w", pady=(8, 0))

    def hide_to_tray(self) -> None:
        self.root.withdraw()

    def open_dashboard(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit_application(self) -> None:
        if self.is_quitting:
            return
        self.is_quitting = True
        if self.tray:
            self.tray.stop()
        self.recorder.stop()
        self.server.stop()
        self.root.destroy()

    def toggle_pause(self) -> None:
        self.recorder.set_paused(not self.recorder.paused)
        self.pause_var.set("Resume" if self.recorder.paused else "Pause")
        self.refresh()

    def refresh(self) -> None:
        if self.is_quitting:
            return
        selected_range = DASHBOARD_RANGES.get(self.dashboard_range_var.get(), "today")
        apps, domains, active_total, idle_total = self.db.aggregate_dashboard(selected_range)
        range_text = dashboard_range_label(selected_range)
        self.status_var.set(self.recorder.status)
        self.dashboard_title_var.set(self.dashboard_range_var.get())
        self.total_var.set(f"Active: {display_duration(active_total)}")
        self.idle_var.set(f"Idle: {display_duration(idle_total)}")
        self.apps_title_var.set(f"Top apps {range_text}")
        self.domains_title_var.set(f"Top websites {range_text}")
        app_items = [(row["app_name"], int(row["total"])) for row in apps]
        domain_items = [(row["domain"], int(row["total"])) for row in domains]
        self._draw_bars(self.apps_canvas, app_items)
        self._draw_bars(self.domains_canvas, domain_items)
        self._draw_pie(self.apps_pie_canvas, app_items, "Apps")
        self._draw_pie(self.domains_pie_canvas, domain_items, "Websites")
        self._refresh_timeline()
        self._refresh_download_dates(keep_selection=True)
        if not self.is_quitting:
            if self.refresh_after_id:
                self.root.after_cancel(self.refresh_after_id)
            self.refresh_after_id = self.root.after(3000, self._scheduled_refresh)

    def _scheduled_refresh(self) -> None:
        self.refresh_after_id = None
        self.refresh()

    def _bind_dashboard_mousewheel(self, _event: object) -> None:
        self.root.bind_all("<MouseWheel>", self._on_dashboard_mousewheel)

    def _unbind_dashboard_mousewheel(self, _event: object) -> None:
        self.root.unbind_all("<MouseWheel>")

    def _on_dashboard_mousewheel(self, event: object) -> None:
        delta = getattr(event, "delta", 0)
        if delta:
            self.dashboard_canvas.yview_scroll(int(-1 * (delta / 120)), "units")

    def _on_list_mousewheel(self, event: object, canvas: Canvas) -> str:
        delta = getattr(event, "delta", 0)
        if delta:
            canvas.yview_scroll(int(-1 * (delta / 120)), "units")
        return "break"

    def _draw_bars(self, canvas: Canvas, items: list[tuple[str, int]]) -> None:
        canvas.delete("all")
        width = max(canvas.winfo_width(), 360)
        label_font = tkfont.Font(family="Segoe UI", size=9)
        value_font = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        left_pad = 14
        right_pad = 14
        value_space = 72
        label_width = min(170, max(105, int(width * 0.34)))
        bar_left = left_pad + label_width + 12
        bar_right = max(bar_left + 40, width - right_pad - value_space)
        y = 16
        items = [(label, value) for label, value in items if value > 0]
        max_value = max([value for _, value in items], default=1)
        if not items:
            canvas.create_text(left_pad, 28, text="No activity yet", anchor="w", fill="#566070", font=label_font)
            canvas.configure(scrollregion=(0, 0, width, 60))
            return
        for label, value in items:
            duration = display_duration(value)
            bar_width = max(4, int((bar_right - bar_left) * (value / max_value)))
            color = DASHBOARD_COLORS[(y // 30) % len(DASHBOARD_COLORS)]
            canvas.create_text(
                left_pad,
                y + 9,
                text=self._fit_text(label, label_width, label_font),
                anchor="w",
                fill="#1f2937",
                font=label_font,
            )
            canvas.create_rectangle(bar_left, y, bar_right, y + 18, fill="#eef2f7", outline="")
            canvas.create_rectangle(bar_left, y, bar_left + bar_width, y + 18, fill=color, outline="")
            canvas.create_text(
                width - right_pad,
                y + 9,
                text=duration,
                anchor="e",
                fill="#1f2937",
                font=value_font,
            )
            y += 30
        canvas.configure(scrollregion=(0, 0, width, y + 10))

    def _draw_pie(self, canvas: Canvas, items: list[tuple[str, int]], title: str) -> None:
        canvas.delete("all")
        width = max(canvas.winfo_width(), 360)
        height = max(canvas.winfo_height(), 190)
        label_font = tkfont.Font(family="Segoe UI", size=9)
        title_font = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        pie_items = self._pie_items(items)
        if not pie_items:
            canvas.create_text(14, 24, text=f"{title} share", anchor="w", fill="#111827", font=title_font)
            canvas.create_text(14, 52, text="No activity yet", anchor="w", fill="#566070", font=label_font)
            return

        total = sum(value for _, value in pie_items)
        size = min(128, height - 46, max(88, int(width * 0.32)))
        x0 = 18
        y0 = 46
        x1 = x0 + size
        y1 = y0 + size
        canvas.create_text(14, 22, text=f"{title} share", anchor="w", fill="#111827", font=title_font)
        start_angle = 90
        for index, (_label, value) in enumerate(pie_items):
            extent = 360 * value / total if total else 0
            canvas.create_arc(
                x0,
                y0,
                x1,
                y1,
                start=start_angle,
                extent=-extent,
                fill=DASHBOARD_COLORS[index % len(DASHBOARD_COLORS)],
                outline="#fbfcfe",
            )
            start_angle -= extent

        legend_x = x1 + 18
        legend_width = max(80, width - legend_x - 14)
        legend_y = y0 + 2
        for index, (label, value) in enumerate(pie_items):
            color = DASHBOARD_COLORS[index % len(DASHBOARD_COLORS)]
            percent = int(round((value / total) * 100)) if total else 0
            text = f"{label} ({percent}%)"
            canvas.create_rectangle(legend_x, legend_y + 3, legend_x + 10, legend_y + 13, fill=color, outline="")
            canvas.create_text(
                legend_x + 16,
                legend_y + 8,
                text=self._fit_text(text, legend_width - 16, label_font),
                anchor="w",
                fill="#1f2937",
                font=label_font,
            )
            legend_y += 24

    def _pie_items(self, items: list[tuple[str, int]]) -> list[tuple[str, int]]:
        positive = [(label, value) for label, value in items if value > 0]
        if len(positive) <= 5:
            return positive[:5]
        others = sum(value for _, value in positive[4:])
        return positive[:4] + [("Others", others)]

    def _fit_text(self, text: str, max_width: int, text_font: tkfont.Font) -> str:
        if text_font.measure(text) <= max_width:
            return text
        ellipsis = "..."
        while text and text_font.measure(text + ellipsis) > max_width:
            text = text[:-1]
        return f"{text}{ellipsis}" if text else ellipsis

    def _refresh_timeline(self) -> None:
        for item in self.timeline.get_children():
            self.timeline.delete(item)
        start, end = today_bounds()
        rows = self.db.sessions_between(start, end)[-100:]
        domain_filter = self.timeline_domain_filter_var.get()
        if domain_filter == "Only rows with domains":
            rows = [row for row in rows if row["domain"]]
        elif domain_filter == "Only rows without domains":
            rows = [row for row in rows if not row["domain"]]
        for index, row in enumerate(reversed(rows)):
            self.timeline.insert(
                "",
                END,
                tags=("plain" if index % 2 == 0 else "yellow",),
                values=(
                    format_ph_datetime(row["started_at"]),
                    format_ph_datetime(row["ended_at"]),
                    display_duration(row["duration_seconds"]),
                    row["activity_type"],
                    row["app_name"],
                    row["domain"],
                    row["page_title"] or row["window_title"],
                ),
            )

    def _download_filter_bounds(self) -> tuple[str | None, str | None]:
        selected = self.download_range_var.get()
        if selected == "This week":
            return this_week_bounds()
        if selected == "This month":
            return this_month_bounds()
        if selected == "Calendar range":
            start_day = parse_date_entry(self.download_start_var.get())
            end_day = parse_date_entry(self.download_end_var.get())
            if not start_day or not end_day:
                return None, None
            if end_day < start_day:
                start_day, end_day = end_day, start_day
            return ph_range_bounds(start_day, end_day)
        return None, None

    def _refresh_download_dates(self, keep_selection: bool = False) -> None:
        selected_days = set(self._selected_download_days()) if keep_selection else set()
        for item in self.download_days.get_children():
            self.download_days.delete(item)

        start, end = self._download_filter_bounds()
        rows = self.db.available_days_between(start, end)
        for index, row in enumerate(rows):
            day = row["day"]
            item = self.download_days.insert(
                "",
                END,
                iid=day,
                tags=("plain" if index % 2 == 0 else "yellow",),
                values=(day, row["session_count"], display_duration(row["active_total"])),
            )
            if keep_selection and day in selected_days:
                self.download_days.selection_set(item)

    def _selected_download_days(self) -> list[str]:
        if not hasattr(self, "download_days"):
            return []
        selection = self.download_days.selection()
        if not selection:
            return []
        days = []
        for item in selection:
            values = self.download_days.item(item, "values")
            if values:
                days.append(str(values[0]))
        return days

    def select_all_download_days(self) -> None:
        self.download_days.selection_set(self.download_days.get_children())

    def _export_rows(self, rows: list[sqlite3.Row], file_type: str, initialfile: str) -> bool:
        extension = ".xlsx" if file_type == "xlsx" else ".csv"
        filetypes = [("Excel workbook", "*.xlsx")] if file_type == "xlsx" else [("CSV files", "*.csv")]
        target = filedialog.asksaveasfilename(
            defaultextension=extension,
            filetypes=filetypes,
            initialfile=initialfile,
        )
        if not target:
            return False
        if file_type == "xlsx":
            write_xlsx(Path(target), rows)
        else:
            write_csv(Path(target), rows)
        return True

    def export_selected_days(self, file_type: str) -> None:
        day_texts = self._selected_download_days()
        selected_days = [day for day in (parse_date_entry(day_text) for day_text in day_texts) if day]
        if not selected_days:
            self.status_var.set("Choose one or more dates to export")
            return

        extension = ".xlsx" if file_type == "xlsx" else ".csv"
        rows = []
        for selected_day in sorted(selected_days):
            start, end = ph_date_bounds(selected_day)
            rows.extend(self.db.sessions_between(start, end))
        label = day_texts[0] if len(day_texts) == 1 else "selected_dates"
        if self._export_rows(rows, file_type, f"activity_{label}{extension}"):
            self.status_var.set(f"Exported {len(selected_days)} selected date(s)")

    def export_today(self, file_type: str) -> None:
        extension = ".xlsx" if file_type == "xlsx" else ".csv"
        today = datetime.now(PH_TZ).date()
        day_text = today.strftime("%Y-%m-%d")
        start, end = ph_date_bounds(today)
        rows = self.db.sessions_between(start, end)
        if self._export_rows(rows, file_type, f"activity_today_{day_text}{extension}"):
            self.status_var.set(f"Exported {day_text}")


EXPORT_COLUMNS = [
    "date_ph",
    "started_at_ph",
    "ended_at_ph",
    "duration_seconds",
    "duration",
    "activity_type",
    "app_name",
    "process_name",
    "domain",
    "title",
    "category",
]


def row_to_export(row: sqlite3.Row) -> list[str]:
    return [
        format_ph_date(row["started_at"]),
        format_ph_datetime(row["started_at"]),
        format_ph_datetime(row["ended_at"]),
        str(row["duration_seconds"]),
        display_duration(row["duration_seconds"]),
        row["activity_type"],
        row["app_name"],
        row["process_name"],
        row["domain"],
        row["page_title"] or row["window_title"],
        row["category"],
    ]


def write_csv(path: Path, rows: list[sqlite3.Row]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(EXPORT_COLUMNS)
        for row in rows:
            writer.writerow(row_to_export(row))


def write_xlsx(path: Path, rows: list[sqlite3.Row]) -> None:
    sheet_rows = [EXPORT_COLUMNS] + [row_to_export(row) for row in rows]
    sheet_xml = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>',
    ]
    for index, values in enumerate(sheet_rows, start=1):
        sheet_xml.append(f'<row r="{index}">')
        for col_index, value in enumerate(values, start=1):
            cell_ref = f"{column_name(col_index)}{index}"
            escaped = html.escape(str(value), quote=True)
            sheet_xml.append(f'<c r="{cell_ref}" t="inlineStr"><is><t>{escaped}</t></is></c>')
        sheet_xml.append("</row>")
    sheet_xml.append("</sheetData></worksheet>")

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES_XML)
        archive.writestr("_rels/.rels", RELS_XML)
        archive.writestr("xl/workbook.xml", WORKBOOK_XML)
        archive.writestr("xl/_rels/workbook.xml.rels", WORKBOOK_RELS_XML)
        archive.writestr("xl/worksheets/sheet1.xml", "".join(sheet_xml))


def column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>"""

RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

WORKBOOK_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Activity" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

WORKBOOK_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""


def main() -> None:
    if already_running():
        ctypes.windll.user32.MessageBoxW(
            None,
            "TrackTivity is already running.",
            APP_NAME,
            0x40,
        )
        return

    db = ActivityDatabase(local_data_dir() / "activity_tracker.sqlite3")
    browser_state = BrowserState()
    server = ExtensionServer(browser_state)
    recorder = ActivityRecorder(db, browser_state)
    server.start()
    recorder.start()

    root = Tk()
    app = DashboardApp(root, db, recorder, server)

    def on_close() -> None:
        app.hide_to_tray()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
