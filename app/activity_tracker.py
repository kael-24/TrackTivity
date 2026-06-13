from __future__ import annotations

import csv
import ctypes
import html
import json
import os
import re
import sqlite3
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, X, Y, Button, Canvas, Frame, Label, StringVar, Tk, filedialog, ttk
from urllib.parse import urlparse


APP_NAME = "Activity Tracker"
HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8765
PH_TZ = timezone(timedelta(hours=8), name="PHT")
FOCUS_POLL_SECONDS = 1
SESSION_SAVE_INTERVAL_SECONDS = 5
BROWSER_PROCESSES = {
    "chrome.exe": "Chrome",
    "msedge.exe": "Edge",
    "firefox.exe": "Firefox",
}


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
    ph_now = datetime.now(PH_TZ)
    ph_start = ph_now.replace(hour=0, minute=0, second=0, microsecond=0)
    ph_end = ph_start + timedelta(days=1)
    start = ph_start.astimezone(UTC)
    end = ph_end.astimezone(UTC)
    return start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")


def local_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "ActivityTracker"
    return Path.cwd() / "data"


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
            self.repair_inferred_browser_sessions()

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

    def aggregate_today(self) -> tuple[list[sqlite3.Row], list[sqlite3.Row], int]:
        start, end = today_bounds()
        with self.lock:
            apps = list(
                self.conn.execute(
                    """
                    select app_name, sum(duration_seconds) as total
                    from activity_sessions
                    where started_at >= ? and started_at < ? and activity_type != 'idle'
                    group by app_name
                    order by total desc
                    limit 10
                    """,
                    (start, end),
                )
            )
            domains = list(
                self.conn.execute(
                    """
                    select domain, sum(duration_seconds) as total
                    from activity_sessions
                    where started_at >= ? and started_at < ? and domain != ''
                    group by domain
                    order by total desc
                    limit 10
                    """,
                    (start, end),
                )
            )
            total_row = self.conn.execute(
                """
                select coalesce(sum(duration_seconds), 0) as total
                from activity_sessions
                where started_at >= ? and started_at < ? and activity_type != 'idle'
                """,
                (start, end),
            ).fetchone()
            return apps, domains, int(total_row["total"] if total_row else 0)


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

        idle_limit = int(self.db.get_setting("idle_timeout_seconds", "300"))
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


