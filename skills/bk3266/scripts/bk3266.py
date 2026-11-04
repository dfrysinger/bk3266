#!/usr/bin/env python3
"""bk3266 — read, inspect, patch and (carefully) write Beken BK3266 SPI flash over UART.

Offline subcommands (no hardware):  info, config, patch, prompts, strip, disasm, pklg
Device subcommands (FTDI + board):  ping, dump, write

Every device run writes a timestamped evidence directory with a full TX/RX log
(serial.jsonl) and a manifest.json. `write` is a dry run unless --yes is given.

Only pyserial is required for device commands (`pip install pyserial`).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------------
# Flash format: 34-byte records = 32 data bytes + CRC16 (big-endian)
# --------------------------------------------------------------------------
RECORD = 34
DATA = 32
SECTOR = 4096


def crc16(data: bytes) -> int:
    """CRC16, poly 0x8005, init 0xFFFF, no reflection, no final XOR."""
    value = 0xFFFF
    for byte in data:
        value ^= byte << 8
        for _ in range(8):
            value = ((value << 1) ^ (0x8005 if value & 0x8000 else 0)) & 0xFFFF
    return value


def raw_of(logical: int) -> int:
    return (logical // DATA) * RECORD + logical % DATA


def logical_of(raw: int) -> int:
    if raw % RECORD >= DATA:
        raise ValueError(f'raw {raw:#x} is a CRC byte, not data')
    return (raw // RECORD) * DATA + raw % RECORD


def crc_extent(raw: bytes) -> int:
    """Return the raw offset where the run of CRC-valid records (from 0) ends."""
    pos = 0
    while pos + RECORD <= len(raw):
        rec = raw[pos:pos + RECORD]
        if crc16(rec[:DATA]) != int.from_bytes(rec[DATA:], 'big'):
            break
        pos += RECORD
    return pos


def strip(raw: bytes, end: int | None = None) -> bytes:
    """Drop CRC bytes and return the logical image up to raw offset `end`."""
    end = crc_extent(raw) if end is None else end
    return b''.join(raw[i:i + DATA] for i in range(0, end - RECORD + 1, RECORD))


# --------------------------------------------------------------------------
# Configuration structure (names/offsets from Beken ToolKit V5.8.6 field report,
# validated against a 2021 BK3266 image; see references/config-map.md)
# --------------------------------------------------------------------------
CONFIG_BASE_BY_SIZE = {512 * 1024: 0x75000}
CONFIG_SIZE = 0x77F

# name: (offset, struct format, unit/notes)
FIELDS = {
    'used':                    (0x000, '<B', '1 = config enabled'),
    'system_flag':             (0x001, '<I', 'bitfield'),
    'vol_a2dp':                (0x009, '<B', 'default music volume index'),
    'vol_hfp':                 (0x00A, '<B', 'default call volume index'),
    'vol_wave':                (0x00B, '<B', 'prompt volume index (all prompts)'),
    'vol_mic':                 (0x00C, '<B', 'mic setting'),
    'lp_channel':              (0x00D, '<B', 'low-battery ADC channel'),
    'lp_interval':             (0x00E, '<I', '10 ms ticks (6000 = 60 s); battery check interval'),
    'lp_threshold':            (0x012, '<H', 'mV-ish; low-battery warning'),
    'lp_pd_threshold':         (0x014, '<H', 'mV-ish; low-voltage shutdown'),
    'sleep_timeout':           (0x022, '<H', '10 ms ticks; idle -> sniff/sleep. NEVER 0 (fires immediately)'),
    'powerdown_timeout':       (0x024, '<i', '10 ms ticks; no-connection power off'),
    'pause_powerdown_timeout': (0x039, '<i', '10 ms ticks; -100 = disabled'),
    'bt_flag':                 (0x059, '<I', 'bit 0x20 link recovery, 0x10 auto reconnect, 0x80 number voice'),
    'disconn_retry_action':    (0x094, '<b', 'UI radio index + 1'),
    'disconn_start':           (0x095, '<I', 'ms before recovery starts'),
    'disconn_retry_count':     (0x099, '<b', '-1 = unlimited (sentinel)'),
    'disconn_retry_interval':  (0x09A, '<I', 'ms between recovery attempts'),
    'auto_conn_count':         (0x09E, '<b', 'auto-reconnect attempts'),
    'auto_conn_interval':      (0x09F, '<H', 'ms (ToolKit clamps >= 4000)'),
    'match_timeout':           (0x0A1, '<h', 'pairing timeout s; -1 sentinel'),
    'pd_cond':                 (0x0A3, '<B', 'bit0 no-connection shutdown, bit1 pause shutdown'),
    'action_disconn':          (0x0A4, '<B', '0 wait for pairing, 1 auto reconnect'),
    'wave_lang_sel':           (0x0E5, '<B', 'selected prompt bank'),
    'feature_flag':            (0x34A, '<I', 'sw_features; bit 0x80000 = sniff (2021 firmware)'),
}
WAVE_INFO = 0x424
WAVE_BANK_STRIDE = 0x7E
WAVE_SLOTS = 42
PROMPT_NAMES = {
    0: 'power-on', 1: 'connected', 2: 'disconnected', 3: 'power-off',
    4: 'pairing', 5: 'low-battery', 6: 'hfp-ring', 13: 'vol-max', 14: 'vol-min',
    **{18 + n: f'number-{n}' for n in range(10)},
}
PROMPT_SLOTS = {v: k for k, v in PROMPT_NAMES.items()}
# Refuse values that are known to be dangerous rather than merely unusual.
GUARDS = {'sleep_timeout': lambda v: v != 0 or 'sleep_timeout=0 fires immediately; use a large value instead'}


def config_base(raw_size: int, override: int | None) -> int:
    if override is not None:
        return override
    if raw_size not in CONFIG_BASE_BY_SIZE:
        raise SystemExit(f'No known config base for {raw_size} byte flash; pass --config-base')
    return CONFIG_BASE_BY_SIZE[raw_size]


def read_logical(raw: bytes, logical: int, n: int) -> bytes:
    return bytes(raw[raw_of(logical + i)] for i in range(n))


def decode_config(raw: bytes, base: int) -> dict:
    out = {'config_logical': hex(base), 'config_raw': hex(raw_of(base)), 'fields': {}, 'prompts': []}
    for name, (off, fmt, note) in FIELDS.items():
        size = struct.calcsize(fmt)
        b = read_logical(raw, base + off, size)
        val = struct.unpack(fmt, b)[0]
        out['fields'][name] = dict(offset=hex(off), raw=hex(raw_of(base + off)), hex=b.hex(), value=val,
                                   display=hex(val) if 'flag' in name else val, note=note)
    bank = out['fields']['wave_lang_sel']['value']
    start = base + WAVE_INFO + bank * WAVE_BANK_STRIDE
    for slot in range(WAVE_SLOTS):
        rec = read_logical(raw, start + slot * 3, 3)
        if rec[0]:
            out['prompts'].append(dict(slot=slot, name=PROMPT_NAMES.get(slot, f'slot-{slot}'), type=rec[0],
                                       page=hex(int.from_bytes(rec[1:], 'little')), raw=hex(raw_of(start + slot * 3))))
    out['populated_banks'] = [b for b in range(5)
                              if any(read_logical(raw, base + WAVE_INFO + b * WAVE_BANK_STRIDE, 3 * WAVE_SLOTS))]
    return out


def patch_logical(img: bytearray, logical: int, new: bytes) -> None:
    """Write data bytes at a logical address and refresh every touched record CRC."""
    touched = set()
    for i, byte in enumerate(new):
        r = raw_of(logical + i)
        img[r] = byte
        touched.add(r // RECORD * RECORD)
    for rec in touched:
        img[rec + DATA:rec + RECORD] = crc16(img[rec:rec + DATA]).to_bytes(2, 'big')


def parse_int(text: str) -> int:
    return int(text, 0)


# --------------------------------------------------------------------------
# Serial link to the BK3266 ROM bootloader
# --------------------------------------------------------------------------
LINKCHECK = bytes.fromhex('01 e0 fc 01 00')
LINKCHECK_ACK = bytes.fromhex('04 0e 05 01 e0 fc 01 00')
# The second handshake. Without it the bootloader drops back to firmware ~0.5 s
# after the first 4 KiB read. Found in ToolKit startup (VA 0x443CE0 -> 0x4042C0).
HELLO = bytes.fromhex('01 e0 fc 02 aa 55')
HELLO_ACK = bytes.fromhex('04 0e 05 01 e0 fc aa 55')
# Leaves download mode and starts the application. The ToolKit sends it 3x after a write.
# Without it the chip stays in download mode for as long as it has power (battery boards: until flat),
# which looks like a dead headset: no LEDs, no charge light, power button ignored.
REBOOT = bytes.fromhex('01 e0 fc 02 0e a5')
EXT_TX = bytes.fromhex('01 e0 fc ff f4')
EXT_RX = bytes.fromhex('04 0e ff 01 e0 fc f4')
CMD_WRITE, CMD_READ, CMD_ERASE, CMD_RDSR, CMD_WRSR, CMD_SPI = 0x07, 0x09, 0x0B, 0x0C, 0x0D, 0x0E


def say(enabled: bool, text: str) -> None:
    print(f'>>> {text}', flush=True)
    if enabled and sys.platform == 'darwin':
        subprocess.Popen(['say', text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Link:
    def __init__(self, port, evidence: Path):
        self.port = port
        self.buffer = bytearray()
        self.log = (evidence / 'serial.jsonl').open('x')
        self.t0 = time.monotonic()

    def _rec(self, direction: str, data: bytes) -> None:
        self.log.write(json.dumps(dict(t=round(time.monotonic() - self.t0, 4), dir=direction, hex=data.hex())) + '\n')
        self.log.flush()

    def send(self, data: bytes) -> None:
        self._rec('tx', data)
        if self.port.write(data) != len(data):
            raise IOError('short serial write')

    def pump(self) -> None:
        data = self.port.read(8192)
        if data:
            self._rec('rx', data)
            self.buffer.extend(data)

    def handshake(self, packet: bytes, expected: bytes, seconds: float, every: float = 0.01) -> None:
        deadline = time.monotonic() + seconds
        nxt = 0.0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= nxt:
                self.send(packet)
                nxt = now + every
            self.pump()
            pos = self.buffer.find(expected)
            if pos >= 0:
                del self.buffer[:pos + len(expected)]
                return
            if len(self.buffer) > 16384:
                del self.buffer[:-8192]
            time.sleep(0.001)
        raise TimeoutError(f'no reply {expected.hex(" ")}')

    def connect(self, wait: float, voice: bool) -> None:
        self.port.reset_input_buffer()
        say(voice, 'Press and hold the power button now.')
        self.handshake(LINKCHECK, LINKCHECK_ACK, wait)
        self.handshake(HELLO, HELLO_ACK, 1.0)
        self.buffer.clear()
        say(voice, 'Connected. You can let go of the button.')

    def request(self, cmd: int, args: bytes = b'', seconds: float = 5) -> bytes:
        payload = bytes([cmd]) + args
        self.send(EXT_TX + struct.pack('<H', len(payload)) + payload)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.pump()
            pos = self.buffer.find(EXT_RX)
            if pos >= 0 and len(self.buffer) >= pos + 9:
                length = int.from_bytes(self.buffer[pos + 7:pos + 9], 'little')
                # Quirk: the SPI-passthrough reply advertises 5 but carries 6 bytes.
                if cmd == CMD_SPI and length == 5 and self.buffer[pos + 9] == cmd:
                    length = 6
                if not 2 <= length <= 8192:
                    raise ValueError('malformed reply length')
                if len(self.buffer) >= pos + 9 + length:
                    body = bytes(self.buffer[pos + 9:pos + 9 + length])
                    del self.buffer[:pos + 9 + length]
                    if body[0] != cmd:
                        self._rec('stale', body)
                        continue
                    if body[1] != 0:
                        raise IOError(f'command {cmd:#x} status {body[1]:#x}')
                    return body[2:]
            time.sleep(0.001)
        raise TimeoutError(f'command {cmd:#x} timed out')

    def jedec(self) -> bytes:
        """Returns manufacturer, type, capacity (leading SPI dummy byte discarded)."""
        return self.request(CMD_SPI, bytes.fromhex('9f 00 00 00'))[1:4]

    def read(self, address: int) -> bytes:
        data = self.request(CMD_READ, struct.pack('<I', address))
        if len(data) != 4100 or int.from_bytes(data[:4], 'little') != address:
            raise ValueError(f'read {address:#x}: length/address mismatch')
        return data[4:]

    def full_read(self, size: int, label: str) -> bytes:
        out = bytearray()
        for addr in range(0, size, SECTOR):
            out += self.read(addr)
            if addr % 0x10000 == 0:
                print(f'  {label}: {addr // 1024 + 4}/{size // 1024} KiB', flush=True)
        return bytes(out)

    def status(self) -> bytes:
        vals = []
        for reg in (0x05, 0x35):
            data = self.request(CMD_RDSR, bytes([reg]))
            if len(data) != 2 or data[0] != reg:
                raise ValueError('status reply mismatch')
            vals.append(data[1])
        return bytes(vals)

    def set_status(self, sr: bytes) -> None:
        echo = self.request(CMD_WRSR, b'\x01' + sr)
        if echo != b'\x01' + sr or self.status() != sr:
            raise IOError(f'status write {sr.hex()} did not verify')

    def replace_sector(self, address: int, data: bytes) -> None:
        a = struct.pack('<I', address)
        if self.request(CMD_ERASE, a, 15) != a:
            raise IOError('erase echo mismatch')
        if self.read(address) != b'\xff' * SECTOR:
            raise IOError(f'sector {address:#x} did not erase')
        if self.request(CMD_WRITE, a + data, 15) != a:
            raise IOError('program echo mismatch')
        if self.read(address) != data or self.read(address) != data:
            raise IOError(f'sector {address:#x} readback mismatch')

    def reboot(self) -> None:
        for _ in range(3):
            self.send(REBOOT)
            time.sleep(0.1)
        self.pump()

    def drain(self) -> None:
        self.buffer.clear()
        quiet = time.monotonic()
        end = quiet + 5
        while time.monotonic() < end:
            data = self.port.read(8192)
            if data:
                self._rec('drain', data)
                quiet = time.monotonic()
            if time.monotonic() - quiet >= 1:
                return
            time.sleep(0.01)


def evidence_dir(base: Path, prefix: str) -> Path:
    d = base / f'{prefix}-{time.strftime("%Y%m%d-%H%M%S")}'
    d.mkdir(parents=True)
    return d


def open_port(name: str, write_timeout: float = 3):
    try:
        import serial  # noqa: PLC0415
    except ImportError:
        raise SystemExit('pyserial is required: pip install pyserial')
    return serial.Serial(name, 115200, timeout=0, write_timeout=write_timeout, exclusive=True)


def save(path: Path, data: bytes) -> None:
    with path.open('xb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# Device commands
# --------------------------------------------------------------------------
def finish(link: 'Link', a, ok: bool) -> None:
    """End a session. Reboot into the application unless asked not to or a write failed mid-way."""
    try:
        if ok and not a.no_reboot:
            link.reboot()
            print('Rebooted the chip out of download mode.', flush=True)
        elif ok:
            print('Left in download mode (--no-reboot). Run `reboot` or disconnect power when done.', flush=True)
    finally:
        link.log.close()


def cmd_ping(a):
    ev = evidence_dir(a.evidence, 'ping')
    with open_port(a.port) as port:
        link = Link(port, ev)
        ok = False
        try:
            link.connect(a.wait, a.say)
            ident = link.jedec()
            sr = link.status()
            ok = True
        finally:
            finish(link, a, ok)
    size = 1 << ident[2]
    result = dict(jedec=ident.hex(' '), capacity_bytes=size, status_sr1_sr2=sr.hex(' '))
    (ev / 'manifest.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


def cmd_dump(a):
    ev = evidence_dir(a.out, 'dump')
    manifest = dict(dumps=[])
    with open_port(a.port) as port:
        link = Link(port, ev)
        images = []
        ok = False
        try:
            link.connect(a.wait, a.say)
            ident = link.jedec()
            size = 1 << ident[2]
            manifest.update(jedec=ident.hex(' '), capacity_bytes=size, status_sr1_sr2=link.status().hex(' '))
            print(f'Flash {ident.hex(" ")}: {size // 1024} KiB', flush=True)
            for n in range(1, a.passes + 1):
                img = link.full_read(size, f'pass {n}')
                save(ev / f'flash-dump-{n}.bin', img)
                images.append(img)
                manifest['dumps'].append(dict(file=f'flash-dump-{n}.bin', sha256=sha(img)))
                (ev / 'manifest.json').write_text(json.dumps(manifest, indent=2))
            ok = True
        finally:
            finish(link, a, ok)
    manifest['identical'] = len({sha(i) for i in images}) == 1
    (ev / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    say(a.say, 'Backup complete.' if manifest['identical'] else 'Backups differ. Do not write.')
    print(json.dumps(manifest, indent=2))
    if not manifest['identical']:
        sys.exit(2)


def cmd_write(a):
    candidate = Path(a.candidate).read_bytes()
    expect = Path(a.expect).read_bytes()
    if len(candidate) != len(expect):
        raise SystemExit('candidate and expected image sizes differ')
    sectors = sorted({i // SECTOR * SECTOR for i in range(len(expect)) if expect[i] != candidate[i]})
    diff = [i for i in range(len(expect)) if expect[i] != candidate[i]]
    print(f'{len(diff)} byte(s) differ across {len(sectors)} sector(s): {[hex(s) for s in sectors]}')
    for i in diff[:64]:
        print(f'  raw {i:#07x}: {expect[i]:02x} -> {candidate[i]:02x}')
    if not sectors:
        raise SystemExit('nothing to write')
    base = config_base(len(expect), a.config_base)
    cfg_sectors = set(range(raw_of(base) // SECTOR * SECTOR, raw_of(base + CONFIG_SIZE - 1) + 1, SECTOR))
    outside = [s for s in sectors if s not in cfg_sectors]
    if outside and not a.allow_any_sector:
        raise SystemExit(f'sectors outside the config region: {[hex(s) for s in outside]} (use --allow-any-sector)')
    if a.max_sectors and len(sectors) > a.max_sectors:
        raise SystemExit(f'{len(sectors)} sectors exceeds --max-sectors {a.max_sectors}')
    if not a.yes:
        print('Dry run. Re-run with --yes to write (practice board first; keep two matching backups).')
        return
    ev = evidence_dir(a.evidence, 'write')
    save(ev / 'expected-before.bin', expect)
    save(ev / 'candidate.bin', candidate)
    manifest = dict(expect_sha256=sha(expect), candidate_sha256=sha(candidate),
                    sectors=[hex(s) for s in sectors], state='PREPARED')

    def state(s, **kw):
        manifest.update(state=s, **kw)
        (ev / 'manifest.json').write_text(json.dumps(manifest, indent=2))
        print(f'[{s}] {kw if kw else ""}', flush=True)

    state('PREPARED')
    with open_port(a.port) as port:
        link = Link(port, ev)
        original_sr = None
        unprotected = False
        current_sector = None
        ok = False
        try:
            link.connect(a.wait, a.say)
            ident = link.jedec()
            if 1 << ident[2] != len(expect):
                raise IOError(f'flash size {1 << ident[2]} != image size {len(expect)}')
            before = link.full_read(len(expect), 'pre-image')
            save(ev / 'before.bin', before)
            if before != expect:
                raise IOError('device flash != --expect image; nothing written')
            original_sr = link.status()
            state('PREIMAGE_VERIFIED', jedec=ident.hex(' '), original_sr=original_sr.hex(' '))
            unlock = bytes.fromhex(a.unprotect_value)
            if ident[0] != 0x85 and not a.unprotect_value_confirmed:
                raise IOError(f'manufacturer {ident[0]:#x} is not Puya; check its datasheet, '
                              'then pass --unprotect-value and --unprotect-value-confirmed')
            if original_sr != unlock:
                unprotected = True
                link.set_status(unlock)
                state('UNPROTECTED')
            for s in sectors:
                current_sector = s
                link.replace_sector(s, candidate[s:s + SECTOR])
                current_sector = None
                state('SECTOR_VERIFIED', sector=hex(s))
            if unprotected:
                link.set_status(original_sr)
                unprotected = False
                state('PROTECTION_RESTORED')
            after = link.full_read(len(expect), 'post-image')
            save(ev / 'after.bin', after)
            if after != candidate:
                raise IOError('post-write full image != candidate')
            state('WRITTEN_AND_VERIFIED', after_sha256=sha(after), final_sr=link.status().hex(' '))
            say(a.say, 'Write verified.')
            ok = True
        except BaseException as exc:
            state('FAILED', error=repr(exc))
            if current_sector is not None:
                try:
                    link.drain()
                    if link.read(current_sector) != expect[current_sector:current_sector + SECTOR]:
                        link.replace_sector(current_sector, expect[current_sector:current_sector + SECTOR])
                    state('SECTOR_RESTORED', sector=hex(current_sector), original_error=repr(exc))
                except BaseException as rec:
                    state('RECOVERY_NEEDED', error=repr(exc), recovery_error=repr(rec))
            raise
        finally:
            if unprotected and original_sr is not None:
                try:
                    link.set_status(original_sr)
                    manifest['protection_restored_on_exit'] = True
                except BaseException as exc:
                    manifest['protection_restore_error'] = repr(exc)
                (ev / 'manifest.json').write_text(json.dumps(manifest, indent=2))
            if not ok:
                print('Write did not complete: chip left in download mode so you can retry or restore.', flush=True)
            finish(link, a, ok)


def cmd_reboot(a):
    ev = evidence_dir(a.evidence, 'reboot')
    with open_port(a.port) as port:
        link = Link(port, ev)
        try:
            port.reset_input_buffer()
            link.handshake(LINKCHECK, LINKCHECK_ACK, a.wait)
            link.reboot()
            link.buffer.clear()
            try:
                link.handshake(LINKCHECK, LINKCHECK_ACK, 1.0, every=0.1)
                raise SystemExit('chip still answers in download mode after reboot')
            except TimeoutError:
                print('Rebooted: chip no longer in download mode. Power it on normally.')
        finally:
            link.log.close()


# --------------------------------------------------------------------------
# Offline commands
# --------------------------------------------------------------------------
INTERESTING = re.compile(rb'(sniff|sleep|discon|reason|reconn|lp_|battery|wave|version|20[12]\d|BK32)', re.I)


def cmd_info(a):
    raw = Path(a.image).read_bytes()
    ext = crc_extent(raw)
    print(f'size {len(raw)} bytes, sha256 {sha(raw)}')
    print(f'CRC-valid records from 0: {ext // RECORD} (raw 0x0..{ext:#x}, logical 0x0..{logical_of(ext - RECORD) + DATA:#x})')
    if b'BK3266' in raw[:0x1000]:
        print('BK3266 signature found in block 0')
    try:
        base = config_base(len(raw), a.config_base)
        cfg = decode_config(raw, base)
        print(f'config at logical {cfg["config_logical"]} (raw {cfg["config_raw"]}), used={cfg["fields"]["used"]["value"]}')
        names = re.findall(rb'[\x20-\x7e]{3,32}', read_logical(raw, base, CONFIG_SIZE))
        print(f'printable strings in config (device name is usually here): {[n.decode() for n in names[:8]]}')
    except SystemExit as e:
        print(e)
    if a.strings:
        seen = set()
        for m in re.finditer(rb'[\x20-\x7e]{6,}', raw):
            s = m.group()
            if INTERESTING.search(s) and s not in seen:
                seen.add(s)
                print(f'  {m.start():#08x}  {s.decode(errors="replace")}')


def cmd_config(a):
    raw = Path(a.image).read_bytes()
    cfg = decode_config(raw, config_base(len(raw), a.config_base))
    if a.json:
        print(json.dumps(cfg, indent=2))
        return
    print(f'config logical {cfg["config_logical"]}  raw {cfg["config_raw"]}')
    for name, f in cfg['fields'].items():
        print(f'  {name:24} {str(f["display"]):>12}  @+{f["offset"]:>6} raw {f["raw"]:>8}  {f["note"]}')
    print(f'prompt banks populated: {cfg["populated_banks"]}')
    for p in cfg['prompts']:
        print(f'  slot {p["slot"]:2} {p["name"]:14} type {p["type"]} page {p["page"]}  raw {p["raw"]}')


def cmd_patch(a):
    raw = Path(a.image).read_bytes()
    base = config_base(len(raw), a.config_base)
    img = bytearray(raw)
    cur = decode_config(raw, base)['fields']

    def put(name, value):
        off, fmt, _ = FIELDS[name]
        guard = GUARDS.get(name)
        if guard and guard(value) is not True:
            raise SystemExit(guard(value))
        patch_logical(img, base + off, struct.pack(fmt, value))
        print(f'{name}: {cur[name]["value"]:#x} -> {value:#x}' if 'flag' in name else f'{name}: {cur[name]["value"]} -> {value}')

    for item in a.set or []:
        name, val = item.split('=', 1)
        put(name, parse_int(val))
    for item in a.clear_bits or []:
        name, val = item.split('=', 1)
        put(name, cur[name]['value'] & ~parse_int(val))
    for item in a.set_bits or []:
        name, val = item.split('=', 1)
        put(name, cur[name]['value'] | parse_int(val))
    bank = cur['wave_lang_sel']['value']
    for item in a.mute_prompt or []:
        slot = PROMPT_SLOTS[item] if item in PROMPT_SLOTS else parse_int(item)
        at = base + WAVE_INFO + bank * WAVE_BANK_STRIDE + slot * 3
        old = read_logical(img, at, 1)[0]
        patch_logical(img, at, b'\x00')
        print(f'prompt slot {slot} ({PROMPT_NAMES.get(slot, "?")}): type {old} -> 0 (unused)')
    diff = [i for i in range(len(raw)) if raw[i] != img[i]]
    if crc_extent(bytes(img)) < crc_extent(raw):
        raise SystemExit('patch broke CRC continuity; refusing')
    Path(a.out).write_bytes(img)
    print(f'{len(diff)} raw byte(s) changed; sectors {sorted({hex(i // SECTOR * SECTOR) for i in diff})}')
    for i in diff:
        print(f'  raw {i:#07x}: {raw[i]:02x} -> {img[i]:02x}')
    print(f'wrote {a.out} sha256 {sha(bytes(img))}')


def cmd_prompts(a):
    raw = Path(a.image).read_bytes()
    base = config_base(len(raw), a.config_base)
    image = strip(raw)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for p in decode_config(raw, base)['prompts']:
        addr = int(p['page'], 16) * 256
        size = int.from_bytes(image[addr:addr + 4], 'little')
        sbc = image[addr + 4:addr + size]
        if not sbc or sbc[0] != 0x9C:
            print(f'slot {p["slot"]}: no SBC sync at {addr:#x}, skipped')
            continue
        name = f'{p["slot"]:02d}-{p["name"]}'
        (out / f'{name}.sbc').write_bytes(sbc)
        if not a.no_wav:
            try:
                subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'sbc', '-i',
                                str(out / f'{name}.sbc'), '-y', str(out / f'{name}.wav')], check=True)
            except (FileNotFoundError, subprocess.CalledProcessError) as e:
                print(f'ffmpeg decode failed for {name}: {e}')
        rows.append(dict(slot=p['slot'], name=p['name'], logical=hex(addr), container_bytes=size, sbc_bytes=len(sbc)))
        print(f'{name}: {len(sbc)} bytes SBC')
    (out / 'manifest.json').write_text(json.dumps(rows, indent=2))


def cmd_strip(a):
    raw = Path(a.image).read_bytes()
    img = strip(raw, a.end)
    Path(a.out).write_bytes(img)
    print(f'{len(img)} logical bytes -> {a.out}')


PYBA2 = 'https://github.com/alephsecurity/pyba2'
PYBA2_COMMIT = 'b61b6b50b025a49a6e324fe92d1de71651509ab4'


def cmd_disasm(a):
    cache = Path(a.pyba2 or Path.home() / '.cache' / 'bk3266' / 'pyba2')
    if not (cache / 'isa').exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['git', 'clone', '--quiet', PYBA2, str(cache)], check=True)
        subprocess.run(['git', '-C', str(cache), 'checkout', '--quiet', PYBA2_COMMIT], check=True)
    sys.path.insert(0, str(cache))
    from isa.instructions import Instruction  # noqa: PLC0415
    data = Path(a.image).read_bytes()
    if a.raw:
        data = strip(data)
    view = memoryview(data)
    start, end = a.start, min(len(data), a.end)
    lines = []
    pos = start
    while pos < end:
        try:
            ins, n = Instruction.lift(view[pos:pos + 6], pos)
            text = str(ins) if n else None
        except Exception:
            n, text = 0, None
        if not n:
            n, text = 1, f'.byte 0x{data[pos]:02x}'
        lines.append(f'{pos:08x}: {data[pos:pos + n].hex():<12} {text}')
        pos += n
    Path(a.out).write_text('\n'.join(lines) + '\n')
    print(f'{len(lines)} lines -> {a.out}')


# PacketLogger (.pklg) parsing: iOS sysdiagnose / PacketLogger Bluetooth captures
HCI_REASONS = {
    0x05: 'Authentication Failure', 0x08: 'Connection (Supervision) Timeout',
    0x13: 'Remote User Terminated', 0x14: 'Remote Low Resources', 0x15: 'Remote Power Off',
    0x16: 'Terminated by Local Host', 0x1A: 'Unsupported Remote Feature', 0x22: 'LMP Response Timeout',
    0x28: 'Instant Passed', 0x3D: 'MIC Failure', 0x3E: 'Failed to Establish',
}
MODES = {0: 'active', 1: 'hold', 2: 'sniff', 3: 'park'}


def pklg_records(blob: bytes):
    pos = 0
    le = None
    while pos + 13 <= len(blob):
        if le is None:
            be_len = int.from_bytes(blob[pos:pos + 4], 'big')
            le = not (9 <= be_len <= len(blob) - pos - 4)
        length = int.from_bytes(blob[pos:pos + 4], 'little' if le else 'big')
        if length < 9 or pos + 4 + length > len(blob):
            break
        ts = int.from_bytes(blob[pos + 4:pos + 12], 'little' if le else 'big')
        secs, usecs = ts >> 32, ts & 0xFFFFFFFF
        yield secs + usecs / 1e6, blob[pos + 12], blob[pos + 13:pos + 4 + length]
        pos += 4 + length


def cmd_pklg(a):
    """Phone captures contain every device the phone talks to (watch, earbuds, car...). Use the summary at the
    end to identify the headset (BK3266 parts report LMP 0x09 / manufacturer 0x000e; Apple devices 0x004c),
    then re-run with --device ADDR (or --handle H for a link that was up before the trace started)."""
    blob = Path(a.capture).read_bytes()
    handles = {}
    devices = {}
    want_addr = a.device.lower() if a.device else None
    want = {int(h, 0) for h in a.handle}
    filtering = bool(want_addr or want)
    preexisting = set()     # handles with traffic but no Connection Complete: links up before the trace began
    count = 0

    def dev(addr):
        return devices.setdefault(addr, dict(connections=0, disconnects={}, version=None, name=None))

    def show(h, line, addr=None):
        addr = addr or handles.get(h)
        if h is not None and addr is None:
            preexisting.add(h)
        if not filtering:
            print(line)
        elif (addr is not None and addr == want_addr) or (h is not None and h in want):
            print(line)

    for t, kind, data in pklg_records(blob):
        count += 1
        stamp = time.strftime('%H:%M:%S', time.localtime(t)) + f'.{int(t % 1 * 1000):03d}'
        if kind == 0x00 and len(data) >= 3:  # HCI command
            op = int.from_bytes(data[:2], 'little')
            if op in (0x0803, 0x0804, 0x0406, 0x080D):
                name = {0x0803: 'Sniff Mode', 0x0804: 'Exit Sniff', 0x0406: 'Disconnect', 0x080D: 'Write Link Policy'}[op]
                p = data[3:]
                extra = ''
                if op == 0x0803 and len(p) >= 10:
                    mx, mn, att, to = struct.unpack('<HHHH', p[2:10])
                    extra = f' max {mx * 0.625:.0f}ms min {mn * 0.625:.0f}ms attempt {att} timeout {to}'
                if op == 0x0406 and len(p) >= 3:
                    extra = f' reason {p[2]:#04x} {HCI_REASONS.get(p[2], "")}'
                h = int.from_bytes(p[:2], "little") & 0xFFF
                show(h, f'{stamp}  CMD  {name} handle {h:#x}{extra}')
        elif kind == 0x01 and len(data) >= 2:  # HCI event
            code, p = data[0], data[2:]
            if code == 0x03 and len(p) >= 9 and p[0] == 0:
                h = int.from_bytes(p[1:3], 'little') & 0xFFF
                addr = ':'.join(f'{b:02x}' for b in reversed(p[3:9]))
                handles[h] = addr
                dev(addr)['connections'] += 1
                show(h, f'{stamp}  EVT  Connection Complete handle {h:#x} {addr}')
            elif code == 0x05 and len(p) >= 4:
                h = int.from_bytes(p[1:3], 'little') & 0xFFF
                addr = handles.get(h)
                if addr:
                    r = dev(addr)['disconnects']
                    r[p[3]] = r.get(p[3], 0) + 1
                show(h, f'{stamp}  EVT  ** Disconnection Complete ** handle {h:#x} {addr or ""} '
                        f'reason {p[3]:#04x} {HCI_REASONS.get(p[3], "")}')
                handles.pop(h, None)
                want.discard(h)
            elif code == 0x0C and len(p) >= 8 and p[0] == 0:  # Read Remote Version Information Complete
                h = int.from_bytes(p[1:3], 'little') & 0xFFF
                mfr, subver = struct.unpack('<HH', p[4:8])
                if h in handles:
                    dev(handles[h])['version'] = (p[3], mfr, subver)
                show(h, f'{stamp}  EVT  Remote Version handle {h:#x} {handles.get(h, "")} LMP {p[3]:#04x} '
                        f'manufacturer {mfr:#06x} subversion {subver:#06x}')
            elif code == 0x07 and len(p) >= 7 and p[0] == 0:  # Remote Name Request Complete
                name = p[7:].split(b'\x00', 1)[0].decode(errors='replace')
                addr = ':'.join(f'{b:02x}' for b in reversed(p[1:7]))
                dev(addr)['name'] = name
                show(None, f'{stamp}  EVT  Remote Name {addr} {name!r}', addr)
            elif code == 0x14 and len(p) >= 6:
                h = int.from_bytes(p[1:3], 'little') & 0xFFF
                iv = int.from_bytes(p[4:6], 'little')
                show(h, f'{stamp}  EVT  Mode Change handle {h:#x} -> {MODES.get(p[3], p[3])} '
                        f'interval {iv * 0.625:.0f}ms status {p[0]:#04x}')
            elif code == 0x0F and len(p) >= 4:
                op = int.from_bytes(p[2:4], 'little')
                if p[0] and op in (0x0803, 0x0804, 0x0406):
                    show(None, f'{stamp}  EVT  Command Status FAILED op {op:#06x} status {p[0]:#04x}')  # no handle
    print(f'({count} records)')
    if devices or preexisting:
        print('\nDevices seen (identify the headset, then re-run with --device ADDR):')
        for addr, d in devices.items():
            v = d['version']
            kind = ''
            if v:
                if v[1] == 0x004C:
                    kind = '  [Apple: watch/earbuds/Mac, not your headset]'
                elif v[1] == 0x000E:
                    kind = '  [likely Beken/BK3266 (Ceva baseband, manufacturer 0x000e)]'
            ver = f'LMP {v[0]:#04x} mfr {v[1]:#06x} subver {v[2]:#06x}' if v else 'version not captured'
            drops = ', '.join(f'{r:#04x}x{n}' for r, n in sorted(d['disconnects'].items())) or 'none'
            name = f' {d["name"]!r}' if d['name'] else ''
            print(f'  {addr}{name}  {ver}  connections {d["connections"]}  disconnect reasons {drops}{kind}')
    if preexisting:
        print('Links already up when the trace started (address unknown; inspect with --handle): '
              + ', '.join(f'{h:#x}' for h in sorted(preexisting))
              + '\n  Tip: start the trace before powering the headset on, so its address and version are captured.')
    if handles:
        print('Links still up at end of trace: ' + ', '.join(f'{h:#x}={a}' for h, a in handles.items()))


# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(prog='bk3266', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    def device(p):
        p.add_argument('--port', required=True, help='serial device, e.g. /dev/cu.usbserial-XXXX or COM5')
        p.add_argument('--wait', type=float, default=300, help='seconds to wait for the power-button catch')
        p.add_argument('--say', action='store_true', help='speak cues (macOS)')
        p.add_argument('--no-reboot', action='store_true',
                       help='stay in download mode afterwards (default: reboot into the application)')

    def cfgbase(p):
        p.add_argument('--config-base', type=parse_int, help='logical config address override')

    p = sub.add_parser('ping', help='catch the bootloader, print flash ID + protection status')
    device(p)
    p.add_argument('--evidence', type=Path, default=Path('bk3266-evidence'))
    p.set_defaults(fn=cmd_ping)

    p = sub.add_parser('reboot', help='leave download mode (fixes a "dead" headset left in the bootloader)')
    p.add_argument('--port', required=True)
    p.add_argument('--wait', type=float, default=5, help='seconds to look for a chip in download mode')
    p.add_argument('--evidence', type=Path, default=Path('bk3266-evidence'))
    p.set_defaults(fn=cmd_reboot)

    p = sub.add_parser('dump', help='full flash backup, N passes, require identical')
    device(p)
    p.add_argument('--out', type=Path, default=Path('bk3266-evidence'))
    p.add_argument('--passes', type=int, default=2)
    p.set_defaults(fn=cmd_dump)

    p = sub.add_parser('write', help='write changed sectors of a candidate image (dry run without --yes)')
    device(p)
    cfgbase(p)
    p.add_argument('candidate')
    p.add_argument('--expect', required=True, help='image the device must currently contain (your verified backup)')
    p.add_argument('--yes', action='store_true')
    p.add_argument('--allow-any-sector', action='store_true', help='permit sectors outside the config region')
    p.add_argument('--max-sectors', type=int, default=4)
    p.add_argument('--unprotect-value', default='00 00', help='SR1 SR2 to write while programming')
    p.add_argument('--unprotect-value-confirmed', action='store_true')
    p.add_argument('--evidence', type=Path, default=Path('bk3266-evidence'))
    p.set_defaults(fn=cmd_write)

    p = sub.add_parser('info', help='summarize a dump: CRC extent, config location, strings')
    p.add_argument('image')
    p.add_argument('--strings', action='store_true')
    cfgbase(p)
    p.set_defaults(fn=cmd_info)

    p = sub.add_parser('config', help='decode configuration fields and prompt table')
    p.add_argument('image')
    p.add_argument('--json', action='store_true')
    cfgbase(p)
    p.set_defaults(fn=cmd_config)

    p = sub.add_parser('patch', help='make a candidate image offline (CRCs recomputed)')
    p.add_argument('image')
    p.add_argument('-o', '--out', required=True)
    p.add_argument('--set', action='append', metavar='FIELD=VALUE')
    p.add_argument('--clear-bits', action='append', metavar='FIELD=MASK')
    p.add_argument('--set-bits', action='append', metavar='FIELD=MASK')
    p.add_argument('--mute-prompt', action='append', metavar='NAME|SLOT', help=f'one of {sorted(PROMPT_SLOTS)}')
    cfgbase(p)
    p.set_defaults(fn=cmd_patch)

    p = sub.add_parser('prompts', help='extract voice prompts as SBC (+ WAV via ffmpeg)')
    p.add_argument('image')
    p.add_argument('-o', '--out', default='voice-prompts')
    p.add_argument('--no-wav', action='store_true')
    cfgbase(p)
    p.set_defaults(fn=cmd_prompts)

    p = sub.add_parser('strip', help='remove CRC bytes -> logical image for disassembly')
    p.add_argument('image')
    p.add_argument('-o', '--out', required=True)
    p.add_argument('--end', type=parse_int, help='raw end offset (default: CRC-valid extent)')
    p.set_defaults(fn=cmd_strip)

    p = sub.add_parser('disasm', help='BA2 linear-sweep listing via pyba2 (fetched on first use)')
    p.add_argument('image', help='logical image (from strip), or raw dump with --raw')
    p.add_argument('-o', '--out', required=True)
    p.add_argument('--raw', action='store_true')
    p.add_argument('--start', type=parse_int, default=0)
    p.add_argument('--end', type=parse_int, default=0x56700)
    p.add_argument('--pyba2', help='path to an existing pyba2 checkout')
    p.set_defaults(fn=cmd_disasm)

    p = sub.add_parser('pklg', help='summarize sniff/disconnect events in an Apple PacketLogger capture')
    p.add_argument('capture')
    p.add_argument('--device', metavar='ADDR', help='only show events for this Bluetooth address (aa:bb:cc:dd:ee:ff)')
    p.add_argument('--handle', action='append', default=[],
                   help='also show this connection handle (e.g. 0x15) for a link that was up before the trace began')
    p.set_defaults(fn=cmd_pklg)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == '__main__':
    main()
