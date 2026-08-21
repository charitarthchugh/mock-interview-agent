# The app, its dependencies, and the browser Python assets. Deliberately not
# the model weights: Nemotron is served by its own vLLM container via
# sparkrun, and Kokoro and Whisper are mounted from the host Hugging Face
# cache. Weights are gigabytes, they change on their own schedule, and baking
# them in would make every code change a multi-gigabyte rebuild.

FROM docker.io/library/python:3.12-slim

# bubblewrap sandboxes the generated reference solution that computes expected
# outputs for test cases. tini reaps the sandbox's orphans.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bubblewrap tini ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# Dependencies resolve from the lockfile in their own layer, so editing the
# app does not reinstall torch.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project

COPY app ./app
COPY static ./static
COPY scripts ./scripts
RUN uv sync --locked

# Written by the app at runtime; a volume normally covers this.
RUN mkdir -p /app/sessions

ENV BACKEND=omni \
    TTS=kokoro \
    TTS_DEVICE=cuda \
    OMNI_HOST=http://localhost:8001 \
    OLLAMA_HOST=http://localhost:11434 \
    HF_HOME=/models/huggingface

EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
