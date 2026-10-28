# OpenAI 兼容网关 · 协议转换学习项目

把一个**非标准的对话接口**翻译成标准 OpenAI 接口，讲清楚反向代理在协议层面到底做了什么。

---

## ⚠️ 声明与立场

**这个项目只用于学习协议转换这项技术，禁止用于反向代理他人服务。**

- **禁止反代第三方系统。** 不管它是学校的、同学的、公司的、还是网上随便一个站的 ——
  只要不是你自己的服务、也没有得到书面授权，就不要把它接进来。
  「我能登录网页」不等于「我可以用 API 调它」：账号授权的是**使用产品**，
  不是**把产品逆向成接口**。
- 未经授权对他人在线系统做接口抓取、鉴权绕过、流量转发，可能违反
  《中华人民共和国网络安全法》《计算机信息系统安全保护条例》以及该服务的用户协议，
  后果由使用者自行承担。
- **本仓库不包含任何第三方站点地址、账号、密码或 token。** 自带的唯一上游是
  `mock_upstream/` 里的本地模拟服务，它照着「私有 agent 后端」的形态写，
  数据全是假的。`.env` 已被 `.gitignore` 排除，提交历史里也没有任何凭据。
- 出了教学范围怎么用、造成什么后果，与作者无关。

**可以怎么用**：指向你自己部署的 Ollama / vLLM，你自己写的服务，你付费购买的 API。
这些都属于「你有权使用的上游」，整条链路就是真实可用的网关。

---

## 这个项目讲什么

一套完全本地、可复现的练习环境，把反代里真正难的部分都覆盖到了：

- **SSE 增量解析** —— 网络 chunk 会随机切断一行，必须自己维护 buffer（`gateway/sse_utils.py`）
- **两种上游事件形态** —— `event:` 名区分 vs `data` 里的 `type` 字段区分，以及
  「同一条端点可能返回流式 SSE 也可能返回整段 JSON」
- **无状态协议 ↔ 有状态上游** —— OpenAI 每轮重发全量历史，上游却有会话记忆
- **tool_calls 完整往返** —— 包括流式分片拼接、`role=tool` 回填、`finish_reason` 约定
- **给没有工具通道的上游补工具调用** —— 提示词模拟 + 还原成标准三段式
- **Windows 上异步生成器 `sleep` 失效**、`trust_env` 让请求变成代理格式等实际踩过的坑

---

## 环境要求

- Python **3.10+**（用到 `X | None` 语法）
- 三个依赖，都很常见：

```bash
pip install -r requirements.txt
```

```
fastapi
uvicorn[standard]
httpx
```

> `tests/smoke.py` 额外用 `openai` 官方 SDK（`pip install openai`）。
> 只想跑网关不跑自检的话不需要装它。

推荐用独立环境，别装进系统 Python：

```bash
conda create -n sipc-gateway python=3.12 pip -y
conda activate sipc-gateway
pip install -r requirements.txt openai
```

---

## 快速开始

**1. 建配置**

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

默认 `MODE=campus`，指向自带的本地模拟上游，**什么都不用改就能跑**。

**2. 一键启动**

双击 `start.bat`，或者：

```bash
python start.py
```

启动后终端会打印：

```
Base URL : http://127.0.0.1:8200/v1
API Key  : sk-lab-xxxxx
模型名   : campus-agent-v1
上游模式 : campus
```

浏览器打开 <http://127.0.0.1:8200> 有接入说明页。

密钥首次启动自动生成，写进项目根目录 `.env`，之后重启不变。停止：`Ctrl+C`（两个服务一起停）。

> `start.bat` / `check.bat` 里写死了 conda 环境路径 `D:\miniconda3\envs\llm-proxy-lab\python.exe`。
> 你的环境名不一样就自己改那一行，或者直接用 `python start.py`。
> 这两个 bat 是**纯 ASCII + CRLF**，改的时候别用会写进中文和 LF 的编辑器，
> 否则 cmd 会把 `if (...)` 块解析错。

**3. 自检**

```bash
python tests/smoke.py      # 或双击 check.bat
```

---

## 配置项一览

全部在 `.env`，改完**必须重启**才生效。完整模板见 `.env.example`。

