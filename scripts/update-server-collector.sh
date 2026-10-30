#!/usr/bin/env bash
# Update the collector on the existing BCLinux ARM64 server.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
project_dir=$(pwd -P)
test -f .env && test -f data/tracker.db || { echo 'Missing .env or data/tracker.db'; exit 1; }
mounted_data=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Source}}{{end}}{{end}}' ai-tracker-collector)
mounted_env=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/.env"}}{{.Source}}{{end}}{{end}}' ai-tracker-collector)
[ "$mounted_data" = "$project_dir/data" ] && [ "$mounted_env" = "$project_dir/.env" ] || { echo 'Collector uses different mounts; stopped.'; exit 1; }
docker image inspect ai-tracker-python:3.11-arm64 >/dev/null

DOCKER_BUILDKIT=0 docker build \
  --cpu-period 100000 --cpu-quota 100000 --memory 1g --memory-swap 1g \
  --build-arg PYTHON_IMAGE=ai-tracker-python:3.11-arm64 -t ai-tracker:current .
docker exec ai-tracker-collector python -m backend.backup

previous="ai-tracker-collector-before-$(date +%Y%m%d-%H%M%S)"
docker stop -t 30 ai-tracker-collector
if ! docker rename ai-tracker-collector "$previous"; then
  docker start ai-tracker-collector
  exit 1
fi

rollback() {
  echo 'New collector failed; restoring the previous collector.'
  docker stop -t 30 ai-tracker-collector || true
  docker rm ai-tracker-collector || true
  docker rename "$previous" ai-tracker-collector
  docker start ai-tracker-collector
  exit 1
}

docker run -d --name ai-tracker-collector \
  --init=false --restart unless-stopped --stop-signal SIGINT --stop-timeout 30 \
  --cpus 0.5 --memory 768m --memory-swap 768m --pids-limit 128 \
  --security-opt seccomp=unconfined \
  --mount "type=bind,src=$project_dir/data,dst=/app/data" \
  --mount "type=bind,src=$project_dir/.env,dst=/app/.env,readonly" \
  -e DB_PATH=/app/data/tracker.db \
  --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 \
  --health-interval 30s --health-timeout 10s --health-retries 3 --health-start-period 30s \
  --health-cmd 'python -m backend.runtime collector' \
  ai-tracker:current python -m backend.collector.collection_worker || rollback

for attempt in $(seq 1 60); do
  status=$(docker inspect -f '{{.State.Status}} {{.State.Health.Status}}' ai-tracker-collector)
  [ "$status" = 'running healthy' ] && break
  sleep 2
done
[ "$status" = 'running healthy' ] || rollback
echo "Collector is healthy. Previous container retained (stopped): $previous"
echo 'Pending collection work resumes automatically; inspect stage results separately.'
