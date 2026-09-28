#!/usr/bin/env bash
# 関数をデプロイし、Cloud Scheduler のジョブを作成・更新する。
# GitHub Actions（.github/workflows/deploy.yml）から実行する。手元から実行してもよい。
#
# 使い方:
#   GCP_PROJECT_ID=my-project bash deploy/deploy.sh
set -euo pipefail

: "${GCP_PROJECT_ID:?GCP_PROJECT_ID を指定してください}"
cd "$(dirname "$0")"
source ./config.sh

gcloud config set project "$GCP_PROJECT_ID" >/dev/null

# シークレットを環境変数として渡す（任意のものは作成されている場合だけ）
SECRETS=""
for name in $REQUIRED_SECRETS; do
  SECRETS+="${name}=${name}:latest,"
done
for name in $OPTIONAL_SECRETS; do
  if gcloud secrets describe "$name" >/dev/null 2>&1; then
    SECRETS+="${name}=${name}:latest,"
  fi
done

echo "== 関数をデプロイ"
gcloud functions deploy "$FUNCTION_NAME" \
  --gen2 \
  --runtime=python312 \
  --region="$REGION" \
  --source=../src \
  --entry-point=run \
  --trigger-http \
  --no-allow-unauthenticated \
  --service-account="$RUNTIME_SA" \
  --build-service-account="projects/${GCP_PROJECT_ID}/serviceAccounts/${BUILD_SA}" \
  --docker-repository="projects/${GCP_PROJECT_ID}/locations/${REGION}/repositories/${DOCKER_REPO}" \
  --set-secrets="${SECRETS%,}" \
  --max-instances=1 \
  --concurrency=1 \
  --memory=256Mi \
  --timeout=60s

bash ./scheduler.sh
