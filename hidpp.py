"""Minimal HID++ client for a Logitech receiver's hidraw node."""
import glob
import os
import select
import time

SHORT, LONG = 0x10, 0x11
SW_ID = 0x0B

FEAT_NAME = 0x0005
FEAT_CHANGE_HOST = 0x1814
FEAT_HOSTS_INFO = 0x1815

# USB product IDs of receivers that speak HID++ with paired devices
RECEIVERS = {
    0xC548: 'Bolt',
    0xC52B: 'Unifying',
    0xC532: 'Unifying',
}

DEVICE_TYPES = {0: 'keyboard', 1: 'remote', 2: 'numpad', 3: 'mouse', 4: 'trackpad',
                5: 'trackball', 6: 'presenter', 7: 'receiver'}


def _hidraw_nodes():
    """Yield (sysfs node, USB device path, product id, report descriptor) for Logitech hidraw nodes."""
    for node in sorted(glob.glob('/sys/class/hidraw/hidraw*')):
        uevent = open(f'{node}/device/uevent').read()
        hid_id = next((line for line in uevent.splitlines() if line.startswith('HID_ID=')), '')
        bus, vendor, product = hid_id.removeprefix('HID_ID=').split(':')
        if vendor != '0000046D':
            continue
        # hidraw/device is the HID device; its parent is the USB interface, then the USB device
        usb_device = os.path.dirname(os.path.dirname(os.path.realpath(f'{node}/device')))
        desc = open(f'{node}/device/report_descriptor', 'rb').read()
        yield node, usb_device, int(product, 16), desc


def find_receivers():
    """Return [(hidraw path, receiver kind)] for each receiver's HID++ (vendor page 0xFF00) interface."""
    return [('/dev/' + os.path.basename(node), RECEIVERS[product])
            for node, _, product, desc in _hidraw_nodes()
            if product in RECEIVERS and desc[:3] == b'\x06\x00\xff']


def input_nodes(receiver_path):
    """Return (keyboard, mouse) hidraw paths of the boot interfaces on the same receiver."""
    hidraws = list(_hidraw_nodes())
    usb = next(dev for node, dev, _, _ in hidraws if node.endswith('/' + os.path.basename(receiver_path)))
    nodes = {}
    for node, dev, _, desc in hidraws:
        if dev != usb:
            continue
        if desc[:4] == b'\x05\x01\x09\x06':
            nodes['keyboard'] = '/dev/' + os.path.basename(node)
        elif desc[:4] == b'\x05\x01\x09\x02':
            nodes['mouse'] = '/dev/' + os.path.basename(node)
    return nodes.get('keyboard'), nodes.get('mouse')


class HidppError(Exception):
    pass


class Receiver:
    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self._features = {}

    def close(self):
        os.close(self.fd)

    def read(self, timeout):
        r, _, _ = select.select([self.fd], [], [], timeout)
        return os.read(self.fd, 64) if r else None

    def request(self, dev, feat_idx, func, params=b'', timeout=3.0, reply=True):
        # Idle devices can take over a second to answer, hence the generous timeout
        msg = bytes([LONG, dev, feat_idx, (func << 4) | SW_ID]) + params
        os.write(self.fd, msg.ljust(20, b'\0'))
        if not reply:
            return None
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            data = self.read(left)
            if not data or data[1] != dev:
                continue
            # HID++ 2.0 error: [0x11, dev, 0xFF, feat_idx, func|sw, err]
            if data[2] == 0xFF and data[3] == feat_idx and data[4] == msg[3]:
                raise HidppError(f'dev {dev} feat {feat_idx} func {func}: error {data[5]}')
            # HID++ 1.0 error (e.g. device not connected): [0x10, dev, 0x8F, ...]
            if data[2] == 0x8F and data[3] == feat_idx:
                raise HidppError(f'dev {dev}: hid++1.0 error {data[5]}')
            if data[2] == feat_idx and data[3] == msg[3]:
                return data[4:]
        raise TimeoutError(f'dev {dev} feat {feat_idx} func {func}')

    def feature_index(self, dev, feature_id):
        """Return the device's index for a feature, or None if it doesn't have it."""
        key = (dev, feature_id)
        if key not in self._features:
            resp = self.request(dev, 0, 0, feature_id.to_bytes(2, 'big'))
            self._features[key] = resp[0] or None
        return self._features[key]

    def _require(self, dev, feature_id):
        idx = self.feature_index(dev, feature_id)
        if idx is None:
            raise HidppError(f'dev {dev} lacks feature 0x{feature_id:04x}')
        return idx

    def ping(self, dev):
        """Return HID++ protocol version tuple, or None if the device is not reachable."""
        try:
            resp = self.request(dev, 0, 1, b'\0\0\x5A')
            return resp[0], resp[1]
        except (HidppError, TimeoutError):
            return None

    def name(self, dev):
        idx = self._require(dev, FEAT_NAME)
        length = self.request(dev, idx, 0)[0]
        out = b''
        while len(out) < length:
            out += self.request(dev, idx, 1, bytes([len(out)]))[:length - len(out)]
        return out.decode(errors='replace')

    def device_type(self, dev):
        idx = self._require(dev, FEAT_NAME)
        return DEVICE_TYPES.get(self.request(dev, idx, 2)[0], 'unknown')

    def host_info(self, dev):
        """Return (host count, current host) with 0-based host numbers."""
        resp = self.request(dev, self._require(dev, FEAT_CHANGE_HOST), 0)
        return resp[0], resp[1]

    def set_host(self, dev, host):
        """Switch the device to a 0-based host channel. The device drops off immediately, so no reply."""
        self.request(dev, self._require(dev, FEAT_CHANGE_HOST), 1, bytes([host]), reply=False)

    def host_name(self, dev, host):
        """Return the name a host gave itself when pairing, or None if unknown."""
        idx = self.feature_index(dev, FEAT_HOSTS_INFO)
        if idx is None:
            return None
        length = self.request(dev, idx, 1, bytes([host]))[4]
        out = b''
        while len(out) < length:
            chunk = self.request(dev, idx, 3, bytes([host, len(out)]))[2:]
            if not chunk.strip(b'\0'):
                break
            out += chunk[:length - len(out)]
        return out.decode(errors='replace') or None
