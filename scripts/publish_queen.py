"""发布 Queen v1 到 Gitee (量子女王2.9.1改编)"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.strategy.crypto import compile_to_pyc, encrypt_pyc
from src.strategy.cloud import CloudStrategyManager

GITEE_OWNER = "WuBenmao"
GITEE_REPO = "mt5-strategies"
GITEE_TOKEN = "你的令牌"
NAME = "Queen"
VERSION = "1.0.0"

def main():
    source_path = Path("scripts/bt_queen.py")
    source = source_path.read_text(encoding="utf-8")
    print(f"读取: {source_path} ({len(source)} 字符)")

    pyc = compile_to_pyc(source, NAME)
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
            "Queen v1.0.0 首发 量子女王改编版\n"
            "========================\n"
            "趋势方向网格 + 分段止盈 + SMC结构入场\n"
            "回测208天 +268U WR85.3% PF1.51 DD10U\n"
            "每月都盈利 | 适配FxPro 100U"
        )
    )
    if ok:
        print(f"[OK] 发布成功! {NAME} v{VERSION} -> Gitee")
    else:
        print("[FAIL] 发布失败")
        sys.exit(1)

if __name__ == "__main__":
    main()
