"""MT5-Ai Launcher - desktop app with embedded browser window.

When packaged as exe: run.exe (no args) = desktop window,
run.exe --engine = run engine, run.exe --web = run web server.
Engine only starts AFTER user authenticates via card key login.
"""

import subprocess, sys, time, os, signal, threading
from pathlib import Path

# Windows flag to suppress console window for child processes
CREATE_NO_WINDOW = 0x08000000


def _app_dir():
    """Return the directory where .env and data/ live."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


# ---- Command-line dispatch for PyInstaller ----
if len(sys.argv) > 1:
    flag = sys.argv[1]
    if flag == "--engine":
        sys.argv = [sys.argv[0]]
        from main import main
        import asyncio
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            pass
        sys.exit(0)
    elif flag == "--web":
        sys.argv = [sys.argv[0]]
        from src.web.server import run
        run(host="0.0.0.0", port=8000)
        sys.exit(0)

APP_DIR = _app_dir()
RESTART_MARKER = APP_DIR / "data" / "_restart_marker"
AUTH_MARKER = APP_DIR / "data" / "_auth_marker"
STOP_MARKER = APP_DIR / "data" / "_stop_marker"

# ---- Auto-initialize on first run ----
ENV_FILE = APP_DIR / ".env"
DATA_DIR = APP_DIR / "data"

if not DATA_DIR.exists():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for sub in ["cloud_strategies", "logs"]:
        (DATA_DIR / sub).mkdir(exist_ok=True)

if not ENV_FILE.exists():
    ENV_FILE.write_text(
        "# ============================================\n"
        "#   MT5-Ai 智能交易系统 配置文件\n"
        "#   修改后需重启生效\n"
        "# ============================================\n"
        "\n"
        "# ---- MT5 账户配置 ----\n"
        "MT5_LOGIN=0  # MT5 账号\n"
        "MT5_PASSWORD=  # MT5 密码\n"
        "MT5_SERVER=  # MT5 服务器\n"
        "MT5_SYMBOL=XAUUSD  # 交易品种\n"
        "MT5_VOLUME=0.01  # 手数\n"
        "\n"
        "# ---- 策略选择 ----\n"
        "# 可选: V2, V3, V4, B1, B2\n"
        "ACTIVE_STRATEGY=V2\n"
        "\n"
        "# ---- 邮件报警（可选，不填则不发送）----\n"
        "EMAIL_SENDER=\n"
        "EMAIL_PASSWORD=\n"
        "EMAIL_RECEIVER=\n"
        "\n"
        "# ---- 策略云端更新 ----\n"
        "GITEE_OWNER=\n"
        "GITEE_REPO=mt5-strategies\n"
        "GITEE_TOKEN=\n"
        "ADMIN_KEY=你的密钥\n"
        "\n"
        "# ---- 卡密授权 (llua.cn V2) ----\n"
        "LLUA_APPKEY=你的LLUA_APPKEY\n"
        "LLUA_APITOKEN=你的LLUA_APITOKEN\n"
        "LLUA_LOGIN_ID=你的LLUA_LOGIN_ID\n"
        "LLUA_HEARTBEAT_ID=你的LLUA_HEARTBEAT_ID\n"
        "LLUA_SUCCESS_CODE=0\n",
        encoding="utf-8",
    )


def _kill_tree(pid):
    """Kill a process and its entire subtree (Windows)."""
    try:
        subprocess.call(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            creationflags=CREATE_NO_WINDOW,
            timeout=10,
        )
    except Exception:
        pass


def _kill_orphans():
    """Kill orphaned engine/web processes and anything on port 8000."""
    current = os.getpid()
    try:
        out = subprocess.check_output(
            ["wmic", "process", "get", "processid,commandline"],
            encoding="oem", timeout=5,
            creationflags=CREATE_NO_WINDOW,
        )
        for line in out.split("\n"):
            if str(current) in line:
                continue
            if "--engine" in line or "--web" in line or "main.py" in line or "web.py" in line:
                parts = line.strip().rpartition(" ")
                pid = parts[-1]
                if pid.isdigit() and int(pid) != current:
                    _kill_tree(int(pid))
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["netstat", "-ano"], encoding="oem", timeout=5,
            creationflags=CREATE_NO_WINDOW,
        )
        for line in out.split("\n"):
            if ":8000" in line and "LISTENING" in line:
                parts = line.strip().split()
                if parts and parts[-1].isdigit():
                    pid = int(parts[-1])
                    if pid != current and pid != 0:
                        _kill_tree(pid)
    except Exception:
        pass


def _wait_port_free(port=8000, timeout=30):
    """Wait until netstat shows port is no longer LISTENING."""
    for i in range(timeout):
        try:
            out = subprocess.check_output(
                ["netstat", "-ano"], encoding="oem", timeout=5,
                creationflags=CREATE_NO_WINDOW,
            )
            occupied = any(f":{port}" in l and "LISTENING" in l for l in out.split("\n"))
            if not occupied:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _cleanup():
    _kill_orphans()
    _wait_port_free()


_cleanup()

os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PYTHONUNBUFFERED"] = "1"


def start_engine():
    if getattr(sys, 'frozen', False):
        cmd = [sys.executable, "--engine"]
    else:
        cmd = [sys.executable, "-u", "main.py"]
    log_path = APP_DIR / "data" / "logs" / "engine.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(str(log_path), "a", encoding="utf-8")
    return subprocess.Popen(
        cmd,
        stdout=log_file, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
        cwd=str(APP_DIR),
    )


def start_web():
    if getattr(sys, 'frozen', False):
        cmd = [sys.executable, "--web"]
    else:
        cmd = [sys.executable, "-u", "web.py"]
    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
        cwd=str(APP_DIR),
    )


def _wait_web_ready(port=8000, timeout=30):
    """Wait until web server is responding on port."""
    import urllib.request
    for _ in range(timeout):
        try:
            urllib.request.urlopen(f"http://localhost:{port}/", timeout=2)
            return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _monitor(engine_ref, web):
    """Background thread: start engine on auth, stop on logout, auto-restart, handle markers."""
    engine = engine_ref[0]  # Use list for mutable reference
    _engine_crashes = 0

    while True:
        # Auth marker: start engine after user logs in
        if engine is None and AUTH_MARKER.exists():
            try:
                AUTH_MARKER.unlink()
            except Exception:
                pass
            engine = start_engine()
            engine_ref[0] = engine
            _engine_crashes = 0

        # Stop marker: user logged out, stop engine
        if engine is not None and STOP_MARKER.exists():
            try:
                STOP_MARKER.unlink()
            except Exception:
                pass
            _kill_tree(engine.pid)
            time.sleep(1)
            engine = None
            engine_ref[0] = None
            _engine_crashes = 0

        # No engine running, just wait
        if engine is None:
            time.sleep(2)
            continue

        # Restart marker: only restart ENGINE, not web
        if RESTART_MARKER.exists():
            try:
                RESTART_MARKER.unlink()
            except Exception:
                pass
            _kill_tree(engine.pid)
            time.sleep(2)
            engine = start_engine()
            engine_ref[0] = engine
            _engine_crashes = 0
            continue

        ret = engine.poll()
        if ret is not None:
            _engine_crashes += 1
            if _engine_crashes >= 5:
                engine = None
                engine_ref[0] = None
                continue
            time.sleep(3)
            engine = start_engine()
            engine_ref[0] = engine

        ret = web.poll()
        if ret is not None:
            if ret == 3:
                _wait_port_free(timeout=60)
                web = start_web()
            else:
                time.sleep(3)
                web = start_web()

        time.sleep(2)


# ---- Start ONLY web server (engine starts after auth) ----
web = start_web()

# ---- Wait for web server ready, then open desktop window ----
_wait_web_ready()

import webview

# Engine reference as list for mutable access in monitor thread
_engine_ref = [None]  # Engine not started until auth

def on_closing():
    """Called when user closes the desktop window — kill everything."""
    engine = _engine_ref[0]
    for p in [engine, web]:
        if p is not None:
            _kill_tree(p.pid)
    time.sleep(1)
    _kill_orphans()


# Start monitor thread
monitor_thread = threading.Thread(target=_monitor, args=(_engine_ref, web), daemon=True)
monitor_thread.start()

# Create desktop window with embedded browser
window = webview.create_window(
    title="笨猫MT5智能策略交易",
    url="http://localhost:8000/dashboard",
    width=1280,
    height=800,
    min_size=(900, 600),
)
window.events.closing += on_closing

webview.start(icon=str(APP_DIR / "benmao.ico") if (APP_DIR / "benmao.ico").exists() else "")

# After webview.start() returns (window closed), ensure cleanup
_kill_orphans()
