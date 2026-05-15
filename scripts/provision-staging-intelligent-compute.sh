#!/usr/bin/env bash
# Provision the AWS Cloud compute environment with Seqera Intelligent Compute
# (Seqera scheduler) enabled, in the staging showcase workspace.
#
# COMP-1701 — pairs with compute-envs/staging-seqera-intelligent-compute.yaml.
#
# This is a one-shot, idempotent-by-name provisioning script. The showcase
# automation workflow itself does not create compute environments; it only
# launches pipelines against CEs that already exist. Run this once (or after
# the CE is deleted) before the autotest workflow can pick the CE up.
#
# Prerequisites:
#   - tower-cli >= v0.27.0 (the --sched-enabled option landed in PR
#     seqeralabs/tower-cli#610, first released in v0.27.0).
#   - TOWER_ACCESS_TOKEN and TOWER_API_ENDPOINT exported (staging endpoint).
#   - An AWS credentials record already present in the staging workspace.
#     Discover its name with:
#         tw credentials list --workspace 14715071736572
#     and export it as CREDENTIALS_NAME below. The intent is to reuse the
#     same AWS credentials backing the existing AWS Batch CE
#     (seqera_aws_ireland_fusion_nvme).

set -euo pipefail

: "${TOWER_ACCESS_TOKEN:?TOWER_ACCESS_TOKEN must be set}"
: "${TOWER_API_ENDPOINT:?TOWER_API_ENDPOINT must point at the staging API}"
: "${CREDENTIALS_NAME:?CREDENTIALS_NAME must be set to the AWS credentials record in the workspace}"

WORKSPACE_ID="${WORKSPACE_ID:-14715071736572}"
CE_NAME="${CE_NAME:-seqera_aws_intelligent_compute_ireland}"
REGION="${REGION:-eu-west-1}"
WORK_DIR="${WORK_DIR:-s3://seqera-showcase/scratch/cicd/intelligent-compute}"
PROVISIONING_MODEL="${PROVISIONING_MODEL:-SPOT_FIRST}"

tw compute-envs add aws-cloud \
  --workspace "${WORKSPACE_ID}" \
  --name "${CE_NAME}" \
  --credentials "${CREDENTIALS_NAME}" \
  --region "${REGION}" \
  --work-dir "${WORK_DIR}" \
  --sched-enabled \
  --provisioning-model "${PROVISIONING_MODEL}"
