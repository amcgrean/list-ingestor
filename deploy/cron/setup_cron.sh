#!/usr/bin/env bash
# Install Pi cron jobs for the material list ingestor.
#
# Run once after deploying to the Pi:
#   ssh agility-ai "bash /home/amcgrean/services/list-ingestor/deploy/cron/setup_cron.sh"
#
# Jobs installed:
#   2:00 AM daily  — promote frequently-corrected descriptions to ItemAlias entries
#   2:05 AM daily  — print daily digest to logs (review weekly)
#   Sunday 3:00 AM — rebuild vector index + vacuum DB (weekly maintenance)

APP_DIR="/home/amcgrean/services/list-ingestor"
LOG_DIR="$APP_DIR/logs"

mkdir -p "$LOG_DIR"

# Write new cron entries (preserves any existing unrelated cron jobs)
CRONTAB_NEW=$(crontab -l 2>/dev/null | grep -v "list-ingestor"; cat << 'CRON'
# ── list-ingestor auto-learning jobs ────────────────────────────────
# Promote aliases daily at 2:00 AM
0 2 * * * cd /home/amcgrean/services/list-ingestor && cat scripts/promote_aliases.py | docker compose exec -T web python - >> logs/promote_aliases.log 2>&1

# Daily digest at 2:05 AM (pipe to logs for weekly review)
5 2 * * * cd /home/amcgrean/services/list-ingestor && cat scripts/daily_digest.py | docker compose exec -T web python - >> logs/daily_digest.log 2>&1

# Weekly maintenance Sunday 3 AM: restart web to clear vector index cache (forces rebuild from enriched catalog)
0 3 * * 0 cd /home/amcgrean/services/list-ingestor && docker compose restart web >> logs/weekly_restart.log 2>&1
# ── end list-ingestor jobs ───────────────────────────────────────────
CRON
)

echo "$CRONTAB_NEW" | crontab -
echo "Cron jobs installed:"
crontab -l | grep -A1 "list-ingestor"
