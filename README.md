# The Oracle

FastMCP proxy server with a crystal ball-themed web UI. Bridges two MCP backend services into a unified interface:

- **cloud-chat-assistant** — Multi-cloud LLM chat (Claude, Llama, Phi, Gemini, etc.)
- **speech-to-cli** — Speech recognition and text-to-speech via Azure Speech Services

## Prerequisites

- Python 3.12+
- Sibling projects installed and working:
  - `../cloud-chat-assistant/` with venv and configured credentials
  - `../speech-to-cli/` with Azure Speech key configured

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
./venv/bin/python3 server.py              # http://localhost:8778
./venv/bin/python3 server.py --port 9000  # custom port
```

Open `http://localhost:8778` in a browser to access the crystal ball UI.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ORACLE_CLOUD_CHAT_DIR` | `~/Projects/cloud-chat-assistant` | Path to cloud-chat-assistant project |
| `ORACLE_SPEECH_DIR` | `~/Projects/speech-to-cli` | Path to speech-to-cli project |
| `ORACLE_SPEECH_PYTHON` | `/usr/bin/python3` | Python binary for speech-to-cli |

## Architecture

```
Browser (index.html)
  -> WebSocket -> server.py :8778
    -> FastMCP proxy
      -> cloud-chat-assistant (stdio MCP, namespaced as chat_*)
      -> speech-to-cli (stdio MCP, namespaced as speech_*)
```

The proxy mounts both backends as namespaced tools. The WebSocket bridge serializes tool calls from the browser and streams responses back in real time. Speech operations include progress events for live VU meter visualization.

## Ecosystem

The Oracle is the web frontend for a four-project voice AI system:

| Project | Role |
|---------|------|
| [speech-to-cli](https://github.com/jphein/speech-to-cli) | Audio engine — STT, TTS, VAD, recorder |
| [cloud-chat-assistant](https://github.com/jphein/cloud-chat-assistant) | Multi-cloud LLM provider |
| [gnome-speaks](https://github.com/jphein/gnome-speaks) | GNOME Shell extension — desktop voice UI |
| **the-oracle** (this) | Web frontend — proxies both MCP servers |

The Oracle mounts both backend MCP servers as namespaced tools (`speech_*` and `chat_*`), letting the browser UI access speech and LLM capabilities through a single WebSocket connection.

## Interaction Modes

| Mode | What it does | Tools used |
|------|-------------|------------|
| **Cast** | Text chat — send a message, get a streaming LLM response | `chat_chat` |
| **Speak** | TTS playback — synthesize text and play it aloud | `speech_speak` |
| **Listen** | Speech-to-text — record from mic, return transcription | `speech_listen` |
| **Converse** | Listen then auto-speak — record your voice, send to LLM, speak the response | `speech_listen` → `chat_chat` → `speech_speak` |
| **Talk** | Full-duplex voice — speak a prompt aloud and immediately listen for the user's reply in one call | `speech_talk` |

### Talk vs Converse

- **Converse** is a multi-step workflow: listen → LLM → speak. Three separate tool calls chained together.
- **Talk** is a single atomic operation: speak text + listen for reply. The TTS→STT handoff happens inside the speech engine with no round-trip back to the browser.

### Half-Duplex vs Full-Duplex

This is an audio routing setting, not an interaction mode:

- **Full duplex** (headphones): TTS and STT overlap — the recorder prewarms during TTS so listening starts immediately.
- **Half duplex** (speakers): TTS must finish before the mic opens to avoid transcribing speaker output. Set automatically based on audio output device.

The `half_duplex` config in `~/.config/speech-to-cli/config.json` controls this (`"auto"`, `"true"`, or `"false"`).
