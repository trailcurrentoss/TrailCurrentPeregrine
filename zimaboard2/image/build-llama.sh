#!/usr/bin/env bash
# ============================================================================
# TrailCurrent Peregrine (ZimaBoard 2) — build llama.cpp (Vulkan + CPU)
#
# Runs INSIDE the ubuntu:24.04 builder container (build.sh starts it). Builds
# the pinned tag from versions.env and installs it as a relocatable tree:
#
#   cache/llama.cpp/<tag>/bin/llama-server, llama-bench, llama-cli
#   cache/llama.cpp/<tag>/lib/libggml*.so, libllama.so
#
# The image stages that tree to /opt/llama.cpp/<tag>/. The output is cached:
# a second run with the same tag does nothing.
# ============================================================================

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=versions.env
source "$HERE/versions.env"

CACHE="$HERE/cache/llama.cpp"
DEST="$CACHE/$LLAMA_TAG"

log() { echo "[build-llama] $*"; }

if [[ ! -f /.dockerenv && "${PEREGRINE_IN_BUILDER:-}" != 1 ]]; then
    echo "build-llama.sh installs build packages with apt and must run inside" >&2
    echo "the builder container. Run ./zimaboard2/image/build.sh instead." >&2
    exit 1
fi

if [[ -x "$DEST/bin/llama-server" && -f "$DEST/.complete" ]]; then
    log "llama.cpp $LLAMA_TAG already built — $DEST"
    exit 0
fi

log "installing build dependencies"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    build-essential cmake git ca-certificates pkg-config \
    libvulkan-dev glslc spirv-headers >/dev/null

SRC="$(mktemp -d)"
trap 'rm -rf "$SRC"' EXIT

log "fetching $LLAMA_TAG ($LLAMA_COMMIT)"
git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$LLAMA_TAG" \
    "$LLAMA_REPO" "$SRC/llama.cpp"
got="$(git -C "$SRC/llama.cpp" rev-parse HEAD)"
if [[ "$got" != "$LLAMA_COMMIT" ]]; then
    echo "ERROR: tag $LLAMA_TAG resolves to $got, expected $LLAMA_COMMIT" >&2
    exit 1
fi

# GGML_NATIVE=OFF: the build host is not the target. The N150 (Gracemont)
# has AVX2/FMA/F16C/BMI2 and no AVX-512, so enable exactly that set.
log "configuring"
cmake -S "$SRC/llama.cpp" -B "$SRC/build" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="/opt/llama.cpp/$LLAMA_TAG" \
    -DCMAKE_INSTALL_RPATH='$ORIGIN/../lib' \
    -DBUILD_SHARED_LIBS=ON \
    -DGGML_VULKAN=ON \
    -DGGML_NATIVE=OFF \
    -DGGML_AVX=ON -DGGML_AVX2=ON -DGGML_FMA=ON -DGGML_F16C=ON -DGGML_BMI2=ON \
    -DGGML_AVX512=OFF \
    -DLLAMA_CURL=OFF \
    -DLLAMA_BUILD_TESTS=OFF \
    -DLLAMA_BUILD_EXAMPLES=OFF \
    -DLLAMA_BUILD_SERVER=ON

log "building (this takes a while — Vulkan shaders are compiled here)"
cmake --build "$SRC/build" --config Release -j "$(nproc)" \
    --target llama-server llama-bench llama-cli

log "installing to $DEST"
rm -rf "$DEST"
mkdir -p "$DEST/bin" "$DEST/lib"
install -m 755 "$SRC/build/bin/llama-server" "$SRC/build/bin/llama-bench" \
    "$SRC/build/bin/llama-cli" "$DEST/bin/"
find "$SRC/build" -name '*.so*' \( -type f -o -type l \) -exec cp -a {} "$DEST/lib/" \;
install -m 644 "$SRC/llama.cpp/LICENSE" "$DEST/LICENSE"
echo "$LLAMA_TAG $LLAMA_COMMIT" > "$DEST/VERSION"

# Patch every ELF to find its libs relative to itself.
apt-get install -y -qq --no-install-recommends patchelf >/dev/null
for f in "$DEST"/bin/*; do patchelf --set-rpath '$ORIGIN/../lib' "$f"; done
for f in "$DEST"/lib/*.so*; do
    [[ -L "$f" ]] || patchelf --set-rpath '$ORIGIN' "$f"
done

log "smoke test"
"$DEST/bin/llama-server" --version
touch "$DEST/.complete"
log "done — $(du -sh "$DEST" | cut -f1)"
