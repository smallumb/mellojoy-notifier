# デプロイ関連スクリプトの共通設定（bootstrap_gcp.sh / deploy.sh / scheduler.sh から読み込む）
# GCP_PROJECT_ID は環境変数で渡す

REGION="asia-northeast1"
FUNCTION_NAME="mellojoy-notifier"
GITHUB_REPO="smallumb/mellojoy-notifier"

# サービスアカウント（名前 → メールアドレス）
RUNTIME_SA="mellojoy-runtime@${GCP_PROJECT_ID}.iam.gserviceaccount.com"   # 関数の実行用
BUILD_SA="mellojoy-build@${GCP_PROJECT_ID}.iam.gserviceaccount.com"       # 関数のビルド用
INVOKER_SA="mellojoy-invoker@${GCP_PROJECT_ID}.iam.gserviceaccount.com"   # Cloud Scheduler から呼び出す用
DEPLOY_SA="mellojoy-deployer@${GCP_PROJECT_ID}.iam.gserviceaccount.com"   # GitHub Actions からデプロイする用

# ビルドしたイメージの置き場（古いものは自動で消す）
DOCKER_REPO="mellojoy-notifier"

# Workload Identity 連携（GitHub Actions → GCP）
WIF_POOL="github"
WIF_PROVIDER="github"

# 関数に環境変数として渡すシークレット（SLACK_WEBHOOK_URL は作成されている場合だけ渡す）
REQUIRED_SECRETS="LINE_CHANNEL_ACCESS_TOKEN LINE_USER_ID"
OPTIONAL_SECRETS="SLACK_WEBHOOK_URL"
