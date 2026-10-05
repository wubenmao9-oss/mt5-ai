"""FastAPI Web Dashboard - Full API"""
import os, sys, sqlite3, logging, asyncio, json, tempfile, time, hashlib, base64, random, string, uuid, socket
import urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
import MetaTrader5 as mt5
from dotenv import load_dotenv


def _app_dir():
    """Application directory (where .env and data/ live)."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent.parent


def _resource_path(relative):
    """Path to bundled resource (static files inside exe)."""
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS) / relative
    return Path(__file__).resolve().parent / relative


logger = logging.getLogger(__name__)
BASE = _app_dir()
DB_PATH = BASE / "data" / "trades.db"
STATIC_DIR = _resource_path("src/web/static")
ENV_PATH = BASE / ".env"
STATE_FILE = BASE / "data" / "engine_state.json"
CONFIG_HOT_FILE = BASE / "data" / "config_hot.json"
STRATEGY_SWITCH_FILE = BASE / "data" / "strategy_switch.json"
AUTH_MARKER = BASE / "data" / "_auth_marker"
STOP_MARKER = BASE / "data" / "_stop_marker"

load_dotenv(str(ENV_PATH))

_manager = None
_mt5_inited = False
_emailer_cache = None
_server_offset = None
_server_offset_ts = 0


def _get_server_offset():
    """MT5 服务器时间与本地时间的偏移（秒）。p.time 是服务器时间，需要偏移才能和 time.time() 对齐。"""
    global _server_offset, _server_offset_ts
    if _server_offset is not None and time.time() - _server_offset_ts < 120:
        return _server_offset
    try:
        for sym in ["XAUUSD", "EURUSD", "GBPUSD"]:
            tick = mt5.symbol_info_tick(sym)
            if tick and tick.time > 0:
                _server_offset = tick.time - time.time()
                _server_offset_ts = time.time()
                return _server_offset
    except Exception:
        pass
    return _server_offset if _server_offset is not None else 0


def _get_emailer():
    """Get or create a cached EmailAlerter from .env config."""
    global _emailer_cache
    if _emailer_cache is not None:
        return _emailer_cache
    sender = os.getenv("EMAIL_SENDER", "")
    pwd = os.getenv("EMAIL_PASSWORD", "")
    recv = os.getenv("EMAIL_RECEIVER", "")
    if not (sender and pwd and recv):
        return None
    try:
        from src.notifier.email_alerter import EmailAlerter
        _emailer_cache = EmailAlerter(sender, pwd, recv)
        return _emailer_cache
    except Exception:
        return None


def _email_close(direction: str, pnl: float, hold_seconds: float, reason: str = "Manual Close"):
    """Send a close-position email notification."""
    alerter = _get_emailer()
    if not alerter:
        return
    try:
        now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
        m = int(hold_seconds // 60) if hold_seconds >= 0 else 0
        hold_str = f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m"
        alerter.send_close(
            time_str=now_str, direction=direction,
            reason=reason, pnl=round(pnl, 2), hold=hold_str,
        )
    except Exception as e:
        logger.warning("Email close notify failed: %s", e)


_web_filling_cache = {}

def _get_web_filling(symbol: str) -> int:
    """Auto-detect filling mode for web server MT5 operations."""
    if symbol in _web_filling_cache:
        return _web_filling_cache[symbol]
    info = mt5.symbol_info(symbol)
    if info is None:
        return mt5.ORDER_FILLING_IOC
    fm = info.filling_mode
    if fm & 2:
        filling = mt5.ORDER_FILLING_IOC
    elif fm & 1:
        filling = mt5.ORDER_FILLING_FOK
    else:
        filling = mt5.ORDER_FILLING_RETURN
    _web_filling_cache[symbol] = filling
    return filling

# ---- Card Auth (llua.cn V2) ----
# 敏感配置请写入 .env（参考 .env.example），不要硬编码在源码中
LLUA_HOST = os.getenv("LLUA_HOST", "https://wy.llua.cn/v2/")
LLUA_APPKEY = os.getenv("LLUA_APPKEY", "你的LLUA_APPKEY")
LLUA_APITOKEN = os.getenv("LLUA_APITOKEN", "你的LLUA_APITOKEN")
LLUA_LOGIN_ID = os.getenv("LLUA_LOGIN_ID", "你的LLUA_LOGIN_ID")
LLUA_HEARTBEAT_ID = os.getenv("LLUA_HEARTBEAT_ID", "你的LLUA_HEARTBEAT_ID")
LLUA_SUCCESS_CODE = int(os.getenv("LLUA_SUCCESS_CODE", "0"))

# ---- Cloud Strategy (Gitee) ----
GITEE_OWNER = os.getenv("GITEE_OWNER", "")
GITEE_REPO = os.getenv("GITEE_REPO", "mt5-strategies")
GITEE_TOKEN = os.getenv("GITEE_TOKEN", "")
ADMIN_KEY = os.getenv("ADMIN_KEY", "你的密钥")

# ---- Admin uploaded strategies (pending backtest/publish) ----
_ADMIN_STRATEGIES = {}  # {name: {"source": str, "version": str, "backtest_result": dict|None}}

_auth_session = {
    "authenticated": False,
    "kami": None, "token": None, "vip": 0,
    "kmtype": None, "ktype": None, "note": None,
    "markcode": None, "value": None, "heartbeat_task": None,
}

def _get_device_code():
    mac = uuid.getnode()
    hostname = socket.gethostname()
    return hashlib.md5(f"MT5Ai-{hostname}-{mac}".encode()).hexdigest()

def _llua_sign(params_str):
    return hashlib.md5((params_str + "&" + LLUA_APPKEY).encode()).hexdigest()

def _llua_encrypt(data):
    return base64.b64encode(data.encode("utf-8")).decode("utf-8")

def _llua_decrypt(data):
    return base64.b64decode(data.encode("utf-8")).decode("utf-8")

def _llua_check_calc(srv_time, value):
    return hashlib.md5((str(int(srv_time)) + LLUA_APPKEY + value).encode()).hexdigest()

def _llua_post_sync(params_dict):
    parts = [f"{k}={v}" for k, v in params_dict.items()]
    encrypted = _llua_encrypt("&".join(parts))
    url = LLUA_HOST + LLUA_APITOKEN
    req = urllib.request.Request(url, data=encrypted.encode("utf-8"))
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            # Try plain JSON first (error responses), then Base64 encrypted
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                pass
            decrypted = _llua_decrypt(raw)
            return json.loads(decrypted)
    except Exception as e:
        logger.error("llua API call failed: %s", e)
        return {"code": -1, "msg": str(e)}

async def _llua_login(kami):
    markcode = _get_device_code()
    t = str(int(time.time()))
    value = "".join(random.choices(string.ascii_letters + string.digits, k=16))
    sign = _llua_sign(f"kami={kami}&markcode={markcode}&t={t}")
    result = await asyncio.to_thread(_llua_post_sync, {
        "id": LLUA_LOGIN_ID, "kami": kami, "markcode": markcode,
        "t": t, "sign": sign, "value": value,
    })
    return result, value, markcode

async def _llua_heartbeat():
    if not _auth_session["authenticated"]:
        return {"code": -1, "msg": "Not authenticated"}, ""
    kami = _auth_session["kami"]
    markcode = _auth_session["markcode"]
    kamitoken = _auth_session["token"]
    t = str(int(time.time()))
    value = "".join(random.choices(string.ascii_letters + string.digits, k=16))
    sign = _llua_sign(f"kami={kami}&markcode={markcode}&t={t}&kamitoken={kamitoken}")
    result = await asyncio.to_thread(_llua_post_sync, {
        "id": LLUA_HEARTBEAT_ID, "kami": kami, "markcode": markcode,
        "t": t, "sign": sign, "kamitoken": kamitoken, "value": value,
    })
    return result, value

def _invalidate_session():
    _auth_session.update({
        "authenticated": False, "kami": None, "token": None,
        "vip": 0, "kmtype": None, "ktype": None, "note": None,
    })
    if _auth_session.get("heartbeat_task"):
        _auth_session["heartbeat_task"].cancel()
        _auth_session["heartbeat_task"] = None

async def _heartbeat_loop():
    while _auth_session["authenticated"]:
        await asyncio.sleep(30)
        try:
            result, value = await _llua_heartbeat()
            if result.get("code") not in (LLUA_SUCCESS_CODE, 200):
                logger.warning("Heartbeat failed: %s", result.get("msg"))
                _invalidate_session()
                break
            msg = result.get("msg", {})
            if isinstance(msg, dict) and msg.get("endtime"):
                _auth_session["vip"] = msg["endtime"]
            srv_time = result.get("time", 0)
            if srv_time and value:
                expected_check = _llua_check_calc(srv_time, value)
                actual_check = msg.get("check", "") if isinstance(msg, dict) else ""
                if actual_check and expected_check != actual_check:
                    logger.warning("Heartbeat check validation failed")
                    _invalidate_session()
                    break
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Heartbeat error: %s", e)
            _invalidate_session()
            break

TF_MAP = {"M1":mt5.TIMEFRAME_M1,"M5":mt5.TIMEFRAME_M5,"M15":mt5.TIMEFRAME_M15,
          "M30":mt5.TIMEFRAME_M30,"H1":mt5.TIMEFRAME_H1,"H4":mt5.TIMEFRAME_H4,"D1":mt5.TIMEFRAME_D1}

# ---- Shared Engine State (IPC via JSON file between processes) ----
def save_engine_state(d: dict) -> None:
    """Write engine state to shared JSON file atomically (write-to-temp then rename)."""
    d["_timestamp"] = datetime.now(timezone.utc).isoformat()
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception as e:
        logger.warning("Save engine state failed: %s", e)

def load_engine_state() -> dict:
    """Read engine state from shared JSON file (called from web process for /api/status)."""
    if not STATE_FILE.exists():
        return {}
    try:
        raw = STATE_FILE.read_text(encoding="utf-8")
        return json.loads(raw) if raw.strip() else {}
    except Exception as e:
        logger.warning("Load engine state failed: %s", e)
        return {}

# ---- FastAPI App ----
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Web server started: http://localhost:8000")
    yield
    if _mt5_inited:
        mt5.shutdown()

app = FastAPI(title="MT5-Ai API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/") or path.startswith("/api/auth/"):
        return await call_next(request)
    if not _auth_session.get("authenticated"):
        return JSONResponse(status_code=401, content={"detail": "未验证，请先登录"})
    if _auth_session.get("vip", 0) > 0 and _auth_session["vip"] <= time.time():
        _invalidate_session()
        return JSONResponse(status_code=401, content={"detail": "卡密已到期"})
    return await call_next(request)

STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ---- Lazy MT5 init for API-only access (separate process) ----
def _ensure_mt5():
    global _mt5_inited
    if _mt5_inited:
        # Verify connection is still alive
        try:
            acc = mt5.account_info()
            if acc is not None:
                return
        except Exception:
            pass
        # Connection lost, reinitialize
        _mt5_inited = False
        logger.info("MT5 connection lost, reconnecting...")
    load_dotenv(str(ENV_PATH), override=True)
    login = int(os.getenv("MT5_LOGIN", "0"))
    pwd = os.getenv("MT5_PASSWORD", "")
    srv = os.getenv("MT5_SERVER", "")
    if not (login and pwd and srv):
        raise RuntimeError("MT5 credentials missing in .env")
    ok = mt5.initialize(login=login, password=pwd, server=srv)
    if not ok:
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    _mt5_inited = True

def _db():
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def _load_trade_stats() -> dict:
    db = _db()
    if not db:
        return {"total_trades": 0, "wins": 0, "win_rate": 0, "total_pnl": 0}
    cur = db.execute(
        "SELECT COUNT(*) as t, COALESCE(SUM(CASE WHEN profit>0 THEN 1 ELSE 0 END),0) as w, "
        "COALESCE(SUM(profit),0) as p FROM trades WHERE close_time IS NOT NULL"
    )
    r = cur.fetchone()
    db.close()
    return {
        "total_trades": r["t"],
        "wins": r["w"],
        "win_rate": round(r["w"] / r["t"] * 100, 1) if r["t"] else 0,
        "total_pnl": round(r["p"], 2),
    }

def _mt5_daily_pnl() -> float:
    """Get today's realized PnL directly from MT5 history deals."""
    try:
        _ensure_mt5()
        from datetime import timezone as _tz
        now = datetime.now(_tz.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        deals = mt5.history_deals_get(day_start, now)
        if not deals:
            return 0.0
        # Only count actual BUY/SELL exit deals (not balance deposits, adjustments, etc.)
        total = sum(
            d.profit + d.commission + d.swap
            for d in deals
            if d.entry != mt5.DEAL_ENTRY_IN
            and d.type in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL)
        )
        return round(total, 2)
    except Exception as e:
        logger.debug("MT5 daily PnL fetch failed: %s", e)
        return 0.0


