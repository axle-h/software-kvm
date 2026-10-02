# software-kvm

Share a Logitech keyboard, mouse and monitor between a Linux computer and another
computer, switching them all with one Easy-Switch press. Nothing needs installing on the
other computer, so it works with locked-down work machines.

Press Easy-Switch on one device and the other device and the monitor follow it.

## What you need

- **This computer:** Linux, Python 3.11+, [`ddcutil`](https://www.ddcutil.com/), and
  systemd for the service.
- **Keyboard and mouse:** Logitech devices with Easy-Switch, paired to a Logi Bolt or
  Unifying receiver on this computer. They can be paired to the other computer any way
  you like, e.g. Bluetooth.
- **Monitor:** DDC/CI support, enabled in its on-screen menu. Most external monitors
  have it.
- **The other computer:** any OS. It runs nothing.

Tested with an MX Mechanical, an MX Master 3S on a Bolt receiver, and a Dell U4320Q.

## Usage

**To the other computer:** press the other computer's Easy-Switch button on either the
keyboard or the mouse. The other device moves too, and the monitor switches to the other
computer's input.

**Back to this computer:** press this computer's Easy-Switch button on one device. The
monitor switches back as soon as it reconnects. Then press the button on the other
device.

Pressing the buttons on both devices out of habit is fine.

## Install

```sh
git clone https://github.com/axle-h/software-kvm.git
cd software-kvm
./install.sh
```

`install.sh` does the following:

- installs a udev rule (with sudo) giving the logged-in user access to Logitech
  receivers and GPU I2C buses
- creates `~/.config/software-kvm/config.toml` from `config.example.toml`
- installs a systemd user service that starts at login

On a first install, fill in the config before starting the service:

```sh
./kvm.py probe
```

```
Bolt receiver /dev/hidraw3
  device 2: MX Master 3S (mouse)
    channel 1: WORK-LAPTOP
    channel 2: Logitech Bolt receiver  <- this computer
  ...
monitor DP-2: DELL U4320Q, serial "...", i2c bus 10
  current input: 0x0f (DP-1)
  supported inputs: 0x1b, 0x0f (DP-1), 0x13, 0x11 (HDMI-1), 0x12 (HDMI-2)
```

Copy the values into the config:

- `other_channel`: the other computer's Easy-Switch button
- `this_input`: the current input
- `other_input`: the other computer's input

Then start the service:

```sh
systemctl --user enable --now software-kvm
```

Re-run `./install.sh` after moving the checkout.

## Configuration

`~/.config/software-kvm/config.toml` (see `config.example.toml`):

| Key | Meaning |
|---|---|
| `other_channel` | The other computer's Easy-Switch button, 1-3. |
| `active_window` | A device dropping off only counts as an Easy-Switch press if a keyboard or mouse was used within this many seconds (default 120). |
| `monitor.this_input` / `monitor.other_input` | Monitor inputs as a name (`DP-1`, `HDMI-2`, ...) or a raw VCP `0x60` value (e.g. `0x1b`, often USB-C). |
| `monitor.serial` / `monitor.connector` | Picks the monitor when more than one is connected. |

Restart after editing: `systemctl --user restart software-kvm`.

## How it works

`kvm.py` listens on the receiver's HID++ interface for device connection notifications.

- **A device drops off after recent input:** the daemon treats it as an Easy-Switch
  press. It sends the other devices to the same channel (HID++ feature `0x1814`, Change
  Host) and switches the monitor input over DDC/CI (VCP `0x60`).
- **A device reconnects:** the daemon switches the monitor back. It does the same at
  startup and after resuming from suspend, if a device is reachable.

The recent-input check stops a device going to sleep from sending everything away.
In testing, devices didn't report dropping off when they went to sleep, so the check may
never come into play. If a switch is ignored, nudge the mouse and press again.

DDC/CI keeps working while the monitor is showing another input, so this computer can
always take the monitor back.

`hidpp.py` is a small HID++ client using only the standard library.

## Limitations

- **Coming back takes one press per device.** While the keyboard and mouse are on the
  other computer, this computer can't reach them. Making that a single press would need
  software on the other computer, or a USB HID bridge such as a CH9329 cable, which would
  let this computer type and point for it.
- **Waking from suspend is untested.** Waking this computer by switching a device back
  depends on USB wakeup support for the receiver and the hubs between it and the
  computer.
- **Only one receiver is supported.** If several are plugged in, the first one is used.

## Troubleshooting

```sh
journalctl --user -u software-kvm -f        # live log
./kvm.py --dry-run                          # log what would happen without doing it
systemctl --user disable --now software-kvm # turn it off
```

Each switch logs how long the keyboard and mouse have been idle, for example
`switched away, idle {'keyboard': 9, 'mouse': 1}`. A drop ignored as sleep logs
`assuming sleep`.

If `probe` shows no devices or says permission denied, check that the udev rule is
installed and log out and back in.

## License

MIT
