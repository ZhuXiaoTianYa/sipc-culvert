"""
OpenAI 兼容网关。

对外只暴露标准 OpenAI 协议：
  POST /v1/chat/completions
  GET  /v1/models
  GET  /health

对内在 MODE 指定的两种上游之间切换。任何支持自定义 base_url 的客户端
（OpenAI SDK、LangChain、Cherry Studio、Continue、各种 CLI）都能直连。
"""
from __future__ import annotations

import secrets
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .adapters import CampusAdapter, OpenAICompatAdapter, SSEJSONAdapter, UpstreamError
from .config import CFG
from .sse_utils import dumps, encode_done, encode_sse

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class APIError(Exception):
    """按 OpenAI 错误格式返回。客户端都认得这个结构。"""

    def __init__(self, status: int, message: str, code: str, type_: str = "invalid_request_error"):
        self.status = status
        self.message = message
        self.code = code
        self.type = type_


@asynccontextmanager
async def lifespan(app: FastAPI):
    if CFG.mode == "openai":
        if not CFG.upstream_base_url:
            raise RuntimeError("MODE=openai 时必须配置 UPSTREAM_BASE_URL")
        app.state.adapter = OpenAICompatAdapter(CFG.upstream_base_url, CFG.upstream_api_key)
    elif CFG.mode == "ssejson":
        app.state.adapter = SSEJSONAdapter(
            CFG.ssejson_base_url,
            CFG.ssejson_token,
            CFG.ssejson_model,
            tools_mode=CFG.ssejson_tools,
        )
    else:
        app.state.adapter = CampusAdapter(
            CFG.campus_base_url,
            CFG.campus_user,
            CFG.campus_pass,
            model_name=CFG.campus_model,
        )
    yield
    await app.state.adapter.aclose()


app = FastAPI(title="OpenAI-Compatible Gateway", version="1.0.0", lifespan=lifespan)


@app.exception_handler(APIError)
async def _api_error_handler(_: Request, exc: APIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"message": exc.message, "type": exc.type, "code": exc.code}},
    )


@app.exception_handler(UpstreamError)
async def _upstream_error_handler(_: Request, exc: UpstreamError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={
            "error": {
                "message": exc.message,
                "type": "upstream_error",
                "code": exc.code or "upstream_error",
            }
        },
    )


def require_key(authorization: str | None = Header(default=None)) -> None:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise APIError(401, "缺少 Authorization: Bearer <网关密钥>", "invalid_api_key")
    token = authorization.split(" ", 1)[1].strip()
    if not secrets.compare_digest(token, CFG.gateway_key):
        raise APIError(401, "网关密钥不正确", "invalid_api_key")


@app.get("/health")
async def health() -> dict[str, Any]:
    adapter = getattr(app.state, "adapter", None)
    return {
        "ok": True,
        "mode": CFG.mode,
        "adapter": type(adapter).__name__ if adapter else None,
        # 上游是不是真的在流式返回。false 说明它一次性给全文，网关只能整段转发，
        # 自检脚本据此判断「首末包跨度」这条断言还适不适用。
        "upstream_stream": getattr(adapter, "upstream_streams", None),
        "tools_mode": CFG.ssejson_tools if CFG.mode == "ssejson" else None,
        "time": int(time.time()),
    }


@app.get("/v1/models")
async def list_models(_: None = Depends(require_key)) -> dict[str, Any]:
    adapter = app.state.adapter
    if isinstance(adapter, OpenAICompatAdapter):
        try:
            data = await adapter.list_models()
        except UpstreamError:
            data = []
    else:
        name = getattr(adapter, "model_name", CFG.campus_model)
        data = [{"id": name, "object": "model", "created": int(time.time()), "owned_by": "lab"}]
    if not data:
        data = [{"id": CFG.campus_model, "object": "model", "owned_by": "lab"}]
    return {"object": "list", "data": data}


@app.post("/v1/debug/upstream")
async def debug_upstream(
    body: dict[str, Any] | None = None,
    _: None = Depends(require_key),
) -> dict[str, Any]:
    """排障用：拿当前 .env 的配置真打一次上游，原样返回它到底回了什么。

    不翻译、不聚合、不抛业务错 —— 只把状态码 / 响应头 / 响应体前 4KB 摊开。
    网关说「没内容」的时候，先打这个：是上游根本没给，还是我们没认出来。
    """
    if CFG.mode != "ssejson":
        raise APIError(400, "该接口只在 MODE=ssejson 下可用", "unsupported_mode")

    msgs = (body or {}).get("messages") or [{"role": "user", "content": "你好"}]
    headers = {
        "Authorization": f"Bearer {CFG.ssejson_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
    }
    async with httpx.AsyncClient(timeout=45.0) as cli:
        try:
            r = await cli.post(CFG.ssejson_base_url, json={"messages": msgs}, headers=headers)
        except httpx.HTTPError as e:
            raise UpstreamError(
                502, f"连不上上游: {type(e).__name__}: {e}", "upstream_unreachable"
            ) from e

    tok = CFG.ssejson_token
    return {
        "url": CFG.ssejson_base_url,
        "status": r.status_code,
        "content_type": r.headers.get("content-type"),
        "bytes": len(r.content),
        "token": (tok[:12] + "..." + tok[-6:]) if len(tok) > 24 else "<过短或未配置>",
        "body_head": r.text[:4000],
    }


