"""启动 Web 看板 (http://localhost:8000)"""
from src.web.server import run

if __name__ == "__main__":
    run(host="0.0.0.0", port=8000)
