#!/usr/bin/env bash
# GCP の初期設定。最初に1回だけ Cloud Shell（または gcloud にログイン済みの端末）で実行する。
# 何度実行しても同じ状態になるように書いているので、途中で失敗したらそのまま再実行してよい。
#
# 使い方:
#   GCP_PROJECT_ID=my-project bash deploy/bootstrap_gcp.sh
#
# 前提:
#   - プロジェクトが作成済みで、請求先アカウント（billing）が紐づいている
#   - 実行する人がプロジェクトのオーナー
set -euo pipefail

: "${GCP_PROJECT_ID:?GCP_PROJECT_ID を指定してください（例: GCP_PROJECT_ID=my-project bash deploy/bootstrap_gcp.sh）}"
cd "$(dirname "$0")"
source ./config.sh

gcloud config set project "$GCP_PROJECT_ID" >/dev/null
PROJECT_NUMBER="$(gcloud projects describe "$GCP_PROJECT_ID" --format='value(projectNumber)')"

echo "== API を有効化"
gcloud services enable \
  cloudfunctions.googleapis.com run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com cloudscheduler.googleapis.com firestore.googleapis.com \
  secretmanager.googleapis.com iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com

echo "== Firestore（ネイティブモード）"
if ! gcloud firestore databases describe --database='(default)' >/dev/null 2>&1; then
  gcloud firestore databases create --location="$REGION" --type=firestore-native
fi

echo "== イメージの置き場（新しい2つだけ残す）"
if ! gcloud artifacts repositories describe "$DOCKER_REPO" --location="$REGION" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$DOCKER_REPO" --location="$REGION" --repository-format=docker
fi
POLICY_FILE="$(mktemp)"
cat > "$POLICY_FILE" <<'JSON'
[
  {"name": "keep-latest", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 2}},
  {"name": "delete-others", "action": {"type": "Delete"}, "condition": {"tagState": "any"}}
]
JSON
gcloud artifacts repositories set-cleanup-policies "$DOCKER_REPO" --location="$REGION" \
  --policy="$POLICY_FILE" --no-dry-run >/dev/null
rm -f "$POLICY_FILE"

echo "== サービスアカウント"
create_sa() {
  local email="$1" display="$2"
  local name="${email%%@*}"
  if ! gcloud iam service-accounts describe "$email" >/dev/null 2>&1; then
    gcloud iam service-accounts create "$name" --display-name="$display"
  fi
}
create_sa "$RUNTIME_SA" "mellojoy-notifier 実行用"
create_sa "$BUILD_SA"   "mellojoy-notifier ビルド用"
create_sa "$INVOKER_SA" "mellojoy-notifier 呼び出し用（Cloud Scheduler）"
create_sa "$DEPLOY_SA"  "mellojoy-notifier デプロイ用（GitHub Actions）"

grant_project() {
  gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" \
    --member="serviceAccount:$1" --role="$2" --condition=None >/dev/null
}
grant_act_as() {
  gcloud iam service-accounts add-iam-policy-binding "$1" \
    --member="serviceAccount:$2" --role=roles/iam.serviceAccountUser >/dev/null
}

# 実行用: Firestore の読み書き・シークレットの読み取り
grant_project "$RUNTIME_SA" roles/datastore.user
grant_project "$RUNTIME_SA" roles/secretmanager.secretAccessor
# ビルド用: Cloud Build でのビルドとイメージの書き込み
grant_project "$BUILD_SA" roles/cloudbuild.builds.builder
# 呼び出し用: 関数（裏側の Cloud Run サービス）を呼び出す
grant_project "$INVOKER_SA" roles/run.invoker
# デプロイ用: 関数のデプロイ・Scheduler の管理・シークレットの有無の確認（値は読めない）
grant_project "$DEPLOY_SA" roles/cloudfunctions.developer
grant_project "$DEPLOY_SA" roles/cloudscheduler.admin
grant_project "$DEPLOY_SA" roles/secretmanager.viewer
grant_act_as "$RUNTIME_SA" "$DEPLOY_SA"
grant_act_as "$BUILD_SA"   "$DEPLOY_SA"
grant_act_as "$INVOKER_SA" "$DEPLOY_SA"

echo "== Workload Identity 連携（GitHub Actions の main ブランチだけを許可）"
if ! gcloud iam workload-identity-pools describe "$WIF_POOL" --location=global >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "$WIF_POOL" --location=global --display-name="GitHub Actions"
fi
if ! gcloud iam workload-identity-pools providers describe "$WIF_PROVIDER" \
    --workload-identity-pool="$WIF_POOL" --location=global >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers create-oidc "$WIF_PROVIDER" \
    --workload-identity-pool="$WIF_POOL" --location=global \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
    --attribute-condition="assertion.repository=='${GITHUB_REPO}' && assertion.ref=='refs/heads/main'"
fi
gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WIF_POOL}/attribute.repository/${GITHUB_REPO}" \
  >/dev/null

echo "== シークレット（値は画面に表示されません。空のまま Enter でスキップ）"
put_secret() {
  local name="$1" value
  read -rsp "  ${name}: " value; echo
  if [ -z "$value" ]; then
    echo "    スキップしました"
    return
  fi
  if ! gcloud secrets describe "$name" >/dev/null 2>&1; then
    gcloud secrets create "$name" --replication-policy=automatic >/dev/null
  fi
  printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- >/dev/null
  echo "    登録しました"
}
for name in $REQUIRED_SECRETS $OPTIONAL_SECRETS; do
  put_secret "$name"
done
for name in $REQUIRED_SECRETS; do
  gcloud secrets describe "$name" >/dev/null 2>&1 || echo "  ⚠️ ${name} が未登録です。再実行して登録してください"
done

cat <<EOF

== 完了
GitHub のリポジトリ（Settings → Secrets and variables → Actions → Variables）に次の3つを登録してください:

  GCP_PROJECT_ID   = ${GCP_PROJECT_ID}
  GCP_WIF_PROVIDER = projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WIF_POOL}/providers/${WIF_PROVIDER}
  GCP_DEPLOY_SA    = ${DEPLOY_SA}

予算アラート（例: 月100円）は、コンソールの「お支払い → 予算とアラート」から作成してください:
  https://console.cloud.google.com/billing/budgets
EOF
