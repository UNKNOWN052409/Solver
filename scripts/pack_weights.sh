#!/bin/bash
# pack_weights.sh — split a big binary into git-safe chunks (<100MB) + manifest.
# GitHub per-file hard limit = 100MiB; default chunk = 90MiB (safe margin).
#
# Usage:   ./scripts/pack_weights.sh <file> [chunk_mib]
# Output:  weights/<file>.part-NN ... + weights/WEIGHTS.manifest
#          (original file stays in place, still git-ignored)
set -euo pipefail

FILE="${1:?usage: pack_weights.sh <file> [chunk_mib]}"
CHUNK_MIB="${2:-90}"
CHUNK=$((CHUNK_MIB * 1024 * 1024))

[ -f "$FILE" ] || { echo "pack: file not found: $FILE" >&2; exit 1; }

BASE="$(basename "$FILE")"
OUTDIR="weights"
mkdir -p "$OUTDIR"

SIZE=$(stat -c%s "$FILE")
SHA=$(sha256sum "$FILE" | awk '{print $1}')
NPARTS=$(( (SIZE + CHUNK - 1) / CHUNK ))
[ "$NPARTS" -eq 0 ] && NPARTS=1

echo "pack: $BASE  size=$SIZE  sha256=$SHA"
echo "pack: chunk=${CHUNK_MIB}MiB  parts=$NPARTS"

# clean old parts of this file
rm -f "$OUTDIR/$BASE".part-*

split -b "$CHUNK" -d "$FILE" "$OUTDIR/$BASE.part-"

MANIFEST="$OUTDIR/WEIGHTS.manifest"
{
  echo "file: $BASE"
  echo "size: $SIZE"
  echo "sha256: $SHA"
  echo "chunk_mib: $CHUNK_MIB"
  echo "chunk_bytes: $CHUNK"
  echo "parts: $NPARTS"
  for p in "$OUTDIR/$BASE".part-*; do
    echo "part_sha: $(basename "$p") $(sha256sum "$p" | awk '{print $1}')"
  done
} > "$MANIFEST"

echo "pack: wrote $NPARTS parts + $MANIFEST"
echo "pack: verify with: ./scripts/restore_weights.sh"