def _db_daily_trade_count() -> int:
    """Count trades closed today from DB."""
    db = _db()
    if not db:
        return 0
    try:
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cur = db.execute(
            "SELECT COUNT(*) as c FROM trades WHERE close_time IS NOT NULL AND close_time LIKE ?",
            (today_str + "%",)
        )
        r = cur.fetchone()
        return r["c"] if r else 0
    except Exception:
        return 0
    finally:
        db.close()

# ---- Hot Config (IPC via config_hot.json between web and engine) ----
CONFIG_LIMITS = {
    "volume": {"min": 0.01, "max": 0.05, "type": "float"},
    "sl_pct": {"min": 0.001, "max": 0.05, "type": "float"},
    "tp_pct": {"min": 0.002, "max": 0.10, "type": "float"},
    "max_trades": {"min": 1, "max": 10, "type": "int"},
    "max_loss": {"min": 5.0, "max": 100.0, "type": "float"},
    "session_start": {"min": 0, "max": 23, "type": "int"},
    "session_end": {"min": 1, "max": 24, "type": "int"},
    "rsi_overbought": {"min": 50.0, "max": 95.0, "type": "float"},
    "rsi_oversold": {"min": 5.0, "max": 50.0, "type": "float"},
    "trend_sma_fast": {"min": 5, "max": 50, "type": "int"},
    "trend_sma_slow": {"min": 10, "max": 200, "type": "int"},
    "trend_rsi_period": {"min": 5, "max": 30, "type": "int"},
    "entry_bb_period": {"min": 10, "max": 50, "type": "int"},
    "entry_bb_std": {"min": 1.0, "max": 4.0, "type": "float"},
    "entry_rsi_period": {"min": 3, "max": 20, "type": "int"},
    "trend_strength_threshold": {"min": 10.0, "max": 90.0, "type": "float"},
    "bb_touch_buffer": {"min": 0.0, "max": 0.01, "type": "float"},
    "cooldown_after_trade_minutes": {"min": 10, "max": 240, "type": "int"},
    "cooldown_no_signal_minutes": {"min": 10, "max": 240, "type": "int"},
    "cooldown_error_minutes": {"min": 5, "max": 60, "type": "int"},
    "sl_atr_multiplier": {"min": 0.5, "max": 5.0, "type": "float"},
    "tp_atr_multiplier": {"min": 0.5, "max": 10.0, "type": "float"},
}

