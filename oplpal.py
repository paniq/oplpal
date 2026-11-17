#!/usr/bin/env python3
"""
OPL PAL - a tiny tracker for a pretend Sound Blaster.

Run it, press Space, mash keys. Sound comes from the Nuked-OPL3 chip emulator
(bundled, LGPL-2.1+), so this is real OPL3: 18 FM voices, stereo, 8 waveforms
and the rhythm-mode drum section. Graphics use the kitty graphics protocol
(kitty, Ghostty, WezTerm...); in other terminals you just don't get the picture.

    ./oplpal.py [song.json] [--no-gfx | --gfx] [--layout de|fr|us] [--wav out.wav]
"""
import base64, collections, ctypes, fcntl, json, math, os, random, re, select
import signal, struct, subprocess, sys, termios, threading, time, wave, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
RATE = 48000
ROWS, NPAT, NTRK, NMEL = 16, 8, 8, 6      # rows/pattern, patterns, tracks, melodic tracks
CHUNK = 512                                # audio block, in frames
GAP = 48                                   # samples between key-off and key-on (clean retrigger)
FPS = 20
GAIN = 1.4                                 # headroom so busy songs don't clip

# ───────────────────────────── the OPL3 chip ──────────────────────────────

def load_lib():
    so = os.path.join(HERE, "libopl.so")
    srcs = [os.path.join(HERE, f) for f in ("opl3.c", "oplshim.c")]
    stale = not os.path.exists(so) or any(os.path.getmtime(s) > os.path.getmtime(so) for s in srcs)
    if stale:
        try:
            r = subprocess.run(["gcc", "-O2", "-shared", "-fPIC", "-o", so, *srcs],
                               capture_output=True, text=True)
            err = r.stderr if r.returncode else ""
        except FileNotFoundError:
            err = "gcc not found"
        if err:
            sys.exit("Couldn't build the OPL3 emulator.\n  sudo apt install build-essential\n\n" + err)
    lib = ctypes.CDLL(so)
    lib.opl_new.restype = ctypes.c_void_p
    lib.opl_new.argtypes = [ctypes.c_uint32]
    lib.opl_write.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint8]
    lib.opl_gen.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_float]
    return lib


def op(mult=1, tl=0, ar=15, dr=0, sl=0, rr=7, ws=0, sus=1, vib=0, am=0, ksl=0, ksr=0):
    """One FM operator. tl: 0=loud..63=silent. ar/dr/sl/rr: envelope. ws: waveform 0-7.
    sus=1 holds at the sustain level until key-off; sus=0 keeps fading (plucks)."""
    return dict(mult=mult, tl=tl, ar=ar, dr=dr, sl=sl, rr=rr, ws=ws, sus=sus, vib=vib, am=am, ksl=ksl, ksr=ksr)


#  name      feedback, connection(0=FM 1=additive), modulator, carrier
INSTRUMENTS = [
    ("Sine",   0, 0, op(1, 36, 15, 7, 8, 6, sus=0),            op(1, 4, 15, 3, 2, 8)),
    ("Saw",    5, 0, op(1, 20, 15, 8, 6, 7, sus=0),            op(1, 2, 15, 5, 3, 7)),
    ("Square", 0, 0, op(1, 63),                                 op(1, 3, 15, 0, 0, 7, ws=6)),
    ("Pluck",  3, 0, op(2, 22, 15, 9, 15, 7, sus=0),           op(1, 3, 15, 7, 5, 6, sus=0)),
    ("EPiano", 2, 0, op(1, 26, 15, 8, 9, 7, sus=0),            op(1, 3, 15, 4, 4, 6, sus=0)),
    ("Bell",   0, 0, op(7, 18, 15, 5, 12, 4, sus=0),           op(1, 4, 15, 4, 10, 4, sus=0)),
    ("Organ",  3, 1, op(1, 12, 15, 0, 0, 8),                   op(2, 12, 15, 0, 0, 8)),
    ("Brass",  4, 0, op(1, 22, 10, 4, 6, 7),                   op(1, 2, 11, 3, 3, 7)),
    ("Pad",    2, 0, op(1, 32, 6, 0, 0, 5, vib=1),             op(1, 4, 6, 2, 2, 5, vib=1)),
    ("Zap",    6, 0, op(4, 14, 15, 9, 15, 7, sus=0),           op(1, 2, 15, 7, 15, 6, sus=0)),
]

# Rhythm mode borrows OPL channels 6-8:  6 = kick (2 operators), 7 = hat + snare, 8 = tom + cymbal
DRUM_KIT = {
    6: (0, 0, op(1, 24, 15, 8, 10, 7, sus=0),  op(1, 8, 15, 6, 15, 7, sus=0),  (343, 2)),   # kick  ~65 Hz
    7: (0, 0, op(1, 8, 15, 8, 15, 9, sus=0),   op(1, 8, 15, 7, 15, 7, sus=0),   (437, 3)),   # hat / snare
    8: (0, 0, op(1, 6, 15, 7, 15, 8, sus=0),   op(1, 8, 15, 5, 15, 5, sus=0),   (880, 2)),   # tom / cymbal
}
DRUM_NAMES = ["KCK", "KCK", "SNR", "SNR", "TOM", "CYM", "CYM", "HAT", "HAT", "HAT", "HAT", "HAT"]
DRUM_BIT = {"KCK": 0x10, "SNR": 0x08, "TOM": 0x04, "CYM": 0x02, "HAT": 0x01}
DRUM_LETTER = {"k": 60, "s": 62, "t": 64, "c": 65, "h": 67}

OPS = [0, 1, 2, 8, 9, 10, 16, 17, 18]      # operator register offsets for the 9 voices of a bank
MELCH = [0, 1, 2, 3, 4, 5]                 # track -> OPL voice
PAN = [0x30, 0x10, 0x20, 0x10, 0x20, 0x30]  # 0x10 left, 0x20 right, 0x30 both


def fnum_block(note):
    hz = 440.0 * 2 ** ((note - 69) / 12)
    for blk in range(8):
        f = int(hz * (1 << (20 - blk)) / 49716 + 0.5)
        if f < 1024:
            return f, blk
    return 1023, 7


