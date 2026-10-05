"""BTP 策略发布脚本: 编译 → 加密 → 验证云端加载 → 推送到 Gitee

用法: python scripts/publish_btp.py
"""
import sys
import json
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
import os

BASE = Path(__file__).resolve().parent.parent
load_dotenv(BASE / ".env")

from src.strategy.crypto import compile_to_pyc, encrypt_pyc, decrypt_pyc, decrypt_to_module, extract_strategy_class
from src.strategy.cloud import CloudStrategyManager, CLOUD_DIR, MANIFEST_FILE

STRATEGY_NAME = "BTP"
VERSION = "1.1.0"
SOURCE_FILE = BASE / "src" / "strategy" / "engine_btp.py"
ENC_FILE = CLOUD_DIR / f"{STRATEGY_NAME}.enc"


def main():
    print(f"BTP 策略发布到 Gitee")
    print("=" * 60)

    if not SOURCE_FILE.exists():
        print(f"错误: 源码不存在 {SOURCE_FILE}")
        return
    source = SOURCE_FILE.read_text(encoding="utf-8")
    print(f"[1/7] 读取源码: {len(source)} 字节")

    pyc = compile_to_pyc(source, STRATEGY_NAME)
    print(f"[2/7] 编译 .pyc: {len(pyc)} 字节")

    enc = encrypt_pyc(pyc)
    print(f"[3/7] 加密 .enc: {len(enc)} 字节")

    dec = decrypt_pyc(enc)
    if dec != pyc:
        print("错误: 加密-解密往返校验失败")
        return
    print("[4/7] 加密往返校验: OK")

    try:
        mod = decrypt_to_module(enc, f"cloud_{STRATEGY_NAME}_verify")
        cls_name, cls = extract_strategy_class(mod)
        print(f"[4.5/7] 云端加载验证: OK (类名 {cls_name})")
    except Exception as e:
        print(f"错误: 云端加载验证失败: {e}")
        return

    CLOUD_DIR.mkdir(parents=True, exist_ok=True)
    ENC_FILE.write_bytes(enc)
    print(f"[5/7] 保存本地: {ENC_FILE}")

    owner = os.getenv("GITEE_OWNER", "")
    repo = os.getenv("GITEE_REPO", "mt5-strategies")
    token = os.getenv("GITEE_TOKEN", "")
    if not owner or not token:
        print("错误: .env 未配置 GITEE_OWNER / GITEE_TOKEN")
        return

    print(f"[6/7] 发布到 Gitee: {owner}/{repo} ...")
    cm = CloudStrategyManager(owner, repo, token)
    changelog = (
        "BTP v1.1.0: 修复点差侵蚀bug + 适配FxPro GOLD(点差0.23)。"
        "关键修复: TP/SL改为基于实际成交价(ASK/BID)计算, 避免点差导致SL秒触发。"
        "参数更新: TP=0.50 SL=0.40 Hold=180s 评分≥1 智能出场已禁用。"
        "回测41天(FxPro GOLD): 1062笔 WR51.8% PnL+90.7U PF1.44 28/13盈日, 日均+2.21U。"
    )
    ok = cm.publish_to_gitee(STRATEGY_NAME, VERSION, ENC_FILE, changelog=changelog)
    if not ok:
        print("错误: 发布到 Gitee 失败")
        return
    print(f"[6/7] 发布成功: strategies/{STRATEGY_NAME}.enc v{VERSION}")

    manifest = cm.get_manifest()
    manifest.setdefault("strategies", {})[STRATEGY_NAME] = {
        "version": VERSION,
        "file": str(ENC_FILE),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp = MANIFEST_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(MANIFEST_FILE)
    print(f"[7/7] 本地 manifest 已更新")

    print("=" * 60)
    print(f"完成! 策略 {STRATEGY_NAME} v{VERSION} 已发布到 Gitee。")


if __name__ == "__main__":
    main()
