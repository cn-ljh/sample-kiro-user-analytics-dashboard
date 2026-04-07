#!/bin/bash
# deploy.sh - Deploy Kiro User Analytics Dashboard
# Usage: ./deploy.sh [build|deploy|update|crawler|health|all]
#
# Prerequisites:
#   - AWS CLI configured with appropriate permissions
#   - Docker with buildx support
#   - Set environment variables or create .env file (see .env.example)
#
# Architecture: ECS Fargate + ALB + Glue/Athena
# NOTE: App Runner does NOT support WebSocket, which Streamlit requires.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Load .env if present ──
if [ -f "${SCRIPT_DIR}/.env" ]; then
    set -a; source "${SCRIPT_DIR}/.env"; set +a
fi

# ── Configuration (override via environment or .env) ──
PROJECT_NAME="${PROJECT_NAME:-kiro-user-report-dashboard}"
AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
ECR_REPO="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${PROJECT_NAME}"
IMAGE_URI="${ECR_REPO}:latest"
STACK_NAME="${STACK_NAME:-${PROJECT_NAME}}"
TEMPLATE_FILE="${SCRIPT_DIR}/cloudformation/template.yaml"

# Required for CloudFormation deploy
S3_DATA_PATH="${S3_DATA_PATH:?Set S3_DATA_PATH (e.g. my-bucket/prefix)}"
VPC_ID="${VPC_ID:?Set VPC_ID}"
SUBNET_IDS="${SUBNET_IDS:?Set SUBNET_IDS (comma-separated)}"
DOMAIN_NAME="${DOMAIN_NAME:?Set DOMAIN_NAME (e.g. kiro.example.com)}"
CERTIFICATE_ARN="${CERTIFICATE_ARN:?Set CERTIFICATE_ARN (ACM certificate ARN)}"
HOSTED_ZONE_ID="${HOSTED_ZONE_ID:-}"
IDENTITY_STORE_ID="${IDENTITY_STORE_ID:-}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[$(date +%H:%M:%S)]${NC} $1"; }
warn() { echo -e "${YELLOW}[$(date +%H:%M:%S)] WARN:${NC} $1"; }
err() { echo -e "${RED}[$(date +%H:%M:%S)] ERROR:${NC} $1"; exit 1; }

# ── ECR Login ──
ecr_login() {
    log "Logging into ECR..."
    aws ecr get-login-password --region "${AWS_REGION}" | \
        docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com" 2>&1 | tail -1
}

# ── Build & Push Docker Image ──
build_image() {
    log "Building and pushing Docker image (linux/amd64)..."
    cd "${SCRIPT_DIR}/app"

    # Ensure ECR repo exists
    aws ecr describe-repositories --repository-names "${PROJECT_NAME}" --region "${AWS_REGION}" >/dev/null 2>&1 || \
        aws ecr create-repository --repository-name "${PROJECT_NAME}" --region "${AWS_REGION}" --image-scanning-configuration scanOnPush=true

    ecr_login

    # Build amd64 image (required for Fargate, even if building on ARM64 host)
    docker buildx build --platform linux/amd64 \
        -t "${IMAGE_URI}" \
        --push .

    cd "${SCRIPT_DIR}"
    log "Image pushed: ${IMAGE_URI}"
}

# ── Deploy CloudFormation Stack ──
deploy_stack() {
    log "Validating CloudFormation template..."
    aws cloudformation validate-template --template-body "file://${TEMPLATE_FILE}" --region "${AWS_REGION}" >/dev/null

    PARAMS="ParameterKey=AwsAccountId,ParameterValue=${AWS_ACCOUNT_ID}"
    PARAMS="${PARAMS} ParameterKey=S3DataPath,ParameterValue=${S3_DATA_PATH}"
    PARAMS="${PARAMS} ParameterKey=ImageUri,ParameterValue=${IMAGE_URI}"
    PARAMS="${PARAMS} ParameterKey=VpcId,ParameterValue=${VPC_ID}"
    PARAMS="${PARAMS} ParameterKey=SubnetIds,ParameterValue=\"${SUBNET_IDS}\""
    PARAMS="${PARAMS} ParameterKey=DomainName,ParameterValue=${DOMAIN_NAME}"
    PARAMS="${PARAMS} ParameterKey=CertificateArn,ParameterValue=${CERTIFICATE_ARN}"
    if [ -n "${HOSTED_ZONE_ID}" ]; then
        PARAMS="${PARAMS} ParameterKey=HostedZoneId,ParameterValue=${HOSTED_ZONE_ID}"
    fi
    if [ -n "${IDENTITY_STORE_ID}" ]; then
        PARAMS="${PARAMS} ParameterKey=IdentityStoreId,ParameterValue=${IDENTITY_STORE_ID}"
    fi

    # Check if stack exists
    if aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${AWS_REGION}" >/dev/null 2>&1; then
        log "Updating existing stack: ${STACK_NAME}..."
        eval aws cloudformation update-stack \
            --stack-name "${STACK_NAME}" \
            --template-body "file://${TEMPLATE_FILE}" \
            --parameters ${PARAMS} \
            --capabilities CAPABILITY_NAMED_IAM \
            --region "${AWS_REGION}" || {
                warn "No updates to be performed (stack is current)"
                return 0
            }
        log "Waiting for stack update..."
        aws cloudformation wait stack-update-complete --stack-name "${STACK_NAME}" --region "${AWS_REGION}"
    else
        log "Creating new stack: ${STACK_NAME}..."
        eval aws cloudformation create-stack \
            --stack-name "${STACK_NAME}" \
            --template-body "file://${TEMPLATE_FILE}" \
            --parameters ${PARAMS} \
            --capabilities CAPABILITY_NAMED_IAM \
            --region "${AWS_REGION}"
        log "Waiting for stack creation..."
        aws cloudformation wait stack-create-complete --stack-name "${STACK_NAME}" --region "${AWS_REGION}"
    fi

    log "Stack outputs:"
    aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${AWS_REGION}" \
        --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' --output table
}

