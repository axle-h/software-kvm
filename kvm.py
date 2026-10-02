#!/usr/bin/env python3
"""Keep a Logitech keyboard + mouse and a DDC/CI monitor on the same computer.

Easy-Switch a device away from this computer and the other devices plus the monitor
follow it. Easy-Switch a device back and the monitor follows. Devices on the other
computer can't be reached from here, so each one must be switched back by hand.
"""
import argparse
import glob
import logging
import os
import re
import select
import subprocess
import time
import tomllib
from pathlib import Path

import hidpp

CONFIG_PATH = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'software-kvm' / 'config.toml'

# MCCS VCP 0x60 input source values; monitors may add vendor-specific ones (e.g. USB-C)
INPUT_NAMES = {0x01: 'VGA-1', 0x02: 'VGA-2', 0x03: 'DVI-1', 0x04: 'DVI-2',
               0x0F: 'DP-1', 0x10: 'DP-2', 0x11: 'HDMI-1', 0x12: 'HDMI-2'}

log = logging.getLogger('kvm')


class Config:
    def __init__(self, path):
        try:
            data = tomllib.loads(path.read_text())
        except FileNotFoundError:
            raise SystemExit(f'no config at {path}; copy config.example.toml there and run "kvm.py probe"')
        monitor = data.get('monitor', {})
        self.other_host = data['other_channel'] - 1
        self.active_window = data.get('active_window', 120)
        self.this_input = parse_input(monitor['this_input'])
        self.other_input = parse_input(monitor['other_input'])
        self.monitor_serial = monitor.get('serial')
        self.monitor_connector = monitor.get('connector')


def parse_input(value):
    if isinstance(value, int):
        return value
    names = {name.lower(): code for code, name in INPUT_NAMES.items()}
    return names[value.lower()] if value.lower() in names else int(value, 0)


def input_name(code):
    return f'0x{code:02x}' + (f' ({INPUT_NAMES[code]})' if code in INPUT_NAMES else '')


class Monitor:
    def __init__(self, connector_path):
        self.connector = os.path.basename(connector_path).split('-', 1)[1]
        edid = open(f'{connector_path}/edid', 'rb').read()
        descriptors = {d[3]: d[5:].split(b'\n')[0].decode(errors='replace').strip()
                       for d in (edid[i:i + 18] for i in range(54, 126, 18)) if d[:3] == b'\0\0\0'}
        self.model = descriptors.get(0xFC, '?')
        self.serial = descriptors.get(0xFF, '')
        # DisplayPort carries DDC over its AUX channel (connector/i2c-N); others use the ddc link
        aux = glob.glob(f'{connector_path}/i2c-*')
        bus = aux[0] if aux else os.path.realpath(f'{connector_path}/ddc')
        self.bus = int(os.path.basename(bus).removeprefix('i2c-'))

    def ddc(self, *args):
        cmd = ['ddcutil', '--bus', str(self.bus), '--noverify', *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15)

    def get_input(self):
        out = self.ddc('getvcp', '60', '--brief').stdout.split()
        return int(out[-1].lstrip('x'), 16) if out else None

    def set_input(self, value):
        if self.get_input() == value:
            return
        log.info('monitor -> input %s', input_name(value))
        for _ in range(3):
            result = self.ddc('setvcp', '60', f'0x{value:02x}')
            if result.returncode == 0:
                return
            time.sleep(1)
        log.error('monitor did not accept input 0x%02x: %s', value,
                  result.stdout.strip() or result.stderr.strip())

    def supported_inputs(self):
        caps = self.ddc('capabilities').stdout
        section = re.search(r'Feature: 60 .*?\n(.*?)(?=\n\s*Feature:|\Z)', caps, re.S)
        return [int(v, 16) for v in re.findall(r'^\s+([0-9a-f]{2}):', section.group(1), re.M)] if section else []


def find_monitors():
    monitors = []
    for path in sorted(glob.glob('/sys/class/drm/card*-*')):
        name = os.path.basename(path).split('-', 1)[1]
        if name.startswith(('eDP', 'LVDS', 'DSI', 'Writeback')):
            continue
        # sysfs reports edid as size 0, so read it to see whether there is one
        if open(f'{path}/status').read().strip() == 'connected' and open(f'{path}/edid', 'rb').read(1):
            monitors.append(Monitor(path))
    return monitors


def select_monitor(config):
    monitors = find_monitors()
    if config.monitor_serial:
        monitors = [m for m in monitors if m.serial == config.monitor_serial]
    if config.monitor_connector:
        monitors = [m for m in monitors if m.connector == config.monitor_connector]
    if len(monitors) != 1:
        raise RuntimeError(f'expected one matching monitor, found {len(monitors)}; '
                           'set monitor.serial or monitor.connector in the config')
    return monitors[0]


def open_receiver():
    receivers = hidpp.find_receivers()
    if not receivers:
        raise RuntimeError('no Logitech receiver found')
    return hidpp.Receiver(receivers[0][0])


