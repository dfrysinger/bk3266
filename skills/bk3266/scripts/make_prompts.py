#!/usr/bin/env python3
"""Make replacement voice prompts for BK3266 headsets: soft chimes (or speech) rendered exactly as the
headset will play them, plus previews you can listen to first.

The prompt player runs the DAC at 8 kHz, while the clips are SBC frames *labelled* 32 kHz (mono,
16 blocks, 8 subbands, bitpool 16, 40-byte frames of 128 samples). So this makes 8 kHz audio, encodes it
as if it were 32 kHz with ffmpeg, then decodes that SBC back into an 8 kHz WAV preview, so you hear
the codec's real quality.

    python3 make_prompts.py OUTDIR                      # the "sleep" chime set (verified on hardware)
    afplay OUTDIR/power-on.wav                          # listen (macOS)
    python3 bk3266.py patch dump.bin -o new.bin --replace-prompt power-on=OUTDIR/power-on.sbc ...

Needs ffmpeg (with its built-in SBC encoder). Speech uses macOS `say`.
Frame budgets below are for the 2021 "Cozy" layout; `bk3266.py patch --replace-prompt` enforces the
real limit for any image.
"""
import math
import struct
import subprocess
import sys
import wave
from pathlib import Path

RATE = 8000
FRAME_SAMPLES = 128
BUDGET = {'power-on': 102, 'power-off': 76, 'connected': 63, 'disconnected': 76, 'pairing': 140,
          'hfp-ring': 268, 'call-reject': 83, 'vol-max': 38, 'redial': 16}   # vol-max's clip is shared with vol-min


def tone_seq(notes, note_s=0.17, partials=(1.0, 0.45, 0.15)):
    """Soft bell notes: a few harmonics (so tiny drivers convey low pitches), 5 ms attack, 90 ms decay.
    A frequency of 0 is a rest."""
    total = int(RATE * (len(notes) * note_s + 0.25))
    buf = [0.0] * total
    for k, f in enumerate(notes):
        start = int(RATE * k * note_s)
        for i in range(int(RATE * (note_s + 0.25))):
            t = i / RATE
            env = min(1.0, t / 0.005) * math.exp(-t / 0.09)
            s = sum(a * math.sin(2 * math.pi * f * (h + 1) * t) for h, a in enumerate(partials) if f and f * (h + 1) < 3800)
            if start + i < total:
                buf[start + i] += env * s
    return buf


def load_audio(path, tempo=1.0, pitch=1.0, compress=False):
    """Any audio file (e.g. a voiceover MP3) as 8 kHz mono floats. `tempo` > 1 speeds speech up without
    changing pitch, to squeeze a phrase into a short slot; `pitch` < 1 lowers the voice (0.89 ≈ 2 semitones)
    without changing speed. `compress` evens out loud syllables, which otherwise distort through the low-bitpool
    SBC codec and the tiny driver. The 150 Hz high-pass keeps tiny drivers clean."""
    af = ['aresample=44100', 'highpass=f=150']   # fixed working rate so the pitch math holds for any input
    # Trim lead/tail silence first: compression would lift the breath/room tail and waste the slot on it.
    trim_edges = 'silenceremove=start_periods=1:start_threshold=-45dB'
    af += [trim_edges, 'areverse', trim_edges, 'areverse']
    if pitch != 1.0:                     # resample-shift the pitch, then restore the duration
        af += [f'asetrate=44100*{pitch}', 'aresample=44100', f'atempo={1 / pitch:.4f}']
    if tempo != 1.0:
        af.append(f'atempo={tempo}')
    if compress:
        af.append('acompressor=threshold=0.1:ratio=4:attack=5:release=80:makeup=2')
    af.append('lowpass=f=3400')          # nothing above ~3.5 kHz survives 8 kHz playback anyway
    af = ','.join(af)
    raw = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', str(path), '-ac', '1', '-ar', str(RATE),
                          '-af', af, '-f', 's16le', '-'], check=True, capture_output=True).stdout
    return [v / 32768 for v in struct.unpack(f'<{len(raw) // 2}h', raw)]


def speech(text, out, voice='Samantha', rate=175):
    """Speech from macOS `say` (free, robotic). For better voices, load a recorded or generated file with
    load_audio()."""
    aiff = out / 'tmp-say.aiff'
    subprocess.run(['say', '-v', voice, '-r', str(rate), '-o', str(aiff), text], check=True)
    buf = load_audio(aiff)
    aiff.unlink()
    return buf


