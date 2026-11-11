#!/usr/bin/env bash
# Build the companion overlay and its .buildinfo sidecar.
#
# Two build shapes, because the two callers want different things:
#
#   --native      (default) only this machine's architecture: ~6 s. What a dev
#                 loop needs; the companion is exercised locally, never shipped.
#   --universal   arm64 + x86_64 joined by lipo: ~90 s, because Swift emitting
#                 Intel code on an Apple Silicon host is ~5x slower than native
#                 codegen. CI owns this; the release asset must run on a judge's
#                 Intel Mac, and nobody waits on a runner.
#
# Other flags:
#   --force       rebuild even when the sidecar already matches the source
#   --debug       compile with -Onone (marginally faster compile, slower helper)
#
# Two caching layers keep the dev loop honest:
#   1. Early exit when .buildinfo's source hash already matches the source and
#      the binary exists — a no-op rebuild costs nothing.
#   2. A shared cache keyed by (source hash, archs, mode) under
#      ~/Library/Caches/butler/companion, so the dozen worktrees stop rebuilding
#      byte-identical code.
#
# Usage: bash scripts/build-companion.sh [--universal|--native] [--force] [--debug]
# Fails loudly (stderr, non-zero) when a requested architecture cannot compile.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/butler_core/overlay_swift/ButlerOverlay.swift"
SERVICES="$ROOT/butler_core/services"
OUT="$SERVICES/ButlerOverlay"
INFO="$SERVICES/.buildinfo"
CACHE_ROOT="${HOME}/Library/Caches/butler/companion"

MODE="native"
FORCE=0
OPT="-O"
for arg in "$@"; do
    case "$arg" in
        --universal) MODE="universal" ;;
        --native) MODE="native" ;;
        --force) FORCE=1 ;;
        --debug) OPT="-Onone" ;;
        *) printf 'build-companion: unknown flag %s\n' "$arg" >&2; exit 2 ;;
    esac
done

fail() {
    printf 'build-companion: %s\n' "$*" >&2
    exit 1
}

[ -f "$SRC" ] || fail "Swift source not found: $SRC"
command -v swiftc >/dev/null 2>&1 || fail \
    "swiftc not found; install Xcode Command Line Tools (xcode-select --install)"
command -v shasum >/dev/null 2>&1 || fail "shasum not found"
if [ "$MODE" = "universal" ]; then
    command -v lipo >/dev/null 2>&1 || fail \
        "lipo not found; install Xcode Command Line Tools (xcode-select --install)"
fi

SOURCE_SHA="$(shasum -a 256 "$SRC" | awk '{print $1}')"
HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in
    arm64) HOST_TARGET="arm64-apple-macos11" ;;
    x86_64) HOST_TARGET="x86_64-apple-macos11" ;;
    *) fail "unsupported host architecture: $HOST_ARCH" ;;
esac

if [ "$MODE" = "universal" ]; then
    ARCHS_JSON='["arm64", "x86_64"]'
    ARCH_TAG="universal"
else
    ARCHS_JSON="[\"$HOST_ARCH\"]"
    ARCH_TAG="$HOST_ARCH"
fi

# ── layer 1: nothing to do ────────────────────────────────────────────────────
if [ "$FORCE" -eq 0 ] && [ -x "$OUT" ] && [ -f "$INFO" ]; then
    if grep -q "\"source_sha256\": \"$SOURCE_SHA\"" "$INFO" 2>/dev/null; then
        have_all=1
        for arch in arm64 x86_64; do
            case "$MODE:$arch" in
                native:*) continue ;;
            esac
            if ! lipo -archs "$OUT" 2>/dev/null | tr ' ' '\n' | grep -qx "$arch"; then
                have_all=0
            fi
        done
        if [ "$have_all" -eq 1 ]; then
            printf 'build-companion: up to date for %s (%s)\n' "$MODE" "${SOURCE_SHA:0:12}"
            lipo -archs "$OUT" 2>/dev/null || true
            exit 0
        fi
    fi
