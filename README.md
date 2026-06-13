# Activity Tracker

Offline-first Windows activity tracker inspired by Digital Wellbeing. It records the focused desktop application and, when the companion browser extension is installed, the focused Chrome/Edge tab domain and title.

## Current MVP

- Windows foreground app tracking.
- Chrome/Edge focused-tab tracking through a local-only extension.
- SQLite storage on your laptop.
- Tkinter dashboard with daily totals, top apps/sites, and session timeline.
- CSV and XLSX export.
- Pause/resume tracking.
- Idle detection.
- Philippines-time dashboard and exports while SQLite stores UTC internally.
- No cloud database, login, telemetry, or external API calls.

## Run the Desktop App

Requirements:

- Windows
- Python 3.10 or newer

Run:

```powershell
python app\activity_tracker.py
```

The local database is created at:

```text
%LOCALAPPDATA%\ActivityTracker\activity_tracker.sqlite3
```

## Install the Browser Extension Manually

Chrome:

1. Open `chrome://extensions`.
2. Enable `Developer mode`.
3. Click `Load unpacked`.
4. Select the `browser-extension` folder in this project.
5. Keep the desktop app running while browsing.

Edge:

1. Open `edge://extensions`.
2. Enable `Developer mode`.
3. Click `Load unpacked`.
4. Select the `browser-extension` folder in this project.
5. Keep the desktop app running while browsing.

The extension sends only the currently focused tab's browser name, domain-derived URL metadata, and title to `http://127.0.0.1:8765`. The desktop app stores only domain and title, not full URLs.

The extension also sends a lightweight heartbeat every 2 seconds while a browser window is focused. This helps the desktop app recover the active tab when Windows returns focus to the browser without a normal tab-change event.

## Export

Use the dashboard's export buttons to create:

- `activity_export.csv`
- `activity_export.xlsx`

Exports are written to your chosen folder.

Exported dates and times are displayed in Philippines time (`UTC+08:00`). The SQLite database keeps timestamps in UTC for consistency.

## Notes

- Browser tab tracking works best while the desktop app is open.
- If the extension is not installed, browser usage falls back to normal app/window tracking.
- The desktop app checks the focused app every 1 second and saves ongoing session updates every 5 seconds. It still saves immediately when the active app/site changes, tracking is paused, idle state changes, or the app closes.
- Idle time is recorded as `Idle`; tracking resumes automatically when keyboard or mouse activity returns.
- Android support is not implemented yet; it will need a separate native Android app because Android exposes app usage through different OS APIs.
