# MQTT CA certificate — install and rotation

Peregrine uses TLS to connect to the TrailCurrent Headwaters MQTT broker
(self-signed CA, not a public CA). This doc covers where the CA cert
lives on the board, how to install it, when it needs to be rotated, and
how to roll it out across multiple boards.

## Where it lives

| Item | Value |
|---|---|
| Cert path on the board | `/home/trailcurrent/ca.pem` |
| Env var pointing at it | `MQTT_CA_CERT` (set in `~/assistant.env`) |
| Owning user | `trailcurrent` |
| Code that reads it | `src/assistant.py` → paho-mqtt's `tls_set(ca_certs=...)` |
| **Source of truth (operator)** | https://headwaters.local → **Settings** → **CA Certificate** panel |
| Source of truth (repo) | `data/keys/ca.pem` in the Headwaters repo (only if you have shell access to the host) |

The first-login wizard ([image_build/docs/05-first-login.md](../image_build/docs/05-first-login.md))
writes `MQTT_CA_CERT=/home/trailcurrent/ca.pem` into `~/assistant.env`
when you opt into TLS, but it does **not** copy the cert itself — that's
a manual `scp` step.

## Headwaters cert lifetimes

Headwaters' `scripts/generate-certs.sh` issues two certs with very
different lifetimes:

| Cert | Validity | What expires |
|---|---|---|
| **CA** (`ca.pem`) | 10 years | The trust root installed on Peregrine |
| **Server cert** (`server.crt`) | 825 days (~2.25 years) | Used by mosquitto on Headwaters; capped at 825 days by Apple/iOS requirements |

**The key invariant:** as long as the CA stays the same, server-cert
rotations do NOT require touching any Peregrine board. Mosquitto just
reloads the new server cert; every Peregrine already trusts the CA that
signed it.

You only need to roll out a new `ca.pem` to fielded boards when the CA
itself changes — either the 10-year clock runs out, or you regenerate
the CA on purpose (key compromise, organizational change, switching to
a real PKI, etc.).

## When you do NOT need to touch Peregrine

You re-ran `generate-certs.sh` on Headwaters because:

- The server cert hit its 825-day limit
- You changed the Headwaters hostname/SAN list
- You restarted mosquitto and want fresh cert material

The CA is unchanged, so every fielded Peregrine keeps working. Verify
by tailing one board's logs after the Headwaters restart:

```bash
ssh trailcurrent@peregrine.local sudo journalctl -u voice-assistant -f
```

You should see MQTT reconnect and resume publishing within a few seconds.
If you see TLS errors instead, the CA *did* change — fall through to the
rotation procedure below.

## Getting the CA off Headwaters

This is the same procedure whether you're doing an initial install or a
rotation — only the destination differs.

