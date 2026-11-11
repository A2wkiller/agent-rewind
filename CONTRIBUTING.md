# Contributing to TraceGate
Thank you for helping build and improve TraceGate! We welcome contributions to our execution proxy, token firewall,
---
## 🛠️ Local Development Setup
TraceGate uses [`uv`](https://github.com/astral-sh/uv) for fast, deterministic Python virtual environment and depend
### 1. Clone & Install
```bash
git clone [https://github.com/](https://github.com/)<your-username>/tracegate.git
cd tracegate
# Create a virtual environment and install dependencies in editable mode
uv venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
uv pip install -e ".[dev]"
cp .env.example .env
# Edit .env with your local API keys (Anthropic, Gemini, OpenAI) if running live tests
pytest test_tracegate_agentic_loop.py -v
feat: A new feature (e.g., feat(mcp): add support for tool schema validation)
fix: A bug fix (e.g., fix(bridge): handle broken WebSocket reconnects)
feat/<short-description>
fix/<short-description>
docs/<short-description>
3. Run pytest test_tracegate_agentic_loop.py -v.
---
