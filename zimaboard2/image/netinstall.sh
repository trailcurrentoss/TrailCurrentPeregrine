#!/usr/bin/env bash
# ============================================================================
# TrailCurrent Peregrine (ZimaBoard 2) — install over the network
#
#   ./zimaboard2/image/netinstall.sh [options] [user@host]
#
#   --check        only inspect the board (read-only) and stop
#   --yes          do not ask for the ERASE confirmation
#   -p, --port N   SSH port on the board (default 22)
#   -i FILE        SSH identity file
#   --http-port N  port this machine serves the installer on (default 8765)
#
# REPLACES whatever runs on the board (e.g. ZimaOS) with Peregrine:
#
#   1. SSH into the board's current OS and check it can kexec
#   2. serve the network-install bundle from this machine over HTTP
#   3. kexec the board into the Peregrine installer (ZimaOS stops here)
#   4. the installer downloads its live system and the payload into RAM,
#      verifies every checksum, and only then erases the eMMC and installs
#   5. the board reboots into Peregrine; progress streams to this terminal
#
# The board is chosen by, in order: the user@host argument, the
# PEREGRINE_NETINSTALL_TARGET environment variable, or netinstall.env next to
# this script (gitignored; written when you answer the prompt).
# ============================================================================

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_CFG="$HERE/netinstall.env"

CHECK_ONLY=false
ASSUME_YES=false
SSH_PORT=22
HTTP_PORT=8765
SSH_ID=()
TARGET=""

usage() { sed -n '3,25p' "$0" | sed 's/^# \{0,1\}//'; }
die()   { echo "netinstall: $*" >&2; exit 1; }
log()   { echo "[netinstall] $*"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check) CHECK_ONLY=true; shift ;;
        --yes) ASSUME_YES=true; shift ;;
        -p|--port) SSH_PORT="$2"; shift 2 ;;
        -i) SSH_ID=(-i "$2"); shift 2 ;;
        --http-port) HTTP_PORT="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        -*) die "unknown option $1 (see --help)" ;;
        *) TARGET="$1"; shift ;;
    esac
done

# ── Which board ─────────────────────────────────────────────────────────────
if [[ -z "$TARGET" ]]; then
    TARGET="${PEREGRINE_NETINSTALL_TARGET:-}"
fi
if [[ -z "$TARGET" && -f "$LOCAL_CFG" ]]; then
    # shellcheck disable=SC1090
    source "$LOCAL_CFG"
    TARGET="${PEREGRINE_NETINSTALL_TARGET:-}"
fi
if [[ -z "$TARGET" ]]; then
    if [[ -t 0 ]]; then
        read -rp "Board to install on (user@host of its current OS): " TARGET
        [[ -n "$TARGET" ]] || die "no board given"
        read -rp "Save this to $(basename "$LOCAL_CFG") for next time? [y/N]: " yn
        if [[ "${yn,,}" == y* ]]; then
            ( umask 077; echo "PEREGRINE_NETINSTALL_TARGET=$TARGET" > "$LOCAL_CFG" )
            log "saved (gitignored, mode 600)"
        fi
    else
        die "no board given. Pass user@host, set PEREGRINE_NETINSTALL_TARGET, or run
interactively to be asked (see netinstall.env.example)."
    fi
fi
[[ "$TARGET" == *@* ]] || die "give the board as user@host"
HOST="${TARGET#*@}"

SSH_OPTS=(-p "$SSH_PORT" "${SSH_ID[@]}" -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=3)
rsh() { ssh "${SSH_OPTS[@]}" -o BatchMode=yes "$TARGET" "$@"; }

BUNDLE="$(ls -1dt "$HERE"/output/peregrine-zimaboard2-*-netinstall 2>/dev/null | head -1 || true)"
[[ -d "$BUNDLE" ]] || die "no network-install bundle in $HERE/output — run ./zimaboard2/image/build.sh first"
for f in netboot.iso vmlinuz initrd kexec peregrine/release peregrine/SHA256SUMS; do
    [[ -e "$BUNDLE/$f" ]] || die "bundle incomplete: $f missing in $BUNDLE"
done
# shellcheck disable=SC1091
VERSION=$(source "$BUNDLE/peregrine/release"; echo "$PEREGRINE_VERSION")

# ── 1. Inspect the board (read-only) ────────────────────────────────────────
log "connecting to $TARGET"
rsh true 2>/dev/null || die "cannot log in to $TARGET with an SSH key (BatchMode). Check: ssh -p $SSH_PORT $TARGET"