1. In a browser on the same LAN, go to **https://headwaters.local**
   (accept the cert warning the first time — that's the whole reason
   you're about to install the CA).
2. Open **Settings**.
3. Scroll to the **CA Certificate** panel. The PEM text is shown in a
   read-only textarea.
4. Click the **Copy** button next to the textarea. (Description on the
   page: *"Trust this CA on other MQTT/HTTPS clients (Home Assistant,
   mosquitto_sub, browsers) to talk to this system securely."*)
5. On your laptop, save it to a file:
   ```bash
   # Paste the copied PEM into the heredoc, between the markers
   cat > /tmp/ca.pem <<'EOF'
   -----BEGIN CERTIFICATE-----
   ...paste here...
   -----END CERTIFICATE-----
   EOF
   ```

**Alternative — pull it via API:**

```bash
# The -k is needed the first time because you don't trust the CA yet.
# Once you have it, swap to: --cacert /tmp/ca.pem
curl -sk https://headwaters.local/api/settings/ca-certificate \
    | jq -r .certificate > /tmp/ca.pem
```

Quick sanity check before you push it anywhere:

```bash
openssl x509 -in /tmp/ca.pem -noout -subject -dates
# Expected subject contains: CN = TrailCurrent-CA
```

## Initial install on a single board

Run this whenever you've just flashed a board, or you skipped MQTT at
the first-login wizard and want to enable it after the fact.

```bash
# 1. Get /tmp/ca.pem onto your laptop (see "Getting the CA off Headwaters" above)

# 2. Push it to the Peregrine board
scp /tmp/ca.pem trailcurrent@peregrine.local:/home/trailcurrent/ca.pem

# 3. Confirm assistant.env points at it (it does if the wizard set up MQTT with TLS)
ssh trailcurrent@peregrine.local "grep -E '^MQTT_(USE_TLS|CA_CERT)=' ~/assistant.env"
# Expected:
#   MQTT_USE_TLS=true
#   MQTT_CA_CERT=/home/trailcurrent/ca.pem
```

If `assistant.env` is missing those lines (you skipped TLS at the
wizard), add them by hand:

```bash
ssh trailcurrent@peregrine.local
nano ~/assistant.env
# Add or set:
#   MQTT_USE_TLS=true
#   MQTT_CA_CERT=/home/trailcurrent/ca.pem
exit
```

Then restart the assistant:

```bash
ssh -t trailcurrent@peregrine.local sudo systemctl restart voice-assistant
```

## Rotating the CA on fielded boards

This is the procedure when the CA cert itself has changed (10-year
expiry, or a deliberate regeneration). The mechanics are the same as
the initial install — paho-mqtt re-reads `~/ca.pem` on each TLS
handshake, so overwriting the file and restarting `voice-assistant`
is enough.

**Step 1 — Grab the new CA from Headwaters.** Use the
[Getting the CA off Headwaters](#getting-the-ca-off-headwaters) section
above. The textarea on the Settings page always shows the **currently
served** CA, so right after Headwaters' `generate-certs.sh` runs you
just refresh the page and copy again.

Confirm you're holding the new one, not the old:

```bash
openssl x509 -in /tmp/ca.pem -noout -enddate
# notAfter should be ~10 years in the future from the rotation date
```

**Step 2 — Roll out to one board first.**

```bash
scp /tmp/ca.pem trailcurrent@peregrine.local:/home/trailcurrent/ca.pem
ssh -t trailcurrent@peregrine.local sudo systemctl restart voice-assistant

# Watch the reconnect
ssh trailcurrent@peregrine.local sudo journalctl -u voice-assistant -n 50 --no-pager
```

If that one board reconnects cleanly, proceed to the rest.

**Step 3 — Roll out to the fleet.** Fail-stop loop so a bad cert
doesn't get silently sprayed across every board:

```bash
NEW_CA=/tmp/ca.pem
for host in peregrine-shop.local peregrine-truck.local peregrine-shed.local; do
    echo "=== $host ==="
    scp "$NEW_CA" "trailcurrent@$host:/home/trailcurrent/ca.pem" \
        && ssh -t "trailcurrent@$host" 'sudo systemctl restart voice-assistant' \
        || { echo "FAIL on $host"; break; }
done
```

**Order of operations matters.** If you regenerated the CA *and* the
server cert at the same time on Headwaters, fielded boards have
already lost MQTT — you're racing to push the new CA out as fast as
possible. To avoid that race, you can keep mosquitto running on the
old server cert briefly while you roll the new CA to every Peregrine
*first*, then restart Headwaters to pick up the new server cert.
Whether that's worth the complexity depends on fleet size; for a
handful of boards just push fast.

## Verify after rotation

On each board:

```bash
ssh trailcurrent@peregrine.local

# 1. Confirm the file landed and the expiry is the new one
openssl x509 -in ~/ca.pem -noout -subject -dates

# 2. Confirm voice-assistant is healthy and reconnected to MQTT
systemctl status voice-assistant --no-pager
sudo journalctl -u voice-assistant -n 50 --no-pager | grep -i 'mqtt\|tls\|connect'
```

A healthy MQTT reconnect log looks like one of:

```
MQTT connected to <broker>:8883 (TLS)
```

A bad CA looks like:

```
[SSL: CERTIFICATE_VERIFY_FAILED] ...
```

If you see verify-failed errors, the cert on the board doesn't match
what mosquitto is presenting. Confirm `~/ca.pem` is the same file as
Headwaters' `data/keys/ca.pem` (compare with `sha256sum`).

## Proactive expiry checks

The CA only rolls once a decade, so it's the kind of thing you forget
about. Two ways to get warned ahead of time:

**On-board, ad-hoc:**

```bash
# Days remaining on the CA installed on the board
ssh trailcurrent@peregrine.local \
    "openssl x509 -in ~/ca.pem -noout -enddate"
```

**Set a calendar reminder** for ~30 days before the CA's `notAfter`
date. The current CA's expiry is recorded in the Headwaters repo:

```bash
openssl x509 -in TrailCurrentHeadwaters/data/keys/ca.pem -noout -enddate
```

There is no automated monitoring of cert expiry on the board today.
If/when fleet size justifies it, the obvious place is a periodic
systemd timer that pings something if `notAfter` is within N days.

## Related

- Initial setup of TLS at first boot → [image_build/docs/05-first-login.md](../image_build/docs/05-first-login.md#step-2--mqtt-broker-optional)
- The `assistant.env` template → [image_build/files/env/assistant.env.example](../image_build/files/env/assistant.env.example)
- Software releases (use the same multi-board-loop pattern) → [software-releases.md](software-releases.md)
- Headwaters cert generator → `TrailCurrentHeadwaters/scripts/generate-certs.sh`
