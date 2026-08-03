#!/bin/bash
#
# Back up everything that cannot be rebuilt from the git repo.
#
# That is three things: the database (users, passkeys, schedule groups, holidays,
# the audit trail), the config file (device addresses and the TruPortal
# password), and the launchd plist. Everything else on this machine comes back
# with `git clone` and `pip install`.
#
# Run it from cron or by hand:
#
#   deploy/backup.sh                      # writes to /usr/local/var/backups/building-controls
#   deploy/backup.sh /Volumes/Backup/bms  # or wherever
#
# Restore is deliberately manual -- see RESTORE, printed at the end of a run.
#
# The gateway does NOT need to be stopped. The database is copied with sqlite3's
# own .backup, which takes a consistent snapshot of a live database. A plain
# `cp` of bms.db would not: writes live in bms.db-wal until a checkpoint, so the
# copy could be missing the last few minutes, or be torn mid-transaction.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:-/usr/local/var/backups/building-controls}"
KEEP="${BMS_BACKUP_KEEP:-30}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$DEST/$STAMP"

DB="${BMS_DB:-$REPO/data/bms.db}"
CONFIG="${BMS_CONFIG:-$REPO/config/devices.yaml}"
PLIST="/Library/LaunchDaemons/com.building-controls.gateway.plist"

# The backup holds password hashes, a TruPortal password and the audit trail.
# umask before mkdir, so the directory is never briefly world-readable.
umask 077
mkdir -p "$OUT"

fail() { echo "backup FAILED: $*" >&2; exit 1; }

# --- database ---------------------------------------------------------------
if [ -f "$DB" ]; then
  # .backup over a live database; VACUUM INTO would also work but fails if the
  # target exists, and .backup is the one that has been in sqlite3 for decades.
  sqlite3 "$DB" ".backup '$OUT/bms.db'" || fail "sqlite3 .backup on $DB"

  # A backup nobody checked is a guess. Verify the copy opens and its schema is
  # intact before this run is allowed to count as a success.
  sqlite3 "$OUT/bms.db" "PRAGMA integrity_check;" | grep -qx ok \
    || fail "the copied database did not pass integrity_check"
  users=$(sqlite3 "$OUT/bms.db" "SELECT COUNT(*) FROM app_user;")
  [ "$users" -gt 0 ] || fail "the copied database has no user accounts"
  echo "  database      $users account(s), integrity ok"
else
  echo "  database      not found at $DB -- skipped" >&2
fi

# --- config and plist -------------------------------------------------------
if [ -f "$CONFIG" ]; then
  cp -p "$CONFIG" "$OUT/devices.yaml"
  echo "  config        $CONFIG"
else
  echo "  config        not found at $CONFIG -- skipped" >&2
fi

if [ -f "$PLIST" ]; then
  cp -p "$PLIST" "$OUT/"
  echo "  launchd       $PLIST"
fi

# Which commit was running, so a restore can put the code back to match the
# database it is restoring. A schema the code does not expect is the one
# failure mode a file copy cannot fix on its own.
if git -C "$REPO" rev-parse HEAD >/dev/null 2>&1; then
  {
    git -C "$REPO" rev-parse HEAD
    git -C "$REPO" status --porcelain
  } > "$OUT/VERSION"
  echo "  code version  $(git -C "$REPO" rev-parse --short HEAD)"
fi

cat > "$OUT/RESTORE" <<'RESTORE'
Restore
=======

1. Stop the gateway:
     sudo launchctl bootout system/com.building-controls.gateway

2. Put the code back to the commit this backup was taken from (see VERSION):
     git -C <repo> checkout <sha>

3. Copy the files back:
     cp bms.db      <repo>/data/bms.db
     cp devices.yaml <repo>/config/devices.yaml
     chmod 600      <repo>/config/devices.yaml

   Delete any stale bms.db-wal and bms.db-shm beside the old database first.
   They belong to the database they were written with, and a leftover WAL from a
   different file is how a good backup turns into a corrupt one.

4. Start it:
     sudo launchctl bootstrap system /Library/LaunchDaemons/com.building-controls.gateway.plist

5. Check it came back:
     curl -s localhost:8237/health

Passkeys survive a restore -- they are rows in this database, bound to the
origin, not to the machine. Sessions survive too, which is worth knowing if the
reason for the restore was that someone got in: revoke them.

     .venv/bin/python -m bms.useradmin passwd <username>
RESTORE

# --- retention ---------------------------------------------------------------
# Oldest first, delete past KEEP. No arrays or `mapfile` here: macOS still ships
# bash 3.2, and this has to run on the mini as-is. Each deletion is guarded on
# the directory containing a RESTORE file, so a mistyped $DEST cannot rm -rf
# something this script did not create.
cd "$DEST"
total=$(ls -1d 20*-* 2>/dev/null | wc -l | tr -d ' ')
if [ "$total" -gt "$KEEP" ]; then
  ls -1d 20*-* | sort | head -n "$((total - KEEP))" | while read -r dir; do
    if [ -f "$dir/RESTORE" ]; then
      rm -rf "${DEST:?}/${dir:?}"
      echo "  pruned        $dir"
    fi
  done
fi

count=$(ls -1d "$DEST"/20*-* 2>/dev/null | wc -l | tr -d ' ')
echo "backup ok: $OUT  ($count kept, oldest $(ls -1d "$DEST"/20*-* | head -1 | xargs basename))"
