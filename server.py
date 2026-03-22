#!/usr/bin/env python3
"""The Oracle — FastMCP proxy server + WebSocket bridge.

Mounts cloud-chat-assistant and speech-to-cli as namespaced tools on a
unified FastMCP proxy, then bridges browser WebSocket clients to it.

Usage:
    ./venv/bin/python3 server.py              # http://localhost:8778
    ./venv/bin/python3 server.py --port 9000  # custom port
"""

import asyncio
import json
import os
import re
import sys
from pathlib import Path

from aiohttp import web
import aiohttp

from fastmcp import FastMCP, Client
from fastmcp.server import create_proxy
from fastmcp.client.transports import StdioTransport

# ── Paths ──────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
INDEX_HTML = SCRIPT_DIR / "index.html"

CHAT_DIR = Path(os.environ.get("ORACLE_CLOUD_CHAT_DIR", Path.home() / "Projects" / "cloud-chat-assistant"))
CHAT_SCRIPT = CHAT_DIR / "mcp_cloud_chat.py"
CHAT_PYTHON = CHAT_DIR / "venv" / "bin" / "python3"

SPEECH_DIR = Path(os.environ.get("ORACLE_SPEECH_DIR", Path.home() / "Projects" / "speech-to-cli"))
SPEECH_SCRIPT = SPEECH_DIR / "mcp_speech.py"
SPEECH_PYTHON = Path(os.environ.get("ORACLE_SPEECH_PYTHON", "/usr/bin/python3"))

PORT = 8778

# ── Model type detection ───────────────────────────────────────────

SERVERLESS_MODELS = {
    "grok-3", "grok-3-mini", "DeepSeek-R1",
    "Meta-Llama-3.1-405B-Instruct", "Meta-Llama-3.1-8B-Instruct",
    "Llama-3.2-11B-Vision-Instruct", "Llama-3.2-90B-Vision-Instruct",
    "Llama-3.3-70B-Instruct", "Llama-4-Scout-17B-16E-Instruct",
    "Phi-4", "Cohere-command-r-plus-08-2024", "Cohere-command-r-08-2024",
    "Codestral-2501", "Ministral-3B",
}
GOOGLE_MODELS = {"gemini-2.5-flash", "gemini-2.5-pro", "gemini-3.1-pro-preview"}
BEDROCK_MODELS = {
    "claude-opus-4.5", "claude-opus-4.6", "claude-sonnet-4",
    "claude-sonnet-4.5", "claude-sonnet-4.6", "claude-haiku-4.5",
    "nova-pro", "nova-lite", "nova-2-lite",
    "llama4-maverick-17b", "llama4-scout-17b", "palmyra-x4", "palmyra-x5",
}
PUTER_MODELS = {
    "claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001",
    "gpt-5.4-2026-03-05", "gpt-5.2-chat-latest", "o3", "o3-pro",
    "deepseek-chat", "deepseek-reasoner",
    "grok-4", "grok-4-fast",
    "mistral-large-latest",
}


def _model_type(model):
    if model in SERVERLESS_MODELS:
        return "serverless"
    if model in GOOGLE_MODELS:
        return "google"
    if model in BEDROCK_MODELS:
        return "bedrock"
    if model in PUTER_MODELS:
        return "puter"
    return "deployed"


# ── FastMCP Proxy ─────────────────────────────────────────────────

oracle: FastMCP | None = None
oracle_client: Client | None = None
chat_lock = asyncio.Lock()  # serialize configure+chat to prevent interleaving
listen_lock = asyncio.Lock()  # serialize listen calls
chat_ready = False
speech_ready = False

# Dedicated speech client for listen — bypasses proxy to stream progress
speech_listen_client: Client | None = None
_listen_ws = None  # active WebSocket to stream progress to


def _make_transport(python_path, script_path):
    """Create a StdioTransport for an MCP server subprocess."""
    py = str(python_path) if python_path.exists() else sys.executable
    # Ensure subprocesses get essential env vars for PipeWire/audio access
    import os
    env = dict(os.environ)
    env.setdefault("HOME", str(Path.home()))
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return StdioTransport(
        command=py,
        args=[str(script_path)],
        keep_alive=True,
        log_file=Path("/tmp/oracle-mcp-stderr.log"),
        env=env,
    )


