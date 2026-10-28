"""
一键启动：模拟上游（8100）+ OpenAI 兼容网关（8200）。

用法：
    python start.py

启动后：
    - 浏览器打开 http://127.0.0.1:8200 看接口信息
    - Base URL: http://127.0.0.1:8200/v1
    - API Key : 见终端输出 / 项目根目录 .env

Ctrl+C 同时停掉两个服务。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable

MOCK_PORT = 8100
GW_PORT = 8200


def wait_http(url: str, name: str, timeout: float = 45.0) -> bool:
    """轮询等待服务就绪。比 sleep 固定秒数可靠得多。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status < 500:
                    return True
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
            time.sleep(0.4)
    print(f"[!] {name} 在 {timeout:.0f}s 内没起来：{url}")
    return False


def main() -> int:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(ROOT)
    # 子进程输出直接透传到当前终端，方便看日志
    env["PYTHONUNBUFFERED"] = "1"

    print("=" * 62)
    print(" OpenAI 兼容网关 · 本地练习环境")
    print("=" * 62)

    procs: list[tuple[str, subprocess.Popen]] = []

    def spawn(name: str, module: str, port: int, level: str = "warning") -> subprocess.Popen:
        cmd = [
            PY, "-m", "uvicorn", module,
            "--host", "127.0.0.1", "--port", str(port),
            "--log-level", level,
        ]
        p = subprocess.Popen(cmd, cwd=str(ROOT), env=env)
        procs.append((name, p))
        return p

    try:
        print("[1/2] 启动模拟上游 (自定义协议) ...")
        mock = spawn("mock", "mock_upstream.app:app", MOCK_PORT)
        if not wait_http(f"http://127.0.0.1:{MOCK_PORT}/api/health", "模拟上游"):
            mock.terminate()
            return 1
        print(f"      OK  http://127.0.0.1:{MOCK_PORT}")

        print("[2/2] 启动 OpenAI 兼容网关 ...")
        gw = spawn("gateway", "gateway.app:app", GW_PORT)
        if not wait_http(f"http://127.0.0.1:{GW_PORT}/health", "网关"):
            gw.terminate()
            return 1
        print(f"      OK  http://127.0.0.1:{GW_PORT}")

        # 读取（首次会自动生成）网关密钥
        sys.path.insert(0, str(ROOT))
        from gateway.config import CFG

        print()
        print("-" * 62)
        print(f"  Base URL : http://127.0.0.1:{GW_PORT}/v1")
        print(f"  API Key  : {CFG.gateway_key}")
        _models = {
            "campus": CFG.campus_model,
            "ssejson": CFG.ssejson_model,
            "openai": CFG.upstream_model or "<客户端指定>",
        }
        print(f"  模型名   : {_models.get(CFG.mode, CFG.campus_model)}")
        print(f"  上游模式 : {CFG.mode}")
        print("-" * 62)
        print("  浏览器打开 http://127.0.0.1:%d 可看接入说明" % GW_PORT)
        print("  Ctrl+C 停止全部服务")
        print()

        # 任一进程退出就整体收尾
        while True:
            time.sleep(0.5)
            for name, p in procs:
                if p.poll() is not None:
                    print(f"[!] {name} 进程已退出 (code={p.returncode})，正在收尾")
                    return p.returncode or 1
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在停止 ...")
        return 0
    finally:
        for name, p in procs:
            if p.poll() is None:
                p.terminate()
        for name, p in procs:
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                p.kill()
        print("已全部停止。")


if __name__ == "__main__":
    raise SystemExit(main())
