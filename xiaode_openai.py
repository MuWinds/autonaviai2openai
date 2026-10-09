#!/usr/bin/env python3
"""OpenAI-compatible API for 高德「小德」(amap AINative chat).

Wraps the TongYi Bailian `multimodal-dialog` WebSocket protocol that the amap
app uses (`wss://cmg-ws-mit.amap.com/ws/voice_chat`), captured via MITM.

Endpoints
  POST /v1/chat/completions   (stream + non-stream)
  GET  /v1/models

The handshake uses device constants (appkey/tid/diu/sign) that are fixed and
reusable from any host -- no device needed at runtime.

Stateless mode (no login): the server does NOT rely on any server-side session
memory. On every request it opens a BRAND-NEW conversation (fresh dialog_id +
conversation_id) and flattens the ENTIRE `messages` history into the single
`text` field sent to the model -- the same way a typical agent carries its full
context. So the caller is expected to send the whole conversation each turn.

Env switches:
  XIAODE_RANDOM_DIU=1   randomise the device id (URL diu/adiu + payload adiu) on
                        every request, so the server's device-bound memory never
                        leaks between calls (true stateless isolation).
  XIAODE_KEEP_ACCOUNT=1 keep the account/PII fields (uid/nickname/phone/...) that
                        are otherwise stripped by default.

Tool calling: the upstream model has no native function calling, so we emulate the
OpenAI `tools`/`tool_calls` contract by PROMPT INJECTION -- the schemas are described
in the prompt and the model answers with a delimited JSON block, which we parse and
return as a normal `tool_calls` response. The client executes the function and sends
the result back as a `role:"tool"` message.

Usage:
  python server/xiaode_openai.py [port]        # default 8790
  curl http://127.0.0.1:8790/v1/chat/completions -H 'Content-Type: application/json' \
       -d '{"model":"xiaode","messages":[{"role":"user","content":"你好"}]}'
"""
import asyncio, json, logging, os, re, ssl, sys, time, traceback, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
# Protocol frame templates live INSIDE this repo (server/frames/) so the server is
# self-contained: cloning just server/ is enough to run it.
TPL = os.path.join(HERE, "frames")

# ---------------- logging ----------------
LOG_FILE = os.path.join(HERE, "xiaode.log")
logger = logging.getLogger("xiaode")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    _fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%Y-%m-%d %H:%M:%S")
    _fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    _fh.setFormatter(_fmt)
    logger.addHandler(_fh)
    _sh = logging.StreamHandler(sys.stderr)
    _sh.setFormatter(_fmt)
    logger.addHandler(_sh)
logger.info("=== xiaode server starting (pid=%d) log=%s ===", os.getpid(), LOG_FILE)

# ---- device constants (fixed, from MITM capture) ----
HOST = "cmg-ws-mit.amap.com"
PATH = "/ws/voice_chat"
APPKEY = "672ec8ef"
WORKSPACE = "ws-hus10xw8wl0fx0ez"
DIP = "10880"
DIV = "ANDH170000"
# `tid` + `sign` are validated by the upstream and are BOUND together, so they must
# be sent exactly as captured. The `tid` here is an anonymous per-install device
# token (the capture was not logged in), not an account identity.
TID = "asetDgCP8Y8DAJD5O1LEzGy1"
# Device id (`diu`/`adiu`): the upstream does NOT validate it, so we use a neutral
# placeholder -- the real captured device id has been removed for privacy.
# Set XIAODE_RANDOM_DIU=1 to use a fresh random id per request instead.
DIU = "wbfhcffi0000000000000000000000"
SDKVER = "V1.4.7-02E-202608252026"
SIGN = "a67286934fd4440ee701baec6b6298ce"
KEEPALIVE = "60"

MODEL_ID = "xiaode"
MODEL_ALIASES = {"xiaode"}

