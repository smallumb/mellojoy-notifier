#!/usr/bin/env bash
# Cloud Scheduler のジョブを「なければ作る・あれば更新する」。deploy.sh から呼ばれる。
# 時刻は日本時間（Asia/Tokyo）で書く。
set -euo pipefail

: "${GCP_PROJECT_ID:?GCP_PROJECT_ID を指定してください}"
cd "$(dirname "$0")"
source ./config.sh

URL="$(gcloud functions describe "$FUNCTION_NAME" --gen2 --region="$REGION" --format='value(serviceConfig.uri)')"

# ジョブ名 と スケジュール
# 12:00・12:10 は10分ごとのジョブが担当するため、毎分側からは除いて重複起動を避ける
JOBS=(
  "mellojoy-every-10min|*/10 * * * *"   # 通常時：10分ごと
  "mellojoy-noon|1-9 12 * * *"          # 12:01〜12:09：毎分
)

for job in "${JOBS[@]}"; do
  name="${job%%|*}"
  schedule="${job#*|}"
  if gcloud scheduler jobs describe "$name" --location="$REGION" >/dev/null 2>&1; then
    action=update
  else
    action=create
  fi
  echo "== ジョブ ${name}（${schedule}）を ${action}"
  gcloud scheduler jobs "$action" http "$name" \
    --location="$REGION" \
    --schedule="$schedule" \
    --time-zone="Asia/Tokyo" \
    --uri="$URL" \
    --http-method=POST \
    --oidc-service-account-email="$INVOKER_SA" \
    --oidc-token-audience="$URL" \
    --attempt-deadline=90s \
    --max-retry-attempts=0 >/dev/null
done
