"""
上游适配器：把不同形态的上游统一成 OpenAI 语义。

两个实现，对应真实世界的两类情况：

  CampusAdapter        —— 「自定义协议」上游：有状态会话 + 自定义 SSE 事件。
                          这是你遇到校内/私有 agent 时的形态。
                          网关要替客户端兜住会话、上下文、格式差异。

  OpenAICompatAdapter  —— 「标准协议」上游：纯透传。
                          接 Ollama / vLLM / 各家云厂商 API 走这条。

两者的差别正好说明「反代」的本质：
  适配层负责把非标准上游翻译成标准协议；
  上游本来就是标准的，就只需要转发和换鉴权，不需要翻译。
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, AsyncIterator

import httpx

from .config import CFG
from .sse_utils import SSEDecoder, SSEEvent, openai_chunk

# 是否让 httpx 去读环境里的 HTTP_PROXY / HTTPS_PROXY / NO_PROXY。
#
# 默认关掉。原因：网关转发到哪个上游是 .env 里明确写死的，不该被环境里一个
# 游荡的代理变量悄悄改写 —— 那样同一份配置在两台机器上行为不同，出问题极难复现。
# （实测踩过：环境里带着 HTTP_PROXY 时，httpx 会把请求行写成
#   POST http://host/path 这种代理式绝对 URI，而不是 POST /path。）
# 上游确实要走代理的话，在 .env 里设 UPSTREAM_TRUST_ENV=1，或者直接用代理地址当前缀。
TRUST_ENV = CFG.upstream_trust_env


class UpstreamError(Exception):
    """上游返回了错误，会被 app.py 转成 OpenAI 风格的错误体。"""

    def __init__(self, status: int, message: str, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def messages_to_query(
    messages: list[dict[str, Any]],
) -> tuple[str, str, list[dict[str, Any]]]:
    """
    把 OpenAI 的 messages 数组压成「query + 对话指纹 + 工具结果」。

    返回 (conv_key, query, tool_results)：
      conv_key     —— 判断这轮属于哪个上游会话（system + 首条 user 决定，全程稳定）
      query        —— 真正发给上游的文本
      tool_results —— 客户端回传的工具执行结果，非空表示这是工具调用后的第二轮

    为什么必须拆开：OpenAI 协议无状态，每次请求重发全量历史；
    而上游 agent 往往有状态，会话里自己记着历史。
    每轮都把全量历史重新塞一遍，上游会看到重复内容，答得乱七八糟。
    这是「无状态协议 ↔ 有状态上游」桥接里最容易踩的坑。
    """
    system_parts = [m.get("content", "") for m in messages if m.get("role") == "system"]
    user_msgs = [m for m in messages if m.get("role") == "user"]

    if not user_msgs:
        raise UpstreamError(400, "messages 里没有 user 内容", "invalid_request")

    first_user = user_msgs[0].get("content", "")
    conv_key = _sha1("\x00".join(system_parts) + "\x01" + str(first_user))

    last = messages[-1] if messages else {}
    tool_results: list[dict[str, Any]] = []

    # 最后一条是 role=tool：说明客户端执行完工具，把结果送回来了
    if last.get("role") == "tool":
        tcid = last.get("tool_call_id")
        name = "tool"
        for m in messages:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    if tc.get("id") == tcid:
                        name = tc.get("function", {}).get("name", "tool")
        content = last.get("content", "")
        tool_results.append({"name": name, "content": content})
        query = content or "(工具已执行)"
        return conv_key, query, tool_results

    query = last.get("content", "") or ""
    # 上游没有 system 通道，首轮把 system 设定拼进 query
    if len(user_msgs) == 1 and system_parts:
        query = "\n".join(system_parts) + "\n\n" + query

    return conv_key, query, tool_results


# ============================================================ 自定义协议上游
class CampusAdapter:
    """
    对接「自定义协议」上游（这里是本地模拟服务，形态照着私有 agent 来）。

    要做三件事：
      1. 登录换 session token，过期自动重登
      2. 按对话指纹维护 conv_id，把无状态请求映射到有状态上游会话
      3. 解析自定义 SSE（meta/delta/usage/done）转成 OpenAI chunk
    """

    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        timeout: float = 120.0,
        model_name: str = "campus-agent-v1",
    ):
        self.base_url = base_url.rstrip("/")
        self.user = user
        self.password = password
        self.model_name = model_name
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=15.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            trust_env=TRUST_ENV,
        )
        self._session: str | None = None
        self._session_deadline: float = 0.0
        self._convs: dict[str, str] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------------------------------------------------------- 会话管理
    async def _login(self) -> None:
        try:
            r = await self._client.post(
                f"{self.base_url}/api/auth/login",
                json={"user": self.user, "pass": self.password},
            )
        except httpx.HTTPError as e:
            raise UpstreamError(502, f"登录上游失败: {e}", "upstream_unreachable") from e

        if r.status_code != 200:
            raise UpstreamError(
                502, f"上游登录被拒 ({r.status_code}): {r.text[:200]}", "upstream_auth_failed"
            )

        data = r.json()
        self._session = data["session"]
        self._session_deadline = time.time() + data.get("expires_in", 3600) - 30

    async def _ensure_session(self) -> str:
        if not self._session or time.time() >= self._session_deadline:
            await self._login()
        assert self._session
        return self._session

    async def _new_conv(self, session: str) -> str:
        r = await self._client.post(
            f"{self.base_url}/api/conv/new", headers={"X-Session": session}
        )
        if r.status_code != 200:
            raise UpstreamError(502, f"新建上游会话失败: {r.text[:200]}", "upstream_error")
        return r.json()["conv_id"]

    async def _ensure_conv(self, conv_key: str, session: str) -> str:
        conv_id = self._convs.get(conv_key)
        if conv_id:
            return conv_id
        conv_id = await self._new_conv(session)
        self._convs[conv_key] = conv_id
        return conv_id

    async def _send_stream(self, url: str, body: dict, headers: dict) -> httpx.Response:
        req = self._client.build_request("POST", url, json=body, headers=headers)
        try:
            return await self._client.send(req, stream=True)
        except httpx.HTTPError as e:
            raise UpstreamError(502, f"上游请求失败: {e}", "upstream_unreachable") from e

    # ---------------------------------------------------------- 流式对话
    async def stream_chunks(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        产出 OpenAI chunk 对象（dict）。由 app 层决定编码成 SSE 还是聚合成完整响应。
        返回对象而不是字节，是为了让「流式」和「非流式」共用同一套转换逻辑。
        """
        model_name = model or self.model_name
        conv_key, query, tool_results = messages_to_query(messages)
        session = await self._ensure_session()
        conv_id = await self._ensure_conv(conv_key, session)

        def build_body() -> dict[str, Any]:
            """重试时会重新读 conv_id，所以做成闭包而不是提前算好。"""
            b: dict[str, Any] = {"conv_id": conv_id, "query": query}
            if tools:
                b["tools"] = tools
            if tool_results:
                b["tool_results"] = tool_results
            return b

        url = f"{self.base_url}/api/agent/stream"
        resp = await self._send_stream(url, build_body(), {"X-Session": session})

        # 会话可能在上游被清掉，401 就重登 + 重建会话再试一次
        if resp.status_code == 401:
            await resp.aclose()
            self._session = None
            self._convs.pop(conv_key, None)
            session = await self._ensure_session()
            conv_id = await self._ensure_conv(conv_key, session)
            resp = await self._send_stream(url, build_body(), {"X-Session": session})

        if resp.status_code != 200:
            raw = (await resp.aread()).decode("utf-8", "replace")
            await resp.aclose()
            code = {429: "rate_limit_exceeded", 401: "unauthorized", 400: "invalid_request"}.get(
                resp.status_code, "upstream_error"
            )
            raise UpstreamError(resp.status_code if resp.status_code < 500 else 502, raw[:500], code)

        chunk_id = "chatcmpl-" + _sha1(str(time.time_ns()))
        finish_reason = "stop"
        try:
            # OpenAI 客户端期待首个 delta 带 role
            yield openai_chunk(chunk_id, model_name, {"role": "assistant", "content": ""})

            dec = SSEDecoder()

            def emit(ev) -> dict[str, Any] | None:
                """上游事件 → OpenAI chunk。工具调用和普通文本在这里分流。"""
                nonlocal finish_reason

                if ev.event == "delta":
                    try:
                        piece = json.loads(ev.data).get("t", "")
                    except json.JSONDecodeError:
                        piece = ""
                    if piece:
                        return openai_chunk(chunk_id, model_name, {"content": piece})

                elif ev.event == "tool_call":
                    try:
                        d = json.loads(ev.data)
                    except json.JSONDecodeError:
                        return None
                    tc: dict[str, Any] = {"index": d.get("index", 0)}
                    if "id" in d:
                        # 首片必须把 id / type / name 一起给出去，
                        # 之后客户端才会把后续 arguments 分片挂到同一个调用上
                        tc["id"] = d["id"]
                        tc["type"] = "function"
                        tc["function"] = {
                            "name": d.get("name", ""),
                            "arguments": d.get("arguments", ""),
                        }
                    else:
                        # 后续片只带 index + arguments 增量，不能重复 name
                        tc["function"] = {"arguments": d.get("arguments", "")}
                    finish_reason = "tool_calls"
                    return openai_chunk(chunk_id, model_name, {"tool_calls": [tc]})

                elif ev.event == "done":
                    try:
                        finish_reason = json.loads(ev.data).get("finish_reason") or finish_reason
                    except json.JSONDecodeError:
                        pass

                return None

            async for text in resp.aiter_text():
                for ev in dec.feed(text):
                    c = emit(ev)
                    if c is not None:
                        yield c

            for ev in dec.close():  # 收尾，处理没以空行结尾的残留事件
                c = emit(ev)
                if c is not None:
                    yield c

            yield openai_chunk(chunk_id, model_name, {}, finish_reason=finish_reason)
        finally:
            await resp.aclose()

    async def complete(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """非流式：跑完整个流再聚合。省得在上游那边维护第二套调用逻辑。"""
        text_parts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        finish_reason = "stop"
        chunk_id = "chatcmpl-" + _sha1(str(time.time_ns()))
        model_name = model or self.model_name

        async for c in self.stream_chunks(messages, model_name, tools):
            chunk_id = c.get("id", chunk_id)
            for ch in c.get("choices", []):
                delta = ch.get("delta") or {}
                if delta.get("content"):
                    text_parts.append(delta["content"])

                # 把分片的 tool_calls 按 index 拼回完整结构
                for tc in delta.get("tool_calls") or []:
                    i = tc.get("index", 0)
                    slot = calls.setdefault(
                        i,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]

                if ch.get("finish_reason"):
                    finish_reason = ch["finish_reason"]

        text = "".join(text_parts)
        message: dict[str, Any] = {
            "role": "assistant",
            # 有工具调用时 content 按规范应为 null
            "content": text if text else (None if calls else ""),
        }
        if calls:
            message["tool_calls"] = [calls[i] for i in sorted(calls)]

        return {
            "id": chunk_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                    "logprobs": None,
                }
            ],
            "usage": {
                "prompt_tokens": max(1, len(messages) * 8),
                "completion_tokens": max(1, len(text) // 2),
                "total_tokens": max(1, len(messages) * 8) + max(1, len(text) // 2),
            },
        }


# ============================================================ 标准协议上游
class OpenAICompatAdapter:
    """
    对接任何 OpenAI 兼容上游（Ollama、vLLM、DeepSeek、通义、OpenRouter…）。

    不需要翻译，只转发 + 换鉴权。关键点：流式必须逐块透传，
    一旦先 read() 整个响应体，流式就退化成一坨一次性返回了。
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=15.0), trust_env=TRUST_ENV
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def list_models(self) -> list[dict[str, Any]]:
        try:
            r = await self._client.get(f"{self.base_url}/models", headers=self._headers())
            r.raise_for_status()
            return r.json().get("data", [])
        except httpx.HTTPError as e:
            raise UpstreamError(502, f"取模型列表失败: {e}", "upstream_unreachable") from e

    async def stream_raw(self, payload: dict[str, Any]) -> AsyncIterator[bytes]:
        """字节级透传上游的 SSE 流。"""
        req = self._client.build_request(
            "POST", f"{self.base_url}/chat/completions", json=payload, headers=self._headers()
        )
        try:
            resp = await self._client.send(req, stream=True)
        except httpx.HTTPError as e:
            raise UpstreamError(502, f"上游请求失败: {e}", "upstream_unreachable") from e

        if resp.status_code != 200:
            raw = (await resp.aread()).decode("utf-8", "replace")
            await resp.aclose()
            raise UpstreamError(resp.status_code, raw[:500], "upstream_error")

        try:
            async for chunk in resp.aiter_raw():
                if chunk:
                    yield chunk
        finally:
            await resp.aclose()

    async def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            r = await self._client.post(
                f"{self.base_url}/chat/completions", json=payload, headers=self._headers()
            )
        except httpx.HTTPError as e:
            raise UpstreamError(502, f"上游请求失败: {e}", "upstream_unreachable") from e
        if r.status_code != 200:
            raise UpstreamError(r.status_code, r.text[:500], "upstream_error")
        return r.json()


# ============================================================ type/content 风格 SSE
class SSEJSONAdapter:
    """
    对接「树洞型」上游：同一条端点上可能出现两种响应形状，都要能吃下去。

    形状 A —— 流式 SSE（请求体里带 stream:true，或上游默认就流式）：

        data: {"type": "content", "content": "字"}
        data: {"type": "done", "reply": "全文", "conversation_id": "t179...",
               "usage": {"prompt_tokens": 806, "completion_tokens": 51, "total_tokens": 857}}

    形状 B —— 一次性 JSON（没带 stream，上游把整段话攒好了一次性返回）：

        {"code": 0, "message": "ok",
         "data": {"reply": "全文", "conversation_id": "t179...",
                  "usage": {"prompt_tokens": 667, "completion_tokens": 22, "total_tokens": 689}}}

    为什么会两种都有：这类接口的 stream 开关通常可选，不带就退化成 B。

    **必须两种都认**。只认 A 的后果非常隐蔽：上游明明回了 200 和一大段完整文本，
    适配层却把所有事件当垃圾丢掉，最后聚合成一个空字符串，客户端只看到
    "returned no content"，完全查不到真正原因。这个坑本项目实际踩过一次。

    另外，这类上游没有工具调用通道，所以 tools 走「提示词模拟」：
    把工具清单塞进 system prompt，要求模型在需要时输出约定的一行 JSON，
    适配层再把它还原成 OpenAI 的 tool_calls 三段式。
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        model_name: str,
        timeout: float = 120.0,
        tools_mode: str = "emulate",
    ):
        self.base_url = base_url
        self.token = token
        self.model_name = model_name
        self.tools_mode = (tools_mode or "emulate").lower()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=15.0), trust_env=TRUST_ENV
        )
        # conv_key -> 上游返回的 conversation_id，用来维持多轮上下文
        self._convs: dict[str, str] = {}

        # 下面两个是「观测到的事实」，/health 会暴露出去，方便自检脚本判断
        self.upstream_streams = True   # 上游是否真的在流式返回
        self._ask_stream = True        # 请求时是否在 body 里带 stream:true

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------ 请求构造
    def _conv_key(self, messages: list[dict[str, Any]]) -> str:
        system = "".join(
            str(m.get("content") or "") for m in messages if m.get("role") == "system"
        )
        first_user = next(
            (str(m.get("content") or "") for m in messages if m.get("role") == "user"), ""
        )
        return _sha1(system + "\x01" + first_user)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
        }

    def _fold_tool_results(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把 OpenAI 的 role=tool 消息折叠成普通文本。

        这类上游没有「工具结果」这个通道，只能当成一条 user 消息塞回去，
        否则整条消息会被上游直接忽略，模型永远看不到工具跑了什么。
        """
        names: dict[str, str] = {}
        for m in messages:
            if m.get("role") == "assistant":
                for tc in m.get("tool_calls") or []:
                    names[tc.get("id")] = (tc.get("function") or {}).get("name", "tool")

        out: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role")
            if role == "tool":
                nm = names.get(m.get("tool_call_id"), "tool")
                out.append(
                    {"role": "user", "content": f"[工具 {nm} 的执行结果]\n{m.get('content') or ''}"}
                )
            elif role == "assistant" and m.get("tool_calls") and not m.get("content"):
                out.append({"role": "assistant", "content": "[已调用工具]"})
            else:
                out.append({"role": role, "content": m.get("content") or ""})
        return out

    def _tools_prompt(self, tools: list[dict[str, Any]] | None) -> str:
        if not tools or self.tools_mode != "emulate":
            return ""
        spec: list[dict[str, Any]] = []
        for t in tools:
            fn = t.get("function") if t.get("type") in (None, "function") else None
            if not isinstance(fn, dict) or not fn.get("name"):
                continue
            spec.append(
                {
                    "name": fn.get("name"),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters") or {},
                }
            )
        if not spec:
            return ""
        return (
            "你可以调用下面这些工具。规则：\n"
            "1. 需要调用工具时，只输出一行 JSON，前后不要有任何别的文字，也不要用代码块包裹：\n"
            '   {"tool_call": {"name": "工具名", "arguments": {"参数": "值"}}}\n'
            "2. 一次只调用一个工具。\n"
            "3. 不需要调用工具时，就正常回答，不要输出这种 JSON。\n"
            "4. 看到 [工具 xxx 的执行结果] 之后，用自然语言把结论讲清楚。\n"
            "可用工具：\n" + json.dumps(spec, ensure_ascii=False)
        )

    def _build_body(
        self,
        messages: list[dict[str, Any]],
        conv_id: str | None,
        prompt: str,
    ) -> dict[str, Any]:
        msgs = [
            {"role": m["role"], "content": m.get("content") or ""}
            for m in messages
            if m.get("role") in ("system", "user", "assistant")
        ]
        if prompt:
            msgs = [{"role": "system", "content": prompt}] + msgs

        body: dict[str, Any] = {"messages": msgs}
        if conv_id:
            # 顺手把上游给的会话 id 带回去，它认就能接着聊，不认也无害。
            #
            # 注意这里**仍然发全量历史**。曾经想「有 conv_id 就只发最后一条」，
            # 但那要求上游确实认这个字段名，而字段名是猜的。一旦猜错，
            # 上下文会整体丢失，用户每轮都像在跟失忆的人说话 —— 比带着重复历史糟得多。
            # 发全量历史这条路是实测过的：上游会正确回答最后一轮的问题。
            body["conversation_id"] = conv_id
        if self._ask_stream:
            body["stream"] = True
        return body

    # ------------------------------------------------------------ 发请求
    async def _send(self, body: dict[str, Any]) -> httpx.Response:
        req = self._client.build_request(
            "POST", self.base_url, json=body, headers=self._headers()
        )
        try:
            return await self._client.send(req, stream=True)
        except httpx.HTTPError as e:
            raise UpstreamError(502, f"上游请求失败: {e}", "upstream_unreachable") from e

    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        resp = await self._send(body)

        # 有的上游不认 stream 字段，直接 4xx。去掉再试一次，并记住以后别带了。
        if resp.status_code in (400, 404, 405, 415, 422) and self._ask_stream:
            first = (await resp.aread()).decode("utf-8", "replace")
            await resp.aclose()
            self._ask_stream = False
            resp = await self._send({k: v for k, v in body.items() if k != "stream"})
            if resp.status_code != 200:
                raw = (await resp.aread()).decode("utf-8", "replace")
                await resp.aclose()
                raise UpstreamError(
                    502,
                    f"上游拒绝请求（带 stream 时：{first[:200]!r}；去掉后：{raw[:200]!r}）",
                    "upstream_error",
                )

        if resp.status_code != 200:
            raw = (await resp.aread()).decode("utf-8", "replace")
            await resp.aclose()
            code = {401: "unauthorized", 403: "forbidden", 429: "rate_limit_exceeded"}.get(
                resp.status_code, "upstream_error"
            )
            raise UpstreamError(
                resp.status_code if resp.status_code < 500 else 502, raw[:500], code
            )

        ctype = (resp.headers.get("content-type") or "").lower()
        if "text/event-stream" not in ctype:
            # 上游没在流式返回，后面就别再问它要流了
            self._ask_stream = False
            self.upstream_streams = False
        return resp

    # ------------------------------------------------------------ 响应解析
    @staticmethod
    def _json_to_events(obj: Any) -> list[SSEEvent]:
        """把一次性 JSON 响应翻成等价的两条「伪 SSE 事件」。

        这样上层只需要一套 _emit 逻辑，两种响应形状在进入翻译层之前就被抹平了。
        """
        data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
        reply = (
            data.get("reply")
            or data.get("content")
            or data.get("text")
            or data.get("answer")
            or ""
        )

        # 也兼容上游直接回 OpenAI 形状的情况
        if not reply and isinstance(obj.get("choices"), list) and obj["choices"]:
            msg = (obj["choices"][0] or {}).get("message") or {}
            reply = msg.get("content") or ""
            if not reply and msg.get("tool_calls"):
                reply = ""

        events: list[SSEEvent] = []
        if reply:
            events.append(
                SSEEvent(data=json.dumps({"type": "content", "content": reply}, ensure_ascii=False))
            )
        done: dict[str, Any] = {"type": "done"}
        cid = data.get("conversation_id") or obj.get("conversation_id")
        if cid:
            done["conversation_id"] = cid
        if isinstance(data.get("usage"), dict):
            done["usage"] = data["usage"]
        fr = data.get("finish_reason") or obj.get("finish_reason")
        if fr:
            done["finish_reason"] = fr
        events.append(SSEEvent(data=json.dumps(done, ensure_ascii=False)))
        return events

    def _decode_body(self, raw: str) -> list[SSEEvent]:
        """响应体不是 SSE 时，尽力认出它到底是什么。"""
        text = raw.strip()
        if not text:
            raise UpstreamError(502, "上游返回了空响应体", "empty_upstream_response")

        obj: Any = None
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            # 不是 JSON —— 可能是 content-type 没写对的 SSE，交给解码器再试
            dec = SSEDecoder()
            evs = dec.feed(text)
            evs.extend(dec.close())
            return evs

        if not isinstance(obj, dict):
            raise UpstreamError(
                502, f"上游返回了无法识别的 JSON：{text[:300]!r}", "unexpected_upstream_shape"
            )

        code = obj.get("code")
        if code not in (None, 0, "0", 200, "200"):
            raise UpstreamError(
                502,
                f"上游返回 code={code} message={obj.get('message') or obj.get('msg') or ''}",
                "upstream_business_error",
            )

        events = self._json_to_events(obj)
        if not any('"content"' in e.data for e in events):
            raise UpstreamError(
                502,
                "上游 200 但响应里没有正文。原始响应："
                + text[:500],
                "empty_upstream_response",
            )
        return events

    # ------------------------------------------------------------ 工具模拟
    @staticmethod
    def _extract_tool_call(text: str) -> tuple[str, str, int, int] | None:
        """从模型输出里抠出 {"tool_call": {...}}。

        返回 (工具名, arguments 字符串, 片段起止下标)。带上下标是为了把
        JSON 前后模型顺带说的话留下来 —— 它会一边说「我来看一下」一边调工具，
        把那句话丢掉对用户是信息损失。
        """
        if not text or "tool_call" not in text:
            return None
        for i, ch in enumerate(text):
            if ch != "{":
                continue
            depth = 0
            for j in range(i, len(text)):
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        frag = text[i : j + 1]
                        try:
                            obj = json.loads(frag)
                        except json.JSONDecodeError:
                            break
                        payload = obj.get("tool_call") if isinstance(obj, dict) else None
                        if isinstance(payload, dict) and payload.get("name"):
                            args = payload.get("arguments")
                            if isinstance(args, (dict, list)):
                                args = json.dumps(args, ensure_ascii=False)
                            return str(payload["name"]), str(args or "{}"), i, j + 1
                        break
        return None

    def _tool_call_chunks(
        self, chunk_id: str, model_name: str, name: str, args: str
    ) -> list[dict[str, Any]]:
        """OpenAI 的 tool_calls 是分片协议：首片给 id/type/name，后续片只给 arguments 增量。"""
        # id 必须每次都不同。之前用 sha1(name+args) 生成，同样的工具同样的参数会撞 id，
        # 而客户端是拿 id 把执行结果匹配回某一次调用的 —— 撞了就会串结果。
        call_id = "call_" + _sha1(f"{name}\x00{args}\x00{time.time_ns()}")[:24]
        return [
            openai_chunk(
                chunk_id,
                model_name,
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": ""},
                        }
                    ]
                },
            ),
            openai_chunk(
                chunk_id,
                model_name,
                {"tool_calls": [{"index": 0, "function": {"arguments": args}}]},
            ),
        ]

    # ------------------------------------------------------------ 主流程
    async def stream_chunks(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        model_name = model or self.model_name
        conv_key = self._conv_key(messages)
        conv_id = self._convs.get(conv_key)
        fold = self._fold_tool_results(messages)
        prompt = self._tools_prompt(tools)
        emulate = bool(prompt)

        body = self._build_body(fold, conv_id, prompt)
        resp = await self._post(body)

        chunk_id = "chatcmpl-" + _sha1(str(time.time_ns()))
        raw_head = ""
        st: dict[str, Any] = {
            "finish_reason": "stop",
            "usage": None,
            "chars": 0,
            "events": 0,
            "types": {},
            "reply": "",
            "buf": "",
        }
        started = False

        def head() -> dict[str, Any]:
            return openai_chunk(chunk_id, model_name, {"role": "assistant", "content": ""})

        def gate(chunks: list[dict[str, Any]]):
            nonlocal started
            for c in chunks:
                if not started:
                    started = True
                    yield head()
                yield c

        try:
            ctype = (resp.headers.get("content-type") or "").lower()
            if "text/event-stream" in ctype:
                dec = SSEDecoder()
                event_batches = []

                async def sse_events():
                    async for text in resp.aiter_text():
                        nonlocal raw_head
                        if len(raw_head) < 1500:
                            raw_head += text
                        for ev in dec.feed(text):
                            yield ev
                    for ev in dec.close():
                        yield ev

                async for ev in sse_events():
                    for c in self._emit(ev, chunk_id, model_name, conv_key, st, emulate):
                        for g in gate([c]):
                            yield g
            else:
                raw = (await resp.aread()).decode("utf-8", "replace")
                raw_head = raw
                for ev in self._decode_body(raw):
                    for c in self._emit(ev, chunk_id, model_name, conv_key, st, emulate):
                        for g in gate([c]):
                            yield g

            if emulate and st["buf"]:
                for g in gate(self._finalize_emulated(st, chunk_id, model_name)):
                    yield g

            # 上游只给了 done.reply、一直没发增量 —— 用全文兜底补一片
            if not st["chars"] and st["reply"] and not emulate:
                st["chars"] = len(st["reply"])
                for g in gate([openai_chunk(chunk_id, model_name, {"content": st["reply"]})]):
                    yield g

            if not started:
                raise UpstreamError(
                    502,
                    "上游返回 200 但没有任何可用内容（收到 "
                    f"{st['events']} 个 SSE 事件，type 分布 {st['types'] or '空'}）。"
                    f"原始响应开头：{raw_head[:500]!r}",
                    "empty_upstream_response",
                )

            yield openai_chunk(
                chunk_id,
                model_name,
                {},
                finish_reason=st["finish_reason"],
                usage=st["usage"],
            )
        finally:
            await resp.aclose()

    def _emit(
        self,
        ev: SSEEvent,
        chunk_id: str,
        model_name: str,
        conv_key: str,
        st: dict[str, Any],
        emulate: bool = False,
    ) -> list[dict[str, Any]]:
        """把一条 SSE 事件翻成 0..n 个 OpenAI chunk，同时把诊断信息记进 st。"""
        st["events"] += 1
        try:
            payload = json.loads(ev.data)
        except (json.JSONDecodeError, TypeError):
            st["types"]["<非JSON>"] = st["types"].get("<非JSON>", 0) + 1
            return []

        if not isinstance(payload, dict):
            st["types"]["<非对象>"] = st["types"].get("<非对象>", 0) + 1
            return []

        kind = payload.get("type")
        label = kind if isinstance(kind, str) and kind else "<无type>"
        st["types"][label] = st["types"].get(label, 0) + 1

        if label == "content":
            piece = str(payload.get("content") or "")
            if not piece:
                return []
            st["chars"] += len(piece)
            if emulate:
                # 要先看完整段输出，才知道模型是不是在调工具，所以先攒着
                st["buf"] += piece
                return []
            return [openai_chunk(chunk_id, model_name, {"content": piece})]

        # 上游在流里主动报错（常见于 token 过期 / 额度用尽），别当没看见
        if label in ("error", "err", "exception"):
            msg = payload.get("message") or payload.get("error") or payload
            raise UpstreamError(502, f"上游在流内报错: {msg}", "upstream_stream_error")

        if label == "done":
            cid = payload.get("conversation_id")
            if cid:
                self._convs[conv_key] = str(cid)
            reply = payload.get("reply")
            if isinstance(reply, str) and reply:
                st["reply"] = reply
                if emulate and not st["buf"]:
                    st["buf"] = reply
            fr = payload.get("finish_reason")
            if isinstance(fr, str) and fr:
                st["finish_reason"] = fr
            if isinstance(payload.get("usage"), dict):
                st["usage"] = payload["usage"]
            return []

        return []

    def _finalize_emulated(
        self, st: dict[str, Any], chunk_id: str, model_name: str
    ) -> list[dict[str, Any]]:
        """工具模拟收尾：模型要是吐了约定的 JSON，就换成 tool_calls，否则当普通文本。"""
        text = st["buf"]
        found = self._extract_tool_call(text)
        if found is None:
            return [openai_chunk(chunk_id, model_name, {"content": text})]

        name, args, start, end = found
        st["finish_reason"] = "tool_calls"
        chunks: list[dict[str, Any]] = []

        # 模型常常一边说「我来看一下」一边调工具，那句人话别丢
        residual = (text[:start] + text[end:]).strip()
        if residual:
            chunks.append(openai_chunk(chunk_id, model_name, {"content": residual}))

        chunks.extend(self._tool_call_chunks(chunk_id, model_name, name, args))
        return chunks

    async def complete(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """非流式：聚合同一条流，工具调用的分片在这里拼回完整结构。"""
        chunk_id = "chatcmpl-" + _sha1(str(time.time_ns()))
        model_name = model or self.model_name
        finish_reason = "stop"
        usage: dict[str, Any] | None = None
        parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}

        async for c in self.stream_chunks(messages, model_name, tools):
            chunk_id = c.get("id", chunk_id)
            if isinstance(c.get("usage"), dict):
                usage = c["usage"]
            for ch in c.get("choices", []):
                if ch.get("finish_reason"):
                    finish_reason = ch["finish_reason"]
                d = ch.get("delta") or {}
                piece = d.get("content")
                if piece:
                    parts.append(piece)
                for tc in d.get("tool_calls") or []:
                    slot = calls.setdefault(
                        tc.get("index", 0), {"id": "", "name": "", "arguments": ""}
                    )
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]

        text = "".join(parts)
        if not text and not calls:
            raise UpstreamError(502, "上游没有返回任何文本内容", "empty_upstream_response")

        message: dict[str, Any] = {"role": "assistant", "content": text or None}
        if calls:
            finish_reason = "tool_calls"
            message["tool_calls"] = [
                {
                    "id": v["id"] or f"call_{i}",
                    "type": "function",
                    "function": {"name": v["name"], "arguments": v["arguments"] or "{}"},
                }
                for i, v in sorted(calls.items())
            ]

        if not usage:
            usage = {
                "prompt_tokens": max(1, len(messages) * 8),
                "completion_tokens": max(1, len(text) // 2),
                "total_tokens": max(1, len(messages) * 8) + max(1, len(text) // 2),
            }

        return {
            "id": chunk_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                    "logprobs": None,
                }
            ],
            "usage": usage,
        }
