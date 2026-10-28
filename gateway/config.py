"""
配置加载。优先 .env，缺项用默认值；网关密钥首次启动自动生成并写回 .env。

只依赖标准库，不额外引入 python-dotenv —— 反代练习里少一个依赖少一个坑。
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"


def _parse_env(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def _ensure_gateway_key(env: dict[str, str]) -> str:
    """没有密钥就生成一个，并写回 .env，保证重启后不变。"""
    key = os.environ.get("GATEWAY_KEY") or env.get("GATEWAY_KEY")
    if key:
        return key

    key = "sk-lab-" + secrets.token_urlsafe(24)
    if ENV_FILE.exists():
        text = ENV_FILE.read_text(encoding="utf-8")
        if not text.endswith("\n"):
            text += "\n"
        text += f"\nGATEWAY_KEY={key}\n"
        ENV_FILE.write_text(text, encoding="utf-8")
    else:
        ENV_FILE.write_text(
            "# 本文件由网关首次启动自动生成，可直接编辑\n"
            f"GATEWAY_KEY={key}\n",
            encoding="utf-8",
        )
    return key


@dataclass(frozen=True)
class Config:
    mode: str                 # campus = 自定义协议上游；openai = 标准 OpenAI 兼容上游
    gateway_key: str
    host: str
    port: int

    # campus 模式
    campus_base_url: str
    campus_user: str
    campus_pass: str
    campus_model: str

    # openai 模式
    upstream_base_url: str
    upstream_api_key: str
    upstream_model: str

    # ssejson 模式：type/content 风格 SSE 上游（地址、token 由你填）
    ssejson_base_url: str
    ssejson_token: str
    ssejson_model: str
    ssejson_tools: str

    # 是否让 httpx 读环境里的 HTTP_PROXY / HTTPS_PROXY。默认关：
    # 上游地址是明确配置的，不该被环境里游荡的代理变量改写行为。
    upstream_trust_env: bool = False

    mock_port: int = 8100

    @property
    def adapter_kind(self) -> str:
        return self.mode


def load() -> Config:
    env = _parse_env(ENV_FILE)

    def pick(name: str, default: str = "") -> str:
        return os.environ.get(name) or env.get(name, default)

    return Config(
        mode=pick("MODE", "campus").lower(),
        gateway_key=_ensure_gateway_key(env),
        host=pick("HOST", "127.0.0.1"),
        port=int(pick("PORT", "8200")),
        campus_base_url=pick("CAMPUS_BASE_URL", "http://127.0.0.1:8100"),
        campus_user=pick("CAMPUS_USER", "demo"),
        campus_pass=pick("CAMPUS_PASS", "demo"),
        campus_model=pick("CAMPUS_MODEL", "campus-agent-v1"),
        upstream_base_url=pick("UPSTREAM_BASE_URL", ""),
        upstream_api_key=pick("UPSTREAM_API_KEY", ""),
        upstream_model=pick("UPSTREAM_MODEL", ""),
        ssejson_base_url=pick("SSEJSON_BASE_URL", "http://127.0.0.1:8100/api/v1/treehole/chat"),
        ssejson_token=pick("SSEJSON_TOKEN", "mock-token"),
        ssejson_model=pick("SSEJSON_MODEL", "ssejson-agent-v1"),
        ssejson_tools=pick("SSEJSON_TOOLS", "emulate").lower(),
        upstream_trust_env=pick("UPSTREAM_TRUST_ENV", "0").lower() in ("1", "true", "yes"),
        mock_port=int(pick("MOCK_PORT", "8100")),
    )


CFG = load()