PROBE=$(rsh 'sh -s' <<'EOF'
echo "kernel=$(uname -r)"
echo "uid=$(id -u)"
if [ "$(id -u)" = 0 ] || sudo -n true 2>/dev/null; then echo "sudo=nopasswd"; elif command -v sudo >/dev/null; then echo "sudo=password"; else echo "sudo=none"; fi
if [ -e /sys/kernel/kexec_loaded ]; then echo "kexec_kernel=yes"; else echo "kexec_kernel=no"; fi
echo "kexec_disabled=$(cat /proc/sys/kernel/kexec_load_disabled 2>/dev/null || echo ?)"
echo "lockdown=$(cat /sys/kernel/security/lockdown 2>/dev/null | grep -o '\[[a-z]*\]' | tr -d '[]' || echo none)"
sb=$(od -An -t u1 /sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c 2>/dev/null | awk '{print $NF}')
echo "secureboot=${sb:-unknown}"
echo "mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)"
echo "systemd=$(command -v systemctl >/dev/null && echo yes || echo no)"
echo "fetch=$(command -v curl >/dev/null && echo curl || { command -v wget >/dev/null && echo wget || echo none; })"
echo "client=${SSH_CONNECTION%% *}"
for d in /sys/block/mmcblk[0-9]; do [ -e "$d" ] && echo "emmc=$(basename "$d"):$(( $(cat "$d/size") / 2097152 ))G"; done
for d in /sys/block/sd? /sys/block/nvme?n?; do [ -e "$d" ] && echo "disk=$(basename "$d"):$(( $(cat "$d/size") / 2097152 ))G"; done
exit 0
EOF
)
val() { sed -n "s/^$1=//p" <<< "$PROBE" | head -1; }

echo ""
echo "  Board:        $TARGET"
echo "  Kernel:       $(val kernel)"
echo "  Root access:  $(val sudo)"
echo "  kexec:        kernel support $(val kexec_kernel), load disabled=$(val kexec_disabled), lockdown=$(val lockdown)"
echo "  Secure Boot:  $(case "$(val secureboot)" in 1) echo on;; 0) echo off;; *) echo unknown;; esac)"
echo "  RAM:          $(val mem_mb) MB"
echo "  eMMC:         $(sed -n 's/^emmc=//p' <<< "$PROBE" | tr '\n' ' ')"
echo "  Other disks:  $(sed -n 's/^disk=//p' <<< "$PROBE" | tr '\n' ' ')"
echo "  Installing:   Peregrine $VERSION from $BUNDLE"
echo ""

PROBLEMS=()
[[ "$(val sudo)" == none ]] && PROBLEMS+=("no root access (not root and no sudo)")
[[ "$(val kexec_kernel)" == no ]] && PROBLEMS+=("the running kernel has no kexec support — use the USB installer")
[[ "$(val kexec_disabled)" == 1 ]] && PROBLEMS+=("kexec is disabled (kernel.kexec_load_disabled=1) — use the USB installer")
[[ "$(sed -n 's/^emmc=//p' <<< "$PROBE" | wc -l)" -eq 1 ]] || PROBLEMS+=("expected exactly one eMMC")
(( $(val mem_mb) >= 6000 )) || PROBLEMS+=("less than 6 GB RAM — the installer holds the payload in RAM")
[[ "$(val lockdown)" == none || "$(val lockdown)" == "" ]] || \
    log "note: kernel lockdown is '$(val lockdown)'; kexec will use the signed-kernel path (kexec -s)"
if (( ${#PROBLEMS[@]} )); then
    for p in "${PROBLEMS[@]}"; do echo "  ✗ $p"; done
    die "this board cannot be installed over the network as it is"
fi
echo "  ✓ The board can be installed over the network."
$CHECK_ONLY && exit 0

MYIP="$(val client)"
[[ -n "$MYIP" ]] || die "could not tell which address the board sees this machine as"
BASE="http://$MYIP:$HTTP_PORT"

# ── 2. Serve the bundle ─────────────────────────────────────────────────────
STATUS_FIFO="$(mktemp -u)"; mkfifo "$STATUS_FIFO"
python3 - "$BUNDLE" "$HTTP_PORT" > "$STATUS_FIFO" 2>/dev/null <<'EOF' &
import functools, http.server, sys
root, port = sys.argv[1], int(sys.argv[2])
class H(http.server.SimpleHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        msg = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        if self.path == "/status":
            print(msg, flush=True)
        self.send_response(204); self.end_headers()
    def log_message(self, fmt, *args):
        if self.command == "GET" and str(args[1]) == "200":
            print(f"[board fetched {self.path}]", flush=True)
srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), functools.partial(H, directory=root))
srv.daemon_threads = True
srv.serve_forever()
EOF
SERVER_PID=$!
cleanup() { kill "$SERVER_PID" 2>/dev/null || true; rm -f "$STATUS_FIFO"; }
trap cleanup EXIT
exec 3<"$STATUS_FIFO"
sleep 1
kill -0 "$SERVER_PID" 2>/dev/null || die "could not start the HTTP server on port $HTTP_PORT"

case "$(val fetch)" in
    curl) FETCH="curl -sf -m 5 -o /dev/null" ;;
    wget) FETCH="wget -q -T 5 -O /dev/null" ;;
    *)    FETCH="" ;;
