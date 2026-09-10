#!/bin/bash
# restore_weights.sh — rejoin split chunks into the original file, verified.
# Reads weights/WEIGHTS.manifest, checks every part's sha256, cats them back,
# then verifies the final file sha256 before replacing the target.
#
# Usage:   ./scripts/restore_weights.sh            (restores to repo root)
#          ./scripts/restore_weights.sh /some/dir   (restores elsewhere)
set -euo pipefail

MANIFEST="weights/WEIGHTS.manifest"
DESTDIR="${1:-.}"

[ -f "$MANIFEST" ] || { echo "restore: $MANIFEST not found — run from repo root" >&2; exit 1; }
mkdir -p "$DESTDIR"

BASE=$(awk '/^file:/{print $2}' "$MANIFEST")
SIZE=$(awk '/^size:/{print $2}' "$MANIFEST")
SHA=$(awk '/^sha256:/{print $2}' "$MANIFEST")
NPARTS=$(awk '/^parts:/{print $2}' "$MANIFEST")

echo "restore: $BASE  size=$SIZE  sha256=$SHA  parts=$NPARTS"

# every part must exist + match its recorded sha
while read -r _ part psha; do
  [ -f "weights/$part" ] || { echo "restore: MISSING $part" >&2; exit 1; }
  got=$(sha256sum "weights/$part" | awk '{print $1}')
  [ "$got" = "$psha" ] || { echo "restore: BAD SHA $part (got $got want $psha)" >&2; exit 1; }
  echo "  ok  $part"
done < <(grep '^part_sha:' "$MANIFEST")

TMP="$DESTDIR/$BASE.tmp"
cat weights/"$BASE".part-* > "$TMP"
GOT=$(sha256sum "$TMP" | awk '{print $1}')
[ "$GOT" = "$SHA" ] || { echo "restore: final sha mismatch (got $GOT want $SHA)" >&2; rm -f "$TMP"; exit 1; }
GOTSIZE=$(stat -c%s "$TMP")
[ "$GOTSIZE" = "$SIZE" ] || { echo "restore: size mismatch (got $GOTSIZE want $SIZE)" >&2; rm -f "$TMP"; exit 1; }

mv "$TMP" "$DESTDIR/$BASE"
echo "restore: OK -> $DESTDIR/$BASE  ($GOTSIZE bytes, sha verified)"
