#!/usr/bin/env bash
# Daily database backup loop (M6). Wakes once per day at BACKUP_TIME (HH:MM in
# the container's TIME_ZONE, default UTC) and runs the tested `backup_database`
# management command. Runs as its own compose service so backups are independent
# of web/worker health. No cron dependency: a plain sleep loop keeps the image
# lean. A failed run logs a warning and retries the next day rather than exiting.
set -euo pipefail

RUN_TIME="${BACKUP_TIME:-03:17}"
log() { echo "[$(date --iso-8601=seconds)] $*"; }

while true; do
    now="$(date +%s)"
    target="$(date -d "today ${RUN_TIME}" +%s 2>/dev/null || date -d "${RUN_TIME}" +%s)"
    if [ "$target" -le "$now" ]; then
        # Already past today's slot — schedule for tomorrow.
        target=$(( target + 86400 ))
    fi
    wait_secs=$(( target - now ))
    log "Next database backup in ${wait_secs}s (at ${RUN_TIME})"

    # Sleep in the background so a SIGTERM (docker stop) interrupts it cleanly
    # instead of leaving an uninterruptible sleep behind.
    sleep "$wait_secs" &
    wait $! || true

    log "Running database backup"
    if python manage.py backup_database; then
        log "Backup run finished"
    else
        log "WARNING: backup run failed; will retry at the next ${RUN_TIME}"
    fi
done