esac
if [[ -n "$FETCH" ]]; then
    rsh "$FETCH $BASE/peregrine/release" || die "the board cannot reach $BASE — is a firewall blocking port $HTTP_PORT on this machine?"
    read -r -t 2 _ <&3 || true    # swallow the probe's log line
    log "board can reach $BASE"
fi

# ── 3. Confirm ──────────────────────────────────────────────────────────────
if ! $ASSUME_YES; then
    echo ""
    echo "  This ERASES the board's eMMC ($(sed -n 's/^emmc=//p' <<< "$PROBE")) and everything on it,"
    echo "  including the current operating system, and installs Peregrine $VERSION."
    read -rp "  Type ERASE to continue: " answer
    [[ "$answer" == ERASE ]] || die "cancelled — nothing was changed"
fi

# ── 4. kexec into the installer ─────────────────────────────────────────────
log "copying the installer kernel to the board"
rsh 'rm -rf /tmp/peregrine-netinstall && mkdir -p /tmp/peregrine-netinstall'
for f in kexec vmlinuz initrd; do
    rsh "cat > /tmp/peregrine-netinstall/$f" < "$BUNDLE/$f"
done
rsh 'chmod 755 /tmp/peregrine-netinstall/kexec'

CMDLINE="boot=casper url=$BASE/netboot.iso ip=dhcp noprompt fsck.mode=skip quiet loglevel=3 systemd.show_status=false peregrine.unattended=1 peregrine.payload=$BASE/peregrine peregrine.done=reboot console=tty1"
REMOTE="cd /tmp/peregrine-netinstall && \
( ./kexec -l vmlinuz --initrd=initrd --append='$CMDLINE' 2>/dev/null || \
  ./kexec -s -l vmlinuz --initrd=initrd --append='$CMDLINE' ) && \
echo KEXEC-LOADED && \
( (sleep 1; systemctl kexec 2>/dev/null || ./kexec -e) >/dev/null 2>&1 & )"
if [[ "$(val uid)" == 0 ]]; then
    WRAP=(sh -c "$REMOTE")
elif [[ "$(val sudo)" == nopasswd ]]; then
    WRAP=(sudo -n sh -c "$REMOTE")
else
    WRAP=(sudo sh -c "$REMOTE")
fi
log "loading the installer kernel and handing the board over to it"
if [[ "$(val sudo)" == password ]]; then
    ssh "${SSH_OPTS[@]}" -t "$TARGET" "$(printf '%q ' "${WRAP[@]}")" | tee /dev/stderr | grep -q KEXEC-LOADED \
        || die "kexec failed on the board; it is still running its current OS"
else
    rsh "$(printf '%q ' "${WRAP[@]}")" | grep -q KEXEC-LOADED \
        || die "kexec failed on the board; it is still running its current OS"
fi
log "the board is restarting into the installer (its screen goes dark briefly)"

# ── 5. Follow the install ───────────────────────────────────────────────────
RESULT=""
deadline=$((SECONDS + 3600))
while (( SECONDS < deadline )); do
    if read -r -t 30 line <&3; then
        echo "  $line"
        case "$line" in
            *"PEREGRINE-INSTALL: OK"*) RESULT=OK; break ;;
            *"PEREGRINE-INSTALL: FAILED"*) RESULT=FAILED; break ;;
        esac
    fi
done

case "$RESULT" in
    OK)
        log "installed. The board is rebooting into Peregrine."
        echo ""
        echo "  It gets new SSH host keys, so clear the old ones first:"
        echo "    ssh-keygen -R $HOST; ssh-keygen -R peregrine.local"
        echo "  Then, once it is up (first boot takes a minute or two):"
        echo "    ssh trailcurrent@peregrine.local      (password: trailcurrent)"
        ;;
    FAILED)
        die "the installer reported a failure — the board's screen shows the step and a shell"
        ;;
    *)
        die "no result from the installer within an hour — check the board's screen"
        ;;
esac