class DashboardApp:
    def __init__(self, root: Tk, db: ActivityDatabase, recorder: ActivityRecorder, server: ExtensionServer) -> None:
        self.root = root
        self.db = db
        self.recorder = recorder
        self.server = server
        self.status_var = StringVar(value="Starting")
        self.total_var = StringVar(value="Today: 0s")
        self.pause_var = StringVar(value="Pause")
        self.root.title(APP_NAME)
        self.root.geometry("980x660")
        self._build()
        self.refresh()

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
        self.settings_tab = Frame(notebook, padx=12, pady=12)
        notebook.add(self.dashboard_tab, text="Dashboard")
        notebook.add(self.timeline_tab, text="Timeline")
        notebook.add(self.settings_tab, text="Settings")

        self._build_dashboard()
        self._build_timeline()
        self._build_settings()

    def _build_dashboard(self) -> None:
        Label(self.dashboard_tab, textvariable=self.total_var, font=("Segoe UI", 20, "bold")).pack(anchor="w")
        charts = Frame(self.dashboard_tab)
        charts.pack(fill=BOTH, expand=True, pady=12)

        left = Frame(charts)
        left.pack(side=LEFT, fill=BOTH, expand=True, padx=(0, 8))
        Label(left, text="Top apps today", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.apps_canvas = Canvas(left, height=230, background="#f8f9fb", highlightthickness=1, highlightbackground="#d9dde5")
        self.apps_canvas.pack(fill=BOTH, expand=True, pady=(6, 0))

        right = Frame(charts)
        right.pack(side=RIGHT, fill=BOTH, expand=True, padx=(8, 0))
        Label(right, text="Top websites today", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.domains_canvas = Canvas(right, height=230, background="#f8f9fb", highlightthickness=1, highlightbackground="#d9dde5")
        self.domains_canvas.pack(fill=BOTH, expand=True, pady=(6, 0))

        actions = Frame(self.dashboard_tab)
        actions.pack(fill=X)
        Button(actions, text="Export CSV", command=self.export_csv).pack(side=LEFT, padx=(0, 8))
        Button(actions, text="Export XLSX", command=self.export_xlsx).pack(side=LEFT)

    def _build_timeline(self) -> None:
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
        self.timeline.pack(fill=BOTH, expand=True)

    def _build_settings(self) -> None:
        Label(self.settings_tab, text="Local data", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        Label(self.settings_tab, text=str(self.db.db_path)).pack(anchor="w", pady=(4, 16))
        Label(self.settings_tab, text="Browser extension endpoint", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        Label(self.settings_tab, text=f"http://{HTTP_HOST}:{HTTP_PORT}/active-tab").pack(anchor="w", pady=(4, 16))
        Label(self.settings_tab, text="Display timezone: Philippines time (UTC+08:00)").pack(anchor="w")
        Label(self.settings_tab, text="Focus check: every 1 second").pack(anchor="w")
        Label(self.settings_tab, text="SQLite save interval: every 5 seconds while a session is unchanged").pack(anchor="w")
        Label(self.settings_tab, text="Idle timeout: 5 minutes by default; idle is recorded, not paused").pack(anchor="w")

    def toggle_pause(self) -> None:
        self.recorder.set_paused(not self.recorder.paused)
        self.pause_var.set("Resume" if self.recorder.paused else "Pause")
        self.refresh()

    def refresh(self) -> None:
        apps, domains, total = self.db.aggregate_today()
        self.status_var.set(self.recorder.status)
        self.total_var.set(f"Today: {display_duration(total)}")
        self._draw_bars(self.apps_canvas, [(row["app_name"], int(row["total"])) for row in apps])
        self._draw_bars(self.domains_canvas, [(row["domain"], int(row["total"])) for row in domains])
        self._refresh_timeline()
        self.root.after(3000, self.refresh)

    def _draw_bars(self, canvas: Canvas, items: list[tuple[str, int]]) -> None:
        canvas.delete("all")
        width = max(canvas.winfo_width(), 420)
        y = 16
        items = [(label, value) for label, value in items if value > 0]
        max_value = max([value for _, value in items], default=1)
        if not items:
            canvas.create_text(18, 28, text="No activity yet", anchor="w", fill="#566070")
            return
        for label, value in items:
            bar_width = int((width - 180) * (value / max_value))
            canvas.create_text(12, y + 9, text=label[:28], anchor="w", fill="#1f2937")
            canvas.create_rectangle(150, y, 150 + bar_width, y + 18, fill="#2563eb", outline="")
            canvas.create_text(160 + bar_width, y + 9, text=display_duration(value), anchor="w", fill="#1f2937")
            y += 32

    def _refresh_timeline(self) -> None:
        for item in self.timeline.get_children():
            self.timeline.delete(item)
        start, end = today_bounds()
        rows = self.db.sessions_between(start, end)[-100:]
        for row in reversed(rows):
            self.timeline.insert(
                "",
                END,
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

    def export_csv(self) -> None:
        target = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            initialfile="activity_export.csv",
        )
        if not target:
            return
        start, end = today_bounds()
        rows = self.db.sessions_between(start, end)
        write_csv(Path(target), rows)

    def export_xlsx(self) -> None:
        target = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            filetypes=[("Excel workbook", "*.xlsx")],
            initialfile="activity_export.xlsx",
        )
        if not target:
            return
        start, end = today_bounds()
        rows = self.db.sessions_between(start, end)
        write_xlsx(Path(target), rows)


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
    db = ActivityDatabase(local_data_dir() / "activity_tracker.sqlite3")
    browser_state = BrowserState()
    server = ExtensionServer(browser_state)
    recorder = ActivityRecorder(db, browser_state)
    server.start()
    recorder.start()

    root = Tk()
    app = DashboardApp(root, db, recorder, server)

    def on_close() -> None:
        recorder.stop()
        server.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