# Account/personalisation identity. It lives in its own file, server/account.json
# (uid / nickname / phone / ...). The upstream server does NOT validate these
# (verified by ablation), so they are STRIPPED from every request by default to
# avoid shipping PII. Set XIAODE_KEEP_ACCOUNT=1 to inject them from account.json.
ACCOUNT_FIELDS = ("uid", "is_login", "bind_phone", "contact", "user_info",
                  "user_level", "user_city", "user_loc", "phone_system")
KEEP_ACCOUNT = os.environ.get("XIAODE_KEEP_ACCOUNT") == "1"
ACCOUNT_FILE = os.path.join(HERE, "account.json")


def load_account():
    try:
        with open(ACCOUNT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


ACCOUNT = load_account()

# Device identity (diu/adiu). The server does not validate these, but it DOES use
# `adiu` as the key for device-bound server-side memory. NOTE: the real key is the
# `adiu` inside the request payload, not the URL query -- randomising only the URL
# is NOT enough. Set XIAODE_RANDOM_DIU=1 to randomise both (URL diu/adiu AND the
# payload adiu) per request so memory never leaks across calls.
RANDOM_DIU = os.environ.get("XIAODE_RANDOM_DIU") == "1"

# Upstream hard limit on the `text` field: beyond roughly 2000 characters the
# server replies `task-failed` and drops the socket (which surfaces as
# "no close frame received or sent"). Measured: 2000 chars OK, 2500 chars FAIL.
# We cap the flattened prompt to stay under it. Override with XIAODE_MAX_TEXT.
MAX_TEXT_CHARS = int(os.environ.get("XIAODE_MAX_TEXT", "2000"))


def smart_truncate(text, limit):
    """Trim `text` to <= limit chars, keeping the head (instructions) and the tail
    (most recent turn + tool-call rules). Returns (text, was_truncated)."""
    if limit <= 0 or len(text) <= limit:
        return text, False
    avail = max(1, limit - 30)  # reserve room for the elision marker
    head = avail // 3
    tail = avail - head
    removed = len(text) - head - tail
    return text[:head] + ("\n…[中间省略 %d 字]…\n" % removed) + text[-tail:], True


def new_diu():
    """Return a device id: fixed constant by default, random when RANDOM_DIU."""
    return ("wbfhcffi" + uuid.uuid4().hex[:22]) if RANDOM_DIU else DIU


def build_url(csid, diu):
    q = ("bizType=16&dip=%s&appkey=%s&sdkver=%s&div=%s&tid=%s&keepAlive=%s"
         "&diu=%s&adiu=%s&csid=%s&sign=%s") % (
        DIP, APPKEY, SDKVER, DIV, TID, KEEPALIVE, diu, diu, csid, SIGN)
    return "wss://%s%s?%s" % (HOST, PATH, q)


_runtask_tpl = json.load(open(os.path.join(TPL, "runtask.json"), encoding="utf-8"))
_respond_tpl = json.load(open(os.path.join(TPL, "respond.json"), encoding="utf-8"))


def build_frames(text, task_id, dialog_id, csid, conversation_id, diu):
    rt = json.loads(json.dumps(_runtask_tpl))
    rp = json.loads(json.dumps(_respond_tpl))
    rt["header"]["task_id"] = task_id
    rt["payload"]["input"]["dialog_id"] = dialog_id
    rp["header"]["task_id"] = task_id
    rp["payload"]["input"]["dialog_id"] = dialog_id
    rp["payload"]["input"]["text"] = text
    for msg in (rt, rp):
        av = msg["payload"]["parameters"]["biz_params"]["context"]["custom"]["autonav"]
        av["csid"] = csid
        av["adiu"] = diu  # device id also lives in the payload, not only the URL
        av["dialogId"] = "dialog_AND_%s_%d" % (diu, int(time.time() * 1000))
        # stateless: the conversation_id equals this call's own task_id unless the
        # caller explicitly pins one (reuse across turns of one live session).
        av["conversation_id"] = conversation_id or task_id
        if KEEP_ACCOUNT:
            av.update(ACCOUNT)  # inject identity from server/account.json
        else:
            for k in ACCOUNT_FIELDS:
                av.pop(k, None)
    return rt, rp


async def xiaode_chat(text, on_delta=None, on_think=None, conversation_id=None,
                      dialog_id=None, timeout=60):
    """Run one round-trip on a FRESH conversation. Returns (answer, task_id).

    With the defaults (conversation_id=None, dialog_id=None) each call opens a
    brand-new conversation: a random dialog_id and conversation_id == its own
    task_id. All context must therefore be carried inside `text`.
    """
    import websockets
    task_id = uuid.uuid4().hex
    dialog_id = dialog_id or str(uuid.uuid4())
    csid = str(uuid.uuid4())
    diu = new_diu()
    rt, rp = build_frames(text, task_id, dialog_id, csid, conversation_id, diu)
    url = build_url(csid, diu)
    ctx = ssl.create_default_context()
    hdr = {"Authorization": "bearer "}
    answer = ""
    prev = ""
    got_done = False
    rt_s, rp_s = json.dumps(rt, ensure_ascii=False), json.dumps(rp, ensure_ascii=False)
    t0 = time.time()
    n_events = 0
    last_ev = None
    logger.info("WS open task=%s diu=%s csid=%s text_len=%d run-task=%dB respond=%dB url=%s",
                task_id, diu, csid, len(text), len(rt_s), len(rp_s), url)
    try:
        async with websockets.connect(url, ssl=ctx, extra_headers=hdr,
                                      open_timeout=12, close_timeout=5, max_size=2**24) as ws:
            logger.info("WS connected (%.2fs) subprotocol=%r", time.time() - t0, ws.subprotocol)
            await ws.send(rt_s)
            logger.debug("WS -> run-task sent")
            # wait for Started
            ts = time.time()
            while time.time() - ts < 15:
                m = await asyncio.wait_for(ws.recv(), timeout=15)
                if isinstance(m, (bytes, bytearray)):
                    logger.debug("WS <- BIN %dB", len(m))
                    continue
                try:
                    ev = json.loads(m).get("payload", {}).get("output", {}).get("event")
                except Exception:
                    logger.debug("WS <- non-json %dB", len(m))
                    continue
                last_ev, n_events = ev, n_events + 1
                logger.debug("WS <- %s", ev)
                if ev == "Started":
                    break
            logger.info("WS -> continue-task sent (%.2fs)", time.time() - t0)
            await ws.send(rp_s)
            ts = time.time()
            while time.time() - ts < timeout:
                try:
                    m = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    logger.warning("WS recv timeout after %.2fs", time.time() - t0)
                    break
                if isinstance(m, (bytes, bytearray)):
                    continue
                try:
                    j = json.loads(m)
                except Exception:
                    continue
                out = j.get("payload", {}).get("output", {})
                ev = out.get("event") or j.get("header", {}).get("event")
                last_ev, n_events = ev, n_events + 1
                if ev and ("fail" in str(ev).lower() or "error" in str(ev).lower()):
                    logger.warning("WS <- %s raw=%s", ev, json.dumps(j, ensure_ascii=False)[:800])
                if ev == "RespondingContent":
                    # think fragments (markdown components with sub_type think are inside 'container')
                    for r in (out.get("extra_info") or {}).get("result", []):
                        try:
                            comp = json.loads(r).get("component")
                        except Exception:
                            comp = None
                        if not comp:
                            continue
                        if comp.get("type") == "container":
                            t = comp.get("data", {}).get("data", {}).get("title")
                            if t and on_think:
                                on_think(t)
                    cur = out.get("text", "")
                    if cur and cur != prev:
                        delta = cur[len(prev):] if cur.startswith(prev) else cur
                        prev = cur
                        answer = cur
                        if delta and on_delta:
                            on_delta(delta)
                    if out.get("finished"):
                        got_done = True
                        break
                elif ev in ("task-finished", "Stopped"):
                    got_done = True
                    break
            # stop
            fin = {"header": {"action": "finish-task", "streaming": "duplex", "task_id": task_id},
                   "payload": {"input": {"dialog_id": dialog_id, "directive": "Stop"}, "model": ""}}
            try:
                await ws.send(json.dumps(fin, ensure_ascii=False))
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.warning("WS finish-task send failed: %s: %s", type(e).__name__, e)
    except Exception as e:
        logger.error("WS FAILED after %.2fs: %s: %s (events=%d last_ev=%s answer_len=%d)",
                     time.time() - t0, type(e).__name__, e, n_events, last_ev, len(answer))
        logger.error("WS traceback:\n%s", traceback.format_exc())
        raise
    logger.info("WS done %.2fs events=%d answer_len=%d got_done=%s",
                time.time() - t0, n_events, len(answer), got_done)
    return answer, task_id


# ---------------- HTTP ----------------
ROLE_LABEL = {"system": "系统", "user": "用户", "assistant": "助手", "tool": "工具结果"}

# ---------------- prompt-based tool calling ----------------
# The upstream model has NO native function-calling. We emulate the OpenAI
# tool-call contract via prompt injection: the tool schemas are described in the
# prompt and the model is told to answer with a delimited JSON block. We parse
# that block and surface it as a standard `tool_calls` response. The CLIENT then
# executes the function and sends the result back as a `role:"tool"` message.
TOOL_OPEN = "<<<TOOL_CALL>>>"
TOOL_CLOSE = "<<<END_TOOL_CALL>>>"
_TOOL_RE = re.compile(re.escape(TOOL_OPEN) + r"\s*(\{.*?\})\s*" + re.escape(TOOL_CLOSE), re.S)
_TOOL_RE_ALT = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


def _tool_defs(tools, functions):
    out = []
    for t in (tools or []):
        out.append(t.get("function", t) if isinstance(t, dict) else t)
    out.extend(functions or [])
    return [f for f in out if isinstance(f, dict) and f.get("name")]


def build_tool_prompt(tools, functions):
    """Render the tool schemas + the required output format into a prompt block.

    Framing matters: the model complies with a *format/transformation* instruction
    ("convert the request into a call") but refuses *capability* claims ("you have
    a tool"). So we never say it "has tools" -- we ask it to convert.
    """
    defs = _tool_defs(tools, functions)
    if not defs:
        return ""
    lines = []
    for f in defs:
        params = f.get("parameters") or f.get("input_schema") or {}
        props = params.get("properties") or {}
        sig = ", ".join("%s: %s" % (k, v.get("type", "")) for k, v in props.items())
        lines.append("- %s(%s)：%s" % (f["name"], sig, f.get("description", "")))
    first = defs[0]
    props0 = (first.get("parameters") or {}).get("properties") or {}
    ex_key = next(iter(props0), "arg")
    return (
        "【函数调用转换规则】\n"
        "判断最后一条用户消息是否需要用到下面的函数：\n"
        "- 需要：把请求转换成函数调用，用 %s 和 %s 包起来，只输出这个块，不要任何解释。\n"
        "- 不需要：正常用中文回答用户。\n\n"
        "可用函数：\n%s\n\n"
        "转换示例：\n用户：（与某个函数相关的请求）\n"
        "输出：%s\n{\"name\": \"%s\", \"arguments\": {\"%s\": \"<%s>\"}}\n%s\n\n"
        "现在处理最后一条用户消息：\n" % (
            TOOL_OPEN, TOOL_CLOSE, "\n".join(lines),
            TOOL_OPEN, first["name"], ex_key, ex_key, TOOL_CLOSE))


def parse_tool_call(text):
    """Return {"name","arguments"(json str)} if the model emitted a tool call."""
    t = text or ""
    m = _TOOL_RE.search(t) or _TOOL_RE_ALT.search(t)
    obj = None
    if m:
        try:
            obj = json.loads(m.group(1))
        except Exception:
            obj = None
    if obj is None:
        # fallback: a bare {"name": ..., "arguments": {...}} object in the text
        for cand in re.findall(r"\{[^{}]*\"name\"\s*:\s*\"[^\"]+\"[^{}]*\}", t):
            try:
                o = json.loads(cand)
            except Exception:
                continue
            if o.get("name"):
                obj = o
                break
    if not obj or not obj.get("name"):
        return None
    args = obj.get("arguments", obj.get("args", obj.get("parameters", {})))
    args_str = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
    return {"name": obj["name"], "arguments": args_str}


def tool_call_message(call):
    """Build an OpenAI-compatible assistant message carrying a tool_call."""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "call_" + uuid.uuid4().hex[:24],
            "type": "function",
            "function": {"name": call["name"], "arguments": call["arguments"]},
        }],
    }


