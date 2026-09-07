#!/bin/bash
# Nightly: the database (consistently, via .backup) plus the photographs.
# The photos are the irreplaceable half -- the rows can be retyped.
set -euo pipefail
SRC=/var/www/vhosts/digitalwaterhouse.com/httpdocs/artbymccarthy
DEST=/var/backups/artbymccarthy
STAMP=$(date +%Y%m%d)
mkdir -p "$DEST"
sqlite3 "$SRC/data/gallery.db" ".backup '$DEST/gallery-$STAMP.db'"
tar -czf "$DEST/photos-$STAMP.tar.gz" -C "$SRC/data" photos
find "$DEST" -name 'gallery-*.db' -mtime +30 -delete
find "$DEST" -name 'photos-*.tar.gz' -mtime +30 -delete
echo "$(date -Is) backup ok"
