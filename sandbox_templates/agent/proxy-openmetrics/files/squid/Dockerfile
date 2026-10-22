# Pin to bookworm explicitly. `debian:latest` has rolled past the version with
# squid 5.7, so we hold to bookworm to keep build behavior reproducible.
# Any squid 5.x/6.x is fine for the interview's forward-proxy use case.
FROM debian:bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends squid && \
    rm -rf /var/lib/apt/lists/*

COPY squid.conf /etc/squid/squid.conf

CMD ["squid", "-N", "-d", "1"]