def echo(buf, delay=0.11, feedback=0.38, taps=5, tail=0.6):
    """Feedback-style echo; each repeat quieter and slightly darker."""
    d = int(RATE * delay)
    out = buf + [0.0] * int(RATE * tail)
    rep, gain = buf[:], 1.0
    for k in range(1, taps + 1):
        gain *= feedback
        prev = 0.0
        for i, v in enumerate(rep):
            prev += 0.45 * (v - prev)
            rep[i] = prev
        for i, v in enumerate(rep):
            if i + k * d < len(out):
                out[i + k * d] += gain * v
    return out


def trim(buf, thresh=0.01):
    idx = [i for i, v in enumerate(buf) if abs(v) > thresh]
    return buf[max(0, idx[0] - 40):idx[-1] + 200] if idx else buf


def fit(buf, frames, fade_s=0.15):
    n = frames * FRAME_SAMPLES
    if len(buf) <= n:
        return buf
    buf, f = buf[:n], int(RATE * fade_s)
    for i in range(f):
        buf[n - f + i] *= (f - i) / f
    return buf


def render(out, name, buf, wet=True, level=0.89):
    """`level` is the peak (0.89 = -1 dBFS, right for chimes; speech sounds cleaner around 0.5 = -6 dBFS)."""
    budget = BUDGET[name]
    buf = trim(buf)
    if wet:
        buf = trim(echo(buf), thresh=0.004)
    buf = fit(buf, budget)
    peak = max(abs(v) for v in buf) or 1
    buf = [v / peak * level for v in buf]
    frames = math.ceil(len(buf) / FRAME_SAMPLES)
    buf += [0.0] * (frames * FRAME_SAMPLES - len(buf))
    pcm = struct.pack(f'<{len(buf)}h', *(int(v * 32767) for v in buf))
    sbc = out / f'{name}.sbc'
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 's16le', '-ar', '32000', '-ac', '1',
                    '-i', '-', '-c:a', 'sbc', '-b:a', '80k', str(sbc)], input=pcm, check=True)
    dec = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', str(sbc), '-f', 's16le', '-'],
                         check=True, capture_output=True).stdout
    with wave.open(str(out / f'{name}.wav'), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(dec)
    print(f'{name:13s} {len(buf) / RATE:4.2f} s  {sbc.stat().st_size // 40:3d}/{budget} frames')


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else 'prompts-out')
    out.mkdir(parents=True, exist_ok=True)
    G3, B3, D4, G4 = 196.0, 246.94, 293.66, 392.0
    # "Sleep" set: low (G3 = 196 Hz) and soft. Rising = waking/joining, falling = sleeping/leaving,
    # pairing = an unresolved repeated "ping" (open-ended, not a success sound).
    render(out, 'power-on', tone_seq([G3, B3, D4, G4]))
    render(out, 'power-off', tone_seq([G4, D4, B3, G3]))
    render(out, 'connected', tone_seq([G3, D4], note_s=0.22))
    render(out, 'disconnected', tone_seq([D4, G3], note_s=0.22))
    render(out, 'pairing', tone_seq([D4, 0, 0, D4], note_s=0.22))
    # Calls and controls, same family. The ring is a soft fast trill (what makes it read as "phone")
    # in a ring-ring … ring-ring cadence; call reject is a flat "nuh-uh"; volume and redial are single taps.
    def trill(seconds, a=D4, b=G4, step=0.05):
        return [a if i % 2 == 0 else b for i in range(int(seconds / step))]
    rest = lambda seconds: [0] * int(seconds / 0.05)
    ring = trill(0.4) + rest(0.2) + trill(0.4) + rest(0.9) + trill(0.4) + rest(0.2) + trill(0.4)
    render(out, 'hfp-ring', tone_seq(ring, note_s=0.05, partials=(1.0, 0.4, 0.12)), wet=False)
    render(out, 'call-reject', tone_seq([G3, G3], note_s=0.2))
    render(out, 'vol-max', tone_seq([D4], note_s=0.15))
    render(out, 'redial', tone_seq([B3], note_s=0.12), wet=False)
    # Speech instead, e.g.: render(out, 'power-on', speech('Powering on', out), wet=False)


if __name__ == '__main__':
    main()