def save_config_hot(items: list) -> None:
    data = {"pending": items, "_applied": False, "_timestamp": datetime.now(timezone.utc).isoformat()}
    try:
        CONFIG_HOT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_HOT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CONFIG_HOT_FILE)
    except Exception as e:
        logger.warning("Save config_hot failed: %s", e)

def load_current_config() -> dict:
    """Load current effective config from engine_state.json or defaults."""
    try:
        es = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        cfg = es.get("config")
        if cfg and isinstance(cfg, dict):
            return cfg
    except Exception:
        pass
    from src.strategy.config import StrategyConfig
    return StrategyConfig().to_dict()

# ======================================================================
# API Routes
# ======================================================================

@app.get("/", response_class=HTMLResponse)
async def landing():
    html = STATIC_DIR / "landing.html"
    if html.exists():
        return HTMLResponse(html.read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return HTMLResponse("<h1>Benmao MT5</h1>")

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    html = STATIC_DIR / "index.html"
    if html.exists():
        return HTMLResponse(html.read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return HTMLResponse("<h1>MT5-Ai Dashboard</h1><p>Use API at /api/*</p>")

@app.get("/landing", response_class=HTMLResponse)
async def landing_alias():
    return await landing()

# ---- Auth Endpoints (no auth required) ----
class LoginRequest(BaseModel):
    kami: str

@app.post("/api/auth/login")
async def auth_login(req: LoginRequest):
    kami = req.kami.strip()
    if not kami:
        return {"ok": False, "msg": "卡密不能为空"}
    if _auth_session["authenticated"]:
        return {"ok": True, "msg": "already authenticated",
                "vip": _auth_session["vip"], "kmtype": _auth_session["kmtype"]}
    result, value, markcode = await _llua_login(kami)
    code = result.get("code")
    msg = result.get("msg")
    if code != LLUA_SUCCESS_CODE:
        err_msg = msg if isinstance(msg, str) else json.dumps(msg, ensure_ascii=False)
        return {"ok": False, "msg": f"验证失败({code}): {err_msg}"}
    login_data = msg if isinstance(msg, dict) else {}
    srv_time = result.get("time", 0)
    if srv_time and value:
        expected_check = _llua_check_calc(srv_time, value)
        actual_check = login_data.get("check", "")
        if actual_check and expected_check != actual_check:
            return {"ok": False, "msg": "数据校验失败，响应可能被篡改"}
    if srv_time and abs(srv_time - int(time.time())) > 30:
        return {"ok": False, "msg": "服务器时间偏差过大，请检查系统时间"}
    _auth_session.update({
        "authenticated": True, "kami": kami,
        "token": login_data.get("token"), "vip": login_data.get("vip", 0),
        "kmtype": login_data.get("kmtype"), "ktype": login_data.get("ktype"),
        "note": login_data.get("note"), "markcode": markcode, "value": value,
    })
    if _auth_session["heartbeat_task"] and not _auth_session["heartbeat_task"].done():
        _auth_session["heartbeat_task"].cancel()
    _auth_session["heartbeat_task"] = asyncio.create_task(_heartbeat_loop())
    logger.info("Card auth success: kami=%s kmtype=%s vip=%s", kami[:4]+"****", login_data.get("kmtype"), login_data.get("vip"))
    # Signal launcher to start engine (IPC via file marker)
    try:
        AUTH_MARKER.parent.mkdir(parents=True, exist_ok=True)
        AUTH_MARKER.write_text("1", encoding="utf-8")
    except Exception:
        pass
    return {"ok": True, "msg": "验证成功",
            "vip": login_data.get("vip", 0), "kmtype": login_data.get("kmtype"),
            "ktype": login_data.get("ktype"), "note": login_data.get("note", "")}

@app.get("/api/auth/status")
async def auth_status():
    if not _auth_session["authenticated"]:
        return {"authenticated": False}
    if _auth_session["vip"] > 0 and _auth_session["vip"] <= time.time():
        _invalidate_session()
        return {"authenticated": False, "reason": "expired"}
    return {
        "authenticated": True,
        "vip": _auth_session["vip"],
        "kmtype": _auth_session["kmtype"],
        "ktype": _auth_session["ktype"],
        "note": _auth_session["note"],
    }

@app.post("/api/auth/logout")
async def auth_logout():
    _invalidate_session()
    # Signal launcher to stop engine
    try:
        STOP_MARKER.parent.mkdir(parents=True, exist_ok=True)
        STOP_MARKER.write_text("1", encoding="utf-8")
    except Exception:
        pass
    return {"ok": True, "msg": "已退出"}


@app.post("/api/test-email")
async def test_email():
    """Send a test email to verify email configuration."""
    try:
        from src.notifier.email_alerter import EmailAlerter
        load_dotenv(str(ENV_PATH))
        sender = os.getenv("EMAIL_SENDER", "")
        pwd = os.getenv("EMAIL_PASSWORD", "")
        recv = os.getenv("EMAIL_RECEIVER", "")
        if not (sender and pwd and recv):
            return {"ok": False, "msg": "邮箱未配置: 请在 .env 中填写 EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECEIVER"}
        alerter = EmailAlerter(sender, pwd, recv)
        if not alerter._enabled:
            return {"ok": False, "msg": "邮箱未启用"}
        alerter.send_order("TEST", "BUY", "TEST", 0.01, 0.0)
        return {"ok": True, "msg": f"测试邮件已发送至 {recv}"}
    except Exception as e:
        return {"ok": False, "msg": f"发送失败: {e}"}

# ---- Admin Strategy API (requires X-Admin-Key header) ----
def _verify_admin(request: Request):
    key = request.headers.get("X-Admin-Key", "")
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="管理员密钥错误")

@app.get("/api/admin/strategy/list")
async def admin_list_strategies(request: Request):
    """List all managed strategies (uploaded + cloud)."""
    _verify_admin(request)
    from src.strategy.cloud import CloudStrategyManager, CLOUD_DIR
    result = {"uploaded": {}, "cloud": {}}
    for name, info in _ADMIN_STRATEGIES.items():
        result["uploaded"][name] = {
            "version": info.get("version", ""),
            "backtested": info.get("backtest_result") is not None,
            "backtest_passed": info.get("backtest_result", {}).get("passed", False) if info.get("backtest_result") else False,
        }
    # List cloud strategies
    for enc_file in CLOUD_DIR.glob("*.enc"):
        result["cloud"][enc_file.stem] = {"file": str(enc_file)}
    if GITEE_OWNER and GITEE_TOKEN:
        try:
            cm = CloudStrategyManager(GITEE_OWNER, GITEE_REPO, GITEE_TOKEN)
            manifest = cm.get_manifest()
            for name, info in manifest.get("strategies", {}).items():
                if name not in result["cloud"]:
                    result["cloud"][name] = info
        except Exception:
            pass
    return result

@app.post("/api/admin/strategy/upload")
async def admin_upload_strategy(request: Request):
    """Upload a strategy .py source file for backtesting."""
    _verify_admin(request)
    body = await request.json()
    name = body.get("name", "")
    source = body.get("source", "")
    version = body.get("version", "1.0.0")
    if not name or not source:
        raise HTTPException(status_code=400, detail="需要 name 和 source 字段")
    _ADMIN_STRATEGIES[name] = {"source": source, "version": version, "backtest_result": None}
    return {"ok": True, "msg": f"策略 {name} v{version} 已上传，等待回测"}

@app.post("/api/admin/strategy/backtest")
async def admin_backtest_strategy(request: Request):
    """Run backtest on an uploaded strategy."""
    _verify_admin(request)
    body = await request.json()
    name = body.get("name", "")
    days = body.get("days", 30)
    if name not in _ADMIN_STRATEGIES:
        raise HTTPException(status_code=404, detail=f"策略 {name} 未上传")
    info = _ADMIN_STRATEGIES[name]
    try:
        from src.strategy.backtest import BacktestRunner
        runner = BacktestRunner(symbol=os.getenv("MT5_SYMBOL", "XAUUSD"))
        result = runner.run_from_source(info["source"], f"bt_{name}", days=days)
        result_dict = {
            "total_trades": result.total_trades,
            "wins": result.wins, "losses": result.losses,
            "win_rate": round(result.win_rate, 2),
            "total_pnl": round(result.total_pnl, 2),
            "profit_factor": round(result.profit_factor, 2),
            "max_drawdown": round(result.max_drawdown, 2),
            "passed": result.passed,
            "reason": result.reason,
        }
        _ADMIN_STRATEGIES[name]["backtest_result"] = result_dict
        return {"ok": True, "result": result_dict}
    except Exception as e:
        logger.error("Backtest failed for %s: %s", name, e)
        raise HTTPException(status_code=500, detail=f"回测失败: {e}")

@app.post("/api/admin/strategy/publish")
async def admin_publish_strategy(request: Request):
    """Compile, encrypt, and publish a strategy to Gitee."""
    _verify_admin(request)
    body = await request.json()
    name = body.get("name", "")
    if name not in _ADMIN_STRATEGIES:
        raise HTTPException(status_code=404, detail=f"策略 {name} 未上传")
    info = _ADMIN_STRATEGIES[name]
    bt = info.get("backtest_result")
    if not bt or not bt.get("passed"):
        raise HTTPException(status_code=400, detail="策略未通过回测，不能发布")
    if not GITEE_OWNER or not GITEE_TOKEN:
        raise HTTPException(status_code=500, detail="Gitee 配置缺失 (GITEE_OWNER/GITEE_TOKEN)")
    try:
        from src.strategy.crypto import compile_to_pyc, encrypt_pyc
        from src.strategy.cloud import CloudStrategyManager, CLOUD_DIR
        # Compile → encrypt
        pyc = compile_to_pyc(info["source"], name)
        enc = encrypt_pyc(pyc)
        enc_path = CLOUD_DIR / f"{name}.enc"
        CLOUD_DIR.mkdir(parents=True, exist_ok=True)
        enc_path.write_bytes(enc)
        # Publish to Gitee
        cm = CloudStrategyManager(GITEE_OWNER, GITEE_REPO, GITEE_TOKEN)
        ok = cm.publish_to_gitee(name, info["version"], enc_path, changelog=body.get("changelog", ""))
        if ok:
            return {"ok": True, "msg": f"策略 {name} v{info['version']} 已发布到 Gitee"}
        else:
            raise HTTPException(status_code=500, detail="发布到 Gitee 失败")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Publish failed for %s: %s", name, e)
        raise HTTPException(status_code=500, detail=f"发布失败: {e}")

@app.post("/api/admin/strategy/check-updates")
async def admin_check_updates(request: Request):
    """Manually trigger cloud strategy update check + download."""
    _verify_admin(request)
    if not GITEE_OWNER or not GITEE_TOKEN:
        raise HTTPException(status_code=500, detail="Gitee 配置缺失")
    try:
        from src.strategy.cloud import CloudStrategyManager
        cm = CloudStrategyManager(GITEE_OWNER, GITEE_REPO, GITEE_TOKEN)
        downloaded = cm.download_all_updates()
        return {"ok": True, "downloaded": downloaded}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"检查更新失败: {e}")

