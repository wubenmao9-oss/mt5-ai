"""发布 ADM v2.1.0 — 修复下单dict bug"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.strategy.crypto import compile_to_pyc, encrypt_pyc
from src.strategy.cloud import CloudStrategyManager

GITEE_OWNER = "WuBenmao"
GITEE_REPO = "mt5-strategies"
GITEE_TOKEN = "你的令牌"
NAME = "ADM"
VERSION = "2.1.0"

def main():
    source_path = Path("src/strategy/engine_xau.py")
    source = source_path.read_text(encoding="utf-8")
    print(f"读取: {source_path} ({len(source)} 字符)")

    pyc = compile_to_pyc(source, "strategy_engine_xau")
    print(f"编译 .pyc: {len(pyc)} 字节")

    enc = encrypt_pyc(pyc)
    enc_path = Path("data/cloud_strategies") / f"{NAME}.enc"
    enc_path.parent.mkdir(parents=True, exist_ok=True)
    enc_path.write_bytes(enc)
    print(f"加密 .enc: {len(enc)} 字节 → {enc_path}")

    print("\n发布到 Gitee...")
    cm = CloudStrategyManager(GITEE_OWNER, GITEE_REPO, GITEE_TOKEN)
    ok = cm.publish_to_gitee(
        NAME, VERSION, enc_path,
        changelog=(
            "ADM v2.1.0 修复下单bug\n"
            "========================\n"
            "[修复] res.deal→res['deal'] dict误用导致多笔无止损开仓\n"
            "[改进] 直接在下单时传SL/TP，取代事后TRADE_ACTION_SLTP\n"
            "回测208天 +1168U WR58.5% PF4.22 DD4U\n"
            "样本外68天 +477U WR57.7% PF4.09 DD4U"
        )
    )
    if ok:
        print(f"[OK] 发布成功! {NAME} v{VERSION} -> Gitee")
    else:
        print("[FAIL] 发布失败")
        sys.exit(1)

if __name__ == "__main__":
    main()
