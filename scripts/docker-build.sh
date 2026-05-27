#!/usr/bin/env bash
# Build the Gootier image for linux/amd64 (the ECS Fargate target).
#
# Why: Apple Silicon Docker defaults to arm64. An arm64 image will fail
# to start on Fargate with a cryptic "exec format error". Always pass
# --platform linux/amd64 in the build command, never rely on host defaults.
#
# Usage:
#   ./scripts/docker-build.sh                # tags as gootier:local
#   IMAGE=gootier TAG=$(git rev-parse --short HEAD) ./scripts/docker-build.sh
set -euo pipefail

IMAGE="${IMAGE:-gootier}"
TAG="${TAG:-local}"
PLATFORM="linux/amd64"

cd "$(dirname "$0")/.."

echo "Building ${IMAGE}:${TAG} for ${PLATFORM}…"
docker buildx build \
  --platform "${PLATFORM}" \
  --load \
  -t "${IMAGE}:${TAG}" \
  .

echo
echo "Verifying platform of the loaded image:"
docker image inspect "${IMAGE}:${TAG}" --format '  os={{.Os}} arch={{.Architecture}}'
echo "Done."