@app.get("/api/status")
async def get_status():
    """1. 全局系统状态 - 合并引擎进程状态 + MT5实时数据 + 数据库统计"""
    # Load engine state from shared file (written by engine process)
    es = load_engine_state()

    # Live MT5 account info (from web process's own MT5 connection)
    acc = None
    try:
        _ensure_mt5()
        a = mt5.account_info()
        if a:
            acc = {
                "balance": round(a.balance, 2),
                "equity": round(a.equity, 2),
                "margin_free": round(a.margin_free, 2),
                "margin_level": round(a.margin_level, 2) if hasattr(a, 'margin_level') and a.margin_level else 0,
                "leverage": a.leverage,
                "currency": a.currency,
                "server": a.server,
                "name": a.name,
            }
    except Exception as e:
        logger.debug("MT5 live account fetch failed: %s", e)

    # DB stats
    stats = _load_trade_stats()

    # Engine status info from manager's get_status()
    eng_info = es.get("engine_info", {})

    # Daily PnL: DB first (most reliable), then MT5 history (catches manual closes),
    # then engine state cache
    daily_pnl = 0.0
    # Source 1: DB trades closed today
    db = _db()
    if db:
        try:
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            cur = db.execute(
                "SELECT COALESCE(SUM(profit),0) as dp FROM trades "
                "WHERE close_time IS NOT NULL AND close_time LIKE ?",
                (today_str + "%",)
            )
            r = cur.fetchone()
            db_daily = round(r["dp"], 2) if r else 0.0
            if db_daily != 0.0:
                daily_pnl = db_daily
        except Exception:
            pass
        finally:
            db.close()
    # Source 2: MT5 history (if DB has nothing or as supplement)
    if daily_pnl == 0.0:
        mt5_daily = _mt5_daily_pnl()
        if mt5_daily != 0.0:
            daily_pnl = mt5_daily
    # Source 3: engine state cache
    if daily_pnl == 0.0 and es.get("daily_pnl", 0) != 0:
        daily_pnl = es.get("daily_pnl", 0)

    # Check for strategy switch error
    switch_error = ""
    try:
        if STRATEGY_SWITCH_FILE.exists():
            sw = json.loads(STRATEGY_SWITCH_FILE.read_text(encoding="utf-8"))
            switch_error = sw.get("_error", "")
    except Exception:
        pass

    return {
        "status": "success",
        "data": {
            "engine_state": es.get("engine_state", "INIT"),
            "engine_info": {
                "state": eng_info.get("state", "N/A"),
                "streak": eng_info.get("streak", "0W/0L"),
                "silence_reason": eng_info.get("silence_reason", ""),
                "position": eng_info.get("position"),
                "consecutive_losses": eng_info.get("cl", 0),
            },
            "switch_error": switch_error,
            "daily_pnl": daily_pnl,
            "daily_trades": es.get("daily_trades", 0) or _db_daily_trade_count(),
            "daily_max_drawdown": es.get("daily_max_drawdown", 0),
            "macro_bias": es.get("macro_bias", "NEUTRAL"),
            "cooldown_active": es.get("cooldown_active", False),
            "last_error": es.get("last_error", ""),
            "active_strategy": es.get("active_strategy", os.getenv("ACTIVE_STRATEGY", "B2")),
            "available_strategies": es.get("available_strategies", ["V2","V3","V4","B1","B2","T1"]),
            "account": acc,
            "stats": stats,
            "config": es.get("config", {}),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    }

@app.get("/api/positions")
async def get_positions():
    """2. 实时活跃持仓 - 直接从MT5拉取"""
    try:
        _ensure_mt5()
        pos = mt5.positions_get()
        if not pos:
            return {"status": "success", "data": []}
        result = []
        for p in pos:
            result.append({
                "ticket": p.ticket,
                "symbol": p.symbol,
                "direction": "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL",
                "volume": p.volume,
                "open_price": p.price_open,
                "current_price": p.price_current,
                "sl": p.sl,
                "tp": p.tp,
                "profit": round(p.profit, 2),
                "swap": p.swap,
                "open_time": datetime.fromtimestamp(p.time - _get_server_offset()).isoformat(),
                "duration_min": round((time.time() + _get_server_offset() - p.time) / 60, 1),
                "sl_status": "break_even" if p.sl and p.price_open and (
                    (p.type == mt5.ORDER_TYPE_BUY and p.sl > p.price_open) or
                    (p.type == mt5.ORDER_TYPE_SELL and p.sl < p.price_open)
                ) else ("profit_lock" if p.sl and p.price_open and abs(p.sl - p.price_open) >= 10 else "initial"),
                "magic": p.magic,
                "comment": p.comment or "",
            })
        return {"status": "success", "data": result}
    except Exception as e:
        return {"status": "error", "message": str(e), "data": []}

@app.post("/api/killswitch")
async def killswitch():
    """3. 紧急熔断 - 平所有持仓 + 撤销所有挂单"""
    try:
        _ensure_mt5()
        pos = mt5.positions_get()
        closed = 0
        if pos:
            for p in pos:
                tick = mt5.symbol_info_tick(p.symbol)
                if not tick:
                    continue
                price = tick.bid if p.type == mt5.ORDER_TYPE_BUY else tick.ask
                close_type = mt5.ORDER_TYPE_SELL if p.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
                req = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": p.symbol,
                    "volume": p.volume,
                    "type": close_type,
                    "position": p.ticket,
                    "price": price,
                    "deviation": 50,
                    "magic": 0,
                    "comment": "killswitch",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": _get_web_filling(p.symbol),
                }
                r = mt5.order_send(req)
                if r and r.retcode == mt5.TRADE_RETCODE_DONE:
                    closed += 1
                    direction = "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL"
                    _email_close(direction, round(p.profit, 2), time.time() + _get_server_offset() - p.time, "Killswitch")

        # Also cancel all pending orders
        orders = mt5.orders_get()
        cancelled = 0
        if orders:
            for o in orders:
                req = {
                    "action": mt5.TRADE_ACTION_REMOVE,
                    "order": o.ticket,
                }
                r = mt5.order_send(req)
                if r and r.retcode == mt5.TRADE_RETCODE_DONE:
                    cancelled += 1

        # Update shared state
        save_engine_state({
            "engine_state": "KILLED",
            "last_error": f"Killswitch: closed {closed} positions, cancelled {cancelled} orders",
        })
        logger.warning("KILLSWITCH: closed %d pos, cancelled %d orders", closed, cancelled)
        return {"status": "success", "closed_positions": closed, "cancelled_orders": cancelled}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _close_position_by_ticket(ticket: int) -> dict:
    """Close a single position by ticket. Returns result dict with details for email."""
    pos = mt5.positions_get(ticket=ticket)
    if not pos or len(pos) == 0:
        return {"ok": False}
    p = pos[0]
    direction = "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL"
    open_time = p.time
    tick = mt5.symbol_info_tick(p.symbol)
    if not tick:
        return {"ok": False}
    price = tick.bid if p.type == mt5.ORDER_TYPE_BUY else tick.ask
    close_type = mt5.ORDER_TYPE_SELL if p.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": p.symbol,
        "volume": p.volume,
        "type": close_type,
        "position": p.ticket,
        "price": price,
        "deviation": 20,
        "magic": p.magic,
        "comment": "manual_close",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": _get_web_filling(p.symbol),
    }
    result = mt5.order_send(req)
    ok = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
    return {
        "ok": ok,
        "direction": direction,
        "profit": round(p.profit, 2) if ok else 0,
        "hold_seconds": time.time() + _get_server_offset() - open_time if ok else 0,
        "ticket": ticket,
    }


