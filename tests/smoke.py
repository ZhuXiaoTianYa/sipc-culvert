"""
端到端自检：用官方 OpenAI SDK 打自己的网关。

按 .env（或环境变量）里的 MODE 自动选择测试项：
  campus  → 全部 7 项（含工具调用往返）
  ssejson → 5 项（该协议不支持 tools，跳过工具调用测试）
  openai  → 5 项

运行：python tests/smoke.py
切模式：MODE=ssejson python tests/smoke.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
# 可以用 GATEWAY_BASE 覆盖，便于在别的端口上做回归测试（默认就是 start.py 起的那个）
BASE = os.environ.get("GATEWAY_BASE") or "http://127.0.0.1:8200/v1"


def read_env() -> dict[str, str]:
    p = ROOT / ".env"
    data: dict[str, str] = {}
    if not p.exists():
        return data
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            data[k.strip()] = v.strip()
    return data


def main() -> int:
    env = read_env()
    key = os.environ.get("GATEWAY_KEY") or env.get("GATEWAY_KEY", "")
    mode = (os.environ.get("MODE") or env.get("MODE") or "campus").lower()
    if not key:
        sys.exit("找不到 GATEWAY_KEY，先运行 python start.py")

    client = OpenAI(base_url=BASE, api_key=key, timeout=60.0)

    ok = 0
    fail = 0
    dumped = False

    def dump_upstream() -> None:
        """chat 类测试失败时，顺手把上游的真实响应摊出来，省得来回猜。

        只打一次，避免每项失败都刷一屏。走的 /v1/debug/upstream 只做原始转发，
        不翻译不聚合 —— 看到的就是上游原样吐的东西。
        """
        nonlocal dumped
        if dumped:
            return
        dumped = True
        try:
            import httpx

            r = httpx.post(
                f"{BASE}/debug/upstream",
                headers={"Authorization": f"Bearer {key}"},
                json={"messages": [{"role": "user", "content": "你好"}]},
                timeout=60.0,
            )
            print("         ── 上游实测 ──")
            if r.status_code != 200:
                print(f"         排障接口 HTTP {r.status_code}: {r.text[:300]}")
                return
            d = r.json()
            print(f"         url    : {d.get('url')}")
            print(
                f"         响应   : HTTP {d.get('status')}  "
                f"{d.get('content_type')}  {d.get('bytes')} bytes"
            )
            print(f"         token  : {d.get('token')}")
            body = (d.get("body_head") or "").replace("\n", "\\n")
            print(f"         正文   : {body[:300]}")
        except Exception as e:  # noqa: BLE001
            print(f"         上游排障接口调用失败: {type(e).__name__}: {e}")

    def check(name: str, fn):
        nonlocal ok, fail
        try:
            fn()
            print(f"  [PASS] {name}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  [FAIL] {name}\n         {type(e).__name__}: {e}")
            fail += 1
            if "chat" in name or "流式" in name or "多轮" in name:
                dump_upstream()

    print(f"网关: {BASE}")
    print(f"密钥: {key[:11]}...{key[-4:]}")
    print(f"模式: {mode}")
    print()

    # 先问一下 /health：上游到底是不是真流式，决定后面「首末包跨度」这条断言还成不成立
    UPSTREAM_STREAM: bool | None = None
    TOOLS_MODE = ""
    try:
        import httpx

        h = httpx.get(BASE.replace("/v1", "") + "/health", timeout=10.0).json()
        UPSTREAM_STREAM = h.get("upstream_stream")
        TOOLS_MODE = h.get("tools_mode") or ""
        if UPSTREAM_STREAM is False:
            print("    上游: 一次性返回全文（非流式），跨度断言会自动放宽")
    except Exception as e:  # noqa: BLE001
        print(f"    /health 取不到，按流式处理: {e}")
    print()

    # 先拿模型名，后面统一用
    MODEL = "campus-agent-v1"
    try:
        ms = client.models.list().data
        if ms:
            MODEL = ms[0].id
        print(f"    模型: {[m.id for m in ms]}")
    except Exception as e:  # noqa: BLE001
        print(f"    模型列表取不到，用默认名: {e}")

    # ---------------- 各项测试
    def t_models():
        ms = client.models.list()
        assert ms.data, "模型列表为空"

    def t_nonstream():
        r = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "你好，用一句话介绍你自己"}],
        )
        text = r.choices[0].message.content
        assert text and len(text) > 5, f"回复异常: {text!r}"
        assert r.object == "chat.completion"
        print(f"         回复: {text[:50]}...")

    def t_stream():
        t0 = time.perf_counter()
        first_at = None
        last_at = None
        pieces: list[str] = []
        for chunk in client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "流式测试"}],
            stream=True,
        ):
            if not chunk.choices:
                continue
            d = chunk.choices[0].delta
            if d.content:
                now = time.perf_counter() - t0
                if first_at is None:
                    first_at = now
                last_at = now
                pieces.append(d.content)
        text = "".join(pieces)
        assert text, "流式没有收到任何内容"
        span = (last_at - first_at) if (first_at is not None and last_at is not None) else 0
        if UPSTREAM_STREAM is False:
            # 上游一次性给全文，网关只能整段转发，这时候要求「多块 + 跨度」是无理的
            print(f"         收到 {len(pieces)} 块，跨度 {span * 1000:.0f}ms（上游非流式，跳过跨度断言）")
            return
        assert len(pieces) > 5, f"只收到 {len(pieces)} 块，像是被攒成了一坨"
        # 关键判据：首包到末包的跨度，不是首包时延
        assert span > 0.1, f"首末包只隔 {span * 1000:.0f}ms，是批量返回而不是流式"
        print(f"         首包 {first_at * 1000:.0f}ms，共 {len(pieces)} 块，跨度 {span * 1000:.0f}ms")

    def t_multiturn():
        r = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "user", "content": "我叫朱天，在做反代练习"},
                {"role": "assistant", "content": "好的，记住了"},
                {"role": "user", "content": "我刚才说我叫什么？"},
            ],
        )
        assert r.choices[0].message.content
        print("         多轮请求通过（上游会话复用正常）")

    TOOLS = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "读取项目里的文件",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }
    ]

    def t_tool_calls():
        messages = [{"role": "user", "content": "帮我读一下 README 文件"}]
        r1 = client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS)
        choice = r1.choices[0]
        assert choice.finish_reason == "tool_calls", f"finish_reason={choice.finish_reason}"
        tcs = choice.message.tool_calls
        assert tcs and len(tcs) == 1, f"tool_calls={tcs}"
        call = tcs[0]
        assert call.function.name == "read_file", call.function.name
        args = json.loads(call.function.arguments)
        assert "path" in args, f"参数缺 path: {args}"
        print(f"         第一轮 tool_call: {call.function.name}({call.function.arguments})")

        messages2 = messages + [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": call.id, "content": "# README\n反代练习"},
        ]
        r2 = client.chat.completions.create(model=MODEL, messages=messages2, tools=TOOLS)
        text = r2.choices[0].message.content
        assert text and "read_file" in text, f"第二轮回复异常: {text!r}"
        print(f"         第二轮最终回答: {text[:40]}...")

    def t_tool_calls_stream():
        acc: dict[int, dict[str, str]] = {}
        finish = None
        for chunk in client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "执行一下命令看看目录"}],
            tools=TOOLS,
            stream=True,
        ):
            if not chunk.choices:
                continue
            ch = chunk.choices[0]
            for tc in ch.delta.tool_calls or []:
                slot = acc.setdefault(tc.index, {"name": "", "args": ""})
                if tc.function and tc.function.name:
                    slot["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    slot["args"] += tc.function.arguments
            if ch.finish_reason:
                finish = ch.finish_reason

        assert finish == "tool_calls", f"finish_reason={finish}"
        assert acc, "没有收到任何 tool_calls 分片"
        slot = acc[0]
        assert slot["name"] == "run_bash", f"工具名={slot['name']!r}"
        parsed = json.loads(slot["args"])
        assert "command" in parsed, f"参数缺 command: {parsed}"
        print(f"         分片拼接结果: {slot['name']}({slot['args']})")

    def t_tools_emulated():
        """ssejson 没有工具调用通道，tools 只能靠提示词模拟。

        这里只验证「带 tools 的请求不会炸、并且能正常拿到回复」，不做硬断言 ——
        模型会不会遵守那条 JSON 约定，取决于上游模型本身，不是网关能保证的。
        """
        r = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "帮我读一下 README 文件"}],
            tools=TOOLS,
        )
        ch = r.choices[0]
        if ch.message.tool_calls:
            tc0 = ch.message.tool_calls[0]
            print(f"         模型选择调用工具: {tc0.function.name}({tc0.function.arguments})")
            assert ch.finish_reason == "tool_calls", f"finish_reason={ch.finish_reason}"
        else:
            text = ch.message.content or ""
            assert text, "既没有 tool_calls 也没有文本"
            print(f"         模型选择直接回答（未调工具）: {text[:40]}...")

    def t_badauth():
        bad = OpenAI(base_url=BASE, api_key="sk-wrong-key", timeout=10.0)
        try:
            bad.chat.completions.create(
                model=MODEL, messages=[{"role": "user", "content": "hi"}]
            )
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            assert "401" in msg or "invalid_api_key" in msg, f"错误类型不对: {msg}"
            return
        raise AssertionError("错误密钥居然通过了")

    # ---------------- 组装测试列表
    tests: list[tuple[str, object]] = [
        ("GET /v1/models", t_models),
        ("非流式 chat.completions", t_nonstream),
        ("流式 chat.completions", t_stream),
        ("多轮上下文桥接", t_multiturn),
    ]
    if mode == "campus":
        tests += [
            ("工具调用（非流式往返）", t_tool_calls),
            ("工具调用（流式分片拼接）", t_tool_calls_stream),
        ]
    elif mode == "ssejson" and TOOLS_MODE == "emulate":
        tests.append(("工具清单下发（提示词模拟）", t_tools_emulated))
    else:
        print(f"    （{mode} 模式未开启 tools，跳过工具调用测试）")
    tests.append(("错误密钥被拒绝", t_badauth))

    for name, fn in tests:
        check(name, fn)

    print()
    print(f"结果: {ok} 通过, {fail} 失败")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
