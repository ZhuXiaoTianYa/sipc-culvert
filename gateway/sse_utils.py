"""
SSE 编解码 —— 反代里最核心、也最容易写错的一块。

为什么不能简单 `for line in resp.iter_lines()` 了事：
  1. 网络 chunk 的边界会随机切断一行，甚至切断 "\\n\\n" 分隔符，
     所以必须自己维护 buffer，做增量解析。
  2. 一个事件可以有多行 data，要按规范用 "\\n" 拼接。
  3. 冒号开头的是注释（很多服务用它做心跳保活），必须跳过。
  4. 规范说 `data: xxx` 冒号后有一个可选空格，要去掉，但不能多去。

这个文件是纯函数 + 一个小状态机，你可以直接拿去做单元测试。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field


@dataclass
class SSEEvent:
    event: str = "message"
    data: str = ""
    id: str | None = None
    retry: int | None = None


@dataclass
class SSEDecoder:
    """
    增量 SSE 解码器。用法：

        dec = SSEDecoder()
        async for chunk in resp.aiter_text():
            for ev in dec.feed(chunk):
                handle(ev)

    流结束时记得调一次 dec.close()，把残留的最后一个事件吐出来
    （有些服务发完 data 就直接关连接，不补最后的空行）。
    """

    _buf: str = ""
    _cur: dict[str, list[str]] = field(default_factory=dict)

    def feed(self, chunk: str) -> list[SSEEvent]:
        self._buf += chunk
        out: list[SSEEvent] = []

        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r")

            # 空行 = 一个事件结束
            if line == "":
                ev = self._flush()
                if ev is not None:
                    out.append(ev)
                continue

            # 注释 / 心跳，忽略
            if line.startswith(":"):
                continue

            if ":" in line:
                name, _, value = line.partition(":")
                if value.startswith(" "):
                    value = value[1:]
            else:
                # 只有字段名没有冒号，值是空字符串
                name, value = line, ""

            self._cur.setdefault(name, []).append(value)

        return out

    def close(self) -> list[SSEEvent]:
        """流结束时收尾：把没有以空行结尾的残留事件也吐出来。"""
        out: list[SSEEvent] = []
        if self._buf.strip():
            chunk, self._buf = self._buf, ""
            out.extend(self.feed(chunk + "\n"))
        ev = self._flush()
        if ev is not None:
            out.append(ev)
        return out

    def _flush(self) -> SSEEvent | None:
        if not self._cur:
            return None
        cur, self._cur = self._cur, {}
        # 多行 data 按规范用换行拼接
        data = "\n".join(cur.get("data", []))
        retry_raw = cur.get("retry", [None])[-1]
        return SSEEvent(
            event=cur.get("event", ["message"])[-1],
            data=data,
            id=cur.get("id", [None])[-1],
            retry=int(retry_raw) if retry_raw and retry_raw.isdigit() else None,
        )


def encode_sse(data: str, event: str | None = None) -> bytes:
    """编码一个 SSE 事件。返回 bytes，方便直接写进响应流。"""
    parts: list[str] = []
    if event:
        parts.append(f"event: {event}")
    # data 里如果有换行，要拆成多个 data: 行
    for line in data.split("\n"):
        parts.append(f"data: {line}")
    return ("\n".join(parts) + "\n\n").encode("utf-8")


def encode_done() -> bytes:
    """OpenAI 协议约定的流终止标记。"""
    return b"data: [DONE]\n\n"


# ------------------------------------------------------------------ OpenAI 分块
def openai_chunk(
    chunk_id: str,
    model: str,
    delta: dict,
    finish_reason: str | None = None,
    index: int = 0,
    usage: dict | None = None,
) -> dict:
    """构造一个 chat.completion.chunk 对象。

    usage 只在最后一包里给（OpenAI 的 stream_options.include_usage 就是这行为），
    中间包带了反而有客户端会报错，所以默认 None 且不写进结果里。
    """
    obj = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": index,
                "delta": delta,
                "finish_reason": finish_reason,
                "logprobs": None,
            }
        ],
    }
    if usage:
        obj["usage"] = usage
    return obj


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
