FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://goteleport.com/static/install.sh | bash -s 18.3.1

# Same account as the beacon image, so both can share ./tbot/data when run unprivileged
# (compose: BEACON_RUN_AS=10001:10001). Default remains root; see the README.
RUN useradd --system --uid 10001 --create-home beacon

ENTRYPOINT ["tbot"]