@app.post("/v1/chat/completions")
async def chat_completions(
    body: dict[str, Any],
    request: Request,
    _: None = Depends(require_key),
):
    messages = body.get("messages") or []
    if not isinstance(messages, list) or not messages:
        raise APIError(400, "messages 不能为空", "invalid_request")

    stream = bool(body.get("stream"))
    client_model = body.get("model")
    tools = body.get("tools") or None
    adapter = app.state.adapter

    async def primed(gen):
        """先手动取第一个 chunk，再交剩下的一并流出去。

        为什么非要这样：StreamingResponse 一返回，200 状态码就已经发出去了，
        之后生成器里再怎么抛 UpstreamError 也改不回来，客户端只会干等到超时。
        先把首包拽出来，上游要是根本没内容，错误就发生在「响应还没开始」之前，
        能正常变成一个 502 JSON，而不是一个卡死的连接。
        """
        try:
            first = await gen.__anext__()
        except StopAsyncIteration:
            raise UpstreamError(
                502, "上游没有返回任何内容", "empty_upstream_response"
            ) from None

        async def rest():
            yield first
            async for item in gen:
                yield item

        return rest()

    # ---------------- 标准 OpenAI 兼容上游：转发即可
    if isinstance(adapter, OpenAICompatAdapter):
        payload = dict(body)
        if CFG.upstream_model:
            payload["model"] = CFG.upstream_model
        if stream:
            payload["stream"] = True
            gen = await primed(adapter.stream_raw(payload))
            return StreamingResponse(gen, media_type="text/event-stream", headers=SSE_HEADERS)
        return JSONResponse(await adapter.complete(payload))

    # ---------------- 自定义协议上游：翻译
    if stream:
        inner = await primed(adapter.stream_chunks(messages, client_model, tools))

        async def event_gen():
            async for chunk in inner:
                yield encode_sse(dumps(chunk))
            yield encode_done()

        return StreamingResponse(
            event_gen(), media_type="text/event-stream", headers=SSE_HEADERS
        )

    return JSONResponse(await adapter.complete(messages, client_model, tools))


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    key_masked = CFG.gateway_key[:11] + "..." + CFG.gateway_key[-4:]
    # 首页展示的模型名必须跟着 MODE 走，否则你照着它填客户端就会填错
    model_show = {
        "campus": CFG.campus_model,
        "ssejson": CFG.ssejson_model,
        "openai": CFG.upstream_model or "透传客户端指定值",
    }.get(CFG.mode, CFG.campus_model)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>OpenAI 兼容网关</title>
<style>
 body{{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
      max-width:720px;margin:48px auto;padding:0 24px;color:#1f2328;line-height:1.7}}
 h1{{font-size:22px;margin-bottom:4px}}
 .sub{{color:#656d76;font-size:13px;margin-bottom:28px}}
 .box{{background:#f6f8fa;border:1px solid #d0d7de;border-radius:8px;padding:16px 18px;margin:16px 0}}
 code{{background:#eff1f3;padding:2px 6px;border-radius:4px;font-size:13px}}
 pre{{background:#0d1117;color:#e6edf3;padding:14px 16px;border-radius:8px;overflow-x:auto;font-size:12.5px;line-height:1.6}}
 .k{{color:#8b949e}} .s{{color:#a5d6ff}}
 .row{{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid #eaeef2;font-size:14px}}
 .row:last-child{{border-bottom:none}}
 .lbl{{color:#656d76}} .val{{font-family:ui-monospace,Consolas,monospace}}
 .tag{{display:inline-block;background:#ddf4ff;color:#0969da;border-radius:20px;
      padding:2px 10px;font-size:12px;margin-left:8px}}
</style></head><body>
<h1>OpenAI 兼容网关 <span class="tag">运行中</span></h1>
<div class="sub">把你写的适配层包成标准 OpenAI 接口，任何客户端都能接。</div>

<div class="box">
  <div class="row"><span class="lbl">Base URL</span>
    <span class="val">http://{CFG.host}:{CFG.port}/v1</span></div>
  <div class="row"><span class="lbl">API Key</span>
    <span class="val">{key_masked}　（完整值见项目根目录 .env）</span></div>
  <div class="row"><span class="lbl">当前上游模式</span>
    <span class="val">{CFG.mode}</span></div>
  <div class="row"><span class="lbl">模型名</span>
    <span class="val">{model_show}</span></div>
</div>

<div class="box">
  <b>curl 验证</b>
<pre>curl http://{CFG.host}:{CFG.port}/v1/chat/completions \\
  -H <span class="s">"Authorization: Bearer $GATEWAY_KEY"</span> \\
  -H <span class="s">"Content-Type: application/json"</span> \\
  -d <span class="s">'{{"model":"{model_show}","stream":true,
       "messages":[{{"role":"user","content":"你好"}}]}}'</span></pre>
</div>

<div class="box">
  <b>Python SDK</b>
<pre><span class="k">from</span> openai <span class="k">import</span> OpenAI

client = OpenAI(base_url=<span class="s">"http://{CFG.host}:{CFG.port}/v1"</span>,
                api_key=<span class="s">"&lt;见 .env&gt;"</span>)
resp = client.chat.completions.create(
    model=<span class="s">"{model_show}"</span>,
    messages=[{{"role": <span class="s">"user"</span>, "content": <span class="s">"你好"</span>}}],
    stream=<span class="k">True</span>,
)
<span class="k">for</span> c <span class="k">in</span> resp:
    <span class="k">if</span> c.choices <span class="k">and</span> c.choices[0].delta.content:
        print(c.choices[0].delta.content, end=<span class="s">""</span>)</pre>
</div>
</body></html>"""
