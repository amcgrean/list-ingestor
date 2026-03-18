#!/usr/bin/env bash
# Deletes remote branches that have been merged into main.
# Run this script with appropriate push permissions.
# Generated: 2026-03-18
#
# Branches NOT deleted (no evidence of merge into main):
#   - claude/fix-sku-upload-system-Vdvkm
#   - codex/merge-buttons-for-file-upload-and-add-features

set -euo pipefail

MERGED_BRANCHES=(
  claude/add-chatgpt-integration-i0mU9
  claude/add-ingester-metrics-3UDTZ
  claude/debug-deployment-TcnuV
  claude/debug-memory-issues-OwQf9
  claude/debug-sku-matching-uNrLw
  claude/enhance-handwritten-ocr-GWc0m
  claude/extract-docker-layer-pBzYP
  claude/fix-404-error-GY7EE
  claude/fix-ai-parsing-WTfIE
  claude/fix-file-upload-error-KT9IK
  claude/fix-issue-ZDefz
  claude/fix-json-parsing-error-2Abs8
  claude/fix-mobile-file-upload-9WPSG
  claude/fix-ocr-detection-h4ItY
  claude/fix-pdf-upload-cpu-ZIOJi
  claude/list-ingestor-descriptions-itAmm
  claude/material-list-erp-converter-NLOJB
  claude/openai-vision-refactor-7Aa21
  claude/optimize-performance-mqA6n
  claude/review-feedback-upgrade-f52KF
  claude/update-sku-csv-ingestion-j8piq
  codex/find-sku-match-correction-tracking-status
  codex/fix-missing-column-in-database
  codex/implement-hybrid-sku-matching-system
  codex/integrate-sku-processing-workflow-into-app
  codex/investigate-app-404-error-on-vercel
)

echo "Deleting ${#MERGED_BRANCHES[@]} merged remote branches..."
for branch in "${MERGED_BRANCHES[@]}"; do
  echo "  Deleting origin/$branch"
  git push origin --delete "$branch"
done
echo "Done."
