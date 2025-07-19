FROM docker.io/python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv /uv /uvx /bin/
RUN mkdir /app
WORKDIR /app
COPY requirements.txt .
RUN uv venv --no-python-downloads && uv pip install --no-cache --requirement requirements.txt && rm requirements.txt
RUN uv run playwright install --with-deps chromium chromium-headless-shell
RUN apt update && apt install -y xauth x11-apps && rm -rf /var/lib/apt/lists/*
COPY . .
ENTRYPOINT ["xvfb-run", "uv", "run", "web_server.py"]