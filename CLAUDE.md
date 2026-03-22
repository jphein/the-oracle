<!-- claude-md-version: a7d06d6 | updated: 2026-03-22 -->
# The Oracle

FastMCP proxy server + crystal ball web frontend. Bridges cloud-chat-assistant (multi-cloud LLM) and speech-to-cli (Azure STT/TTS) into a unified WebSocket interface.

## Quick Start

```bash
source venv/bin/activate
python3 server.py              # http://localhost:8778
python3 server.py --port 9000  # custom port
```

## Architecture

- `server.py` (549 lines) -- aiohttp server: FastMCP proxy, WebSocket bridge, model routing, VU progress forwarding
- `index.html` (1880 lines) -- Single-file frontend: CSS + HTML + JS. Crystal ball UI with starfield, chat, voice controls, 3-page spellbook settings
- Port 8778, WebSocket at `/ws`, static HTML at `/`
- Binds `0.0.0.0` intentionally -- accessed from other machines on the LAN (do not change to localhost)

### Backend Dependencies

Requires sibling projects to be installed with working venvs:
- `~/Projects/cloud-chat-assistant/` -- LLM chat (configurable via `ORACLE_CLOUD_CHAT_DIR`)
- `~/Projects/speech-to-cli/` -- STT/TTS (configurable via `ORACLE_SPEECH_DIR`)

### Data Flow

```
Browser <-> WebSocket <-> server.py <-> FastMCP proxy
                                          |-> chat_* namespace (cloud-chat-assistant stdio)
                                          |-> speech_* namespace (speech-to-cli stdio)
                                          |-> dedicated listen client (progress streaming)
```

## Development

- **No build step** -- edit index.html directly, refresh browser
- **No tests** -- validate by running server and testing in browser
- **Python venv**: `./venv/` with Python 3.12, deps in `requirements.txt`
- Key dep: `fastmcp==3.1.1` (proxy + client), `aiohttp` (HTTP + WebSocket server)

### Model Configuration

Model type detection in `server.py` controls which backend handles each model:
- `SERVERLESS_MODELS` -- Azure AI serverless
- `GOOGLE_MODELS` -- Gemini via Vertex AI
- `BEDROCK_MODELS` -- Claude/Nova/Llama via AWS
- `PUTER_MODELS` -- Free tier via puter.com API

When adding new models, add them to the appropriate set in server.py AND to the `MODELS` object in index.html.

### WebSocket Message Types

Handler dispatch table in `server.py` (`HANDLERS` dict):
- `chat` -- send message to LLM (supports multi-model)
- `configure_chat` / `configure_speech` -- update backend credentials
- `models` / `voices` -- list available models/voices
- `speak` / `listen` / `converse` / `talk` -- voice interaction modes

### Frontend Theming

Fantasy/mystic aesthetic: crystal ball, ancient gold, cyan glow, purple mist. Fonts: Cinzel Decorative (titles), Crimson Pro (body). When modifying UI, maintain the dark mystic color palette (CSS custom properties in `:root`).

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ORACLE_CLOUD_CHAT_DIR` | `~/Projects/cloud-chat-assistant` | Cloud chat project path |
| `ORACLE_SPEECH_DIR` | `~/Projects/speech-to-cli` | Speech project path |
| `ORACLE_SPEECH_PYTHON` | `/usr/bin/python3` | Python for speech subprocess |

## Logs

- MCP subprocess stderr: `/tmp/oracle-mcp-stderr.log`
- Server stdout: connection events, tool calls, config changes
