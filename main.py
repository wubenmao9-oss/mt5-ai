"""MT5-Ai Engine - Multi-Strategy Runner with Web Dashboard Bridge"""

import asyncio
import logging
import os
import sys
import json
from pathlib import Path
from datetime import datetime, timezone

if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from dotenv import load_dotenv


def _app_dir():
    """Application directory (where .env and data/ live)."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


from src.market_data import Tick
from src.trading.mt5_executor import MT5Executor
from src.strategy import StrategyManager, StrategyConfig
from src.strategy import (
    StrategyEngineV2, StrategyEngineB1, StrategyEngineT1,
)
from src.strategy.recorder import Recorder
from src.notifier.email_alerter import EmailAlerter
from src.strategy.cloud import CloudStrategyManager
from src.web.server import save_engine_state, CONFIG_HOT_FILE, STRATEGY_SWITCH_FILE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-24s %(levelname)-8s %(message)s",
)
logger = logging.getLogger("main")
APP_DIR = _app_dir()
load_dotenv(str(APP_DIR / ".env"))

STATE_FILE = APP_DIR / "data" / "engine_state.json"

async def main():
    login = int(os.getenv("MT5_LOGIN", "0"))
    password = os.getenv("MT5_PASSWORD", "")
    server = os.getenv("MT5_SERVER", "")
    symbol = os.getenv("MT5_SYMBOL", "XAUUSD")
    volume = float(os.getenv("MT5_VOLUME", "0.01"))

    if not login or not password or not server:
        logger.error("Set MT5 credentials in .env")
        return

    exe = MT5Executor.get_instance(login=login, password=password, server=server)
    acc = await exe.get_account_info()
    if acc:
        logger.info("Acc: %s Bal: %.2f %s", acc["name"], acc["balance"], acc["currency"])

    config = StrategyConfig(symbol=symbol, volume=volume)
    recorder = Recorder()

    # Email alerter
    email_sender = os.getenv("EMAIL_SENDER", "")
    email_pwd = os.getenv("EMAIL_PASSWORD", "")
    email_recv = os.getenv("EMAIL_RECEIVER", "")
    emailer = (
        EmailAlerter(email_sender, email_pwd, email_recv)
        if email_sender and email_pwd and email_recv
        else None
    )

    # Attach emailer to executor so ALL strategies trigger email on order (including cloud .enc)
    exe._emailer = emailer
    exe._strategy_name = os.getenv("ACTIVE_STRATEGY", "V2")

    # Strategy manager
    active = os.getenv("ACTIVE_STRATEGY", "V2")
    manager = StrategyManager(executor=exe, config=config, recorder=recorder, emailer=emailer)
    manager.register("V2", StrategyEngineV2)
    manager.register("B1", StrategyEngineB1)
    manager.register("T1", StrategyEngineT1)

    # Load cloud strategies (encrypted .enc files)
    cloud_manager = None
    gitee_owner = os.getenv("GITEE_OWNER", "")
    gitee_token = os.getenv("GITEE_TOKEN", "")
    if gitee_owner and gitee_token:
        cloud_manager = CloudStrategyManager(gitee_owner, os.getenv("GITEE_REPO", "mt5-strategies"), gitee_token)
        try:
            downloaded = cloud_manager.download_all_updates()
            if downloaded:
                logger.info("Cloud: downloaded updates: %s", downloaded)
        except Exception as e:
            logger.warning("Cloud: update check failed: %s", e)
    # Load local cloud strategy files
    try:
        cloud_strategies = CloudStrategyManager(gitee_owner or "x", "x", "x").load_cloud_strategies()
        for cname, ccls in cloud_strategies:
            manager.register(cname, ccls)
            logger.info("Cloud: registered strategy %s", cname)
    except Exception as e:
        logger.warning("Cloud: failed to load cloud strategies: %s", e)

    # Activate strategy (with fallback)
    try:
        await manager.activate(active)
    except Exception as e:
        logger.error("Failed to activate %s: %s", active, e)
        # Fallback to V2 if the requested strategy fails (e.g. deleted from cloud)
        if active != "V2" and "V2" in manager._registry:
            logger.info("Falling back to V2 (strategy %s may have been removed)", active)
            await manager.activate("V2")
            active = "V2"
            # Update .env so next restart uses V2 directly
            env_path = APP_DIR / ".env"
            if env_path.exists():
                try:
                    lines = env_path.read_text(encoding="utf-8").splitlines()
                    new_lines = [
                        f"ACTIVE_STRATEGY=V2" if l.strip().startswith("ACTIVE_STRATEGY=") else l
                        for l in lines
                    ]
                    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
                    logger.info("Updated .env: ACTIVE_STRATEGY=V2")
                except Exception:
                    pass
        else:
            logger.error("No fallback strategy available, exiting")
            return

    logger.info("Web dashboard: http://localhost:8000")

    stats = recorder.get_stats()
    logger.info(
        "Status: %s | Trades: %d | WR: %.1f%% | PnL: %.2f",
        "Enabled" if emailer and emailer._enabled else "Disabled",
        stats["total_trades"],
        stats["win_rate"],
        stats["total_profit"],
    )

    async def on_tick(tick: Tick):
        await manager.on_tick(tick)

    await exe.start_quote_stream([symbol], on_tick, interval=1.0)
    logger.info("Engine running... Ctrl+C to stop")

    try:
        _cloud_check_counter = 0
        while True:
            await asyncio.sleep(30)
            _cloud_check_counter += 1
            st = manager.get_status()
            s = recorder.get_stats()

            # Periodic cloud strategy update check (every 10 min = 20 cycles of 30s)
            if cloud_manager and _cloud_check_counter >= 20:
                _cloud_check_counter = 0
                try:
                    downloaded = cloud_manager.download_all_updates()
                    if downloaded:
                        for cname, ccls in cloud_manager.load_cloud_strategies():
                            if cname not in manager._registry:
                                manager.register(cname, ccls)
                                logger.info("Cloud: auto-registered new strategy %s", cname)
                except Exception as e:
                    logger.debug("Cloud: periodic check failed: %s", e)

            # Write engine state to shared file for web dashboard
            state_data = {
                "engine_state": st.get("state", "RUNNING"),
                "daily_pnl": s["total_profit"],
                "daily_trades": s["total_trades"],
                "daily_max_drawdown": st.get("max_dd", 0),
                "macro_bias": st.get("macro_bias", "NEUTRAL"),
                "cooldown_active": st.get("cooldown", False),
                "last_error": st.get("silence_reason", ""),
                "engine_info": {
                    "state": st.get("state", "N/A"),
                    "streak": st.get("streak", "0W/0L"),
                    "silence_reason": st.get("silence_reason", ""),
                    "position": st.get("position"),
                    "cl": st.get("cl", 0),
                },
                "_pid": os.getpid(),
                "active_strategy": manager._active_name or active,
                "available_strategies": manager.list_strategies(),
            }

            # Include current config in engine state for web dashboard
            state_data["config"] = config.to_dict()

            save_engine_state(state_data)

            # Check for pending config hot-update
            try:
                if CONFIG_HOT_FILE.exists():
                    hot = json.loads(CONFIG_HOT_FILE.read_text(encoding="utf-8"))
                    if not hot.get("_applied") and hot.get("pending"):
                        for item in hot["pending"]:
                            k, v = item["key"], item["value"]
                            if hasattr(config, k):
                                try:
                                    cur = getattr(config, k)
                                    setattr(config, k, type(cur)(v))
                                    logger.info("Config hot-update: %s = %s", k, v)
                                except Exception as e:
                                    logger.warning("Config hot-update failed: %s=%s: %s", k, v, e)
                        hot["_applied"] = True
                        hot["_applied_at"] = datetime.now(timezone.utc).isoformat()
                        tmp = CONFIG_HOT_FILE.with_suffix(".tmp")
                        tmp.write_text(json.dumps(hot, ensure_ascii=False), encoding="utf-8")
                        tmp.replace(CONFIG_HOT_FILE)
            except Exception as e:
                logger.debug("Config hot-read failed: %s", e)

            # Check for pending strategy switch
            try:
                if STRATEGY_SWITCH_FILE.exists():
                    sw = json.loads(STRATEGY_SWITCH_FILE.read_text(encoding="utf-8"))
                    if not sw.get("_applied") and sw.get("strategy"):
                        new_name = sw["strategy"]
                        if new_name != manager._active_name and new_name in manager._registry:
                            logger.info("Strategy switch: %s -> %s", manager._active_name, new_name)
                            old_name = manager._active_name
                            try:
                                await manager.activate(new_name)
                                logger.info("Strategy switch succeeded: %s -> %s", old_name, new_name)
                            except Exception as switch_err:
                                logger.error("Strategy switch FAILED: %s -> %s: %s", old_name, new_name, switch_err)
                                # Restore old strategy
                                if old_name and old_name in manager._registry:
                                    try:
                                        await manager.activate(old_name)
                                        logger.info("Restored previous strategy: %s", old_name)
                                    except Exception:
                                        pass
                                sw["_error"] = str(switch_err)
                        sw["_applied"] = True
                        sw["_applied_at"] = datetime.now(timezone.utc).isoformat()
                        tmp = STRATEGY_SWITCH_FILE.with_suffix(".tmp")
                        tmp.write_text(json.dumps(sw, ensure_ascii=False), encoding="utf-8")
                        tmp.replace(STRATEGY_SWITCH_FILE)
            except Exception as e:
                logger.debug("Strategy switch read failed: %s", e)

            try:
                logger.info(
                    "[Status] %s  pos=%s  streak=%s  trades=%d  PnL=%.2f  silence=%s",
                    st["state"],
                    "Open" if st["position"] else "None",
                    st["streak"],
                    s["total_trades"],
                    s["total_profit"],
                    st.get("silence_reason", "?"),
                )
            except Exception as e:
                logger.warning("Status log error: %s", e)
    except asyncio.CancelledError:
        pass

    await exe.stop_quote_stream()
    await manager.stop()
    final = recorder.get_stats()
    logger.info(
        "Final: %d trades | WR: %.1f%% | PnL: %.2f",
        final["total_trades"],
        final["win_rate"],
        final["total_profit"],
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Engine stopped")