class Kvm:
    def __init__(self, config, dry_run=False):
        self.config = config
        self.dry_run = dry_run
        self.rx = open_receiver()
        self.devices = {}       # device index -> type, e.g. 'keyboard'
        self.sent_away = set()  # devices we switched ourselves, so their link drop is expected
        kbd, mouse = hidpp.input_nodes(self.rx.path)
        self.inputs = {os.open(p, os.O_RDONLY | os.O_NONBLOCK): k
                       for k, p in (('keyboard', kbd), ('mouse', mouse)) if p}
        now = time.monotonic()
        self.last_input = {k: now for k in self.inputs.values()}  # starting up implies someone is at the desk
        for dev in range(1, 7):
            self.probe(dev)
        self.claim_monitor()

    def close(self):
        self.rx.close()
        for fd in self.inputs:
            os.close(fd)

    def probe(self, dev):
        if self.rx.ping(dev) is None:
            return
        try:
            if self.rx.feature_index(dev, hidpp.FEAT_CHANGE_HOST) is None:
                return
            kind = self.rx.device_type(dev)
            name = self.rx.name(dev)
        except (hidpp.HidppError, TimeoutError) as e:
            log.warning('device %d: %s', dev, e)
            return
        self.devices[dev] = kind
        log.info('device %d: %s (%s)', dev, name, kind)

    def claim_monitor(self):
        """Take the monitor if any device is with us. Covers reconnects missed while
        asleep or not running."""
        here = [dev for dev in self.devices if self.rx.ping(dev)]
        if here:
            log.info('devices %s are here', here)
            self.do(self.set_monitor, self.config.this_input)

    def set_monitor(self, value):
        select_monitor(self.config).set_input(value)

    def run(self):
        # The monotonic clock stops during suspend and the boot clock doesn't, so a jump
        # in their difference means we just resumed
        slept = time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()
        while True:
            ready, _, _ = select.select([self.rx.fd, *self.inputs], [], [], 5)
            now = time.monotonic()
            if (new_slept := time.clock_gettime(time.CLOCK_BOOTTIME) - now) - slept > 2:
                log.info('resumed from suspend')
                self.claim_monitor()
            slept = new_slept
            for fd in ready:
                if fd in self.inputs:
                    data = os.read(fd, 64)
                    kind = self.inputs[fd]
                    # The mouse interface also carries keyboard consumer keys; report 2 is pointer input
                    if kind == 'keyboard' or data[:1] == b'\x02':
                        self.last_input[kind] = now
                    continue
                data = self.rx.read(0)
                # HID++ 1.0 device connection notification: [0x10, dev, 0x41, proto, flags, ...]
                if data and data[0] == hidpp.SHORT and data[2] == 0x41:
                    self.on_link(data[1], linked=not data[4] & 0x40)

    def on_link(self, dev, linked):
        if dev not in self.devices:
            if not linked:
                return
            self.probe(dev)
            if dev not in self.devices:
                return
        kind = self.devices[dev]
        if linked:
            log.info('device %d (%s) connected', dev, kind)
            self.sent_away.discard(dev)
            self.do(self.set_monitor, self.config.this_input)
            return
        if dev in self.sent_away:
            return
        now = time.monotonic()
        idle = {k: round(now - t) for k, t in self.last_input.items()}
        if min(idle.values(), default=0) > self.config.active_window:
            log.info('device %d (%s) dropped, idle %s; assuming sleep', dev, kind, idle)
            return
        log.info('device %d (%s) switched away, idle %s; following', dev, kind, idle)
        for other in self.devices:
            if other != dev:
                self.sent_away.add(other)
                self.do(self.rx.set_host, other, self.config.other_host)
        self.do(self.set_monitor, self.config.other_input)

    def do(self, fn, *args):
        if self.dry_run:
            log.info('dry run: %s%s', fn.__name__, args)
            return
        try:
            fn(*args)
        except (hidpp.HidppError, RuntimeError, subprocess.SubprocessError) as e:
            log.error('%s%s failed: %s', fn.__name__, args, e)


def probe():
    """Print what a config needs: receivers, paired devices and their channels, monitors and inputs."""
    for path, kind in hidpp.find_receivers():
        print(f'{kind} receiver {path}')
        rx = hidpp.Receiver(path)
        for dev in range(1, 7):
            if rx.ping(dev) is None:
                continue
            try:
                count, current = rx.host_info(dev)
            except hidpp.HidppError:
                print(f'  device {dev}: {rx.name(dev)} (no Easy-Switch)')
                continue
            print(f'  device {dev}: {rx.name(dev)} ({rx.device_type(dev)})')
            for host in range(count):
                marker = '  <- this computer' if host == current else ''
                print(f'    channel {host + 1}: {rx.host_name(dev, host) or "?"}{marker}')
        rx.close()
    print('  (devices currently on another computer are not listed)')
    for m in find_monitors():
        print(f'monitor {m.connector}: {m.model}, serial "{m.serial}", i2c bus {m.bus}')
        current = m.get_input()
        if current is None:
            print('  no DDC/CI response; enable DDC/CI in the monitor menu')
            continue
        print(f'  current input: {input_name(current)}')
        print('  supported inputs: ' + ', '.join(input_name(v) for v in m.supported_inputs()))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', nargs='?', choices=['run', 'probe'], default='run',
                        help='run the daemon (default) or print devices and monitors for the config')
    parser.add_argument('--config', type=Path, default=CONFIG_PATH, help=f'config file (default {CONFIG_PATH})')
    parser.add_argument('--dry-run', action='store_true', help='log actions without performing them')
    args = parser.parse_args()
    if args.command == 'probe':
        return probe()
    config = Config(args.config)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', datefmt='%H:%M:%S')
    while True:
        kvm = None
        try:
            kvm = Kvm(config, args.dry_run)
            kvm.run()
        except (OSError, RuntimeError) as e:
            log.warning('%s; retrying in 5s', e)
            if kvm:
                kvm.close()
            time.sleep(5)


if __name__ == '__main__':
    main()
