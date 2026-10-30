#!/usr/bin/env bash
# Update only the web container on the existing BCLinux ARM64 server.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
project_dir=$(pwd -P)
test -f .env && test -f data/tracker.db || { echo 'Missing .env or data/tracker.db'; exit 1; }
mounted_data=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Source}}{{end}}{{end}}' ai-tracker-web)
[ "$mounted_data" = "$project_dir/data" ] || { echo 'Web container uses a different data directory; stopped.'; exit 1; }
docker image inspect ai-tracker-python:3.11-arm64 >/dev/null

# Keep the current website running while building. No daemon or worker restart.
DOCKER_BUILDKIT=0 docker build \
  --cpu-period 100000 --cpu-quota 100000 --memory 1g --memory-swap 1g \
  --build-arg PYTHON_IMAGE=ai-tracker-python:3.11-arm64 -t ai-tracker:current .
docker exec ai-tracker-web python -m backend.backup

previous="ai-tracker-web-before-$(date +%Y%m%d-%H%M%S)"
docker stop -t 30 ai-tracker-web
if ! docker rename ai-tracker-web "$previous"; then
  docker start ai-tracker-web
  exit 1
fi

rollback() {
  echo 'New web container failed; restoring the previous web container.'
  docker logs --tail 60 ai-tracker-web || true
  docker stop -t 30 ai-tracker-web || true
  docker rm ai-tracker-web || true
  docker rename "$previous" ai-tracker-web
  docker start ai-tracker-web
  exit 1
}

docker run -d --name ai-tracker-web \
  --init=false --restart unless-stopped --stop-signal SIGINT --stop-timeout 30 \
  --cpus 0.5 --memory 768m --memory-swap 768m --pids-limit 128 \
  --security-opt seccomp=unconfined \
  --mount "type=bind,src=$project_dir/data,dst=/app/data" \
  --mount "type=bind,src=$project_dir/.env,dst=/app/.env,readonly" \
  -e DB_PATH=/app/data/tracker.db -e ADMIN_COOKIE_SECURE=false \
  -p 0.0.0.0:29011:8000 \
  --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 \
  --health-interval 30s --health-timeout 10s --health-retries 3 --health-start-period 60s \
  --health-cmd "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=5)\"" \
  ai-tracker:current || rollback

for attempt in $(seq 1 60); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' ai-tracker-web)" = healthy ] && break
  sleep 2
done
[ "$(docker inspect -f '{{.State.Health.Status}}' ai-tracker-web)" = healthy ] || rollback
docker port ai-tracker-web
echo "Web is healthy. Previous container retained (stopped): $previous"
echo 'Open http://120.222.7.246:29011 and refresh. News images will be cached on first view.'
