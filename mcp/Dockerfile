FROM python:3.12-slim

WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY server.py .
COPY auth.py .

# Stateless and no bind mounts, so nothing needs root.
RUN useradd --system --uid 10001 --no-create-home mcp
USER mcp

EXPOSE 8643
CMD ["python", "server.py"]
