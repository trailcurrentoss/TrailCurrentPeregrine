#!/usr/bin/env bash
# ============================================================================
# TrailCurrent Peregrine (ZimaBoard 2) — build-host preflight
#
# Runs on the build host as a normal user (build.sh calls it). Checks the
# host, then fetches every pinned artefact from versions.env into cache/,
# verifying each SHA-256. Already-verified files are not fetched again.
# ============================================================================

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=versions.env
source "$HERE/versions.env"
CACHE="$HERE/cache"

log()  { echo "[preflight] $*"; }
die()  { echo "[preflight] ERROR: $*" >&2; exit 1; }

# fetch URL DEST SHA256
fetch() {
    local url="$1" dest="$2" sha="$3"
    if [[ -f "$dest" ]] && echo "$sha  $dest" | sha256sum -c --quiet 2>/dev/null; then
        log "ok (cached)  $(basename "$dest")"
        return
    fi
    log "downloading  $(basename "$dest")"
    mkdir -p "$(dirname "$dest")"
    curl -fL --retry 5 -C - -sS -o "$dest.part" "$url"
    echo "$sha  $dest.part" | sha256sum -c --quiet || die "SHA-256 mismatch for $url"
    mv "$dest.part" "$dest"
}

[[ "$(uname -m)" == x86_64 ]] || die "the ZimaBoard 2 image must be built on an x86_64 host"
command -v docker >/dev/null || die "docker is required (the build runs as root inside a container)"
docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon (is your user in the 'docker' group?)"
command -v curl >/dev/null || die "curl is required"

avail_gb=$(df -BG --output=avail "$HERE" | tail -1 | tr -dc 0-9)
(( avail_gb >= 20 )) || die "need 20 GB free next to the repo for cache/ and output/ (have ${avail_gb} GB)"
avail_gb=$(df -BG --output=avail "$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)" 2>/dev/null | tail -1 | tr -dc 0-9 || echo 0)
(( ${avail_gb:-0} >= 30 )) || log "WARNING: the Docker root dir may have under 30 GB free (rootfs + live work dirs)"

fetch "$LLM_MODEL_URL" "$CACHE/llm/$LLM_MODEL_FILE" "$LLM_MODEL_SHA256"
fetch "$PIPER_BASE_URL/$PIPER_VOICE.onnx" "$CACHE/piper-voice/$PIPER_VOICE.onnx" "$PIPER_ONNX_SHA256"
fetch "$PIPER_BASE_URL/$PIPER_VOICE.onnx.json" "$CACHE/piper-voice/$PIPER_VOICE.onnx.json" "$PIPER_JSON_SHA256"

[[ -f "$HERE/../../models/hey_peregrine.onnx" ]] || die "models/hey_peregrine.onnx missing from the repo"

log "all artefacts present"
