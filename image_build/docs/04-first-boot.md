# 4. First boot

After flashing and powering on, the board takes ~3 minutes to reach a usable
state. Here's exactly what happens.

## Timeline

| Time | Phase | What's happening |
|---|---|---|
| 0 s | Power on | EDK2 UEFI loads from SPI NOR |
| 5 s | Plymouth splash | TrailCurrent logo appears on HDMI/DisplayPort if connected |
| 15 s | Kernel + userspace | systemd starts up |
| 20 s | `peregrine-firstboot.service` runs | Regenerates machine-id, SSH host keys, expands rootfs |
| 60 s | First reboot | Service marks itself complete, triggers `systemctl reboot` |
| 90 s | Boot #2 | Plymouth splash again |
| 110 s | All services up | `genie-server`, `voice-assistant`, `cpu-performance`, `power-save-hw` |
| 130 s | mDNS announces | `peregrine.local` becomes resolvable |
| 180 s | NPU LLM model loaded | Genie server is ready to serve |

The wake-word model + STT load lazily on the first invocation, not at boot,
so the assistant is "ready" before all components are warmed up.

## What `peregrine-firstboot.service` does

```bash
# Pseudocode of /usr/local/sbin/peregrine-firstboot.sh
1. systemd-machine-id-setup        # regenerate /etc/machine-id
2. dpkg-reconfigure openssh-server  # regenerate SSH host keys
3. growpart /dev/nvme0n1 N          # expand the root partition
4. resize2fs /dev/nvme0n1pN         # expand the filesystem
5. touch /var/lib/peregrine/.firstboot-done
6. systemctl disable peregrine-firstboot
7. systemctl reboot
```

The reboot in step 7 is intentional — it makes sure systemd, the kernel, and
all daemons see the expanded root and the new identity. After that reboot
the service is disabled and never runs again.

## Watching firstboot from another machine

If you have a serial console attached, you'll see the firstboot output live.
Otherwise, after the board comes back up, check the journal:

```bash
ssh trailcurrent@peregrine.local sudo journalctl -u peregrine-firstboot --no-pager
```

Successful firstboot looks like:

```
[firstboot] Regenerated /etc/machine-id
[firstboot] Regenerated SSH host keys
[firstboot] Expanding /dev/nvme0n1 partition 3 to fill disk...
[firstboot] Root filesystem expanded
[firstboot] First-boot complete — service disabled
[firstboot] Rebooting in 3 seconds...
```

## Verifying the board is healthy

After the second boot, log in and check:

```bash
ssh trailcurrent@peregrine.local

# Check critical services
systemctl status genie-server voice-assistant --no-pager

# Check disk usage (should show full NVMe)
df -h /

# Check NPU is running
cat /sys/class/remoteproc/*/state | head -3   # one should say "running"

# Check audio devices
arecord -l   # should list the Jabra Speak (or whatever USB audio device is connected)
aplay -l
```

If any of these are wrong, run the self-test:

```bash
peregrine-self-test
```

## When something is wrong

| Symptom | Check | Fix |
|---|---|---|
| Board never appears on the network | Ethernet link, DHCP server | Connect to a router that hands out IPs |
| `peregrine.local` doesn't resolve | mDNS blocked on the network | Use the IP directly (find via DHCP server / arp scan) |
| Boot hangs at Plymouth splash | Bad image flash | Re-flash the OS image with `flash.sh --os` |
| `firstboot.service` failed | Probably `growpart` couldn't find the disk | Check `journalctl -u peregrine-firstboot` for the error |
| `voice-assistant` keeps restarting | Missing audio device, missing model | Run `peregrine-self-test`, check `journalctl -u voice-assistant` |
| `genie-server` failed | NPU CDSP not running | `cat /sys/class/remoteproc/*/state` — should be `running`. If not, the SPI NOR firmware is wrong; reflash. |

## Next

After successful first boot → [05-first-login.md](05-first-login.md)
