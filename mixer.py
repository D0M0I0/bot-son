"""Capture plusieurs applis à la fois et mélange leur son en trames prêtes pour Discord."""

import threading

import discord
import numpy as np
import psutil

from process_capture import ProcessCapture

RATE = 48000
FRAME_BYTES = 3840  # 20 ms de PCM 48 kHz stéréo 16 bits, ce qu'attend discord.py
MAX_BUFFER = FRAME_BYTES * 10  # au-delà de 200 ms de retard, on jette le plus ancien
SILENCE = b"\x00" * FRAME_BYTES
RESCAN_SECONDS = 2


class _Capture:
    """Tampon d'un processus capturé."""

    def __init__(self, pid, name):
        self.name = name
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.capture = ProcessCapture(pid, self._push, RATE)

    def _push(self, data):
        with self.lock:
            self.buf += data
            if len(self.buf) > MAX_BUFFER:
                del self.buf[: len(self.buf) - MAX_BUFFER]

    def pop_frame(self):
        with self.lock:
            if len(self.buf) < FRAME_BYTES:
                return None
            frame = bytes(self.buf[:FRAME_BYTES])
            del self.buf[:FRAME_BYTES]
        return frame


def _root_pids(names):
    """PID des processus racines portant un de ces noms.

    La capture inclut les processus enfants, donc pour une appli multi-processus
    (Discord, navigateurs…) on ne cible que le processus parent.
    """
    wanted = {n.lower() for n in names}
    procs = {}
    for p in psutil.process_iter(["pid", "ppid", "name"]):
        if p.info["name"] and p.info["name"].lower() in wanted:
            procs[p.info["pid"]] = p.info
    return {
        pid: info["name"]
        for pid, info in procs.items()
        if info["ppid"] not in procs or procs[info["ppid"]]["name"].lower() != info["name"].lower()
    }


class Mixer:
    """Garde à jour une capture par appli sélectionnée, même si elle est relancée."""

    def __init__(self):
        self.volume = 1.0
        self._apps = set()
        self._captures = {}  # pid -> _Capture
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    def set_apps(self, names):
        self._apps = set(names)
        self._wake.set()

    def captured_apps(self):
        with self._lock:
            return {c.name for c in self._captures.values()}

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3)
        with self._lock:
            captures, self._captures = self._captures, {}
        for c in captures.values():
            c.capture.stop()

    def _run(self):
        while not self._stop.is_set():
            self._rescan()
            self._wake.wait(RESCAN_SECONDS)
            self._wake.clear()

    def _rescan(self):
        wanted = _root_pids(self._apps) if self._apps else {}
        with self._lock:
            gone = [pid for pid in self._captures if pid not in wanted]
            removed = [self._captures.pop(pid) for pid in gone]
            new = {pid: name for pid, name in wanted.items() if pid not in self._captures}
        for c in removed:
            c.capture.stop()
        for pid, name in new.items():
            try:
                capture = _Capture(pid, name)
            except Exception as e:
                print(f"Impossible de capturer {name} ({pid}) : {e}")
                continue
            with self._lock:
                self._captures[pid] = capture

    def read_frame(self):
        with self._lock:
            captures = list(self._captures.values())
        frames = [f for f in (c.pop_frame() for c in captures) if f is not None]
        if not frames:
            return SILENCE
        if len(frames) == 1 and self.volume == 1.0:
            return frames[0]
        mixed = sum(np.frombuffer(f, dtype=np.int16).astype(np.int32) for f in frames)
        mixed = mixed * self.volume
        return np.clip(mixed, -32768, 32767).astype(np.int16).tobytes()


class MixerSource(discord.AudioSource):
    """Source discord.py qui lit le mélangeur ; ne l'arrête pas quand le bot quitte le vocal."""

    def __init__(self, mixer):
        self._mixer = mixer

    def read(self):
        # Ne jamais renvoyer b"" : discord.py considérerait la source terminée.
        return self._mixer.read_frame()

    def is_opus(self):
        return False