def format_history(messages):
    """Flatten the whole OpenAI `messages` history into one prompt string.

    Stateless: no server-side memory, the model receives the entire transcript
    each call. A single user message is passed through verbatim; multi-turn
    history is rendered as a labelled transcript the model continues.
    """
    if isinstance(messages, str):
        return messages
    msgs = list(messages or [])
    # single plain user message -> no framing needed
    if len(msgs) == 1 and msgs[0].get("role") == "user":
        c = msgs[0].get("content")
        if isinstance(c, list):
            c = "".join(p.get("text", "") for p in c if isinstance(p, dict))
        return (c or "").strip()
    lines = []
    for m in msgs:
        role = m.get("role", "user")
        c = m.get("content")
        if isinstance(c, list):  # OpenAI multimodal content parts
            c = "".join(p.get("text", "") for p in c if isinstance(p, dict))
        c = (c or "").strip()
        if not c:
            continue
        lines.append("%s：%s" % (ROLE_LABEL.get(role, role), c))
    if not lines:
        return ""
    return "以下是历史对话记录，请基于完整上下文回答最后一条「用户」消息。\n\n" + "\n".join(lines)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "xiaode-openai/1.0"

    def log_message(self, fmt, *a):
        logger.info("HTTP %s", fmt % a)

    def log_error(self, fmt, *a):
        logger.warning("HTTP %s", fmt % a)

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _chunk(self, data: bytes):
        self.wfile.write(("%x\r\n" % len(data)).encode() + data + b"\r\n")

    def _sse(self, obj):
        self._chunk(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode())

    def do_GET(self):
        p = self.path.split("?")[0].rstrip("/")
        if p in ("/v1/models", "/models"):
            self._json(200, {"object": "list", "data": [
                {"id": MODEL_ID, "object": "model", "created": 1700000000, "owned_by": "amap"}]})
            return
        if p in ("", "/health"):
            self._json(200, {"status": "ok", "model": MODEL_ID})
            return
        logger.warning("REQ 404 GET path=%s", self.path)
        self._json(404, {"error": {"message": "not found", "type": "invalid_request_error"}})

    def do_POST(self):
        p = self.path.split("?")[0].rstrip("/")
        if p != "/v1/chat/completions":
            logger.warning("REQ 404 POST path=%s content-length=%s ua=%r",
                           self.path, self.headers.get("Content-Length"),
                           self.headers.get("User-Agent"))
            self._json(404, {"error": {"message": "not found", "type": "invalid_request_error"}})
            return
        raw = b""
        try:
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n) or b"{}"
            req = json.loads(raw)
        except Exception as e:
            logger.error("REQ bad json (%s): %r", e, raw[:200])
            self._json(400, {"error": {"message": "bad json: %s" % e, "type": "invalid_request_error"}})
            return
        text = format_history(req.get("messages"))
        if not text:
            self._json(400, {"error": {"message": "no user message", "type": "invalid_request_error"}})
            return
        stream = bool(req.get("stream"))
        model = req.get("model") or MODEL_ID
        rid = "chatcmpl-" + uuid.uuid4().hex
        created = int(time.time())

        # prompt-injected tool calling (upstream has no native function calling)
        tool_prompt = ""
        if req.get("tool_choice") != "none":
            tool_prompt = build_tool_prompt(req.get("tools"), req.get("functions"))
        # keep the tool rules intact and trim the history so the whole prompt fits
        # under the upstream `text` cap (otherwise the server returns task-failed).
        tool_prompt = tool_prompt[:MAX_TEXT_CHARS]
        hist_budget = MAX_TEXT_CHARS - (len(tool_prompt) + 2 if tool_prompt else 0)
        hist, truncated = smart_truncate(text, max(200, hist_budget))
        text = (hist + "\n\n" + tool_prompt) if tool_prompt else hist
        if len(text) > MAX_TEXT_CHARS:
            text, truncated = smart_truncate(text, MAX_TEXT_CHARS)
        if truncated:
            logger.warning("REQ prompt truncated -> %d chars (upstream caps `text` at ~%d)",
                           len(text), MAX_TEXT_CHARS)

        logger.info("REQ POST path=%s msgs=%d stream=%s tools=%d prompt_len=%d raw_body=%dB",
                    self.path, len(req.get("messages") or []), stream,
                    len(_tool_defs(req.get("tools"), req.get("functions"))), len(text), len(raw))

        # Stateless: every call is a brand-new conversation (fresh dialog_id +
        # conversation_id inside xiaode_chat). The full history is in `text`.
        if not stream:
            try:
                answer, _ = asyncio.run(xiaode_chat(text))
            except Exception as e:
                logger.error("REQ upstream error: %s: %s\n%s", type(e).__name__, e, traceback.format_exc())
                self._json(502, {"error": {"message": "upstream error: %s" % e, "type": "upstream_error"}})
                return
            call = parse_tool_call(answer) if tool_prompt else None
            if call:
                msg, finish = tool_call_message(call), "tool_calls"
            else:
                msg, finish = {"role": "assistant", "content": answer}, "stop"
            self._json(200, {
                "id": rid, "object": "chat.completion", "created": created, "model": model,
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                "usage": {"prompt_tokens": len(text), "completion_tokens": len(answer), "total_tokens": len(text) + len(answer)},
            })
            return

        # streaming
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def head(delta, finish=None):
            return {"id": rid, "object": "chat.completion.chunk", "created": created, "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        try:
            self._sse(head({"role": "assistant", "content": ""}))

            if tool_prompt:
                # buffer the whole answer: a tool call can only be decided as a whole
                answer, _ = asyncio.run(xiaode_chat(text))
                call = parse_tool_call(answer)
                if call:
                    tc = tool_call_message(call)["tool_calls"]
                    tc[0]["index"] = 0
                    self.log_message("stream tool_call %s", call["name"])
                    self._sse(head({"tool_calls": tc}, "tool_calls"))
                else:
                    self._sse(head({"content": answer}, "stop"))
            else:
                def on_delta(d):
                    self._sse(head({"content": d}))

                answer, _ = asyncio.run(xiaode_chat(text, on_delta=on_delta))
                self.log_message("stream done len=%d", len(answer))
                self._sse(head({}, "stop"))
            self._chunk(b"data: [DONE]\n\n")
        except Exception as e:
            logger.error("REQ stream error: %s: %s\n%s", type(e).__name__, e, traceback.format_exc())
            self._sse({"error": {"message": str(e)}})
        self._chunk(b"")
        try:
            self.wfile.flush()
        except Exception:
            pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # e.g. client reset the connection mid-request (WinError 10054)
        logger.warning("conn error from %s: %s", client_address,
                       traceback.format_exc().strip().replace("\n", " | "))


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8790
    srv = Server(("0.0.0.0", port), Handler)
    logger.info("listening on http://0.0.0.0:%d  model=%s random_diu=%s keep_account=%s",
                port, MODEL_ID, RANDOM_DIU, KEEP_ACCOUNT)
    print("[*] xiaode OpenAI-compatible server on http://0.0.0.0:%d" % port)
    print("[*]   POST /v1/chat/completions   GET /v1/models")
    print("[*]   model id: %s" % MODEL_ID)
    print("[*]   stateless full-history mode; random_diu=%s keep_account=%s"
          % (RANDOM_DIU, KEEP_ACCOUNT))
    print("[*]   log file: %s" % LOG_FILE)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
        print("\n[*] bye")


if __name__ == "__main__":
    main()