fi

# ── layer 2: a shared cache keyed by source hash ──────────────────────────────
CACHE_KEY="${SOURCE_SHA:0:16}-${ARCH_TAG}-${OPT#-}"
CACHE_DIR="$CACHE_ROOT/$CACHE_KEY"
CACHE_BIN="$CACHE_DIR/ButlerOverlay"

mkdir -p "$SERVICES"
if [ "$FORCE" -eq 0 ] && [ -x "$CACHE_BIN" ]; then
    printf 'build-companion: cache hit %s\n' "$CACHE_KEY" >&2
    STAGED="$OUT.new"
    rm -f "$STAGED"
    cp "$CACHE_BIN" "$STAGED"
    chmod 755 "$STAGED"
    mv -f "$STAGED" "$OUT"
    printf '{"source_sha256": "%s", "archs": %s, "built_at": %s}\n' \
        "$SOURCE_SHA" "$ARCHS_JSON" "$(date +%s)" > "$INFO"
    printf 'build-companion: lipo -archs '
    lipo -archs "$OUT" 2>/dev/null || true
    exit 0
fi

BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/butler-companion.XXXXXX")"
cleanup() { rm -rf "$BUILD_DIR"; }
trap cleanup EXIT

# Per-architecture module caches, PERSISTED across builds. A cold cache makes a
# 6 s compile take 42 s (Foundation/AppKit are re-parsed every time), while one
# shared cache for both slices lets an arm64 module decide the x86_64 compile's
# fate. One cache per arch gives both properties.
compile_slice() {
    local arch="$1" target="$2" output="$3"
    local cache="$CACHE_ROOT/module-cache-$arch"
    mkdir -p "$cache/swift" "$cache/clang"
    printf 'build-companion: compiling %s (%s, %s)\n' "$arch" "$target" "$OPT" >&2
    if ! SWIFT_MODULE_CACHE_PATH="$cache/swift" \
         CLANG_MODULE_CACHE_PATH="$cache/clang" \
         swiftc -target "$target" "$OPT" -o "$output" "$SRC"; then
        fail "failed to compile $arch ($target); nothing was replaced"
    fi
    [ -s "$output" ] || fail "swiftc produced no output for $arch"
}

if [ "$MODE" = "universal" ]; then
    compile_slice arm64 arm64-apple-macos11 "$BUILD_DIR/ButlerOverlay-arm64"
    compile_slice x86_64 x86_64-apple-macos11 "$BUILD_DIR/ButlerOverlay-x86_64"
    mkdir -p "$CACHE_DIR"
    rm -f "$CACHE_BIN.new"
    lipo -create \
        "$BUILD_DIR/ButlerOverlay-arm64" \
        "$BUILD_DIR/ButlerOverlay-x86_64" \
        -output "$CACHE_BIN.new" || fail "lipo -create failed; nothing was replaced"
    chmod 755 "$CACHE_BIN.new"
    mv -f "$CACHE_BIN.new" "$CACHE_BIN"
else
    compile_slice "$HOST_ARCH" "$HOST_TARGET" "$BUILD_DIR/ButlerOverlay-host"
    mkdir -p "$CACHE_DIR"
    cp "$BUILD_DIR/ButlerOverlay-host" "$CACHE_BIN.new"
    chmod 755 "$CACHE_BIN.new"
    mv -f "$CACHE_BIN.new" "$CACHE_BIN"
fi

STAGED="$OUT.new"
rm -f "$STAGED"
cp "$CACHE_BIN" "$STAGED"
chmod 755 "$STAGED"
mv -f "$STAGED" "$OUT"

printf '{"source_sha256": "%s", "archs": %s, "built_at": %s}\n' \
    "$SOURCE_SHA" "$ARCHS_JSON" "$(date +%s)" > "$INFO"

printf 'build-companion: lipo -archs '
lipo -archs "$OUT" 2>/dev/null || true