@app.post("/api/positions/{ticket}/close")
async def close_single_position(ticket: int):
    """Close a single position by ticket."""
    try:
        _ensure_mt5()
        r = _close_position_by_ticket(ticket)
        if r.get("ok"):
            _email_close(r["direction"], r["profit"], r["hold_seconds"], "Manual Close")
            return {"ok": True, "ticket": ticket}
        return {"ok": False, "message": "Close failed"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@app.post("/api/positions/close-profitable")
async def close_profitable_positions():
    """Close all positions with positive profit."""
    try:
        _ensure_mt5()
        pos = mt5.positions_get()
        closed = 0
        if pos:
            for p in pos:
                if p.profit > 0:
                    r = _close_position_by_ticket(p.ticket)
                    if r.get("ok"):
                        closed += 1
                        _email_close(r["direction"], r["profit"], r["hold_seconds"], "Close Profitable")
        return {"ok": True, "closed": closed}
    except Exception as e:
        return {"ok": False, "message": str(e)}

@app.get("/api/config")
async def get_config():
    """4. 获取策略配置"""
    return load_current_config()

@app.get("/api/config/limits")
async def get_config_limits():
    """4b. 获取参数安全极值"""
    return CONFIG_LIMITS

# ---- Strategy Switch (IPC via strategy_switch.json) ----
@app.get("/api/strategies")
async def list_strategies():
    """4c. 获取可用策略列表和当前活跃策略"""
    es = load_engine_state()
    available = es.get("available_strategies", ["V2", "V3", "V4", "V5", "B1", "B2"])
    current = es.get("active_strategy", os.getenv("ACTIVE_STRATEGY", "B2"))
    return {"available": available, "current": current}

@app.post("/api/strategies/switch")
async def switch_strategy(request: Request):
    """4d. 切换策略 (写入 strategy_switch.json, 引擎下次循环读取)"""
    body = await request.json()
    strategy = body.get("strategy", "")
    es = load_engine_state()
    available = es.get("available_strategies", ["V2", "V3", "V4", "V5", "B1", "B2"])
    if strategy not in available:
        raise HTTPException(400, detail=f"Unknown strategy: {strategy}. Available: {available}")
    data = {"strategy": strategy, "_applied": False, "_timestamp": datetime.now(timezone.utc).isoformat()}
    try:
        STRATEGY_SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STRATEGY_SWITCH_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STRATEGY_SWITCH_FILE)
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    return {"status": "pending", "strategy": strategy}