async def _call_tool(name, args=None, timeout=120):
    """Call an MCP tool on the Oracle proxy and return the text result."""
    result = await asyncio.wait_for(
        oracle_client.call_tool(name, args or {}),
        timeout=timeout,
    )
    # FastMCP call_tool returns a CallToolResult with .content list
    if hasattr(result, "content"):
        texts = []
        for item in result.content:
            if hasattr(item, "text"):
                texts.append(item.text)
        return "\n".join(texts) if texts else ""
    if isinstance(result, str):
        return result
    return str(result)


def _extract_result_text(result):
    """Extract text from a CallToolResult."""
    if hasattr(result, "content"):
        for item in result.content:
            if hasattr(item, "text"):
                return item.text
    if isinstance(result, str):
        return result
    return str(result)


_VU_CHARS = {" ": 0.0, "▂": 0.15, "▃": 0.3, "▄": 0.45, "▅": 0.6, "▆": 0.75, "▇": 0.9, "█": 1.0}


def _extract_vu(text):
    """Extract VU level (0.0-1.0) from the last block char in a progress message."""
    for ch in reversed(text):
        if ch in _VU_CHARS:
            return _VU_CHARS[ch]
    return 0.0


async def _speech_progress(progress, total, message):
    """Forward speech progress (listen or speak) to the active browser WebSocket."""
    ws = _listen_ws
    if ws is None or message is None:
        return
    clean = re.sub(r'\033\[[0-9;]*m', '', message)
    mode = "listen" if "🎤" in clean else "speak" if "🔊" in clean else "status"
    vu = _extract_vu(clean)
    try:
        await ws.send_json({
            "type": "voice_activity",
            "mode": mode,
            "vu": vu,
            "text": clean,
            "progress": progress,
            "total": total,
        })
    except Exception:
        pass


# ── Multi-response parser ─────────────────────────────────────────

def _parse_multi_response(text, models):
    """Split multi_chat combined response into per-model segments."""
    responses = []
    current_model = None
    current_lines = []

    for line in text.split("\n"):
        stripped = line.strip()
        matched = None
        if isinstance(models, list):
            for m in models:
                if m.lower() in stripped.lower() and stripped.startswith("**"):
                    matched = m
                    break
        if matched:
            if current_model and current_lines:
                responses.append({
                    "model": current_model,
                    "text": "\n".join(current_lines).strip(),
                })
            current_model = matched
            current_lines = []
        else:
            current_lines.append(line)

    if current_model and current_lines:
        responses.append({
            "model": current_model,
            "text": "\n".join(current_lines).strip(),
        })
    if not responses:
        responses.append({
            "model": models[0] if models else "unknown",
            "text": text,
        })
    return responses


# ── WebSocket handler ──────────────────────────────────────────────

async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    print("  [ws] Client connected")

    await ws.send_json({
        "type": "connected",
        "chat_ready": chat_ready,
        "speech_ready": speech_ready,
    })

    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "message": "Invalid JSON"})
                continue

            handler = HANDLERS.get(data.get("type"))
            if handler:
                asyncio.create_task(handler(ws, data))
            else:
                await ws.send_json({"type": "error", "message": "Unknown message type"})
        elif msg.type == aiohttp.WSMsgType.ERROR:
            print(f"  [ws] Error: {ws.exception()}")

    print("  [ws] Client disconnected")
    return ws


async def handle_chat(ws, data):
    if not chat_ready:
        await ws.send_json({"type": "error", "message": "Chat server not ready"})
        return

    message = data.get("message", "")
    multi = data.get("multiChat", False)
    model = data.get("model")

    try:
        if multi and isinstance(model, list):
            async with chat_lock:
                result = await _call_tool("chat_multi_chat", {
                    "message": message,
                    "models": model,
                })
            await ws.send_json({
                "type": "multi_response",
                "responses": _parse_multi_response(result, model),
            })
        else:
            # Lock ensures configure+chat execute atomically
            async with chat_lock:
                if model and isinstance(model, str):
                    mt = _model_type(model)
                    cfg = {"model": model, "model_type": mt, "deployment": model}
                    cfg_result = await _call_tool("chat_configure", cfg)
                    print(f"  [chat] configure({cfg}) -> {cfg_result}")
                result = await _call_tool("chat_chat", {"message": message})
            print(f"  [chat] response: {result[:120]}...")
            await ws.send_json({
                "type": "response",
                "text": result,
                "model": model if isinstance(model, str) else "unknown",
            })
    except asyncio.TimeoutError:
        await ws.send_json({"type": "error", "message": "Request timed out"})
    except Exception as exc:
        await ws.send_json({"type": "error", "message": str(exc)})


