# AGENTS.md

## Cursor Cloud specific instructions

### What this project is
Twitch Drops Miner (TDM) is a single, standalone **Python 3.10+ desktop GUI application** (tkinter + asyncio + aiohttp). There is **no backend, database, or self-hosted service** — all runtime "services" are live Twitch cloud endpoints (`gql.twitch.tv`, `passport.twitch.tv`, `pubsub-edge.twitch.tv`). The entry point is `main.py`.

### Running / building / checking
Standard commands are already documented in the repo; prefer those:
- **Setup / deps:** `setup_env.sh` (creates `env/` venv + `pip install -r requirements.txt`).
- **Run (dev):** `env/bin/python main.py` (add `-v`/`-vv`/`-vvv` for verbosity; `--tray`, `--log`, `--dump` are optional flags parsed in `main.py`).
- **Build (packaging only, not needed for dev):** `build.sh` / `build.spec` (PyInstaller), `appimage/AppImageBuilder.yml`.
- **The only automated check in CI** (`.github/workflows/ci.yml`) is language-file JSON validation — there is no unit test suite or linter configured. Run it with:
  `for f in lang/*.json; do env/bin/python -m json.tool "$f" >/dev/null || echo "BAD $f"; done`

### Non-obvious caveats (important)
- **A GUI display is required — the app cannot run headless.** In this cloud VM a real X display is available at `DISPLAY=:1` (1920x1200); launch with `DISPLAY=:1 env/bin/python main.py`. CI instead uses `xvfb-run` purely for building. The app opens a tkinter window immediately, and even argument-parser errors are shown via `messagebox`, so a display must exist before `main.py` starts.
- **System libraries are prerequisites** and are already installed in the VM snapshot (not in the update script): `python3-tk`, `python3.12-venv`, and for the Linux system tray `libgirepository1.0-dev`, `gir1.2-ayatanaappindicator3-0.1`, `libayatana-appindicator3-1` (PyGObject build also needs `libcairo2-dev pkg-config gcc python3-dev`).
- **Single-instance lock:** the app creates `lock.file` on start and exits with status 3 if another instance holds it. Only one `main.py` can run at a time; remove a stale `lock.file` only if no `main.py` process is running.
- **Settings persistence timing:** settings changed in the GUI (e.g. Priority list, Dark mode) are written to `settings.json` on **graceful shutdown** (`client.save(force=True)`), not immediately on each edit. Close the window or send `SIGTERM`/`SIGINT` (handled on Linux to trigger a clean close + save) to persist.
- **Login is interactive** (Twitch login form + possible CAPTCHA / "new login" email) and needs a real Twitch account, so fully-automated end-to-end mining cannot be exercised without credentials. Everything up to and including the GUI, tab navigation, and Settings changes works without logging in.
- Local runtime state files (`cookies.jar`, `settings.json`, `lock.file`, `cache/`, `log.txt`, `dump.dat`) are all git-ignored.