@app.post("/api/mt5/test")
async def test_mt5_connection():
    """4e. 测试 MT5 账户连接"""
    try:
        import MetaTrader5 as mt5_test
        # Reload .env to pick up recently saved credentials
        load_dotenv(str(ENV_PATH), override=True)
        login = int(os.getenv("MT5_LOGIN", "0"))
        pwd = os.getenv("MT5_PASSWORD", "")
        srv = os.getenv("MT5_SERVER", "")
        if not login or not pwd or not srv:
            return {"ok": False, "error": "Missing MT5_LOGIN/MT5_PASSWORD/MT5_SERVER in .env"}
        ok = mt5_test.initialize(login=login, password=pwd, server=srv)
        if not ok:
            err = mt5_test.last_error()
            mt5_test.shutdown()
            return {"ok": False, "error": f"MT5 connect failed: {err}"}
        acc = mt5_test.account_info()
        symbols = mt5_test.symbols_total()
        sym_info = mt5_test.symbol_info(os.getenv("MT5_SYMBOL", "XAUUSD"))
        mt5_test.shutdown()
        return {
            "ok": True,
            "account": {
                "login": acc.login if acc else None,
                "name": acc.name if acc else None,
                "balance": acc.balance if acc else 0,
                "equity": acc.equity if acc else 0,
                "server": acc.server if acc else None,
            },
            "symbols_total": symbols,
            "symbol_valid": sym_info is not None,
            "symbol_name": os.getenv("MT5_SYMBOL", "XAUUSD"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/mt5/symbols")
async def get_mt5_symbols():
    """4f. 获取 MT5 可交易品种列表"""
    try:
        _ensure_mt5()
        all_syms = mt5.symbols_get()
        if not all_syms:
            return {"symbols": []}
        tradeable = [s.name for s in all_syms if s.visible and not s.custom]
        return {"symbols": sorted(tradeable)}
    except Exception as e:
        return {"symbols": [], "error": str(e)}

# ---- Settings (.env read/write + restart) ----
ENV_SCHEMA = [
    {"key": "MT5_LOGIN",       "label_zh": "MT5 账号",       "label_en": "MT5 Login",       "group": "mt5",   "type": "text"},
    {"key": "MT5_PASSWORD",    "label_zh": "MT5 密码",       "label_en": "MT5 Password",    "group": "mt5",   "type": "password"},
    {"key": "MT5_SERVER",      "label_zh": "MT5 服务器",     "label_en": "MT5 Server",      "group": "mt5",   "type": "text"},
    {"key": "MT5_SYMBOL",      "label_zh": "交易品种",       "label_en": "Symbol",          "group": "mt5",   "type": "text"},
    {"key": "MT5_VOLUME",      "label_zh": "手数",           "label_en": "Volume",          "group": "mt5",   "type": "number", "step": "0.01", "min": "0.01", "max": "1"},
    {"key": "ACTIVE_STRATEGY", "label_zh": "策略",           "label_en": "Strategy",        "group": "mt5",   "type": "select", "options": ["V2","V3","V4","V5","B1","B2"]},
    {"key": "EMAIL_SENDER",    "label_zh": "发件邮箱",       "label_en": "Email Sender",    "group": "email", "type": "text"},
    {"key": "EMAIL_PASSWORD",  "label_zh": "邮箱授权码",     "label_en": "Email Auth Code",  "group": "email", "type": "password"},
    {"key": "EMAIL_RECEIVER",  "label_zh": "收件邮箱",       "label_en": "Email Receiver",  "group": "email", "type": "text"},
    {"key": "EMAIL_SMTP_HOST", "label_zh": "SMTP 服务器",    "label_en": "SMTP Host",       "group": "email", "type": "text"},
    {"key": "EMAIL_SMTP_PORT", "label_zh": "SMTP 端口",      "label_en": "SMTP Port",       "group": "email", "type": "number", "step": "1", "min": "1", "max": "65535"},
]

def _parse_env_text(text: str) -> dict:
    """Parse .env text into {key: value} dict"""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            result[k.strip()] = v.split("#")[0].strip()
    return result

def _read_env() -> dict:
    try:
        text = ENV_PATH.read_text(encoding="utf-8")
        return _parse_env_text(text)
    except FileNotFoundError:
        return {}

@app.get("/api/settings")
async def get_settings():
    """5a. 获取 .env 配置和字段定义"""
    values = _read_env()
    # Dynamic strategy options from engine state
    es = load_engine_state()
    strat_options = es.get("available_strategies", ["V2", "V3", "V4", "V5", "B1", "B2"])
    schema = []
    for s in ENV_SCHEMA:
        if s.get("key") == "ACTIVE_STRATEGY":
            s = {**s, "options": strat_options}
        schema.append(s)
    return {"values": values, "schema": schema}

@app.post("/api/settings")
async def save_settings(values: dict):
    """5b. 保存 .env 配置（保留注释和格式）"""
    try:
        old_text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
        new_lines = []
        for line in old_text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue
            if "=" in stripped:
                k = stripped.split("=", 1)[0].strip()
                if k in values:
                    comment = ""
                    if "#" in stripped:
                        comment = "  # " + stripped.split("#", 1)[1].strip()
                    new_lines.append(f"{k}={values[k]}{comment}")
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)
        # Add any new keys not in file
        existing_keys = set()
        for line in old_text.splitlines():
            s = line.strip()
            if "=" in s and not s.startswith("#"):
                existing_keys.add(s.split("=", 1)[0].strip())
        for k, v in values.items():
            if k not in existing_keys:
                new_lines.append(f"{k}={v}")
        ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        # Reload env vars so subsequent API calls use new values
        load_dotenv(str(ENV_PATH), override=True)
        # Reset MT5 connection so _ensure_mt5 reconnects with new credentials
        global _mt5_inited
        _mt5_inited = False
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, detail=str(e))

