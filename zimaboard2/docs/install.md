# Installing Peregrine on a ZimaBoard 2

This guide takes you from a fresh clone of the repository to a ZimaBoard 2
that answers "hey Peregrine". It puts Peregrine on the board's internal eMMC
from a USB stick. The USB stick carries everything: the system, the voice
models and the LLM. The board needs no network connection during the install.

The ZimaBoard 2 is Peregrine's **premium** board: a 3B language model instead
of the Radxa Dragon Q6A's 1B, for a little more money and more power. If you
are on the budget board, follow [image_build/README.md](../../image_build/README.md)
instead. The [main README](../../README.md#choose-your-board) compares them.

## What you need

- A ZimaBoard 2, **8 GB model**:
  [ZimaBoard 2 at the Zimaspace shop](https://shop.zimaspace.com/products/zimaboard2-single-board-server?sca_ref=12564611.fDJvFR9EWxuoB4a&utm_source=dave-floyd&utm_medium=affiliate&utm_campaign=zimaspace_affiliate&utm_content=Dave-Floyd&sca_source=GitHub)

  > **Affiliate link.** TrailCurrent earns a small percentage if you buy
  > through this link. It costs you nothing extra.

- A build machine: x86_64 Linux with Docker and about 20 GB free next to the
  repository
- A USB stick of 8 GB or more. Writing the installer erases the stick.
- For the normal install: a monitor on the mini-DP port and a USB keyboard.
  The unattended option needs neither (see below).
- An Ethernet cable to your TrailCurrent LAN, for after the install
- A USB speakerphone (Peregrine is developed with a Jabra Speak)

## 0. Set up the build machine (once)

The build runs inside a pinned Ubuntu 24.04 container, so Docker is the only
thing the build machine needs. On Ubuntu or Debian:

```bash
sudo apt install -y docker.io git curl
sudo usermod -aG docker "$USER"      # then log out and back in
docker info >/dev/null && echo "docker OK"
```

Then get the code:

```bash
git clone https://github.com/trailcurrentoss/TrailCurrentPeregrine.git
cd TrailCurrentPeregrine
```

## 1. Build the installer ISO

From the repository root:

```bash
./zimaboard2/image/build.sh --version 1.0
```

Run it as your normal user, not with `sudo`. It first checks the build
machine, then downloads every pinned file (llama.cpp source, the LLM, the
Whisper and Piper models) and checks each one's SHA-256. Downloads are
cached in `zimaboard2/image/cache/`, so later builds skip them.

The result is `zimaboard2/image/output/peregrine-zimaboard2-1.0.iso`, with a
`.sha256` file next to it. The same build also produces the network-install
bundle used by `netinstall.sh` (below).

Optional: install and boot the ISO in a virtual machine before touching the
board. This needs `qemu-system-x86`, `qemu-utils`, `xorriso` and `ovmf`,
KVM access (`/dev/kvm`) and 8 GB of RAM to give the VM:

```bash
./zimaboard2/image/test-vm.sh
```

## 2. Write it to the USB stick

On Linux, find the stick with `lsblk` and write the ISO to the **whole
device**, not to a partition on it:

```bash
sudo dd if=peregrine-zimaboard2-1.0.iso of=/dev/sdX bs=4M conv=fsync status=progress
```

Graphical alternatives work too: balenaEtcher, GNOME Disks ("Restore Disk
Image"), or Rufus in **DD mode** on Windows.

## 3. Firmware (BIOS) settings — once per board

Open the firmware setup with the key shown at power-on. Then:

| Setting | Value | Why |
|---|---|---|
| Boot order | USB first (or use the one-time boot menu) | Boots the installer |
| Secure Boot | Either on or off | The installer and the installed system are both signed (shim + Canonical GRUB + signed kernel) |
| Restore on AC power loss | **Power on** | The board must come back by itself after the house battery is reconnected |
| iGPU pre-allocated memory (DVMT) | Minimum | The LLM uses system RAM through the GPU driver, not this reserve |
| C-states / package C-states | Enabled | Idle power |

## 4. Install

Plug in the USB stick and power on. The installer menu offers:

1. **Install TrailCurrent Peregrine (asks before erasing anything).** This is
   the default, after 15 seconds.
2. **Install unattended.** This erases the internal eMMC without asking, then
   powers off. Use it for a headless install. It refuses to run unless it
   finds exactly one eMMC.
3. **UEFI firmware settings.**

The normal install:

1. It lists the internal disks and pre-selects the eMMC. Press Enter to accept
   it.
2. Type `ERASE` to confirm. Anything else cancels, and nothing is changed.
3. It checks the installer files against their checksums. This catches a bad
   USB write before anything is erased.
4. It partitions the disk, copies the system and the LLM model, and installs
   the bootloader. This takes a few minutes.
5. Remove the USB stick and press Enter. The board reboots into Peregrine.

If the install fails, the screen shows the failing step and drops to a
shell. The full log is in `/run/peregrine-install.log`.

## Alternative: install over the network (no USB stick)

If the board already runs a Linux OS you can SSH into with sudo (stock
ZimaOS, for example), `netinstall.sh` replaces it over the LAN:

```bash
./zimaboard2/image/netinstall.sh --check user@board    # read-only: can it be done?
./zimaboard2/image/netinstall.sh user@board            # asks you to type ERASE
```

It needs SSH **key** login to the board. On ZimaOS the user's home is
`/DATA`, owned by root, so `ssh-copy-id` cannot create `~/.ssh` there. Create
`/DATA/.ssh` with sudo first.

The script `kexec`s the running OS into the installer. The installer
downloads the live system and the full payload from your machine **into
RAM** and verifies every checksum before it erases anything. If anything
fails before that point, a power cycle brings the old OS back. Progress
streams to your terminal, and the board reboots into Peregrine when done.
Your machine's firewall must allow the board to reach TCP port 8765
(`--http-port` changes it).

## 5. First boot

The first boot takes longer than later boots, because it mints the board's
own machine ID and web-UI TLS certificate and grows the root filesystem to
fill the eMMC.

Connect Ethernet and the speakerphone, then:

```bash
ssh trailcurrent@peregrine.local        # password: trailcurrent
```

The first login runs a setup wizard. It forces a password change and can
configure MQTT. After that:

```bash
peregrine-self-test        # audio, iGPU (Vulkan), llama-server, bridge, wake word
peregrine-model list       # installed LLMs and which one is active
```

Then say *"hey Peregrine, what should I check on my trailer tires?"*.
Device commands such as *"turn on all the lights"* work once MQTT points at
your TrailCurrent broker (the wizard asks; see the main README's
"Configuring MQTT").

The self-test checks for the iGPU's Vulkan device even though the LLM runs on
the CPU by default; see [benchmarks.md](benchmarks.md) for why.

## Changing the LLM

The installer bakes in Llama-3.2-3B-Instruct (Llama 3.2 licence, the same
family as the Q6A's 1B model). To use a different GGUF, copy it onto the
board, then:

```bash
sudo peregrine-model import /path/to/Model-Q4_K_M.gguf
sudo peregrine-model set Model-Q4_K_M      # smoke-tested; rolls back on failure
```

The 32 GB eMMC has room for a second model. Put any more on a SATA disk.

## Updating and reinstalling

There is no `deploy.sh` for the ZimaBoard; the repo-root `deploy.sh` is for
the Q6A and refuses to run against a ZimaBoard. To take new code, build a new
ISO from an updated clone and reinstall, either from a USB stick or with
`netinstall.sh`. Run `netinstall.sh --check` first: it reports, without
changing anything, whether the board's current system can be replaced over
the network.

## Reinstalling

Boot the USB stick again. A reinstall erases everything on the eMMC,
`~/assistant.env` and the web-UI certificate included. Copy them off first
if you want to keep them.
