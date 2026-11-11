#!/usr/bin/env bash
# Build the universal companion overlay and its .buildinfo sidecar.
#
# The committed binary must serve a judge's Mac as shipped, including an Intel
# Mac with no Xcode. So the Swift source is compiled twice - once per
# architecture - and lipo joins the two slices into one universal Mach-O:
#
#   butler_core/services/ButlerOverlay   (arm64 + x86_64)
#   butler_core/services/.buildinfo      (source hash + archs + build epoch)
#
# build_if_needed() trusts .buildinfo's source hash instead of file mtimes, so a
# fresh `git clone` (every mtime new) does not force a recompile.
#
# Usage: bash scripts/build-companion.sh
# Idempotent: safe to re-run; it always rebuilds both slices from the same
# source and atomically replaces the binary and sidecar.
# Fails loudly (stderr, non-zero) when either architecture cannot compile.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/butler_core/overlay_swift/ButlerOverlay.swift"
SERVICES="$ROOT/butler_core/services"
OUT="$SERVICES/ButlerOverlay"
INFO="$SERVICES/.buildinfo"

fail() {
    printf 'build-companion: %s\n' "$*" >&2
    exit 1
}

[ -f "$SRC" ] || fail "Swift source not found: $SRC"
command -v swiftc >/dev/null 2>&1 || fail \
    "swiftc not found; install Xcode Command Line Tools (xcode-select --install)"
command -v lipo >/dev/null 2>&1 || fail \
    "lipo not found; install Xcode Command Line Tools (xcode-select --install)"
command -v shasum >/dev/null 2>&1 || fail "shasum not found"

BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/butler-companion.XXXXXX")"
cleanup() { rm -rf "$BUILD_DIR"; }
trap cleanup EXIT

# Keep both compiles on a fresh module cache. A cache entry built for the host
# slice must not decide whether the other slice can compile (a stale x86_64
# Swift module in the shared cache can otherwise fail the cross-compile).
export SWIFT_MODULE_CACHE_PATH="$BUILD_DIR/swift-module-cache"
export CLANG_MODULE_CACHE_PATH="$BUILD_DIR/clang-module-cache"
mkdir -p "$SWIFT_MODULE_CACHE_PATH" "$CLANG_MODULE_CACHE_PATH"

compile_slice() {
    local arch="$1" target="$2" output="$3"
    printf 'build-companion: compiling %s (%s)\n' "$arch" "$target" >&2
    if ! swiftc -target "$target" -O -o "$output" "$SRC"; then
        fail "failed to compile $arch ($target); nothing was replaced"
    fi
    [ -s "$output" ] || fail "swiftc produced no output for $arch"
}

compile_slice arm64 arm64-apple-macos11 "$BUILD_DIR/ButlerOverlay-arm64"
compile_slice x86_64 x86_64-apple-macos11 "$BUILD_DIR/ButlerOverlay-x86_64"

mkdir -p "$SERVICES"
STAGED="$OUT.new"
rm -f "$STAGED"
if ! lipo -create \
        "$BUILD_DIR/ButlerOverlay-arm64" \
        "$BUILD_DIR/ButlerOverlay-x86_64" \
        -output "$STAGED"; then
    rm -f "$STAGED"
    fail "lipo -create failed; nothing was replaced"
fi
chmod 755 "$STAGED"
mv -f "$STAGED" "$OUT"

SOURCE_SHA="$(shasum -a 256 "$SRC" | awk '{print $1}')"
BUILT_AT="$(date +%s)"
printf '{"source_sha256": "%s", "archs": ["arm64", "x86_64"], "built_at": %s}\n' \
    "$SOURCE_SHA" "$BUILT_AT" > "$INFO"

printf 'build-companion: lipo -archs '
lipo -archs "$OUT"
