#!/usr/bin/env bash
# Drop the Monaco editor into static/vendor/monaco/, pinned.
#
# The page has no build step and the box runs offline, so Monaco is served
# from our own static/ rather than a CDN and loaded by its own AMD loader.
# `npm pack` into a temp dir gets the tarball without a package.json, a
# node_modules, or anything global; only min/vs is copied, which is the
# pre-bundled build (editor.main already carries the Python grammar).
#
# Re-run to move versions: bump VERSION, commit the result, and update the
# version named in the README.
set -euo pipefail

VERSION="${VERSION:-0.56.0}"
DEST="${DEST:-$(cd "$(dirname "$0")/.." && pwd)/static/vendor/monaco}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

( cd "$WORK" && npm pack "monaco-editor@${VERSION}" >/dev/null )
tar -xzf "$WORK"/monaco-editor-"${VERSION}".tgz -C "$WORK"

rm -rf "$DEST/vs"
mkdir -p "$DEST"
cp -r "$WORK/package/min/vs" "$DEST/vs"
cp "$WORK/package/LICENSE" "$DEST/LICENSE"
printf 'monaco-editor@%s, min/vs only. Reproduce with scripts/vendor-monaco.sh\n' \
    "$VERSION" > "$DEST/VERSION"

echo "monaco-editor@${VERSION} -> ${DEST}/vs ($(du -sh "$DEST/vs" | cut -f1))"
