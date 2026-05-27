#!/usr/bin/env bash
# Build and push the Gootier image to ECR (always linux/amd64).
#
# Usage:
#   AWS_REGION=us-east-1 \
#   ECR_REPO=123456789012.dkr.ecr.us-east-1.amazonaws.com/gootier \
#   TAG=$(git rev-parse --short HEAD) \
#   ./scripts/ecr-push.sh
#
# If TAG is unset, the current git SHA is used. The image is also tagged
# :latest. Prerequisites: AWS CLI configured, `aws ecr get-login-password`
# permission, and a buildx-enabled Docker daemon (Docker Desktop is fine).
set -euo pipefail

: "${AWS_REGION:?AWS_REGION is required}"
: "${ECR_REPO:?ECR_REPO is required (full URI, no tag)}"
TAG="${TAG:-$(git rev-parse --short HEAD)}"
PLATFORM="linux/amd64"

cd "$(dirname "$0")/.."

echo "Logging in to ECR (${AWS_REGION})…"
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${ECR_REPO%/*}"

echo "Building + pushing ${ECR_REPO}:${TAG} (${PLATFORM})…"
docker buildx build \
  --platform "${PLATFORM}" \
  -t "${ECR_REPO}:${TAG}" \
  -t "${ECR_REPO}:latest" \
  --push \
  .

echo "Pushed ${ECR_REPO}:${TAG} (also :latest)."
