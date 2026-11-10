FROM python:3.12-slim

# openssh-client: ssh.py shells out to `ssh`.
# tbot: the ssh_config a tbot identity generates hardcodes its
# ProxyCommand as an absolute path to the `tbot` binary itself (not just
# data files) — the container needs its own copy to actually connect
# through a Teleport-routed host, matching the mounted identity.
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://goteleport.com/static/install.sh | bash -s 18.3.1

# An account for running unprivileged (compose: BEACON_RUN_AS=10001:10001, see
# the README). The image still starts as root unless told otherwise: switching the
# default would break existing ./state and ./tbot/* bind mounts owned by root.
# ssh needs a passwd entry for its uid, which is why the account exists here.
RUN useradd --system --uid 10001 --create-home beacon

WORKDIR /app

COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY backend/ backend/
COPY frontend/ frontend/
# Skills agents pull with their mesh config (backend/mesh.py).
COPY skills/ skills/

# fleet/ is mounted at runtime — it's persistent state, not part of the image.

EXPOSE 8642
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8642"]
