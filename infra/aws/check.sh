#!/bin/sh
# Static checks for the AWS design; nothing is deployed and no AWS credentials are used.
#   terraform fmt, validate and test (a mock provider, offline), then trivy's misconfiguration
#   checks with the rules built into the trivy binary (no rule download), written to
#   evidence/iac-check-<UTC>.json.
# Fails on any HIGH or CRITICAL finding.
set -eu
cd "$(dirname "$0")"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
evidence="../../evidence/iac-check-$stamp.json"

terraform fmt -check -recursive
terraform init -backend=false -input=false -lockfile=readonly >/dev/null
terraform validate
terraform test
trivy --version | head -1
trivy config --skip-check-update --skip-version-check --format json --output "$evidence" .
trivy config --skip-check-update --skip-version-check --exit-code 1 --severity HIGH,CRITICAL .
echo "evidence: $evidence"
