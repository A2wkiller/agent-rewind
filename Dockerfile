FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
# Enable bytecode compilation and system environment installation
ENV UV_COMPILE_BYTECODE=1 \
   UV_SYSTEM_PYTHON=1 \
   PYTHONUNBUFFERED=1 \
   TRACEGATE_DB_PATH=/data/tracegate_dag.sqlite3 \
   TRACEGATE_USD_TO_INR=86.50
# Install dependencies first for optimal layer caching
COPY pyproject.toml ./
COPY tracegate_mcp.py tracegate_bridge.py tracegate_cli.py test_tracegate_agentic_loop.py index.html ./
RUN uv pip install --no-cache-dir -e ".[dev]" && \
   mkdir -p /data
# Run the automated verification suite during image build so broken builds never ship
RUN pytest test_tracegate_agentic_loop.py -v
EXPOSE 8000 8001
ENTRYPOINT ["tracegate"]
CMD ["up", "--host", "0.0.0.0", "--port", "8000"]