async def handle_configure_chat(ws, data):
    if not chat_ready:
        await ws.send_json({"type": "error", "message": "Chat server not ready"})
        return
    args = {k: v for k, v in data.items() if k != "type"}
    result = await _call_tool("chat_configure", args)
    await ws.send_json({"type": "config_result", "server": "chat", "text": result})


async def handle_configure_speech(ws, data):
    if not speech_ready:
        await ws.send_json({"type": "error", "message": "Speech server not ready"})
        return
    args = {k: v for k, v in data.items() if k != "type"}
    result = await _call_tool("speech_configure", args)
    await ws.send_json({"type": "config_result", "server": "speech", "text": result})


async def handle_models(ws, _data):
    if not chat_ready:
        await ws.send_json({"type": "error", "message": "Chat server not ready"})
        return
    result = await _call_tool("chat_models")
    await ws.send_json({"type": "models_result", "text": result})


async def handle_voices(ws, _data):
    if not speech_ready:
        await ws.send_json({"type": "error", "message": "Speech server not ready"})
        return
    result = await _call_tool("speech_get_voices")
    await ws.send_json({"type": "voices_result", "text": result})


async def handle_speak(ws, data):
    global _listen_ws
    if not speech_listen_client:
        await ws.send_json({"type": "error", "message": "Speech server not ready"})
        return
    args = {"text": data.get("text", "")}
    if "voice" in data:
        args["voice"] = data["voice"]
    if "quality" in data:
        args["quality"] = data["quality"]
    _listen_ws = ws
    try:
        result = await asyncio.wait_for(
            speech_listen_client.call_tool("speak", args),
            timeout=120,
        )
        text = _extract_result_text(result)
        await ws.send_json({"type": "speak_result", "text": text})
    except Exception as exc:
        await ws.send_json({"type": "error", "message": str(exc)})
    finally:
        _listen_ws = None


async def handle_listen(ws, _data):
    global _listen_ws
    if not speech_listen_client:
        await ws.send_json({"type": "error", "message": "Speech server not ready"})
        return
    async with listen_lock:
        _listen_ws = ws
        try:
            result = await asyncio.wait_for(
                speech_listen_client.call_tool("listen", {
                    "mode": "streaming",
                    "silence_timeout": 5,
                    "no_speech_timeout": 15,
                }),
                timeout=120,
            )
            text = _extract_result_text(result)
            await ws.send_json({"type": "listen_result", "text": text})
        except asyncio.TimeoutError:
            await ws.send_json({"type": "error", "message": "Listen timed out"})
        except Exception as exc:
            await ws.send_json({"type": "error", "message": str(exc)})
        finally:
            _listen_ws = None


async def handle_converse(ws, _data):
    """Listen via mic and return transcribed text (converse = listen with progress)."""
    global _listen_ws
    if not speech_listen_client:
        await ws.send_json({"type": "error", "message": "Speech server not ready"})
        return
    async with listen_lock:
        _listen_ws = ws
        try:
            result = await asyncio.wait_for(
                speech_listen_client.call_tool("converse", {
                    "mode": "streaming",
                    "silence_timeout": 5,
                }),
                timeout=120,
            )
            text = _extract_result_text(result)
            await ws.send_json({"type": "converse_result", "text": text})
        except asyncio.TimeoutError:
            await ws.send_json({"type": "error", "message": "Converse timed out"})
        except Exception as exc:
            await ws.send_json({"type": "error", "message": str(exc)})
        finally:
            _listen_ws = None


async def handle_talk(ws, data):
    """Speak text then listen for reply (full-duplex talk)."""
    global _listen_ws
    if not speech_listen_client:
        await ws.send_json({"type": "error", "message": "Speech server not ready"})
        return
    args = {"text": data.get("text", "")}
    if "voice" in data:
        args["voice"] = data["voice"]
    if "quality" in data:
        args["quality"] = data["quality"]
    _listen_ws = ws
    try:
        result = await asyncio.wait_for(
            speech_listen_client.call_tool("talk", args),
            timeout=120,
        )
        text = _extract_result_text(result)
        await ws.send_json({"type": "talk_result", "text": text})
    except asyncio.TimeoutError:
        await ws.send_json({"type": "error", "message": "Talk timed out"})
    except Exception as exc:
        await ws.send_json({"type": "error", "message": str(exc)})
    finally:
        _listen_ws = None


