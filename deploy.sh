#!/usr/bin/env bash
# One-command install / update of AS4PUR with Docker (README, "Installation with Docker").
#
#   ./deploy.sh            build, (re)start, wait until healthy, offer to create the first admin
#   ./deploy.sh --logs     follow the app logs
#   ./deploy.sh --stop     stop the containers (data is kept in the as4pur-data volume)
#
# Safe to run again at any time: an existing .env and certificate are never overwritten.
set -euo pipefail
cd "$(dirname "$0")"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ---- Docker and compose ---------------------------------------------------------
command -v docker >/dev/null 2>&1 || die "Docker is not installed. On Ubuntu: curl -fsSL https://get.docker.com | sudo sh
Then run this script again (with sudo, or after adding yourself to the docker group)."
docker info >/dev/null 2>&1 || die "Cannot talk to the Docker daemon. Start it (sudo systemctl start docker),
or run this script with sudo, or add your user to the docker group and log in again."
if docker compose version >/dev/null 2>&1; then COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then COMPOSE=(docker-compose)
else die "Docker Compose is missing. On Ubuntu: sudo apt install docker-compose-plugin"; fi

case "${1:-}" in
  --logs) exec "${COMPOSE[@]}" logs -f app ;;
  --stop) "${COMPOSE[@]}" down; say "Stopped. Data is kept in the as4pur-data volume."; exit 0 ;;
  "") ;;
  *) die "Unknown option: $1 (use --logs or --stop, or no option to deploy)" ;;
esac

# ---- .env -------------------------------------------------------------------------
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  say "Created .env from .env.example."
fi
if grep -qE '^AS4PUR_INVITE_ALLOWED_DOMAINS=(example\.com)?\s*$' .env; then
  warn "AS4PUR_INVITE_ALLOWED_DOMAINS in .env is still empty or the example.com placeholder.
         Set it to your email domain(s) before inviting users, then run ./deploy.sh again."
fi
if grep -qE '^WEB_CONCURRENCY=' .env; then
  warn "Remove WEB_CONCURRENCY from .env: AS4PUR runs as one process (the container pins it to 1)."
fi

env_value() {  # value from the shell environment, else from .env, else the default
  local name=$1 default=$2 v="${!1:-}"
  [ -n "$v" ] || v=$(grep -E "^${name}=" .env | tail -n1 | cut -d= -f2- | tr -d '\r' || true)
  echo "${v:-$default}"
}
HTTPS_PORT=$(env_value HTTPS_PORT 443)

# ---- TLS certificate ----------------------------------------------------------------
CERT_DIR=deploy/docker/certs
if [ ! -f "$CERT_DIR/fullchain.pem" ] || [ ! -f "$CERT_DIR/privkey.pem" ]; then
  CERT_HOST=$(env_value CERT_HOST "$(hostname -f 2>/dev/null || hostname)")
  say "No certificate in $CERT_DIR - creating a self-signed one for '$CERT_HOST' (valid 825 days)."
  say "To use your own (internal CA) certificate instead, put fullchain.pem and privkey.pem there and re-run."
  mkdir -p "$CERT_DIR"
  cert_args() {  # $1 = directory the files are written to
    echo req -x509 -nodes -newkey rsa:2048 -days 825 \
      -subj "/CN=$CERT_HOST" -addext "subjectAltName=DNS:$CERT_HOST,DNS:localhost,IP:127.0.0.1" \
      -keyout "$1/privkey.pem" -out "$1/fullchain.pem"
  }
  if command -v openssl >/dev/null 2>&1; then
    # shellcheck disable=SC2046  # word splitting is intended: no argument contains a space
    openssl $(cert_args "$CERT_DIR") >/dev/null 2>&1
  else                            # no openssl on the host: run it in a throwaway container
    # shellcheck disable=SC2046
    docker run --rm -v "$PWD/$CERT_DIR:/certs" alpine:3 \
      sh -c 'apk add --no-cache -q openssl >/dev/null && exec openssl "$@"' sh $(cert_args /certs) >/dev/null 2>&1
  fi
  [ -s "$CERT_DIR/privkey.pem" ] || die "Could not create the certificate in $CERT_DIR."
  chmod 600 "$CERT_DIR/privkey.pem"
fi

# ---- build and start ----------------------------------------------------------------
say "Building and starting containers..."
"${COMPOSE[@]}" up -d --build --remove-orphans

say "Waiting for the app to become healthy..."
for _ in $(seq 1 45); do
  status=$(docker inspect -f '{{.State.Health.Status}}' as4pur-app 2>/dev/null || echo starting)
  [ "$status" = healthy ] && break
  if [ "$status" = unhealthy ]; then break; fi
  sleep 2
done
if [ "${status:-}" != healthy ]; then
  "${COMPOSE[@]}" logs --tail 40 app >&2 || true
  die "The app did not become healthy (status: ${status:-unknown}). See the log lines above."
fi
docker image prune -f >/dev/null 2>&1 || true

# ---- first administrator --------------------------------------------------------------
users=$("${COMPOSE[@]}" exec -T app python -c \
  "from app.db import SessionLocal; from app.models import User; s = SessionLocal(); print(s.query(User).count())" \
  2>/dev/null | tr -d '\r' || echo "?")
if [ "$users" = "0" ]; then
  if [ -t 0 ]; then
    say "No accounts exist yet. Create the first administrator now."
    read -r -p "Admin username [admin]: " admin_user
    "${COMPOSE[@]}" exec app python scripts/create_user.py "${admin_user:-admin}" admin
  else
    warn "No accounts exist yet. Create the first administrator with:
         ${COMPOSE[*]} exec app python scripts/create_user.py <username> admin"
  fi
fi

PORT_SUFFIX=$([ "$HTTPS_PORT" = 443 ] && echo "" || echo ":$HTTPS_PORT")
echo
say "AS4PUR is running:  https://$(hostname -f 2>/dev/null || hostname)$PORT_SUFFIX"
echo "    Logs:     ./deploy.sh --logs"
echo "    Stop:     ./deploy.sh --stop"
echo "    Update:   git pull && ./deploy.sh"
