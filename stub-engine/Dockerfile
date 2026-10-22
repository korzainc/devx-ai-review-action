# Stub engine image. Stands in for ghcr.io/korzainc/ai-review while the real
# engine is built, so the action's adapter can be tested end to end.
FROM alpine:3.22

RUN apk add --no-cache bash git jq curl

COPY review.sh /usr/local/bin/review.sh

# The action runs the container as the runner UID, which has no passwd entry.
# Point HOME somewhere writable so git and curl do not trip over it.
ENV HOME=/tmp

# The action overrides this with --user at runtime; the default just keeps
# standalone runs off root. Numeric so hosts without the name can resolve it
# (65534 is nobody).
USER 65534:65534

ENTRYPOINT ["bash", "/usr/local/bin/review.sh"]