class Engine:
    """Sequencer + chip. Everything that touches the chip happens on the audio thread."""

    def __init__(self, song):
        self.song = song
        self.lib = load_lib()
        self.chip = self.lib.opl_new(RATE)
        self.buf = (ctypes.c_int16 * (2 * 4096))()
        self.ring = [0] * 8192
        self.wi = 0
        self.events = collections.deque()
        self.muted = [False] * NTRK
        self.playing = False
        self.mode = "loop"
        self.loop_pat = 0
        self.pat = self.row = 0
        self.nxt = (0, 0)
        self.to_row = 0.0
        self.pending = []
        self.gap = 0
        self.gates = {}
        self.cur_inst = [-1] * NMEL
        self.b0 = [0] * 18
        self.bd = 0xE0                      # deep tremolo + vibrato, rhythm mode on
        self.last_hit = [0.0] * NTRK
        self._init_chip()

    # -- raw chip access
    def w(self, reg, val):
        self.lib.opl_write(self.chip, reg, val & 0xFF)

    def _init_chip(self):
        self.w(0x105, 1)                    # OPL3 mode: 18 voices, stereo, all waveforms
        self.w(0x104, 0)
        self.w(0x01, 0x20)
        for ch in range(18):
            self.w(0x100 * (ch // 9) + 0xC0 + ch % 9, 0x30)
        self.w(0xBD, self.bd)
        for ch, (fb, cnt, m, c, (fn, blk)) in DRUM_KIT.items():
            self.load_patch(ch, fb, cnt, m, c, 0x30)
            self.w(0xA0 + ch, fn & 255)
            self.w(0xB0 + ch, blk << 2 | fn >> 8)

    def load_patch(self, ch, fb, cnt, mod, car, pan):
        bank, c = 0x100 * (ch // 9), ch % 9
        for off, o in ((OPS[c], mod), (OPS[c] + 3, car)):
            self.w(bank + 0x20 + off, o["am"] << 7 | o["vib"] << 6 | o["sus"] << 5 | o["ksr"] << 4 | o["mult"])
            self.w(bank + 0x40 + off, o["ksl"] << 6 | o["tl"])
            self.w(bank + 0x60 + off, o["ar"] << 4 | o["dr"])
            self.w(bank + 0x80 + off, o["sl"] << 4 | o["rr"])
            self.w(bank + 0xE0 + off, o["ws"])
        self.w(bank + 0xC0 + c, pan | fb << 1 | cnt)

    # -- notes
    def trigger(self, t, note, inst, gate=None):
        ch = MELCH[t]
        if self.cur_inst[t] != inst:
            _, fb, cnt, m, c = INSTRUMENTS[inst]
            self.load_patch(ch, fb, cnt, m, c, PAN[t])
            self.cur_inst[t] = inst
        f, blk = fnum_block(note)
        self.w(0xA0 + ch, f & 255)
        self.b0[ch] = blk << 2 | f >> 8
        self.w(0xB0 + ch, self.b0[ch])                       # key off...
        self.pending.append(("r", 0xB0 + ch, self.b0[ch] | 0x20))   # ...key on a hair later
        self.gap = GAP
        self.last_hit[t] = time.monotonic()
        if gate:
            self.gates[t] = gate
        else:
            self.gates.pop(t, None)

    def key_off(self, t):
        ch = MELCH[t]
        self.w(0xB0 + ch, self.b0[ch])
        self.gates.pop(t, None)

    def trigger_drums(self, mask, tracks=()):
        self.bd &= ~mask
        self.w(0xBD, self.bd)
        self.pending.append(("d", mask))
        self.gap = GAP
        for t in tracks:
            self.last_hit[t] = time.monotonic()

    def fire_pending(self):
        for p in self.pending:
            if p[0] == "d":
                self.bd |= p[1]
                self.w(0xBD, self.bd)
            else:
                self.w(p[1], p[2])
        self.pending, self.gap = [], 0

    def silence(self):
        for t in range(NMEL):
            self.key_off(t)
        self.bd &= 0xE0
        self.w(0xBD, self.bd)
        self.pending, self.gap = [], 0

    # -- sequencing
    def spr(self):
        return RATE * 60.0 / (self.song.bpm * 4)

    def following(self, p, r):
        if r + 1 < ROWS:
            return p, r + 1
        if self.mode == "loop":
            return self.loop_pat, 0
        for k in range(1, NPAT + 1):
            q = (p + k) % NPAT
            if not self.song.empty(q):
                return q, 0
        return p, 0

    def play_row(self, p, r):
        self.pat, self.row = p, r
        cells = self.song.pats[p][r]
        mask, dtracks = 0, []
        for t in range(NTRK):
            note, inst = cells[t]
            if note == 0 or self.muted[t]:
                continue
            if t < NMEL:
                if note < 0:
                    self.key_off(t)
                else:
                    self.trigger(t, note, inst)
            elif note > 0:
                mask |= DRUM_BIT[DRUM_NAMES[note % 12]]
                dtracks.append(t)
        if mask:
            self.trigger_drums(mask, dtracks)

    def drain(self):
        while self.events:
            e = self.events.popleft()
            if e[0] == "play":
                self.playing, self.nxt, self.to_row = True, (e[1], e[2]), 0.0
            elif e[0] == "stop":
                self.playing = False
                self.silence()
            elif e[0] == "note":
                self.trigger(e[1], e[2], e[3], gate=int(RATE * 0.28))
            elif e[0] == "drum":
                self.trigger_drums(e[1])
            elif e[0] == "mute":
                if e[1] < NMEL:
                    self.key_off(e[1])

    def fill(self, n):
        """Render n stereo frames into self.buf, running the sequencer as we go."""
        self.drain()
        base = ctypes.addressof(self.buf)
        pos = 0
        while pos < n:
            if self.playing and self.to_row <= 0:
                p, r = self.nxt
                self.play_row(p, r)
                self.nxt = self.following(p, r)
                self.to_row += self.spr()
            step = n - pos
            if self.pending:
                step = min(step, max(1, self.gap))
            if self.playing:
                step = min(step, max(1, math.ceil(self.to_row)))
            for rem in self.gates.values():
                step = min(step, max(1, rem))
            self.lib.opl_gen(self.chip, base + pos * 4, step, GAIN)
            pos += step
            if self.playing:
                self.to_row -= step
            if self.pending:
                self.gap -= step
                if self.gap <= 0:
                    self.fire_pending()
            for t in list(self.gates):
                self.gates[t] -= step
                if self.gates[t] <= 0:
                    self.key_off(t)
        vals = self.buf[0:2 * n:2]
        wi, end = self.wi, self.wi + n
        if end <= len(self.ring):
            self.ring[wi:end] = vals
        else:
            k = len(self.ring) - wi
            self.ring[wi:] = vals[:k]
            self.ring[:end - len(self.ring)] = vals[k:]
        self.wi = end % len(self.ring)

    def scope(self, count):
        r, wi = self.ring[:], self.wi
        return (r[wi:] + r[:wi])[-count:]


# ───────────────────────────── audio output ───────────────────────────────

class Sink:
    """Talks to ALSA directly (PipeWire provides the 'default' ALSA device on Ubuntu)."""

    def __init__(self):
        self.ok, self.why, self.t0 = False, "", time.monotonic()
        try:
            lib = ctypes.CDLL("libasound.so.2")
            h = ctypes.c_void_p()
            if lib.snd_pcm_open(ctypes.byref(h), os.environ.get("OPLPAL_DEVICE", "default").encode(), 0, 0) < 0:
                raise OSError("couldn't open the default audio device")
            lib.snd_pcm_set_params.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
                                               ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
            lib.snd_pcm_writei.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
            lib.snd_pcm_writei.restype = ctypes.c_long
            lib.snd_pcm_recover.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
            # S16_LE, interleaved, 2 ch, resample if needed, 70 ms of buffer
            if lib.snd_pcm_set_params(h, 2, 3, 2, RATE, 1, 70000) < 0:
                raise OSError("audio device refused 48 kHz stereo")
            self.lib, self.h, self.ok = lib, h, True
        except (OSError, AttributeError) as e:
            self.why = str(e)

    def write(self, buf, frames):
        if not self.ok:                                      # silent mode: just keep time
            self.t0 += frames / RATE
            d = self.t0 - time.monotonic()
            if d > 0:
                time.sleep(d)
            else:
                self.t0 = time.monotonic()
            return
        addr, off = ctypes.addressof(buf), 0
        while off < frames:
            r = self.lib.snd_pcm_writei(self.h, addr + off * 4, frames - off)
            if r < 0:
                if self.lib.snd_pcm_recover(self.h, int(r), 1) < 0:
                    self.ok = False
                    return
            else:
                off += r

    def close(self):
        if self.ok:
            self.lib.snd_pcm_drop(self.h)
            self.lib.snd_pcm_close(self.h)


class Audio(threading.Thread):
    def __init__(self, eng):
        super().__init__(daemon=True)
        self.eng, self.sink, self.running, self.error = eng, Sink(), True, None

    def run(self):
        try:
            while self.running:
                self.eng.fill(CHUNK)
                self.sink.write(self.eng.buf, CHUNK)
        except Exception as e:           # surface it in the UI instead of dying silently
            self.error = repr(e)

    def stop(self):
        self.running = False
        self.join(timeout=1)
        self.sink.close()


# ───────────────────────────── the song ───────────────────────────────────

NOTE_NAMES = ["C-", "C#", "D-", "D#", "E-", "F-", "F#", "G-", "G#", "A-", "A#", "B-"]
ROOTS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
SCALES = [("free", None), ("min-pent", [0, 3, 5, 7, 10]), ("maj-pent", [0, 2, 4, 7, 9]),
          ("minor", [0, 2, 3, 5, 7, 8, 10]), ("major", [0, 2, 4, 5, 7, 9, 11]),
          ("blues", [0, 3, 5, 6, 7, 10]), ("dorian", [0, 2, 3, 5, 7, 9, 10])]


def note_name(n):
    return NOTE_NAMES[n % 12] + str(n // 12 - 1)


class Song:
    def __init__(self):
        self.bpm, self.scale, self.root = 116, 1, 0
        self.pats = [[[(0, 0)] * NTRK for _ in range(ROWS)] for _ in range(NPAT)]
        for p in range(NPAT):
            self.pats[p] = [list(r) for r in self.pats[p]]

    def empty(self, p):
        return all(c[0] == 0 for row in self.pats[p] for c in row)

    def save(self, path):
        with open(path, "w") as f:
            json.dump(dict(bpm=self.bpm, scale=self.scale, root=self.root, pats=self.pats), f)

    @staticmethod
    def load(path):
        d = json.load(open(path))
        s = Song()
        s.bpm, s.scale, s.root = int(d["bpm"]), int(d["scale"]), int(d["root"])
        s.pats = [[[tuple(c) for c in row] for row in pat] for pat in d["pats"]]
        assert len(s.pats) == NPAT and all(len(p) == ROWS and all(len(r) == NTRK for r in p) for p in s.pats)
        return s


def parse_note(tok):
    base = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}[tok[0]]
    rest, acc = tok[1:], 0
    if rest[0] in "#b":
        acc, rest = (1 if rest[0] == "#" else -1), rest[1:]
    return 12 * (int(rest) + 1) + base + acc


def put(song, p, t, text, inst=0):
    """Fill a track of a pattern from 16 space-separated tokens: c4 eb3 . ^ (or k s t c h for drums)."""
    for r, tok in enumerate(text.split()):
        if tok == ".":
            cell = (0, 0)
        elif tok == "^":
            cell = (-1, 0)
        elif t >= NMEL:
            cell = (DRUM_LETTER[tok], 0)
        else:
            cell = (parse_note(tok), inst)
        song.pats[p][r][t] = cell


def demo_song():
    s = Song()
    chords = [  # root, arp, pad pair, melody, sparkle
        ("c", "c4 eb4 g4 eb4", ("eb4", "g4"),
         "g4 . . eb5 . . d5 . c5 . . . d5 . . .", "c6 . . . . . . . . . g5 . . . . ."),
        ("ab", "c4 eb4 ab4 eb4", ("c4", "eb4"),
         "c5 . . eb5 . . c5 . ab4 . . . bb4 . . .", "eb6 . . . . . . . . . c6 . . . . ."),
        ("eb", "bb3 eb4 g4 eb4", ("g4", "bb4"),
         "bb4 . . g4 . . bb4 . eb5 . . . d5 . . .", "g5 . . . . . . . . . bb5 . . . . ."),
        ("bb", "bb3 d4 f4 d4", ("d4", "f4"),
         "d5 . . f5 . . d5 . c5 . bb4 . . . . .", "d6 . . . . . . . . . f6 . . . . ^"),
    ]
    for p, (root, arp, pad, mel, spark) in enumerate(chords):
        put(s, p, 0, f"{root}2 . {root}2 . {root}3 . {root}2 . {root}2 . {root}2 . {root}3 . {root}2 .", 1)
        put(s, p, 1, " ".join(arp.split() * 4), 3)
        put(s, p, 2, mel, 2)
        put(s, p, 3, pad[0] + " " + ". " * 15, 8)
        put(s, p, 4, pad[1] + " " + ". " * 15, 8)
        put(s, p, 5, spark, 5)
        put(s, p, 6, "k . . . s . . . k . k . s . . ." if p < 3 else "k . . . s . . . k . . k s . s s")
        put(s, p, 7, "h . h . h . h . h . h . h . h c" if p % 2 == 0 else "h . h . h . h . h . h . h . c .")
    return s


# ───────────────────────────── offline rendering ──────────────────────────

def render_wav(song, path):
    eng = Engine(song)
    eng.mode = "song"
    first = next((p for p in range(NPAT) if not song.empty(p)), 0)
    rows = sum(ROWS for p in range(NPAT) if not song.empty(p)) or ROWS
    total = int(rows * eng.spr())
    eng.events.append(("play", first, 0))
    with wave.open(path, "wb") as w:
        w.setnchannels(2), w.setsampwidth(2), w.setframerate(RATE)
        done, stopped = 0, False
        while done < total + RATE * 2:
            if not stopped and done >= total:
                eng.events.append(("stop",))
                stopped = True
            n = min(CHUNK, (total - done) if not stopped else CHUNK)
            eng.fill(max(1, n))
            w.writeframes(bytes(eng.buf)[:max(1, n) * 4])
            done += max(1, n)


# ───────────────────────────── terminal helpers ───────────────────────────

def fg(c): return f"\x1b[38;2;{c[0]};{c[1]};{c[2]}m"
def bgc(c): return f"\x1b[48;2;{c[0]};{c[1]};{c[2]}m"
RST, BOLD, DIM = "\x1b[0m", "\x1b[1m", "\x1b[2m"

TRACK_RGB = [(255, 107, 129), (255, 176, 70), (255, 226, 90), (110, 230, 130),
             (80, 210, 230), (120, 150, 255), (190, 130, 255), (255, 130, 220)]
DRUM_RGB = {"KCK": (255, 100, 90), "SNR": (255, 170, 70), "TOM": (240, 220, 90),
            "CYM": (90, 220, 240), "HAT": (120, 235, 140)}
INK, FAINT, ACCENT = (225, 228, 240), (88, 94, 120), (255, 205, 90)
BG_ROW, BG_PLAY, BG_CUR = (22, 24, 36), (44, 50, 84), (240, 240, 250)

# Keys arrive as (name, base). name is what the key *types* (used for commands);
# base is the key's position on a US keyboard (used for the piano), so it works on any layout.
FIN = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end",
       "P": "f1", "Q": "f2", "R": "f3", "S": "f4", "Z": "btab"}
TIL = {1: "home", 3: "del", 4: "end", 5: "pgup", 6: "pgdn", 11: "f1", 12: "f2", 13: "f3", 14: "f4",
       15: "f5", 17: "f6", 18: "f7", 19: "f8", 20: "f9", 21: "f10"}
CSI = re.compile(r"\x1b\[([0-9:;?<=>]*)([ -/]*)([@-~])|\x1bO([P-S])")
LEGACY = {"\x11": "quit", "\x03": "quit", "\x13": "save", "\x05": "export", "\r": "enter", "\n": "enter",
          "\t": "tab", "\x7f": "bs", "\x08": "bs", " ": "space"}
NAMED_U = {13: "enter", 9: "tab", 127: "bs", 8: "bs", 27: "esc", 32: "space"}
# fallback for terminals without the kitty keyboard protocol: typed char -> US-position char
LAYOUTS = {
    "us": {},
    "de": {"y": "z", "z": "y"},
    "fr": {"a": "q", "q": "a", "z": "w", "w": "z", "m": ";", ",": "m", "&": "1", "é": "2", '"': "3",
           "'": "4", "(": "5", "-": "6", "è": "7", "_": "8", "ç": "9", "à": "0"},
}


def csi_u(field, mods):
    p = field.split(":")
    num = lambda i: int(p[i]) if i < len(p) and p[i].isdigit() else None
    code, shifted, base = num(0), num(1), num(2)
    if code is None or code >= 57344:             # modifier keys, keypad etc.
        return "unk", None
    if code == 9 and mods & 1:
        return "btab", None
    if code in NAMED_U:
        return NAMED_U[code], None
    if mods & 4:                                   # Ctrl+letter (Ctrl+Alt is AltGr: ignore)
        if mods & 2:
            return "unk", None
        return {"q": "quit", "c": "quit", "s": "save", "e": "export"}.get(chr(code).lower(), "unk"), None
    if mods & 10:                                  # Alt / Super
        return "unk", None
    ch = chr(shifted if (mods & 1 and shifted) else code)
    return ch, chr(base if base else code).lower()


def tokens(data, layout=None):
    layout = layout or {}
    i, out = 0, []
    while i < len(data):
        ch = data[i]
        if ch != "\x1b":
            low = ch.lower()
            out.append((LEGACY[ch], None) if ch in LEGACY else (ch, layout.get(low, low)))
            i += 1
            continue
        m = CSI.match(data, i)
        if not m:
            out.append(("esc", None))
            i += 1
            continue
        i = m.end()
        if m.group(4):
            out.append((FIN[m.group(4)], None))
            continue
        fields, final = m.group(1).split(";"), m.group(3)
        mods = 0
        if len(fields) > 1 and fields[1].split(":")[0].isdigit():
            mods = (int(fields[1].split(":")[0]) - 1) & 15
        if final == "u":
            out.append(csi_u(fields[0], mods))
            continue
        if final == "~":
            f0 = fields[0].split(":")[0]
            name = TIL.get(int(f0)) if f0.isdigit() else None
        else:
            name = FIN.get(final)
        if name in ("up", "down", "left", "right") and mods & 1:
            name = "shift-" + name
        out.append((name or "unk", None))
    return out


def term_size():
    try:
        r, c, xp, yp = struct.unpack("HHHH", fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8))
    except OSError:
        r, c, xp, yp = 24, 80, 0, 0
    cw, ch = (xp / c, yp / r) if xp and yp and c and r else (10, 20)
    return c, r, cw, ch


def probe_term(fd):
    """Ask the terminal what it can do: -> (kitty graphics?, kitty keyboard protocol?)"""
    sys.stdout.write("\x1b[?u\x1b_Gi=31,s=1,v=1,a=q,t=d,f=24;AAAA\x1b\\\x1b[c")
    sys.stdout.flush()
    buf, end = "", time.time() + 0.5
    while time.time() < end:
        if select.select([fd], [], [], 0.05)[0]:
            buf += os.read(fd, 256).decode("latin1")
            if re.search(r"\x1b\[\?[0-9;]*c", buf):
                break
    return "i=31;OK" in buf, bool(re.search(r"\x1b\[\?\d+u", buf))


# ───────────────────────────── the app ────────────────────────────────────

PIANO = {}
for _i, _k in enumerate("zsxdcvgbhnjm"):
    PIANO[_k] = _i
for _i, _k in enumerate("q2w3er5t6y7ui9o0p"):
    PIANO[_k] = 12 + _i

DRUM_PATTERNS = [
    ["k...s...k...s...", "k..ks...k.k.s...", "k.k.s...k..ks.t.", "k...s..kk.k.s...",
     "k..k..s.k..k..s.", "k...s...k.k.s.tt", "k.k.s..sk...s..."],
    ["h.h.h.h.h.h.h.h.", "hhhhhhhhhhhhhhhh", "h.h.h.h.h.h.h.hc", "..h...h...h...hc",
     "h.hhh.h.h.hhh.h.", "c...h.h.h.h.h.h.", "h...h...h...h..c"],
]
HELP = [
    f"{BOLD}HOW IT WORKS{RST}",
    " Time runs down the screen. Each column is one voice and each row",
    " is a 16th-note step. Type a note and it lands at the cursor, then",
    " the cursor steps down. Space plays the pattern on a loop.",
    " You get 8 patterns (A-H). Tab flips LOOP <-> SONG; SONG plays",
    " A, B, C... and skips the empty ones. Ctrl-S saves.",
    "",
    f"{BOLD}NOTES{RST}   Z X C V B N M = C D E F G A B   (S D G H J = sharps)",
    "         Q W E R T Y U I O P = same, one octave up",
    "         1 = note off      Del / Backspace = clear the cell",
    f"{BOLD}DRUMS{RST}   Last two columns are drums:  Z kick  X snare  C tom",
    "         V cymbal  B hat  (OPL3 rhythm section; two columns so you",
    "         can layer kick+hat on the same row)",
    f"{BOLD}SCALE{RST}   F2 cycles a scale lock so every key sounds good; F3 = root",
    f"{BOLD}DICE{RST}    F5 invents a part for the current track (bass/melody/beat)",
    f"{BOLD}EDIT{RST}    F6 clear track   F7 copy pattern to the next   F8 clear it",
    f"{BOLD}MOVE{RST}    arrows, Home/End, PgUp/PgDn = switch pattern",
    f"{BOLD}SOUND{RST}   Shift+arrows: left/right = instrument, up/down = octave",
    "         (also , . and [ ])   F9/F10 or - = : tempo   Enter = mute",
    f"{BOLD}FILE{RST}    Ctrl-S save   Ctrl-E export WAV   Ctrl-Q quit",
    "",
    f"{DIM} press any key to close help{RST}",
]


class App:
    def __init__(self, song, path, gfx, layout="us", use_kbd=True):
        self.song, self.path, self.gfx = song, path, gfx
        self.layout, self.use_kbd, self.kbd = LAYOUTS[layout], use_kbd, False
        self.eng = Engine(song)
        self.audio = Audio(self.eng)
        self.cp = self.cr = self.ct = 0
        self.oct, self.inst, self.step = 4, 3, 1
        self.help = self.dirty = self.armed = False
        self.msg, self.msg_t = "Space = play    F1 = help    F5 = roll the dice", time.monotonic() + 30
        self.resized = True
        self.bg_cache, self.gain, self.frame = {}, 1.0, 0
        self.pal = [bytes(int(255 * v) for v in self._hsv(i / 256)) for i in range(256)]

    @staticmethod
    def _hsv(h):
        h6 = h * 6
        i, f = int(h6) % 6, h6 - int(h6)
        q, t = 1 - f, f
        return [(1, t, .35), (q, 1, .35), (.35, 1, t), (.35, q, 1), (t, .35, 1), (1, .35, q)][i]

    # -- terminal plumbing
    def term_on(self):
        self.fd = sys.stdin.fileno()
        self.old = termios.tcgetattr(self.fd)
        self.lang_hint = getattr(self, "lang_hint", "")
        a = termios.tcgetattr(self.fd)
        a[0] &= ~(termios.IXON | termios.ICRNL | termios.INLCR)
        a[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN)
        a[6][termios.VMIN], a[6][termios.VTIME] = 1, 0
        termios.tcsetattr(self.fd, termios.TCSAFLUSH, a)
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J")
        sys.stdout.flush()
        signal.signal(signal.SIGWINCH, lambda *_: setattr(self, "resized", True))
        gfx, kbd = probe_term(self.fd)
        if self.gfx is None:
            self.gfx = gfx
        self.kbd = kbd and self.use_kbd
        if not self.kbd and self.lang_hint:
            self.say(f"Piano keys look wrong? This terminal can't report key positions - try: ./oplpal.py --layout {self.lang_hint}", 20)
        if self.kbd:                       # 1+4+8: disambiguate, report layout-independent key, all keys
            sys.stdout.write("\x1b[>13u")
            sys.stdout.flush()

    def term_off(self):
        sys.stdout.write(("\x1b[<u" if self.kbd else "") + "\x1b_Ga=d,d=A\x1b\\\x1b[?25h\x1b[?1049l\x1b[0m")
        sys.stdout.flush()
        termios.tcsetattr(self.fd, termios.TCSAFLUSH, self.old)

    def say(self, text, secs=4):
        self.msg, self.msg_t = text, time.monotonic() + secs

    # -- scale helpers
    def pcs(self):
        return SCALES[self.song.scale][1]

    def snap(self, n):
        pcs = self.pcs()
        if not pcs or (n - self.song.root) % 12 in pcs:
            return n
        for d in range(1, 7):
            for c in (n + d, n - d):
                if (c - self.song.root) % 12 in pcs:
                    return c
        return n

    # -- editing
    def enter_note(self, off):
        t, s = self.ct, self.song
        note = max(24, min(107, 12 * (self.oct + 1) + off))
        if t < NMEL:
            note = self.snap(note)
            s.pats[self.cp][self.cr][t] = (note, self.inst)
            self.eng.events.append(("note", t, note, self.inst))
        else:
            s.pats[self.cp][self.cr][t] = (note, 0)
            self.eng.events.append(("drum", DRUM_BIT[DRUM_NAMES[note % 12]]))
            self.eng.last_hit[t] = time.monotonic()
        self.cr = (self.cr + self.step) % ROWS
        self.dirty = True

    def dice(self):
        s, t = self.song, self.ct
        if t >= NMEL:
            tpl = random.choice(DRUM_PATTERNS[t - NMEL])
            for r, ch in enumerate(tpl):
                s.pats[self.cp][r][t] = (DRUM_LETTER[ch], 0) if ch != "." else (0, 0)
            what = "beat" if t == NMEL else "hat pattern"
        else:
            pcs, root = self.pcs() or SCALES[1][1], s.root
            pool = [n for n in range(24, 108) if (n - root) % 12 in pcs]
            if t == 0:
                what = "bass line"
                base = 36 + root
                rhythm = random.choice(["x.x.x.x.x.x.x.x.", "x..x..x.x..x..x.", "x.xx.x.xx.xx.x.x",
                                        "x...x...x..xx...", "x.x...x.x.x.x..x"])
                for r, ch in enumerate(rhythm):
                    if ch == "x":
                        n = self.snap(base + random.choice([0, 0, 0, 7, 12, 10, 3]))
                        s.pats[self.cp][r][t] = (n, self.inst)
                    else:
                        s.pats[self.cp][r][t] = (0, 0)
            else:
                what = "melody"
                centre = 12 * (self.oct + 1) + root + 7
                i = min(range(len(pool)), key=lambda k: abs(pool[k] - centre))
                dens = random.uniform(0.3, 0.6)
                prev_rest = False
                for r in range(ROWS):
                    if (r == 0 and random.random() < 0.7) or (random.random() < dens and not prev_rest):
                        i = max(0, min(len(pool) - 1, i + random.choice([-2, -1, -1, 0, 1, 1, 2])))
                        s.pats[self.cp][r][t] = (pool[i], self.inst)
                        prev_rest = random.random() < 0.3
                    else:
                        s.pats[self.cp][r][t] = (0, 0)
                        prev_rest = False
        self.dirty = True
        self.say(f"Rolled a new {what} on track {t + 1}. Press F5 again for another.")

    def toggle_play(self):
        e = self.eng
        if e.playing:
            e.events.append(("stop",))
            e.playing = False
            return
        e.loop_pat = self.cp
        start = self.cp
        if e.mode == "song" and self.song.empty(start):
            start = next((p for p in range(NPAT) if not self.song.empty(p)), start)
        e.events.append(("play", start, 0))
        e.playing = True

    def do_export(self):
        out = os.path.splitext(self.path)[0] + ".wav"
        self.say("Rendering " + out + " ...")
        self.draw()
        render_wav(self.song, out)
        self.say("Exported " + out)

    # -- keys
    def key(self, k, base=None):
        if k not in ("quit",):
            self.armed = False
        if self.help:
            self.help = False
            return
        s, e = self.song, self.eng
        if len(k) == 1 and k.isalpha():
            k = k.lower()
        pos = base or k                    # physical (US-layout) key, so the piano survives any layout
        if len(pos) == 1 and pos in PIANO:
            return self.enter_note(PIANO[pos])
        if k == "quit":
            if self.dirty and not self.armed:
                self.armed = True
                self.say("Unsaved changes! Ctrl-S saves, Ctrl-Q again quits.")
                return
            return "quit"
        moves = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
        if k in moves:
            self.ct = (self.ct + moves[k][0]) % NTRK
            self.cr = (self.cr + moves[k][1]) % ROWS
        elif k == "home": self.cr = 0
        elif k == "end": self.cr = ROWS - 1
        elif k in ("pgup", "pgdn"):
            self.cp = (self.cp + (1 if k == "pgdn" else -1)) % NPAT
            e.loop_pat = self.cp
        elif k == "space": self.toggle_play()
        elif k == "tab":
            e.mode = "song" if e.mode == "loop" else "loop"
            self.say("SONG mode: plays patterns A to H in order, skipping empty ones." if e.mode == "song"
                     else "LOOP mode: repeats the pattern you're editing.")
        elif k in ("del", "bs"):
            s.pats[self.cp][self.cr][self.ct] = (0, 0)
            self.dirty = True
        elif pos == "1":
            if self.ct < NMEL:
                s.pats[self.cp][self.cr][self.ct] = (-1, 0)
                self.cr = (self.cr + self.step) % ROWS
                self.dirty = True
        elif k == "enter":
            e.muted[self.ct] = not e.muted[self.ct]
            e.events.append(("mute", self.ct))
        elif k in (",", ".", "shift-left", "shift-right"):
            self.inst = (self.inst + (1 if k in (".", "shift-right") else -1)) % len(INSTRUMENTS)
            if self.ct < NMEL:
                e.events.append(("note", self.ct, self.snap(12 * (self.oct + 1)), self.inst))
        elif k in ("[", "shift-down"): self.oct = max(1, self.oct - 1)
        elif k in ("]", "shift-up"): self.oct = min(7, self.oct + 1)
        elif k in ("-", "=", "f9", "f10"):
            s.bpm = max(60, min(240, s.bpm + (5 if k in ("=", "f10") else -5)))
        elif k in ("_", "+"):
            s.bpm = max(60, min(240, s.bpm + (1 if k == "+" else -1)))
        elif k == "f1" or k == "?": self.help = True
        elif k == "f2":
            s.scale = (s.scale + 1) % len(SCALES)
            self.say("Scale lock: " + (ROOTS[s.root] + " " + SCALES[s.scale][0] if s.scale else "off (free)"))
        elif k == "f3":
            s.root = (s.root + 1) % 12
            self.say("Scale root: " + ROOTS[s.root])
        elif k == "f5": self.dice()
        elif k == "f6":
            for r in range(ROWS):
                s.pats[self.cp][r][self.ct] = (0, 0)
            self.dirty = True
        elif k == "f7":
            if self.cp < NPAT - 1:
                s.pats[self.cp + 1] = [list(r) for r in s.pats[self.cp]]
                self.cp += 1
                e.loop_pat = self.cp
                self.dirty = True
                self.say("Copied to pattern " + "ABCDEFGH"[self.cp] + " - now make it different!")
        elif k == "f8":
            for r in range(ROWS):
                s.pats[self.cp][r] = [(0, 0)] * NTRK
            self.dirty = True
        elif k == "save":
            s.save(self.path)
            self.dirty = False
            self.say("Saved " + self.path)
        elif k == "export": self.do_export()

    # -- drawing
    def level(self, t, now):
        return math.exp(-(now - self.eng.last_hit[t]) / 0.35) if self.eng.last_hit[t] else 0.0

    def cell_text(self, t, cell):
        note, inst = cell
        if note == 0:
            return FAINT, " ··· · "
        if note < 0:
            return (150, 150, 170), " ===   "
        if t >= NMEL:
            n = DRUM_NAMES[note % 12]
            return DRUM_RGB[n], f" {n}   "
        return TRACK_RGB[t], f" {note_name(note)} {inst} "

    def draw(self):
        W, H, cw, chh = term_size()
        if self.resized:
            sys.stdout.write("\x1b_Ga=d,d=A\x1b\\\x1b[2J")
            self.resized = False
        if W < 70 or H < 24:
            sys.stdout.write("\x1b[H\x1b[2J" + "Please make the terminal at least 70x24.")
            sys.stdout.flush()
            return
        e, s, now = self.eng, self.song, time.monotonic()
        if e.playing and e.mode == "song":
            self.cp = e.pat
        L = []
        scale = (ROOTS[s.root] + " " + SCALES[s.scale][0]) if s.scale else "free"
        play = f"{fg((110, 230, 130))}▶ PLAY" if e.playing else f"{fg(FAINT)}■ STOP"
        L.append(f" {BOLD}{fg(ACCENT)}♪ OPL PAL{RST}  {play}{RST}  {fg(INK)}{e.mode.upper()}{RST}"
                 f"  BPM {fg(ACCENT)}{s.bpm}{RST}  OCT {fg(ACCENT)}{self.oct}{RST}  SCALE {fg(ACCENT)}{scale}{RST}")
        tabs = " PATTERN "
        for p in range(NPAT):
            ch = "ABCDEFGH"[p]
            playing = e.playing and e.pat == p
            col = (110, 230, 130) if playing else (INK if not s.empty(p) else FAINT)
            tabs += (f"{bgc((70, 76, 120))}{fg((255, 255, 255))}{BOLD}[{ch}]{RST}" if p == self.cp
                     else f"{fg(col)} {ch} {RST}")
        L.append(tabs)
        hdr, vu = "   ", "   "
        for t in range(NTRK):
            name = f"T{t + 1}" if t < NMEL else f"D{t - NMEL + 1}"
            c = TRACK_RGB[t]
            if e.muted[t]:
                hdr += f"{fg(FAINT)}│ {name} mut"
                vu += fg(FAINT) + "│       "
            else:
                hdr += f"{fg(c)}{BOLD}│  {name}   {RST}"
                eighths = int(min(1, self.level(t, now)) * 40)
                bar = "█" * (eighths // 8) + (" ▏▎▍▌▋▊▉"[eighths % 8] if eighths % 8 else "")
                vu += f"{fg(c)}│ {bar:<5} {RST}"
        L += [hdr + RST, vu + RST]
        live = e.playing and e.pat == self.cp
        for r in range(ROWS):
            is_play = live and e.row == r
            rowbg = bgc(BG_PLAY) if is_play else (bgc(BG_ROW) if r == self.cr else "")
            line = f"{rowbg}{fg(ACCENT if r % 4 == 0 else FAINT)}{r:02d} {RST}"
            for t in range(NTRK):
                col, txt = self.cell_text(t, s.pats[self.cp][r][t])
                if e.muted[t]:
                    col = FAINT
                cur = r == self.cr and t == self.ct
                style = f"{bgc(BG_CUR)}{fg((20, 20, 30))}" if cur else f"{rowbg}{fg(col)}"
                line += f"{rowbg}{fg(FAINT)}│{style}{txt}{RST}"
            L.append(line + RST)
        L.append("")
        names = [f"{i} {n[0]}" for i, n in enumerate(INSTRUMENTS)]
        for half in (names[:5], names[5:]):
            row = " INSTR "
            for n in half:
                i = int(n[0])
                row += (f"{bgc(ACCENT)}{fg((20, 20, 30))} {n} {RST} " if i == self.inst else f"{fg(INK)} {n} {RST} ")
            L.append(row)
        L.append(f" {fg(ACCENT)}{self.msg}{RST}" if now < self.msg_t else "")
        k_ = lambda a: f"{fg(ACCENT)}{a}{RST}{fg(FAINT)}"
        L.append(f"{fg(FAINT)} {k_('Z..M')} {k_('Q..P')} notes  {k_('S D G H J')} sharps  {k_('arrows')} move  {k_('1')} note-off{RST}")
        L.append(f"{fg(FAINT)} {k_('Space')} play  {k_('Tab')} loop/song  {k_('PgUp/Dn')} pattern  {k_('Del')} clear  {k_('Enter')} mute{RST}")
        L.append(f"{fg(FAINT)} {k_('Shift+arrows')} instr/octave  {k_('F9 F10')} tempo  {k_('F5')} dice  {k_('F1')} help{RST}")
        if self.audio.error:
            L.append(f" {fg((255, 90, 90))}audio thread died: {self.audio.error}{RST}")
        elif not self.audio.sink.ok:
            L.append(f" {fg((255, 160, 90))}no audio device ({self.audio.sink.why or 'lost it'}) - running silent{RST}")
        if self.help:
            L = L[:2] + HELP
        out = ["\x1b[?2026h"]
        for i in range(min(H, len(L))):
            out.append(f"\x1b[{i + 1};1H{L[i]}\x1b[K")
        for i in range(len(L), H):
            out.append(f"\x1b[{i + 1};1H\x1b[K")
        if self.gfx:
            out.append(self.gfx_panel(W, H, cw, chh, now))
        out.append("\x1b[?2026l")
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    # -- the pretty picture
    def gfx_panel(self, W, H, cw, ch, now):
        if W >= 96:
            x0, y0, cols, rows = 69, 2, W - 70, min(H - 3, 36)
        elif H >= 34:
            x0, y0, cols, rows = 1, 28, min(W - 2, 66), min(H - 29, 16)
        else:
            return ""
        if cols < 12 or rows < 4:
            return ""
        bw, bh = cols * cw, rows * ch
        sc = min(1.0, 560 / bw, 320 / bh)
        iw, ih = max(32, int(bw * sc)), max(16, int(bh * sc))
        px = bytearray(self.background(iw, ih))
        bar_w = iw // NTRK
        for t in range(NTRK):
            hh = int(min(1, self.level(t, now)) * ih * 0.9)
            c = TRACK_RGB[t]
            xa, xb = t * bar_w + 1, (t + 1) * bar_w - 1
            for y in range(ih - hh, ih):
                f = 0.65 - 0.45 * (y - (ih - hh)) / max(1, hh)
                px[(y * iw + xa) * 3:(y * iw + xb) * 3] = bytes((int(c[0] * f), int(c[1] * f), int(c[2] * f))) * (xb - xa)
        win = 1536
        samp = self.eng.scope(win)
        start = next((i for i in range(1, win // 3) if samp[i - 1] < 0 <= samp[i]), 0)
        stride = max(1, (win - start) // iw)
        peak = max(3000, max(abs(v) for v in samp))
        self.gain = self.gain * 0.85 + (0.42 * ih / peak) * 0.15
        mid, prev = ih // 2, ih // 2
        self.frame += 1
        lim = ih - 3
        for x in range(iw):
            seg = samp[start + x * stride:start + (x + 1) * stride] or [0]
            ya = max(2, min(lim, mid - int(max(seg) * self.gain)))
            yb = max(2, min(lim, mid - int(min(seg) * self.gain)))
            col = self.pal[(x * 256 // iw + self.frame * 4) % 256]
            for yy in range(min(ya, yb, prev) - 1, max(ya, yb, prev) + 2):
                px[(yy * iw + x) * 3:(yy * iw + x) * 3 + 3] = col
            prev = (ya + yb) // 2
        data = base64.b64encode(zlib.compress(bytes(px), 1)).decode()
        out = [f"\x1b[{y0 + 1};{x0 + 1}H"]
        for i in range(0, len(data), 4096):
            more = 1 if i + 4096 < len(data) else 0
            if i == 0:
                out.append(f"\x1b_Ga=T,f=24,s={iw},v={ih},o=z,i=1,p=1,c={cols},r={rows},C=1,q=2,m={more};{data[i:i + 4096]}\x1b\\")
            else:
                out.append(f"\x1b_Gm={more};{data[i:i + 4096]}\x1b\\")
        return "".join(out)

    def background(self, w, h):
        if (w, h) not in self.bg_cache:
            rows = []
            for y in range(h):
                f = y / h
                c = (int(10 + 12 * f), int(12 + 4 * f), int(26 + 16 * f))
                if y == h // 2:
                    c = (44, 52, 86)
                elif y % max(1, h // 4) == 0:
                    c = (26, 30, 52)
                rows.append(bytes(c) * w)
            self.bg_cache[(w, h)] = b"".join(rows)
        return self.bg_cache[(w, h)]

    # -- main loop
    def run(self):
        self.term_on()
        sys.setswitchinterval(0.001)
        try:
            self.audio.start()
            nxt = time.monotonic()
            while True:
                wait = max(0.0, nxt - time.monotonic())
                if select.select([self.fd], [], [], wait)[0]:
                    data = os.read(self.fd, 4096).decode("utf-8", "ignore")
                    for k, base in tokens(data, self.layout):
                        if self.key(k, base) == "quit":
                            return
                if time.monotonic() >= nxt:
                    self.draw()
                    nxt = time.monotonic() + 1 / FPS
        finally:
            self.term_off()
            self.audio.stop()


def main():
    args = sys.argv[1:]
    gfx = None
    wav = None
    path = "oplpal-song.json"
    layout, use_kbd = os.environ.get("OPLPAL_LAYOUT", "us"), True
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--no-gfx": gfx = False
        elif a == "--gfx": gfx = True
        elif a == "--wav": wav = args[i + 1]; i += 1
        elif a == "--layout": layout = args[i + 1]; i += 1
        elif a == "--no-kbd": use_kbd = False
        elif a in ("-h", "--help"): print(__doc__); return
        else: path = a
        i += 1
    try:
        song = Song.load(path) if os.path.exists(path) else demo_song()
    except (ValueError, KeyError, AssertionError, OSError) as ex:
        sys.exit(f"Couldn't read {path}: {ex}")
    if wav:
        render_wav(song, wav)
        print("wrote", wav)
        return
    if not sys.stdin.isatty():
        sys.exit("OPL PAL needs a real terminal.")
    if layout not in LAYOUTS:
        sys.exit(f"Unknown layout {layout!r}; choose from: {', '.join(LAYOUTS)}")
    random.seed()
    app = App(song, path, gfx, layout, use_kbd)
    lang = (os.environ.get("LC_ALL") or os.environ.get("LANG") or "")[:2]
    app.lang_hint = lang if lang in ("de", "fr") and layout == "us" else ""
    app.run()


if __name__ == "__main__":
    main()