@app.post("/api/restart")
async def restart_app():
    """5c. 写入重启标记文件, run.py 检测后执行重启"""
    try:
        restart_file = BASE / "data" / "_restart_marker"
        restart_file.parent.mkdir(parents=True, exist_ok=True)
        restart_file.write_text("1", encoding="utf-8")
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, detail=str(e))

class ConfigItem(BaseModel):
    key: str
    value: float | str | int | bool

@app.post("/api/config")
async def update_config(items: list[ConfigItem]):
    """5. 热更新策略参数 (写入 config_hot.json, 引擎下次循环读取)"""
    errors = []
    valid = []
    for item in items:
        lim = CONFIG_LIMITS.get(item.key)
        if not lim:
            errors.append(f"Unknown key: {item.key}")
            continue
        v = item.value
        if lim["type"] == "int":
            v = int(v)
        else:
            v = float(v)
        if v < lim["min"] or v > lim["max"]:
            errors.append(f"{item.key}={v} out of range [{lim['min']}, {lim['max']}]")
            continue
        valid.append({"key": item.key, "value": v})
    if errors:
        raise HTTPException(400, detail="; ".join(errors))
    if valid:
        save_config_hot(valid)
    return {"status": "pending", "updated": [v["key"] for v in valid]}

@app.get("/api/trades")
async def get_trades(limit: int = 100, offset: int = 0):
    """6. 决策记录"""
    db = _db()
    if not db:
        return []
    cur = db.execute(
        "SELECT id, timestamp, direction, strategy, price, volume, order_id, "
        "reason, indicators, cooldown "
        "FROM decisions ORDER BY id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    rows = [dict(r) for r in cur.fetchall()]
    db.close()
    return rows

@app.get("/api/trades/history")
async def get_trade_history(limit: int = 50, offset: int = 0):
    """6b. 已平仓交易记录"""
    db = _db()
    if not db:
        return []
    cur = db.execute(
        "SELECT id, open_time, close_time, direction, volume, open_price, "
        "close_price, profit, profit_pips, strategy, sl, tp, decision_id "
        "FROM trades WHERE close_time IS NOT NULL "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    rows = [dict(r) for r in cur.fetchall()]
    db.close()
    return rows

@app.get("/api/trades/stats")
async def get_trade_stats():
    """7. 交易统计"""
    return _load_trade_stats()

@app.get("/api/klines")
async def get_klines(
    symbol: str = Query(None),
    timeframe: str = Query("M5"),
    count: int = Query(200),
):
    """8. K线数据"""
    if not symbol:
        symbol = os.getenv("MT5_SYMBOL", "XAUUSD")
    tf = TF_MAP.get(timeframe, mt5.TIMEFRAME_M5)
    _ensure_mt5()
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None or len(rates) == 0:
        return []
    return [
        {
            "time": int(r[0]),
            "open": round(r[1], 2),
            "high": round(r[2], 2),
            "low": round(r[3], 2),
            "close": round(r[4], 2),
            "volume": int(r[5]),
        }
        for r in rates
    ]

@app.get("/api/trades/audit")
async def get_trade_audit(from_date: str = "", to_date: str = ""):
    """8b. 历史交易审计数据"""
    db = _db()
    if not db:
        return {"equity_curve": [], "daily_stats": [], "reason_dist": [], "recent_trades": [], "recent_decisions": []}

    date_filter = ""
    params = []
    if from_date:
        date_filter += " AND close_time >= ?"
        params.append(from_date + "T00:00:00")
    if to_date:
        date_filter += " AND close_time <= ?"
        params.append(to_date + "T23:59:59")

    # Equity curve: cumulative PnL
    cur = db.execute(
        "SELECT close_time, profit FROM trades WHERE close_time IS NOT NULL"
        + date_filter + " ORDER BY close_time ASC",
        params
    )
    rows = [dict(r) for r in cur.fetchall()]
    cum = 0.0
    equity_curve = []
    for r in rows:
        cum += r["profit"] or 0
        equity_curve.append({"time": r["close_time"], "equity": round(cum, 2)})

    # Daily stats
    cur2 = db.execute(
        "SELECT DATE(close_time) as day, COUNT(*) as total, "
        "SUM(CASE WHEN profit > 0 THEN 1 ELSE 0 END) as wins, "
        "COALESCE(SUM(profit), 0) as pnl "
        "FROM trades WHERE close_time IS NOT NULL"
        + date_filter + " GROUP BY DATE(close_time) ORDER BY day",
        params
    )
    daily_stats = [dict(r) for r in cur2.fetchall()]

    # Reason distribution
    cur3 = db.execute(
        "SELECT reason, COUNT(*) as cnt FROM decisions GROUP BY reason ORDER BY cnt DESC LIMIT 10"
    )
    reason_dist = [dict(r) for r in cur3.fetchall()]

    # Recent trades with indicators (include open trades too)
    cur4 = db.execute(
        "SELECT t.id, t.open_time, t.close_time, t.direction, t.volume, "
        "t.open_price, t.close_price, t.profit, t.strategy, t.sl, t.tp, "
        "d.indicators, d.reason "
        "FROM trades t LEFT JOIN decisions d ON t.decision_id = d.id "
        "ORDER BY t.id DESC LIMIT 30"
    )
    recent_trades = [dict(r) for r in cur4.fetchall()]

    # Recent decisions
    cur5 = db.execute(
        "SELECT id, timestamp, direction, price, reason, indicators "
        "FROM decisions ORDER BY id DESC LIMIT 20"
    )
    recent_decisions = [dict(r) for r in cur5.fetchall()]

    db.close()
    return {
        "equity_curve": equity_curve,
        "daily_stats": daily_stats,
        "reason_dist": reason_dist,
        "recent_trades": recent_trades,
        "recent_decisions": recent_decisions,
    }

@app.get("/api/decisions")
async def get_decisions(limit: int = 50, offset: int = 0):
    """9. 决策标记（用于K线图标注）"""
    db = _db()
    if not db:
        return []
    cur = db.execute(
        "SELECT timestamp, direction, price, reason FROM decisions "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    rows = [dict(r) for r in cur.fetchall()]
    db.close()
    return rows

def run(host: str = "0.0.0.0", port: int = 8000):
    uvicorn.run(app, host=host, port=port, log_level="info")

if __name__ == "__main__":
    run()