| 配置 | 说明 |
|---|---|
| `GATEWAY_KEY` | 客户端访问 `/v1` 用的密钥。留空则首次启动自动生成 |
| `MODE` | `campus` / `ssejson` / `openai`，三选一 |
| `CAMPUS_BASE_URL` | campus 模式的上游地址 |
| `SSEJSON_BASE_URL` / `SSEJSON_TOKEN` / `SSEJSON_MODEL` | ssejson 模式的三件套 |
| `SSEJSON_TOOLS` | `emulate`（提示词模拟工具调用）/ `off` |
| `UPSTREAM_BASE_URL` / `UPSTREAM_API_KEY` / `UPSTREAM_MODEL` | openai 模式 |
| `UPSTREAM_TRUST_ENV` | 是否让 httpx 读环境里的 `HTTP_PROXY`，默认 `0` |
| `PORT` / `HOST` / `MOCK_PORT` | 端口 |

---

## 验证

另开一个终端：

```bash
python tests/smoke.py
```

它会用**官方 OpenAI SDK** 打你的网关，检查 5 件事：模型列表、非流式、流式、多轮上下文、错误密钥拒绝。
实测输出：

```
[PASS] GET /v1/models
[PASS] 非流式 chat.completions
[PASS] 流式 chat.completions          首包 29ms，共 93 块，总 1.98s
[PASS] 多轮上下文桥接
[PASS] 工具调用（非流式往返）
[PASS] 工具调用（流式分片拼接）
[PASS] 错误密钥被拒绝

结果: 7 通过, 0 失败
```

「首包 29ms、共 93 块、总 1.98s」说明是**逐字流式**：首包很快，但整体铺满了 2 秒，
不是攒完一次性返回 —— 这是判断流式有没有做对的关键指标。

手动验证：

```bash
curl http://127.0.0.1:8200/v1/chat/completions \
  -H "Authorization: Bearer $(grep GATEWAY_KEY .env | cut -d= -f2)" \
  -H "Content-Type: application/json" \
  -d '{"model":"campus-agent-v1","stream":true,"messages":[{"role":"user","content":"你好"}]}'
```

---

## 架构

```
   OpenAI SDK / Cherry Studio / Continue / 任意客户端
                    │
                    │  POST /v1/chat/completions
                    │  Authorization: Bearer sk-lab-xxx
                    ▼
   ┌────────────────────────────────────────┐
   │  gateway  :8200                        │
   │    app.py        路由、鉴权、错误映射   │
   │    adapters.py   协议翻译（核心）       │
   │    sse_utils.py  SSE 增量解析 / 编码    │
   │    config.py     .env 加载、密钥管理    │
   └────────────────────────────────────────┘
                    │
                    │  X-Session: sess_xxx（自定义 header）
                    │  {"conv_id":"conv_xxx","query":"..."}
                    ▼
   ┌────────────────────────────────────────┐
   │  mock_upstream  :8100                  │   ← 换成你的真实上游
   │    POST /api/auth/login   换 session    │
   │    POST /api/conv/new     建会话        │
   │    POST /api/agent/stream 自定义 SSE    │
   └────────────────────────────────────────┘
```

**「反代」不是把请求原样转发出去**，而是三件事：

1. **鉴权换发** —— 客户端只认 `Bearer`，上游要 `X-Session`。网关持有上游凭据，不暴露给客户端。
2. **协议翻译** —— 上游的 SSE 事件名、字段名、结束标记都跟 OpenAI 不一样，要逐块转换。
3. **状态桥接** —— OpenAI 无状态（每次重发全量历史），上游有状态（会话自己记历史）。要替客户端兜住。

---

## 核心：SSE 是怎么转的

**上游发出来的**（自定义协议，注意事件名和字段名）：

```
event: meta
data: {"conv_id":"conv_a1b2","model":"campus-agent-v1"}

event: delta
data: {"t": "你"}

event: usage
data: {"prompt_tokens":12,"completion_tokens":30}

event: done
data: {}
```

**网关转出来的**（标准 OpenAI）：

```
data: {"id":"chatcmpl-x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}

data: {"id":"chatcmpl-x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"你"},"finish_reason":null}]}

data: {"id":"chatcmpl-x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

差异清单：

| 维度 | 上游 | OpenAI |
|---|---|---|
| 鉴权 | `X-Session: sess_xxx` | `Authorization: Bearer sk-xxx` |
| 有状态？ | 是，需要 `conv_id` | 否，全量重发 messages |
| 正文事件名 | `event: delta` | 无事件名，只有 `data:` |
| 正文字段 | `{"t": "字"}` | `{"delta": {"content": "字"}}` |
| 结束标记 | `event: done` | `data: [DONE]` |
| 首包 | 无 role | 必须有 `delta.role = "assistant"` |

---

## 踩过的坑

### 1. 网络 chunk 会随机切断一行

```python
# 错的：chunk 边界和行边界毫无关系，这样会丢字
async for chunk in resp.aiter_text():
    for line in chunk.split("\n"):
        ...