# ── Force ECS Redeployment (after image update) ──
update_service() {
    log "Forcing ECS service redeployment..."
    aws ecs update-service \
        --cluster "${PROJECT_NAME}" \
        --service "${PROJECT_NAME}" \
        --force-new-deployment \
        --region "${AWS_REGION}" \
        --query 'service.deployments[0].status' --output text

    log "Waiting for ECS service to stabilize..."
    aws ecs wait services-stable \
        --cluster "${PROJECT_NAME}" \
        --services "${PROJECT_NAME}" \
        --region "${AWS_REGION}"
    log "ECS service stable ✅"
}

# ── Run Glue Crawler ──
run_crawler() {
    CRAWLER_NAME="${PROJECT_NAME}-crawler"
    log "Starting Glue Crawler: ${CRAWLER_NAME}..."
    aws glue start-crawler --name "${CRAWLER_NAME}" --region "${AWS_REGION}" 2>/dev/null || {
        warn "Crawler already running or not found"
        return 0
    }
    while true; do
        STATE=$(aws glue get-crawler --name "${CRAWLER_NAME}" --region "${AWS_REGION}" --query 'Crawler.State' --output text)
        if [ "$STATE" = "READY" ]; then break; fi
        echo -n "."
        sleep 10
    done
    echo ""
    RESULT=$(aws glue get-crawler --name "${CRAWLER_NAME}" --region "${AWS_REGION}" --query 'Crawler.LastCrawl.Status' --output text)
    log "Crawler result: ${RESULT}"
}

# ── Grant Lake Formation Permissions ──
grant_lakeformation() {
    DB_NAME="${GLUE_DATABASE_NAME:-kiro-user-report}"
    CRAWLER_ROLE="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${PROJECT_NAME}-glue-crawler-role"
    TASK_ROLE="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${PROJECT_NAME}-ecs-task-role"

    log "Granting Lake Formation permissions..."
    for ROLE in "$CRAWLER_ROLE" "$TASK_ROLE"; do
        ROLE_NAME=$(basename "$ROLE")
        aws lakeformation grant-permissions --region "${AWS_REGION}" \
            --principal DataLakePrincipalIdentifier="$ROLE" \
            --permissions ALL \
            --resource "{\"Database\": {\"Name\": \"${DB_NAME}\"}}" 2>/dev/null || true
        aws lakeformation grant-permissions --region "${AWS_REGION}" \
            --principal DataLakePrincipalIdentifier="$ROLE" \
            --permissions ALL \
            --resource "{\"Table\": {\"DatabaseName\": \"${DB_NAME}\", \"TableWildcard\": {}}}" 2>/dev/null || true
        log "  ✅ ${ROLE_NAME}"
    done
}

# ── Health Check ──
health_check() {
    URL=$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${AWS_REGION}" \
        --query 'Stacks[0].Outputs[?OutputKey==`DashboardUrl`].OutputValue' --output text 2>/dev/null)

    if [ -z "$URL" ]; then
        err "Could not determine Dashboard URL"
    fi

    log "Health check: ${URL}/_stcore/health"
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${URL}/_stcore/health" 2>/dev/null || echo "000")
    if [ "$HTTP_CODE" = "200" ]; then
        log "Dashboard healthy ✅ → ${URL}"
    else
        err "Dashboard unhealthy (HTTP ${HTTP_CODE}). Check ECS logs: aws logs tail /ecs/${PROJECT_NAME} --region ${AWS_REGION}"
    fi
}

# ── Main ──
case "${1:-all}" in
    build)       build_image ;;
    deploy)      deploy_stack; grant_lakeformation; run_crawler; health_check ;;
    update)      build_image; update_service; health_check ;;
    crawler)     run_crawler ;;
    health)      health_check ;;
    lakeformation) grant_lakeformation ;;
    all)         build_image; deploy_stack; grant_lakeformation; run_crawler; health_check ;;
    *)
        echo "Usage: $0 [build|deploy|update|crawler|health|lakeformation|all]"
        echo ""
        echo "  build          - Build & push Docker image to ECR"
        echo "  deploy         - Deploy/update CloudFormation stack + Lake Formation + Glue crawler"
        echo "  update         - Build image + force ECS redeployment (code changes)"
        echo "  crawler        - Run Glue crawler"
        echo "  health         - Check dashboard health"
        echo "  lakeformation  - Grant Lake Formation permissions"
        echo "  all            - Full deployment (build + deploy + crawler + health)"
        echo ""
        echo "Environment variables (or .env file):"
        echo "  AWS_REGION        - AWS region (default: us-east-1)"
        echo "  AWS_ACCOUNT_ID    - AWS account ID (auto-detected if not set)"
        echo "  S3_DATA_PATH      - S3 path to Kiro data (required, e.g. my-bucket/prefix)"
        echo "  VPC_ID            - VPC ID (required)"
        echo "  SUBNET_IDS        - Comma-separated subnet IDs (required)"
        echo "  DOMAIN_NAME       - Domain name for dashboard (required, e.g. kiro.example.com)"
        echo "  CERTIFICATE_ARN   - ACM certificate ARN for HTTPS (required)"
        echo "  HOSTED_ZONE_ID    - Route53 hosted zone ID (optional)"
        echo "  IDENTITY_STORE_ID - IAM Identity Center store ID (optional)"
        exit 1
        ;;
esac
