"""发布 自适应双模 v2.0.0 到 Gitee"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.strategy.crypto import compile_to_pyc, encrypt_pyc
from src.strategy.cloud import CloudStrategyManager

# Gitee配置 (从.env读取)
GITEE_OWNER = "WuBenmao"
GITEE_REPO = "mt5-strategies"
GITEE_TOKEN = "你的令牌"
NAME = "ADM"
VERSION = "2.0.0"

def main():
    # 1. 读取策略源码
    source_path = Path("src/strategy/engine_xau.py")
    source = source_path.read_text(encoding="utf-8")
    print(f"读取策略源码: {source_path} ({len(source)} 字符)")

    # 2. 编译为 .pyc
    print("编译为 .pyc...")
    pyc = compile_to_pyc(source, NAME)
    print(f"  .pyc 大小: {len(pyc)} 字节")

    # 3. 加密为 .enc
    print("加密为 .enc...")
    enc = encrypt_pyc(pyc)
    enc_path = Path("data/cloud_strategies") / f"{NAME}.enc"
    enc_path.parent.mkdir(parents=True, exist_ok=True)
    enc_path.write_bytes(enc)
    print(f"  .enc 大小: {len(enc)} 字节 → {enc_path}")

    # 4. 发布到 Gitee
    print("\n发布到 Gitee...")
    cm = CloudStrategyManager(GITEE_OWNER, GITEE_REPO, GITEE_TOKEN)
    ok = cm.publish_to_gitee(
        NAME, VERSION, enc_path,
        changelog=(
            "ADM v2.0.0 首发 — 自适应双模\n"
            "========================\n"
            "三层架构: H4市场状态 + H1结构 + M5入场\n"
            "双模交易: RANGE均值回归 + TREND趋势延续\n"
            "固定SL/TP: 3:1盈亏比 (SL=0.5 TP=1.5)\n"
            "OOS验证: 样本外68天+477U WR57.7% PF4.09 DD4U\n"
            "ATR≥12: 过滤低波动, RANGE WR从52%→57%"
        )
    )
    if ok:
        print(f"[OK] 发布成功! {NAME} v{VERSION} -> Gitee ({GITEE_OWNER}/{GITEE_REPO})")
    else:
        print("[FAIL] 发布失败")
        sys.exit(1)

if __name__ == "__main__":
    main()