# 对的：自己维护 buffer，只在遇到空行时才吐出完整事件
dec = SSEDecoder()
async for chunk in resp.aiter_text():
    for ev in dec.feed(chunk):
        ...
```

`aiter_text()` 返回的分片可能是半个 `data: {"t":"你`，也可能是 `}\n\nevent: del`。
直接按 `\n` 切必然丢内容。正确做法见 `sse_utils.py` 的 `SSEDecoder`，它还处理了：

- 多行 `data:` 按规范用 `\n` 拼接
- `:` 开头的注释行（心跳）跳过
- 流结束时没有空行收尾的残留事件（`close()`）

### 2. 无状态协议 ↔ 有状态上游，历史会重复

OpenAI 每轮都重发全部 messages。如果原样转给有状态的上游，上游会看到重复历史，答得乱七八糟。

`messages_to_query()` 的做法：用 `system + 首条 user` 算一个指纹当会话 key，复用同一个 `conv_id`，
之后每轮只发最后一条 user 的内容。首轮才把 system 设定拼进 query（因为上游没有 system 通道）。

### 3. 中间层会偷偷缓冲，流式退化成一次性

```python
headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"}
```

少了 `X-Accel-Buffering: no`，nginx 会把你的流攒成一大块再发。现象是：本地测好好的，一上反代就卡住几秒然后整段蹦出来。

### 4. Windows 上，异步生成器里的 asyncio.sleep 会失效

这个坑值得单独记一笔，因为现象极具迷惑性。

模拟上游要逐字下发，代码很自然写成：

```python
for ch in reply:
    yield f"event: delta\ndata: {ch}\n\n"
    await asyncio.sleep(0.015)      # 预期每字间隔 15ms
```

实测：93 个字符，**总共只用 1 毫秒**，客户端 50ms 收完，表现和「被攒成一坨」一模一样。

排查路径：

| 检查项 | 结果 |
|---|---|
| 90 次 `asyncio.sleep(0.015)` 在独立脚本里 | 1.586s ✓ 正常 |
| 同一进程内、普通路由里的 `asyncio.sleep(0.05)` ×10 | 0.545s ✓ 正常 |
| 换成 `anyio.sleep` | 93 次共 2ms ✗ 仍然不等 |
| 在生成器里打点，记录每次 sleep 的实际耗时 | 平均 0.01ms ✗ |

结论不是「睡眠坏了」，也不是「事件循环坏了」：**唯独在 `StreamingResponse` 的异步生成器里，
`await sleep()` 会立即返回。**

解法是绕开事件循环定时器，用线程池强制阻塞：

```python
loop = asyncio.get_running_loop()
for ch in reply:
    yield f"event: delta\ndata: {ch}\n\n"
    await loop.run_in_executor(None, time.sleep, 0.02)
```

改完立刻正常：首包 29ms、93 块、总计 1.98s。

**教训**：判断流式是否真的在工作，不能数「服务端 yield 了多少次」，也不能只看首包时延，
必须看**首包到末包之间的跨度**。首包 40ms + 总耗时 60ms，意味着 93 个分片是在 20ms 内挤出来的 ——
那不是流式，是伪装成流式的批量返回。

### 5. 上游返回 200，不代表它成功了

这是本项目最难查的一个坑。上游鉴权失败时很可能**照样回 200**，然后把错误塞在 body 里：

```
HTTP/1.1 200 OK
{"code":401,"message":"token expired"}
```

如果适配层只认 `{"type":"content"}` 这一种形状，就会把所有事件都丢掉，最后聚合成一个
`content: ""` 的正常响应。表现为：**网关一切正常、无报错、smoke 只告诉你「回复异常: ''」**，
而真正的原因（token 过期）离你十万八千里。

三条规则：

1. 适配层的流式转换要**记录诊断信息**（收到几个事件、type 分布、原始响应开头），
   一个可用事件都没有就抛 502 而不是返回空。
2. 有个 `done.reply` 之类「全文兜底字段」的就用它补一片，别丢内容。
3. 流式路径要在**交出响应之前**先取首包。一旦 `StreamingResponse` 返回，200 状态码就发出去了，
   生成器里再抛错也变不成 502，客户端只会卡到超时。

对应到代码：`SSEJSONAdapter.stream_chunks()` 里的 `st` 字典记诊断，`app.py` 的 `primed()`
负责「先取首包再交出去」。

### 6. 环境里游荡的 `HTTP_PROXY` 会改变请求的写法

httpx 默认 `trust_env=True`，会去读 `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`。
一旦命中某个代理，请求行会变成**绝对 URI 形式**：

```
POST http://127.0.0.1:8398/sse HTTP/1.1      ← 走代理时
POST /sse HTTP/1.1                           ← 直连时
```

这不是错误（RFC 7230 要求服务端必须接受绝对形式），但它会让
「同一份 `.env`，在这台机器上好好的，换台机器就路由不到」——因为环境变量不同。
调试时极难看出来，我是靠抓服务端收到的请求行才定位到的。

所以三个适配器的 `httpx.AsyncClient` 全部显式传 `trust_env=TRUST_ENV`，
默认 **False**。上游确实要走代理时，`.env` 里设：

```bash
UPSTREAM_TRUST_ENV=1
```

### 附带一条：上游会话会过期

`CampusAdapter` 在 `_session_deadline` 到点前 30 秒就判过期，401 时自动重登 + 重建会话再重试一次。
真实网关里这步不做，跑一小时后就开始全线 500。

---

## 怎么改成接真实上游

### 换成你本地的 Ollama / vLLM（标准 OpenAI 兼容）

它们本身就是 OpenAI 协议，不需要翻译，走透传模式：

```bash
# .env
MODE=openai
UPSTREAM_BASE_URL=http://127.0.0.1:11434/v1
UPSTREAM_API_KEY=ollama
UPSTREAM_MODEL=qwen2.5:7b
```

重启后网关就变成「转发 + 换鉴权」，代码走 `OpenAICompatAdapter`。
对比一下两种模式的代码量，能直观感受到：**协议越标准，网关越薄**。

### 换成 type/content 风格的 SSE 上游（`MODE=ssejson`）

这类上游**同一个端点可能返回两种完全不同的形状**，取决于请求里带不带 `stream`。
两种都必须认，这是本项目踩过最狠的一次坑。

形状 A —— 流式 SSE：

```
data: {"type": "content", "content": "字"}

data: {"type": "done", "reply": "全文", "conversation_id": "xxx",
       "usage": {"prompt_tokens": 806, "completion_tokens": 51, "total_tokens": 857}}
```

形状 B —— 一次性 JSON（没有 stream，上游把整段话攒好了一次性返回）：

```json
{"code": 0, "message": "ok",
 "data": {"reply": "全文", "conversation_id": "xxx",
          "usage": {"prompt_tokens": 667, "completion_tokens": 22, "total_tokens": 689}}}
```

配三行就能接：

```bash
# .env
MODE=ssejson
SSEJSON_BASE_URL=https://example.com/api/chat
SSEJSON_TOKEN=你的-bearer-token
SSEJSON_MODEL=ssejson-agent-v1
SSEJSON_TOOLS=emulate          # 工具模拟，见下
```

`SSEJSONAdapter` 的处理链路：

1. 请求体里带 `stream: true`，优先要流式；上游要是 4xx 拒绝这个字段，自动去掉重发并记住。
2. 响应按 `Content-Type` 分流：
   - `text/event-stream` → 增量解析（`type=content` → `delta.content`，`type=done` → 收尾）
   - 其它 → 整段读完，当 JSON 解析，把 `data.reply` 变成一条内容、`data.conversation_id` 和
     `data.usage` 挂到收尾包上
3. `code != 0` 直接抛错，把上游的 `message` 带出来。
4. 什么也没认出来 → 502，附上 type 分布和原始响应开头。

为什么非要两种都认：只认 A 的时候，上游明明回了 200 加一大段完整文本，
适配层却把所有事件当垃圾丢掉，最后聚合成空字符串。客户端只看到
`returned no content`，而真正原因（形状对不上）完全看不见。

**多轮**：上游返回的 `conversation_id` 会被缓存并在后续请求里带回去 —— 它认就接着聊，
不认也无害。**但请求里仍然发全量历史**，不依赖那个字段名生效。

这里踩过一次思路错误：最初的设计是「有 `conversation_id` 就只发最后一条 user」，
理由是省 token、别让模型数两遍。问题是那个字段名是猜的，一旦上游不认，
上下文会整体丢失，用户每轮都像在跟一个失忆的人说话 —— 这比带着重复历史糟得多。
发全量历史是实测能正确回答最后一轮问题的路径，所以按它来。

**工具调用（`SSEJSON_TOOLS`）**：这类上游没有工具通道，所以做的是提示词模拟 ——
把工具清单塞进 system prompt，要求模型在需要时只输出一行
`{"tool_call": {"name": ..., "arguments": {...}}}`，适配层再还原成 OpenAI 的
`tool_calls` 三段式（首片给 id/type/name，后续片只给 arguments 增量）。
`role=tool` 的结果会被折叠成 `[工具 xxx 的执行结果]` 一段文本再发回去。

```bash
SSEJSON_TOOLS=emulate   # 默认。代价：每次带 tools 的请求都会多一段 system prompt
SSEJSON_TOOLS=off       # 完全忽略客户端传来的 tools
```

**模型会不会遵守那条 JSON 约定，取决于上游模型本身，网关保证不了。**
如果它选择正常回答（比如树洞型人格会拒绝「读文件」），
适配层就把它当普通文本原样返回，`finish_reason` 仍是 `stop`。这是诚实的行为。

**先用本地端点跑通再换地址。** 项目内置了 1:1 复刻这个格式的模拟端点
`POST /api/v1/treehole/chat`（token 是 `mock-token`），它和真实上游的差别只有内容。

对比一下三种模式的代码量，能直观看出「协议越标准，网关越薄」：

| 模式 | 上游形态 | 适配层要做什么 |
|---|---|---|
| `openai` | 标准 OpenAI 兼容 | 只转发 + 换鉴权，0 行翻译 |
| `ssejson` | 只有 `data:`，靠 `type` 区分；也可能整段 JSON | 认两种形状 + 缓存 conversation_id + 工具模拟 |
| `campus` | `event:` 名 + 自定义字段 + 有状态会话 | 登录、建会话、翻译 4 种事件、tool_calls 分片 |

> `SSEJSON_TOKEN` 填你自己的凭据。填什么、指向哪里，代码不校验 —— 网关只负责翻译协议。

**上游 200 但没内容时不会静默返回空串。** 早期版本会把这种情况翻成一个
`content: ""` 的正常响应，客户端只能干巴巴地报 `returned a completed response with no content`，
完全看不出上游到底说了什么。现在会抛 502，并把上游的原话带出来：

```
502 empty_upstream_response
上游返回 200 但没有任何可用内容（收到 1 个 SSE 事件，type 分布 {'<非JSON>': 1}）。
原始响应开头：'{"code":401,"message":"token expired"}'
```

**排障接口 `/v1/debug/upstream`**：拿当前 `.env` 配置真打一次上游，原样返回状态码、
响应头、响应体前 4KB，不翻译不聚合。想知道「是上游没给还是我们没认出来」就打它：

```bash
curl -X POST http://127.0.0.1:8200/v1/debug/upstream \
  -H "Authorization: Bearer $GATEWAY_KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"你好"}]}'
```

`tests/smoke.py` 里 chat 类用例失败时会自动调它并把结果打出来，所以自检失败时
一眼就能看到上游的真实响应，不用再拿 curl 手搓。

**`/health` 会告诉你上游是不是真流式**：

```json
{"ok": true, "mode": "ssejson", "adapter": "SSEJSONAdapter",
 "upstream_stream": false, "tools_mode": "emulate"}
```

`upstream_stream: false` 表示上游一次性给全文（形状 B），网关只能整段转发，
这时不存在「打字机效果」。`smoke.py` 会读这个字段，自动跳过「首末包跨度」那条断言 ——
否则会用一条对上游不合理的标准去判网关的错。

### 换成你自己的私有服务（自定义协议）

改 `mock_upstream/` 的形态为你的上游，然后同步改 `gateway/adapters.py` 里 `CampusAdapter` 的三个方法：

| 方法 | 干什么 |
|---|---|
| `_login()` | 换 token，填你的鉴权方式 |
| `_new_conv()` | 建会话，没有会话概念的删掉这个方法 |
| `stream_chunks()` | 解析你的上游 SSE，`emit()` 里改字段映射 |

`emit()` 是把上游事件变成 OpenAI chunk 的唯一入口，通常在 10 行以内改完。

---

## 文件说明

```
sipc-culvert/
├── start.py                一键启动（起两个 uvicorn）
├── start.bat               Windows 双击启动
├── requirements.txt
├── .env                    密钥与配置（首次启动生成）
│
├── gateway/                OpenAI 兼容网关
│   ├── app.py              路由、鉴权、错误映射、首页
│   ├── adapters.py         协议翻译（CampusAdapter / OpenAICompatAdapter）
│   ├── sse_utils.py        SSE 增量解析与编码（可单测）
│   └── config.py           .env 加载、密钥生成
│
├── mock_upstream/
│   └── app.py              模拟私有 agent 后端（换成你的上游）
│
└── tests/
    └── smoke.py            用官方 OpenAI SDK 做端到端自检
```

---

## 接入任意第三方客户端

任何支持「自定义 OpenAI Base URL」的客户端都能用，填法统一是这三个值：

- **Base URL**：`http://127.0.0.1:8200/v1`
- **API Key**：`.env` 里的 `GATEWAY_KEY`
- **Model**：`campus-agent-v1`

常见客户端的位置：

| 客户端 | 配置路径 |
|---|---|
| Cherry Studio | 设置 → 模型服务 → 添加 → 类型选 OpenAI，填 Base URL 和 Key |
| Continue (VS Code) | `config.json` 的 `models[].apiBase` / `apiKey` |
| ChatBox | 设置 → 模型 → 新增自定义 OpenAI API |
| OpenAI SDK | `OpenAI(base_url="...", api_key="...")` |
| LangChain | `ChatOpenAI(base_url="...", api_key="...")` |
| OneAPI / New-API | 渠道 → 类型 OpenAI → 代理地址填 Base URL |
| DeepSeek 官方 dsh | 见下一节（走 patch 层，不是填表） |

---

## 接入 DeepSeek 官方 CLI（dsh）· 已实测

`@deepseek-ai/dsh`（`deepseek-harness`）是 DeepSeek 官方的 agent CLI。**它支持自定义 OpenAI 兼容端点**，
但不是填个表单就行，而是通过 `@deepseek-ai/dsh-llm-pi-ai` 插件的 `providers` 配置注册路由。

项目根目录的 `dsh-llm.yml` 就是为这个准备的：

```bash
# 1. 先起网关
python start.py

# 2. 另开终端，把网关密钥放进环境变量（配置里写的是变量名，不是密钥本身）
export LAB_GATEWAY_KEY=$(grep GATEWAY_KEY .env | cut -d= -f2)

# 3. 用 patch 层启动
npx @deepseek-ai/dsh@0.2.1-rc.2 --profile headless --patch ./dsh-llm.yml "你好"
```

> 验证时我用临时 `DSH_HOME` 做了隔离，**没有碰你 `~/.dsh` 里已有的 profile**。
> 你自己跑时不要设 `DSH_HOME`，直接用默认值；`--patch` 是运行时叠加，不会改写 profile 里的 `cordis.patch.yml`，
> 所以现有配置也不会被改动。

实测输出：

```
（模拟上游回答）你问的是「...」。这条链路是：OpenAI SDK -> 反代网关 -> 本地模拟后端 -> 假模型。
```

退出码 0，说明 dsh 完整走完了 `请求 → 网关 → 上游 → SSE 流 → 渲染` 全链路。

### 在 dsh 的图形界面里添加

不想写 patch 文件也可以直接在界面里加。位置在 **设置 → 模型 → 添加自定义服务商**，
按下面填：

| 字段 | 填什么 | 说明 |
|---|---|---|
| 名称 | 随便，比如 `本地网关` | 只是显示名 |
| 协议 / API 类型 | **OpenAI 兼容**（`openai-completions`） | 别选 Anthropic / Gemini |
| Base URL | `http://127.0.0.1:8200/v1` | **必须带 `/v1`**，少写就是 404 |
| API Key | `.env` 里的 `GATEWAY_KEY` | `sk-lab-...` 那串 |
| 模型 ID | `campus-agent-v1` 或 `ssejson-agent-v1` | 必须和 `/v1/models` 返回的一致 |

找不准模型 ID 就打一下：

```bash
curl --noproxy '*' http://127.0.0.1:8200/v1/models \
  -H "Authorization: Bearer <你的 GATEWAY_KEY>"
```

返回里的 `data[0].id` 就是要填的值。它跟着 `MODE` 变：
`campus` → `campus-agent-v1`，`ssejson` → `SSEJSON_MODEL` 的值。

填完点测试/保存，然后在对话界面右下角的模型选择器里选它。

**两个必然遇到的报错**：

- `404 Not Found` —— Base URL 少了 `/v1`，或者末尾多了斜杠
- `model not found` / 模型下拉里空的 —— 模型 ID 和 `/v1/models` 对不上，或者网关没起来

### 两个实测踩到的坑

**1. patch 是整块替换，不是深度合并**

官方参考原文：「patch 会替换目标行的整个 `config` 值，而不是深度合并其中的键」。
所以想改 `llm-pi-ai` 的一个字段，必须把它完整的 `config` 写出来。只写要改的那几个键，其余配置会被清空。

**2. `compat` 开关受协议约束，填错直接拒绝启动**

我第一版写了 `supportsTemperature`，直接报：

```
dsh: INVALID_CONFIG: llm-pi-ai: provider "lab-gateway" sets compat "supportsTemperature",
but no model on the route speaks a protocol that takes it; it exists on anthropic-messages
```

`supportsTemperature` 只属于 `anthropic-messages` 协议，`openai-completions` 不认这个开关。
dsh 宁可拒绝启动，也不让一个「看起来生效了但实际被忽略」的开关留在配置里 —— 这个设计值得学。

### 工具调用（tool_calls）已支持

agent 能不能干活，全看这条链路。网关现在完整支持工具往返。

**上游发出来的**（自定义协议，参数按块增量下发）：

```
event: tool_call
data: {"index":0,"id":"call_a1b2","name":"read_file","arguments":""}

event: tool_call
data: {"index":0,"arguments":"{\"path\":"}

event: tool_call
data: {"index":0,"arguments":" \"README.md\"}"}

event: done
data: {"finish_reason":"tool_calls"}
```

**网关转出来的**（标准 OpenAI 分片）：

```
data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_a1b2","type":"function",
       "function":{"name":"read_file","arguments":""}}]},"finish_reason":null}]}

data: {"choices":[{"delta":{"tool_calls":[{"index":0,
       "function":{"arguments":"{\"path\":"}}]},"finish_reason":null}]}

data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}
```

**转的时候有三条硬约束，错了客户端就挂：**

1. **首片必须带齐 `id` + `type` + `function.name`**，缺一个客户端就没法把后续分片挂到这个调用上。
2. **后续分片只能带 `index` + `function.arguments` 增量**，不能重复 `name`，不能重新给 `id`。
3. **`finish_reason` 必须是 `"tool_calls"`**，不能是 `"stop"`，否则客户端会以为模型说完了，不去执行工具。

**完整往返是三轮：**

```
第1轮  客户端 → tools 定义        → 上游决定调 read_file
       上游 → tool_call 事件       → 网关 → delta.tool_calls 分片
第2轮  客户端执行 read_file       → 把结果作为 role=tool 消息发回
       网关取出 tool_call_id 反查工具名 → 拼成 tool_results 发给上游
第3轮  上游 → 基于结果的最终文本   → 网关 → 正常 delta + finish_reason: stop
```

第二轮有个容易忽略的点：`role=tool` 消息只带 `tool_call_id`，不带工具名。网关必须回到前面的
`assistant.tool_calls` 里按 id 反查，才能告诉上游「这是哪个工具返回的」。这段逻辑在
`messages_to_query()` 里，大约 10 行。

非流式场景下，分片要按 `index` 聚合回完整结构，`content` 置 `null`（有 tool_calls 时的规范要求），
见 `CampusAdapter.complete()`。

---

## 我本机的环境

conda 环境 `llm-proxy-lab`（Python 3.12），依赖见 `requirements.txt`。

```bash
conda activate llm-proxy-lab
# 或直接指定解释器
D:/miniconda3/envs/llm-proxy-lab/python.exe start.py
```

`start.bat` / `check.bat` 里写死的就是这个路径，你的环境名不同就改那一行。

> **Windows 上别用裸 `python` 命令。** 如果 `python` 解析到
> `%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe`，那是应用执行别名
> （`AppInstallerPythonRedirector.exe`），会静默跳转到 Microsoft Store 而**不执行脚本** ——
> 表现就是「敲了命令，什么都没输出」。用 `where python` 确认，或者直接写全路径。
