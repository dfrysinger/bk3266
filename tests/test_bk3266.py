"""Offline tests: CRC/format helpers, patching, pklg parsing, and dump/write
against a simulated BK3266 bootloader (no hardware needed)."""
import contextlib
import importlib.util
import io
import random
import struct
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'bk3266' / 'scripts' / 'bk3266.py'
spec = importlib.util.spec_from_file_location('bk3266', SCRIPT)
bk = importlib.util.module_from_spec(spec)
sys.modules['bk3266'] = bk
spec.loader.exec_module(bk)

SIZE = 64 * 1024          # JEDEC capacity byte 0x10
CFG = 0x8000              # logical config base used by the synthetic image


def synthetic_image(seed=1):
    rnd = random.Random(seed)
    logical = bytearray(rnd.randrange(256) for _ in range((SIZE // 34) * 32))
    logical[CFG] = 1                                                   # used
    logical[CFG + 0x22:CFG + 0x24] = (3000).to_bytes(2, 'little')      # sleep_timeout
    logical[CFG + 0x34A:CFG + 0x34E] = (0x001810C7).to_bytes(4, 'little')
    logical[CFG + 0xE5] = 0                                            # bank 0
    for slot in range(bk.WAVE_SLOTS):
        logical[CFG + 0x424 + slot * 3:CFG + 0x427 + slot * 3] = b'\x02\x10\x00' if slot < 6 else b'\x00\x00\x00'
    for b in range(1, 5):
        start = CFG + 0x424 + b * bk.WAVE_BANK_STRIDE
        logical[start:start + 3 * bk.WAVE_SLOTS] = bytes(3 * bk.WAVE_SLOTS)
    raw = bytearray(b'\xff' * SIZE)
    for i in range(0, len(logical), 32):
        chunk = logical[i:i + 32]
        r = i // 32 * 34
        raw[r:r + 32] = chunk
        raw[r + 32:r + 34] = bk.crc16(chunk).to_bytes(2, 'big')
    return bytes(raw)


class FakeBootloader:
    """Minimal model of the ROM bootloader: needs both handshakes, enforces protection."""

    def __init__(self, flash):
        self.flash = bytearray(flash)
        self.sr = bytearray(b'\x00\x40')
        self.out = bytearray()
        self.inbuf = bytearray()
        self.hello = False
        self.reboots = 0
        self.erases = []
        self.writes = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def reset_input_buffer(self):
        self.out.clear()

    def read(self, n):
        data, self.out = bytes(self.out[:n]), self.out[n:]
        return data

    def reply(self, cmd, status, payload=b''):
        body = bytes([cmd, status]) + payload
        length = 5 if cmd == bk.CMD_SPI else len(body)   # mimic the real length quirk
        self.out += bk.EXT_RX + struct.pack('<H', length) + body

    def write(self, data):
        self.inbuf += data
        while True:
            if self.inbuf.startswith(bk.REBOOT):
                del self.inbuf[:6]
                self.reboots += 1
                self.hello = False
            elif self.reboots and self.inbuf:
                self.inbuf.clear()          # application running: bootloader no longer answers
            elif self.inbuf.startswith(bk.LINKCHECK):
                del self.inbuf[:5]
                self.out += bk.LINKCHECK_ACK
            elif self.inbuf.startswith(bk.HELLO):
                del self.inbuf[:6]
                self.hello = True
                self.out += bk.HELLO_ACK
            elif self.inbuf.startswith(bk.EXT_TX) and len(self.inbuf) >= 7:
                n = int.from_bytes(self.inbuf[5:7], 'little')
                if len(self.inbuf) < 7 + n:
                    break
                payload = bytes(self.inbuf[7:7 + n])
                del self.inbuf[:7 + n]
                self.handle(payload[0], payload[1:])
            else:
                break
        return len(data)

    def handle(self, cmd, args):
        assert self.hello, 'extended command before AA/55 handshake'
        protected = bytes(self.sr) != b'\x00\x00'
        if cmd == bk.CMD_SPI:
            self.reply(cmd, 0, bytes.fromhex('ff 85 60 10'))
        elif cmd == bk.CMD_RDSR:
            self.reply(cmd, 0, bytes([args[0], self.sr[0 if args[0] == 5 else 1]]))
        elif cmd == bk.CMD_WRSR:
            self.sr[:] = args[1:3]
            self.reply(cmd, 0, args)
        elif cmd == bk.CMD_READ:
            a = int.from_bytes(args[:4], 'little')
            self.reply(cmd, 0, args[:4] + bytes(self.flash[a:a + 4096]))
        elif cmd == bk.CMD_ERASE:
            a = int.from_bytes(args[:4], 'little')
            if not protected:
                self.flash[a:a + 4096] = b'\xff' * 4096
                self.erases.append(a)
            self.reply(cmd, 0, args[:4])
        elif cmd == bk.CMD_WRITE:
            a = int.from_bytes(args[:4], 'little')
            if not protected:
                for i, b in enumerate(args[4:]):
                    self.flash[a + i] &= b
                self.writes.append(a)
            self.reply(cmd, 0, args[:4])


class FakeRunningFirmware(FakeBootloader):
    """Headset already on with its debug UART enabled: the firmware ACKs LinkCheck and prints
    log text but never answers the second handshake. After `boot_after` LinkChecks the owner
    has power-cycled it into the real bootloader."""

    def __init__(self, flash, boot_after=3):
        super().__init__(flash)
        self.left = boot_after

    def write(self, data):
        if self.left > 0:
            if data.startswith(bk.LINKCHECK):
                self.left -= 1
                self.out += bk.LINKCHECK_ACK + b'+++adc:3334(mV),adc_avr:3334(mV)\r\n'
            return len(data)
        return super().write(data)


def run(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        bk.main(argv)
    return buf.getvalue()


class FormatTests(unittest.TestCase):
    def test_crc_of_zeros(self):
        self.assertEqual(bk.crc16(bytes(32)), 0x8029)

    def test_address_round_trip(self):
        for logical in (0, 31, 32, 0x75000, 0x75433):
            self.assertEqual(bk.logical_of(bk.raw_of(logical)), logical)
        self.assertEqual(bk.raw_of(0x75433), 0x7C975)

    def test_strip_and_extent(self):
        raw = synthetic_image()
        self.assertEqual(bk.crc_extent(raw), SIZE // 34 * 34)
        self.assertEqual(len(bk.strip(raw)), SIZE // 34 * 32)


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / 'orig.bin'
        self.src.write_bytes(synthetic_image())

    def patch(self, *args):
        out = self.tmp / 'cand.bin'
        run(['patch', str(self.src), '-o', str(out), '--config-base', hex(CFG), *args])
        return self.src.read_bytes(), out.read_bytes()

    def test_mute_prompt_changes_type_and_crc_only(self):
        a, b = self.patch('--mute-prompt', 'low-battery')
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        self.assertIn(bk.raw_of(CFG + 0x424 + 15), diff)
        self.assertLessEqual(len(diff), 3)
        self.assertEqual(bk.crc_extent(b), bk.crc_extent(a))

    def test_clear_sniff_bit(self):
        _, b = self.patch('--clear-bits', 'feature_flag=0x80000')
        self.assertEqual(bk.decode_config(b, CFG)['fields']['feature_flag']['value'], 0x001010C7)

    def test_sleep_timeout_zero_refused(self):
        with self.assertRaises(SystemExit):
            self.patch('--set', 'sleep_timeout=0')

    def test_poke_replaces_matching_bytes_and_fixes_crc(self):
        logical = bk.strip(synthetic_image())
        old = logical[0x1000:0x1003]
        new = bytes(b ^ 0xFF for b in old)
        a, b = self.patch('--poke', f'0x1000={old.hex()}:{new.hex()}')
        self.assertEqual(bk.strip(b)[0x1000:0x1003], new)
        self.assertEqual(bk.crc_extent(b), bk.crc_extent(a))
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        self.assertTrue(all(i // 34 == 0x1000 // 32 for i in diff), 'only that record changes')

    def test_poke_refuses_when_old_bytes_differ(self):
        logical = bk.strip(synthetic_image())
        wrong = bytes(b ^ 1 for b in logical[0x1000:0x1002])
        with self.assertRaises(SystemExit):
            self.patch('--poke', f'0x1000={wrong.hex()}:0000')


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.orig = synthetic_image()
        self.fake = FakeBootloader(self.orig)
        self._open = bk.open_port
        self._settle = bk.REBOOT_SETTLE
        bk.REBOOT_SETTLE = 0
        bk.open_port = lambda *a, **k: self.fake

    def tearDown(self):
        bk.open_port = self._open
        bk.REBOOT_SETTLE = self._settle

    def mute_candidate(self):
        src = self.tmp / 'orig.bin'
        src.write_bytes(self.orig)
        cand = self.tmp / 'cand.bin'
        run(['patch', str(src), '-o', str(cand), '--config-base', hex(CFG), '--mute-prompt', 'low-battery'])
        return src, cand

    def test_connect_waits_out_running_firmware(self):
        self.fake = FakeRunningFirmware(self.orig)
        out = run(['ping', '--port', 'fake', '--wait', '5', '--evidence', str(self.tmp)])
        self.assertIn('firmware is running', out)
        self.assertIn('85 60', out)

    def test_ignore_range_tolerates_unstable_bits_elsewhere(self):
        src, cand = self.mute_candidate()
        flaky = SIZE - 0x100                       # far from the config sector being written
        self.fake.flash[flaky] ^= 0x04
        common = ['write', str(cand), '--expect', str(src), '--port', 'fake', '--wait', '2',
                  '--config-base', hex(CFG), '--yes']
        with self.assertRaises(IOError):
            run(common + ['--evidence', str(self.tmp / 'a')])
        lo = flaky // bk.SECTOR * bk.SECTOR
        self.fake = FakeBootloader(self.fake.flash)
        out = run(common + ['--evidence', str(self.tmp / 'b'), '--ignore-range', f'{lo:#x}-{lo + bk.SECTOR:#x}'])
        self.assertIn('WRITTEN_AND_VERIFIED', out)
        self.assertIn('ignoring raw', out)

    def test_ignore_range_may_not_cover_written_sector(self):
        src, cand = self.mute_candidate()
        sector = bk.raw_of(CFG) // bk.SECTOR * bk.SECTOR
        with self.assertRaises(SystemExit):
            run(['write', str(cand), '--expect', str(src), '--port', 'fake', '--config-base', hex(CFG),
                 '--ignore-range', f'{sector:#x}-{sector + bk.SECTOR + 0x2000:#x}'])

    def test_dump_two_identical_passes(self):
        out = run(['dump', '--port', 'fake', '--wait', '2', '--out', str(self.tmp)])
        self.assertIn('"identical": true', out)
        dumps = sorted(self.tmp.glob('dump-*/flash-dump-*.bin'))
        self.assertEqual([d.read_bytes() for d in dumps], [self.orig, self.orig])
        self.assertTrue(self.fake.reboots, 'dump must reboot the chip out of download mode')

    def test_no_reboot_flag_leaves_download_mode(self):
        run(['ping', '--port', 'fake', '--wait', '2', '--evidence', str(self.tmp), '--no-reboot'])
        self.assertEqual(self.fake.reboots, 0)

    def test_reboot_command(self):
        self.assertIn('Rebooted', run(['reboot', '--port', 'fake', '--wait', '2', '--evidence', str(self.tmp)]))
        self.assertTrue(self.fake.reboots)

    def test_failed_write_stays_in_download_mode(self):
        src = self.tmp / 'orig.bin'
        src.write_bytes(self.orig)
        cand = self.tmp / 'cand.bin'
        run(['patch', str(src), '-o', str(cand), '--config-base', hex(CFG), '--mute-prompt', 'low-battery'])
        self.fake.flash[0] ^= 0xFF
        with self.assertRaises(IOError):
            run(['write', str(cand), '--expect', str(src), '--port', 'fake', '--wait', '2',
                 '--config-base', hex(CFG), '--evidence', str(self.tmp), '--yes'])
        self.assertEqual(self.fake.reboots, 0)

    def test_write_requires_yes_then_writes_and_restores_protection(self):
        src = self.tmp / 'orig.bin'
        src.write_bytes(self.orig)
        cand = self.tmp / 'cand.bin'
        run(['patch', str(src), '-o', str(cand), '--config-base', hex(CFG), '--mute-prompt', 'low-battery'])
        common = ['write', str(cand), '--expect', str(src), '--port', 'fake', '--wait', '2',
                  '--config-base', hex(CFG), '--evidence', str(self.tmp)]
        self.assertIn('Dry run', run(common))
        self.assertEqual(bytes(self.fake.flash), self.orig)
        out = run(common + ['--yes'])
        self.assertIn('WRITTEN_AND_VERIFIED', out)
        self.assertEqual(bytes(self.fake.flash), cand.read_bytes())
        self.assertEqual(bytes(self.fake.sr), b'\x00\x40')
        self.assertEqual(len(self.fake.erases), 1)
        self.assertEqual(self.fake.reboots, 3, 'reboot sent 3x, as the ToolKit does, after a verified write')

    def test_write_aborts_if_device_differs_from_expect(self):
        src = self.tmp / 'orig.bin'
        src.write_bytes(self.orig)
        cand = self.tmp / 'cand.bin'
        run(['patch', str(src), '-o', str(cand), '--config-base', hex(CFG), '--mute-prompt', 'low-battery'])
        self.fake.flash[0] ^= 0xFF
        with self.assertRaises(IOError):
            run(['write', str(cand), '--expect', str(src), '--port', 'fake', '--wait', '2',
                 '--config-base', hex(CFG), '--evidence', str(self.tmp), '--yes'])
        self.assertEqual(self.fake.erases, [])

    def test_code_sector_refused(self):
        src = self.tmp / 'orig.bin'
        src.write_bytes(self.orig)
        cand = bytearray(self.orig)
        cand[10] ^= 1
        (self.tmp / 'cand.bin').write_bytes(cand)
        with self.assertRaises(SystemExit):
            run(['write', str(self.tmp / 'cand.bin'), '--expect', str(src), '--port', 'fake', '--config-base', hex(CFG)])


class PklgTests(unittest.TestCase):
    def test_disconnect_reason_and_mode_change(self):
        def rec(kind, data, t=0x6000000000000000):
            body = t.to_bytes(8, 'big') + bytes([kind]) + data
            return len(body).to_bytes(4, 'big') + body
        blob = (rec(0x01, bytes.fromhex('0c 08 00 0b 00 06 5d 00 21 43'))  # remote version
                + rec(0x01, bytes.fromhex('14 06 00 0b 00 02 00 02'))    # mode change -> sniff, 512 slots
                + rec(0x01, bytes.fromhex('05 04 00 0b 00 08')))          # disconnect, reason 0x08
        path = Path(tempfile.mkdtemp()) / 'c.pklg'
        path.write_bytes(blob)
        out = run(['pklg', str(path)])
        self.assertIn('manufacturer 0x005d subversion 0x4321', out)
        self.assertIn('sniff interval 320ms', out)
        self.assertIn('reason 0x08 Connection (Supervision) Timeout', out)

    def test_device_filter_and_summary(self):
        def rec(data):
            body = (0x6000000000000000).to_bytes(8, 'big') + b'\x01' + data
            return len(body).to_bytes(4, 'big') + body

        def conn(h, addr):   # Connection Complete: status, handle, bdaddr (little-endian)
            return rec(bytes([0x03, 11, 0]) + h.to_bytes(2, 'little') + bytes.fromhex(addr.replace(':', ''))[::-1] + b'\x01\x00')

        def ver(h, lmp, mfr):
            return rec(bytes([0x0C, 8, 0]) + h.to_bytes(2, 'little') + bytes([lmp]) + struct.pack('<HH', mfr, 0x1450))

        def drop(h, reason):
            return rec(bytes([0x05, 4, 0]) + h.to_bytes(2, 'little') + bytes([reason]))
        headset, watch = '27:72:1c:9c:a9:ff', '08:95:42:8e:39:34'
        blob = (conn(0x29, headset) + ver(0x29, 0x09, 0x000E) + conn(0x28, watch) + ver(0x28, 0x0C, 0x004C)
                + drop(0x28, 0x16) + drop(0x29, 0x22)
                + conn(0x29, watch) + drop(0x29, 0x16))      # handle number reused by another device
        path = Path(tempfile.mkdtemp()) / 'c.pklg'
        path.write_bytes(blob)
        full = run(['pklg', str(path)])
        self.assertIn('likely Beken', full)
        self.assertIn('Apple: watch', full)
        self.assertIn('disconnect reasons 0x16x2', full)
        events = run(['pklg', str(path), '--device', headset]).split('(')[0]
        self.assertIn('reason 0x22', events)
        self.assertNotIn(watch, events)
        self.assertEqual(events.count('Disconnection Complete'), 1)

        # A link that was up before the trace: found by handle, and the filter stays on after it ends.
        pre = drop(0x15, 0x13) + conn(0x10, watch) + drop(0x10, 0x16)
        path.write_bytes(pre)
        out = run(['pklg', str(path), '--handle', '0x15'])
        self.assertEqual(out.split('(')[0].count('Disconnection Complete'), 1)
        self.assertIn('already up when the trace started', run(['pklg', str(path)]))


if __name__ == '__main__':
    unittest.main()