HANDLERS = {
    "chat": handle_chat,
    "configure_chat": handle_configure_chat,
    "configure_speech": handle_configure_speech,
    "models": handle_models,
    "voices": handle_voices,
    "speak": handle_speak,
    "listen": handle_listen,
    "converse": handle_converse,
    "talk": handle_talk,
}


# ── HTTP handler ───────────────────────────────────────────────────

async def index_handler(request):
    resp = web.FileResponse(INDEX_HTML)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


# ── App lifecycle ──────────────────────────────────────────────────

async def start_proxy(app):
    global oracle, oracle_client, chat_ready, speech_ready, speech_listen_client

    print("\nBuilding FastMCP proxy...")

    oracle = FastMCP(name="The Oracle")

    # Mount chat backend
    if CHAT_SCRIPT.exists():
        try:
            chat_proxy = create_proxy(_make_transport(CHAT_PYTHON, CHAT_SCRIPT))
            oracle.mount(chat_proxy, namespace="chat")
            chat_ready = True
            print("  [chat] Mounted as chat_* namespace")
        except Exception as exc:
            print(f"  [chat] Failed to mount: {exc}")
    else:
        if not CHAT_DIR.exists():
            print(f"  [chat] Directory not found: {CHAT_DIR}")
            print(f"         Set ORACLE_CLOUD_CHAT_DIR to the cloud-chat-assistant project path")
        else:
            print(f"  [chat] Script not found: {CHAT_SCRIPT}")
            print(f"         Is cloud-chat-assistant installed? Expected {CHAT_SCRIPT.name} in {CHAT_DIR}")

    # Mount speech backend
    if SPEECH_SCRIPT.exists():
        try:
            speech_proxy = create_proxy(_make_transport(SPEECH_PYTHON, SPEECH_SCRIPT))
            oracle.mount(speech_proxy, namespace="speech")
            speech_ready = True
            print("  [speech] Mounted as speech_* namespace")
        except Exception as exc:
            print(f"  [speech] Failed to mount: {exc}")
    else:
        if not SPEECH_DIR.exists():
            print(f"  [speech] Directory not found: {SPEECH_DIR}")
            print(f"          Set ORACLE_SPEECH_DIR to the speech-to-cli project path")
        else:
            print(f"  [speech] Script not found: {SPEECH_SCRIPT}")
            print(f"          Is speech-to-cli installed? Expected {SPEECH_SCRIPT.name} in {SPEECH_DIR}")

    # Connect in-process client to the proxy
    oracle_client = Client(oracle)
    await oracle_client.__aenter__()

    # Dedicated speech client for listen — bypasses proxy to stream progress
    if speech_ready:
        try:
            listen_transport = _make_transport(SPEECH_PYTHON, SPEECH_SCRIPT)
            speech_listen_client = Client(
                listen_transport, progress_handler=_speech_progress,
            )
            await speech_listen_client.__aenter__()
            print("  [speech] Dedicated listen client with progress streaming ready")
        except Exception as exc:
            print(f"  [speech] Listen client failed: {exc}")

    # List all tools on the unified proxy
    tools = await oracle_client.list_tools()
    tool_names = [t.name for t in tools]
    print(f"\n  Oracle proxy ready — {len(tool_names)} tools:")
    for name in sorted(tool_names):
        print(f"    {name}")
    print()


async def stop_proxy(app):
    if speech_listen_client:
        await speech_listen_client.__aexit__(None, None, None)
    if oracle_client:
        await oracle_client.__aexit__(None, None, None)
    print("\nOracle proxy stopped.")


def create_app():
    app = web.Application()
    app.on_startup.append(start_proxy)
    app.on_cleanup.append(stop_proxy)
    app.router.add_get("/", index_handler)
    app.router.add_get("/ws", ws_handler)
    return app


# ── Main ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = PORT
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    print(f"""
 \u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557
 \u2551          \u2726  THE ORACLE  \u2726               \u2551
 \u2551  FastMCP Proxy + WebSocket Bridge      \u2551
 \u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d

 \u2192 http://localhost:{port}
 \u2192 ws://localhost:{port}/ws
""")

    app = create_app()
    web.run_app(app, host="0.0.0.0", port=port, print=None)
