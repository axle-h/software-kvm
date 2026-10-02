#!/bin/sh
# Install the udev rule, a config skeleton and the systemd user service for this checkout.
set -eu
dir=$(cd "$(dirname "$0")" && pwd)
config=${XDG_CONFIG_HOME:-$HOME/.config}/software-kvm/config.toml
unit=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/software-kvm.service
rule=/etc/udev/rules.d/70-software-kvm.rules

command -v ddcutil >/dev/null || { echo "ddcutil not found; install it with your package manager" >&2; exit 1; }

if ! cmp -s "$dir/70-software-kvm.rules" "$rule"; then
    echo "Installing $rule (needs sudo)"
    sudo install -m 644 "$dir/70-software-kvm.rules" "$rule"
    sudo udevadm control --reload
    sudo udevadm trigger
fi

mkdir -p "$(dirname "$unit")"
rm -f "$unit"
sed "s|@DIR@|$dir|" "$dir/software-kvm.service.in" > "$unit"
systemctl --user daemon-reload

if [ ! -e "$config" ]; then
    mkdir -p "$(dirname "$config")"
    cp "$dir/config.example.toml" "$config"
    echo "Created $config"
    echo "Edit it using the output of '$dir/kvm.py probe', then run: systemctl --user enable --now software-kvm"
    exit 0
fi

systemctl --user enable software-kvm
systemctl --user restart software-kvm
echo "software-kvm is running; follow it with: journalctl --user -u software-kvm -f"
