#!/usr/bin/env bash
# Snapshot the SQLite database safely (works while the app is running) and
# keep the last 14 copies. Run daily from cron or a Render cron job:
#   0 4 * * * /path/to/scripts/backup_db.sh
set -euo pipefail
cd "$(dirname "$0")/.."
DB="${DB_PATH:-heardit.db}"
OUT="backups/heardit-$(date +%Y%m%d-%H%M%S).db"
sqlite3 "$DB" ".backup '$OUT'"
gzip -f "$OUT"
ls -1t backups/heardit-*.db.gz | tail -n +15 | xargs -r rm -f
echo "backed up → $OUT.gz ($(ls -1 backups | wc -l | tr -d ' ') kept)"
