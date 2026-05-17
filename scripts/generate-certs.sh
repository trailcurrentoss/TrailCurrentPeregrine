#!/usr/bin/env bash
# ============================================================================
# TrailCurrent Peregrine — TLS certificate generator
#
# Mints a self-signed CA + server certificate for the Peregrine board so the
# LAN-facing chat UI can be served over HTTPS. Models the Headwaters setup:
#
#   - CA valid 10 years (3650 days)
#   - Server cert valid 825 days (Apple/iOS upper limit)
#   - CA is preserved across runs so devices that already trust it stay valid
#   - Server cert SAN auto-detects all local IPv4 addresses and always
#     includes ``peregrine.local`` so the mDNS hostname matches
#
# Outputs (all 0644 except keys 0640, owned by trailcurrent:trailcurrent):
#   /home/trailcurrent/certs/ca.key       Private CA key (NEVER leaves the board)
#   /home/trailcurrent/certs/ca.pem       Public CA cert (distribute to clients)
#   /home/trailcurrent/certs/server.key   Server private key (read by peregrine-chat)
#   /home/trailcurrent/certs/server.crt   Server cert (sent in TLS handshake)
#
# Usage:
#   sudo ./generate-certs.sh              # idempotent — reuse CA if present
#   sudo CERTS_DIR=/tmp/certs ./generate-certs.sh   # override output dir
#   sudo FORCE_SERVER_CERT=1 ./generate-certs.sh    # regenerate server cert
#                                                     even if it still exists
#
# Intended to run at first boot (peregrine-firstboot.sh) and on demand for
# renewal. Safe to re-run; never destroys the CA.
# ============================================================================

set -euo pipefail

CERTS_DIR="${CERTS_DIR:-/home/trailcurrent/certs}"
OWNER="${CERT_OWNER:-trailcurrent:trailcurrent}"
HOSTNAME_FQDN="${PEREGRINE_HOSTNAME:-peregrine.local}"
CA_DAYS=3650
SERVER_DAYS=825

log()  { echo "[gen-certs] $*"; }
fail() { echo "[gen-certs] ERROR: $*" >&2; exit 1; }

command -v openssl >/dev/null || fail "openssl not installed"

mkdir -p "$CERTS_DIR"
chmod 755 "$CERTS_DIR"

# ── Build the SAN list: hostnames + auto-detected IPs ─────────────────────
SAN_ENTRIES=(
    "DNS:${HOSTNAME_FQDN}"
    "DNS:peregrine"
    "DNS:localhost"
    "IP:127.0.0.1"
    "IP:::1"
)

# Pick up every non-loopback IPv4 the box has so users can connect by IP too.
# `hostname -I` is what Debian/Ubuntu provide; fall back to `ip` if missing.
if command -v hostname >/dev/null && hostname -I &>/dev/null; then
    LOCAL_IPS="$(hostname -I)"
else
    LOCAL_IPS="$(ip -o -4 addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | tr '\n' ' ')"
fi
for ip in $LOCAL_IPS; do
    case "$ip" in
        127.*|169.254.*|"") continue ;;
        *) SAN_ENTRIES+=("IP:${ip}") ;;
    esac
done
SAN_LIST="$(IFS=,; echo "${SAN_ENTRIES[*]}")"

log "CN  = ${HOSTNAME_FQDN}"
log "SAN = ${SAN_LIST}"

# ── CA: generate once, reuse forever ──────────────────────────────────────
if [[ ! -f "$CERTS_DIR/ca.key" ]]; then
    log "Generating CA private key (${CA_DAYS}-day validity)"
    openssl genrsa -out "$CERTS_DIR/ca.key" 2048 2>/dev/null
    chmod 640 "$CERTS_DIR/ca.key"
fi

if [[ ! -f "$CERTS_DIR/ca.pem" ]]; then
    log "Issuing self-signed CA certificate"
    cat > "$CERTS_DIR/_ca.cnf" <<'CACFG'
[req]
distinguished_name = req_dn
x509_extensions = v3_ca
prompt = no

[req_dn]
C = US
ST = State
L = City
O = TrailCurrent
OU = Peregrine
CN = TrailCurrent-Peregrine-CA

[v3_ca]
basicConstraints = critical, CA:true
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
CACFG
    openssl req -new -x509 -days "$CA_DAYS" \
        -key "$CERTS_DIR/ca.key" \
        -out "$CERTS_DIR/ca.pem" \
        -config "$CERTS_DIR/_ca.cnf"
    rm -f "$CERTS_DIR/_ca.cnf"
    chmod 644 "$CERTS_DIR/ca.pem"
else
    log "Reusing existing CA (clients that already trust it stay valid)"
fi

# ── Server cert: regenerate when missing, when SANs changed, or on request ─
REGEN=0
if [[ ! -f "$CERTS_DIR/server.crt" || ! -f "$CERTS_DIR/server.key" ]]; then
    REGEN=1
    log "No server cert/key — will generate"
elif [[ "${FORCE_SERVER_CERT:-0}" == "1" ]]; then
    REGEN=1
    log "FORCE_SERVER_CERT=1 — regenerating server cert"
else
    # Detect SAN drift (IPs changed since last mint). Compare the existing
    # cert's SAN to what we'd write now; regenerate if they differ.
    EXISTING_SAN="$(openssl x509 -in "$CERTS_DIR/server.crt" -noout -text 2>/dev/null \
        | awk '/Subject Alternative Name/{getline; gsub(/^[ \t]+/,""); print}')"
    DESIRED_SAN="$(echo "$SAN_LIST" | sed 's/,/, /g')"
    if [[ "$EXISTING_SAN" != "$DESIRED_SAN" ]]; then
        REGEN=1
        log "SAN drift detected (IPs changed?) — regenerating server cert"
        log "  was:  $EXISTING_SAN"
        log "  now:  $DESIRED_SAN"
    fi
fi

if [[ "$REGEN" == "1" ]]; then
    openssl genrsa -out "$CERTS_DIR/server.key" 2048 2>/dev/null
    chmod 640 "$CERTS_DIR/server.key"

    cat > "$CERTS_DIR/_srv.cnf" <<SRVCFG
[req]
distinguished_name = req_dn
req_extensions = v3_server
prompt = no

[req_dn]
C = US
ST = State
L = City
O = TrailCurrent
OU = Peregrine
CN = ${HOSTNAME_FQDN}

[v3_server]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = ${SAN_LIST}
SRVCFG

    openssl req -new \
        -key "$CERTS_DIR/server.key" \
        -out "$CERTS_DIR/server.csr" \
        -config "$CERTS_DIR/_srv.cnf"

    openssl x509 -req -days "$SERVER_DAYS" \
        -in  "$CERTS_DIR/server.csr" \
        -CA  "$CERTS_DIR/ca.pem" \
        -CAkey "$CERTS_DIR/ca.key" \
        -CAcreateserial \
        -out "$CERTS_DIR/server.crt" \
        -extfile "$CERTS_DIR/_srv.cnf" \
        -extensions v3_server

    rm -f "$CERTS_DIR/_srv.cnf" "$CERTS_DIR/server.csr"
    chmod 644 "$CERTS_DIR/server.crt"
    log "Server cert issued (${SERVER_DAYS}-day validity)"
else
    log "Existing server cert still matches current SAN — leaving in place"
fi

# Ownership: cert files must be readable by the peregrine-chat service user.
chown -R "$OWNER" "$CERTS_DIR" 2>/dev/null || true

log "Done. Files in ${CERTS_DIR}:"
ls -l "$CERTS_DIR" | awk '/\.(pem|key|crt)$/{print "  " $0}'
