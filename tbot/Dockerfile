FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://goteleport.com/static/install.sh | bash -s 18.3.1

ENTRYPOINT ["tbot"]
