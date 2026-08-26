FROM python:3.12-slim

# openssh-client: ssh.py shells out to `ssh`.
# tbot: the ssh_config a tbot identity generates hardcodes its
# ProxyCommand as an absolute path to the `tbot` binary itself (not just
# data files) — the container needs its own copy to actually connect
# through a Teleport-routed host, matching the mounted identity.
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://goteleport.com/static/install.sh | bash -s 18.3.1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/

# fleet/ is mounted at runtime — it's persistent state, not part of the image.

EXPOSE 8642
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8642"]
