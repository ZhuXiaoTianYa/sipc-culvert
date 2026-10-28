"""
模拟"校内 Agent 后端" —— 纯本地练手用。

这个服务故意做得"不标准"，让你写适配层时有真实的活干：
  1. 鉴权用自定义 header `X-Session`，不是标准 `Authorization: Bearer`
  2. 有状态：必须先开一个会话拿 conv_id，再带着它提问
  3. 流式响应用自定义 SSE 事件名（meta / delta / usage / done），
     字段叫 `t` 而不是 OpenAI 的 `content`
  4. 结尾没有 `[DONE]` 标记
  5. token 会过期，还带一个简单的每分钟限流

它是你自己的代码，可以随时改、加日志、打断点。
这正是它比对着黑盒猜强的地方。

启动：python -m uvicorn mock_upstream.app:app --port 8100
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections import defaultdict, deque
from typing import Any, AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

app = FastAPI(title="Campus Agent Mock Upstream", version="1.0.0")

# ---------------------------------------------------------------- 内存态存储
# 真实项目这里是 Redis / 数据库，练手用内存就够了
SESSIONS: dict[str, dict[str, Any]] = {}
CONVS: dict[str, dict[str, Any]] = {}
RATE: dict[str, deque[float]] = defaultdict(deque)

SESSION_TTL = 3600          # 会话 1 小时过期
RATE_LIMIT_PER_MIN = 30     # 每分钟最多 30 次提问

DEMO_USER = "demo"
DEMO_PASS = "demo"


# ---------------------------------------------------------------- 工具函数
def _new_session(user: str) -> dict[str, Any]:
    token = "sess_" + secrets.token_urlsafe(18)
    rec = {
        "user": user,
        "created_at": time.time(),
        "expires_at": time.time() + SESSION_TTL,
    }
    SESSIONS[token] = rec
    return {"session": token, **rec}


def _require_session(x_session: str | None) -> dict[str, Any]:
    """校验会话：缺 token / token 无效 / 已过期，三种情况分别报错。"""
    if not x_session:
        raise HTTPException(401, "missing X-Session header")
    rec = SESSIONS.get(x_session)
    if not rec:
        raise HTTPException(401, "invalid session")
    if time.time() > rec["expires_at"]:
        SESSIONS.pop(x_session, None)
        raise HTTPException(401, "session expired")
    return rec


def _check_rate(session: str) -> None:
    """滑动窗口限流，超了返回 429 —— 反代层要能把这种错误转成 OpenAI 的错误体。"""
    now = time.time()
    q = RATE[session]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT_PER_MIN:
        raise HTTPException(429, "rate limit exceeded")
    q.append(now)


def _fake_reply(prompt: str) -> str:
    """
    假的模型回复。不联网、不花钱，但足够验证整条链路。
    换成真实上游时，这里替换成你的 provider 调用即可。
    """
    head = f"（模拟上游回答）你问的是「{prompt[:40]}」。"
    body = (
        "这条链路是：OpenAI SDK -> 反代网关 -> 本地模拟后端 -> 假模型。 "
        "SSE 被逐块转发，客户端能看到逐字输出，说明流式适配是对的。"
    )
    return head + body


# ---------------------------------------------------------------- 工具调用
# 模拟上游「会用的工具」。真实上游里，这一步是模型自己决定调什么。
TOOLS_SPEC: dict[str, dict[str, Any]] = {
    "read_file": {
        "trigger": ("文件", "读", "read", "file"),
        "args": {"path": "README.md"},
    },
    "run_bash": {
        "trigger": ("命令", "执行", "运行", "bash", "shell"),
        "args": {"command": "ls -la"},
    },
}


def _pick_tool(query: str) -> tuple[str, dict[str, Any]] | None:
    """非常朴素的「模型决策」：命不中关键词就当普通对话。"""
    low = query.lower()
    for name, spec in TOOLS_SPEC.items():
        if any(k in low for k in spec["trigger"]):
            return name, spec["args"]
    return None


def _fake_final_reply(query: str, tool_results: list[dict[str, Any]]) -> str:
    """工具执行完回来后的最终回答。"""
    names = ", ".join(str(r.get("name", "?")) for r in tool_results)
    outputs = "；".join(str(r.get("content", ""))[:60] for r in tool_results)
    return (
        f"（模拟上游回答）工具 {names} 的执行结果：{outputs}。"
        "基于这个结果我已经可以继续处理你的问题了，这说明 tool_calls 往返链路是通的。"
    )


# ---------------------------------------------------------------- 路由
@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "sessions": len(SESSIONS), "convs": len(CONVS)}


@app.get("/api/sleep-probe")
async def sleep_probe() -> dict[str, Any]:
    """诊断用：验证本事件循环里 asyncio.sleep 是否真的会等待。"""
    t = time.perf_counter()
    for _ in range(10):
        await asyncio.sleep(0.05)
    return {"elapsed": round(time.perf_counter() - t, 3), "expected": 0.5}


@app.post("/api/auth/login")
async def login(payload: dict[str, Any]) -> dict[str, Any]:
    """用账号密码换 session token，模拟浏览器登录后拿到 cookie 的那一步。"""
    user = payload.get("user", "")
    pwd = payload.get("pass", "")
    if user != DEMO_USER or pwd != DEMO_PASS:
        raise HTTPException(403, "bad credentials")
    info = _new_session(user)
    return {
        "session": info["session"],
        "expires_in": SESSION_TTL,
        "user": user,
    }


@app.post("/api/conv/new")
async def new_conv(x_session: str | None = Header(default=None)) -> dict[str, Any]:
    """开一个新会话。上游是有状态的，这一步反代层要替客户端兜住。"""
    rec = _require_session(x_session)
    conv_id = "conv_" + secrets.token_hex(8)
    CONVS[conv_id] = {
        "user": rec["user"],
        "session": x_session,
        "created_at": time.time(),
        "messages": [],
    }
    return {"conv_id": conv_id}


@app.post("/api/agent/stream")
async def agent_stream(
    payload: dict[str, Any],
    request: Request,
    x_session: str | None = Header(default=None),
) -> StreamingResponse:
    """
    核心接口：带状态提问 + 自定义 SSE 流式返回。

    请求体：
      {"conv_id": "conv_xxx", "query": "你好",
       "tools": [...], "tool_results": [...]}

    响应事件：
      event: meta       data: {"conv_id":...}
      event: delta      data: {"t": "字"}
      event: tool_call  data: {"index":0,"id":"call_x","name":"read_file","arguments":""}
                        data: {"index":0,"arguments":"{\\"path\\":"}   ← 增量字符串
      event: usage      data: {...}
      event: done       data: {"finish_reason":"stop"|"tool_calls"}
    """
    rec = _require_session(x_session)
    _check_rate(x_session)

    conv_id = payload.get("conv_id")
    query = (payload.get("query") or "").strip()
    if not conv_id or conv_id not in CONVS:
        raise HTTPException(400, "unknown conv_id")
    if not query:
        raise HTTPException(400, "empty query")

    tools = payload.get("tools") or []
    tool_results = payload.get("tool_results") or []

    CONVS[conv_id]["messages"].append({"role": "user", "content": query})

    # 这一轮是「调工具」还是「出正文」
    plan: tuple[str, dict[str, Any]] | None = None
    if tool_results:
        reply = _fake_final_reply(query, tool_results)   # 工具结果已回来，出最终回答
    else:
        plan = _pick_tool(query) if tools else None       # 只有客户端提供了 tools 才可能调
        reply = "" if plan else _fake_reply(query)

    call_id = "call_" + secrets.token_hex(6)

    async def event_stream() -> AsyncIterator[str]:
        """手写 SSE，注意格式：`event: xxx\\n` + `data: ...\\n` + 空行分隔。"""

        def send(event: str, data: Any) -> str:
            return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        yield send("meta", {"conv_id": conv_id, "model": "campus-agent-v1", "user": rec["user"]})
        await asyncio.sleep(0.02)

        # ---- 分支 A：决定调用工具
        if plan is not None:
            name, args = plan
            # 第一片：带 id 和 name，arguments 为空串
            yield send("tool_call", {"index": 0, "id": call_id, "name": name, "arguments": ""})
            await asyncio.sleep(0.02)

            # 参数按块增量下发，模拟真实模型的逐块生成
            args_str = json.dumps(args, ensure_ascii=False)
            for i in range(0, len(args_str), 6):
                yield send("tool_call", {"index": 0, "arguments": args_str[i : i + 6]})
                await asyncio.sleep(0.01)

            yield send("usage", {"prompt_tokens": max(1, len(query) // 2), "completion_tokens": 0})
            yield send("done", {"finish_reason": "tool_calls"})
            return

        # ---- 分支 B：正常文本
        loop = asyncio.get_running_loop()
        for ch in reply:
            if await request.is_disconnected():
                return
            yield send("delta", {"t": ch})
            # 这里必须用线程池 sleep，不能用 asyncio.sleep / anyio.sleep。
            # 在本环境（Windows + uvicorn + Starlette StreamingResponse）里，
            # 异步生成器内的 asyncio.sleep 会立即返回而不真正等待（实测 93 次共 1ms），
            # 结果流式退化成一次性输出。改用线程池 sleep 才能拿到真实节奏。
            # 详见 README「一个 Windows 上的流式坑」。
            await loop.run_in_executor(None, time.sleep, 0.02)

        yield send("usage", {
            "prompt_tokens": max(1, len(query) // 2),
            "completion_tokens": max(1, len(reply) // 2),
        })
        # 结束（注意：没有 [DONE]，客户端不能指望它）
        yield send("done", {"finish_reason": "stop"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 关掉中间层缓冲，否则流式会被攒成一坨
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/conv/{conv_id}/history")
async def history(conv_id: str, x_session: str | None = Header(default=None)) -> dict[str, Any]:
    """取会话历史，用于验证反代层有没有正确维护上下文。"""
    _require_session(x_session)
    conv = CONVS.get(conv_id)
    if not conv:
        raise HTTPException(404, "no such conv")
    return {"conv_id": conv_id, "messages": conv["messages"]}


# ================================================================ type/content 风格 SSE
# 这个端点复刻「data 里带 type 字段」的那种上游。
# 关键特征：**没有 event: 行，只有 data:**，事件类型藏在 JSON 的 type 字段里。
# 真实上游的鉴权就是一个 Bearer token，地址和 token 在网关的 .env 里填。
MOCK_TOKEN = "mock-token"


def _treehole_reply(text: str) -> str:
    return (
        f"（模拟上游回答）我听到你说「{text[:30]}」。"
        "这条链路是：OpenAI SDK -> 反代网关 -> type/content 格式上游 -> 假模型。"
        "注意响应里没有 event 行，只有 data，事件类型藏在 type 字段里——"
        "这正是适配层要翻译的地方。"
    )


@app.post("/api/v1/treehole/chat")
async def treehole_chat(
    payload: dict[str, Any],
    request: Request,
    authorization: str | None = Header(default=None),
) -> StreamingResponse:
    """
    请求体：{"messages": [{"role":"user","content":"..."}], "conversation_id": "可选"}
    响应：  data: {"type":"content","content":"字"}
            data: {"type":"done","reply":"全文","conversation_id":"...","usage":{...}}
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    if authorization[7:].strip() != MOCK_TOKEN:
        raise HTTPException(403, "invalid token")

    msgs = payload.get("messages") or []
    text = ""
    for m in reversed(msgs):
        if m.get("role") == "user":
            text = m.get("content") or ""
            break
    if not text:
        raise HTTPException(400, "empty user message")

    conv_id = payload.get("conversation_id") or (
        "t" + str(int(time.time() * 1000)) + secrets.token_hex(3)
    )
    reply = _treehole_reply(text)
    loop = asyncio.get_running_loop()

    async def event_stream() -> AsyncIterator[str]:
        for ch in reply:
            if await request.is_disconnected():
                return
            yield "data: " + json.dumps(
                {"type": "content", "content": ch}, ensure_ascii=False
            ) + "\n\n"
            # 同样要绕开异步生成器里失效的 asyncio.sleep（见 README 第 4 个坑）
            await loop.run_in_executor(None, time.sleep, 0.02)

        yield "data: " + json.dumps(
            {
                "type": "done",
                "reply": reply,
                "conversation_id": conv_id,
                "usage": {
                    "prompt_tokens": max(1, len(text) // 2),
                    "completion_tokens": max(1, len(reply) // 2),
                    "total_tokens": max(1, len(text) // 2) + max(1, len(reply) // 2),
                },
            },
            ensure_ascii=False,
        ) + "\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
