"""
OpenPal - Phase 3a: Sound Card TX/RX Interface
===============================================
Handles real audio hardware — plays encoded audio out through the
sound card to the radio TX, and records incoming audio from the radio RX.

On the field Pi (TX end):
  - USB audio adapter output → SA828-U audio input
  - PTT controlled via GPIO pin or SA828-U VOX

On the home Pi (RX end):
  - Baofeng UV-5R audio out → USB audio adapter line in
  - Kenwood K1 cable (3.5mm TRRS) carries audio

Pi GPIO PTT wiring (field TX end):
  - GPIO 17 (BCM) → SA828-U PTT pin (active LOW)
  - 1kΩ series resistor for protection

Greg VK4GDW / OpenPal Project 2026
In memory of Erik Sundstrup VK4AES (SK)
"""

import numpy as np
import time
import threading
import queue
import sys
import os

try:
    import sounddevice as sd
    SOUNDDEVICE_OK = True
except Exception as e:
    SOUNDDEVICE_OK = False
    print(f"[Soundcard] WARNING: sounddevice unavailable: {e}")

try:
    import RPi.GPIO as GPIO
    GPIO_OK = True
except ImportError:
    GPIO_OK = False

from openpal_modem import SAMPLE_RATE, _save_wav

# ── Hardware configuration ────────────────────────────────────────────────────
# Run: python3 -c "import sounddevice; print(sounddevice.query_devices())"
# on your Pi to find device indices.

TX_DEVICE      = None    # None=system default, or integer index, or name substring
RX_DEVICE      = None    # None=system default

PTT_PIN        = 17      # BCM GPIO pin for PTT (None to disable / use VOX)
PTT_ACTIVE_LOW = True    # True=SA828-U standard (pull LOW to TX)
PTT_LEAD_MS    = 200     # ms to hold PTT before audio (radio settle time)
PTT_TAIL_MS    = 500     # ms to hold PTT after audio (tail)

RX_TIMEOUT_S   = 300     # max seconds to wait for a transmission
RX_SQUELCH     = 0.01    # RMS threshold below which we consider channel idle


# ── PTT controller ────────────────────────────────────────────────────────────

class PTTController:
    """
    Controls radio PTT via Raspberry Pi GPIO.
    Use as a context manager — always releases PTT on exit.
    Falls back to no-op if RPi.GPIO not available (desktop/CI).
    """
    def __init__(self, pin=PTT_PIN, active_low=PTT_ACTIVE_LOW):
        self.pin       = pin
        self.active_low= active_low
        self.enabled   = GPIO_OK and pin is not None
        if self.enabled:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.pin, GPIO.OUT)
            self._set_rx()
            print(f"[PTT] GPIO {self.pin} ready "
                  f"(active {'LOW' if active_low else 'HIGH'})")
        else:
            print("[PTT] GPIO unavailable — PTT disabled (use VOX or manual)")

    def _set_tx(self):
        if self.enabled:
            GPIO.output(self.pin, GPIO.LOW if self.active_low else GPIO.HIGH)

    def _set_rx(self):
        if self.enabled:
            GPIO.output(self.pin, GPIO.HIGH if self.active_low else GPIO.LOW)

    def transmit(self):
        """Key PTT and wait for radio to come up."""
        self._set_tx()
        time.sleep(PTT_LEAD_MS / 1000)
        print("[PTT] Transmitting")

    def receive(self):
        """Wait tail then release PTT."""
        time.sleep(PTT_TAIL_MS / 1000)
        self._set_rx()
        print("[PTT] Receiving")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._set_rx()
        if self.enabled:
            try:
                GPIO.cleanup()
            except Exception:
                pass


# ── TX: play audio through sound card ────────────────────────────────────────

def transmit_audio(audio: np.ndarray,
                   device=TX_DEVICE,
                   ptt: PTTController = None):
    """
    Transmit audio through the sound card.
    Keys PTT if a PTTController is supplied; otherwise assumes VOX or manual.
    Falls back to WAV file if no sound card present (useful for testing).
    """
    duration = len(audio) / SAMPLE_RATE
    print(f"[TX] {duration:.1f}s audio, "
          f"{'PTT GPIO' if ptt and ptt.enabled else 'VOX/manual PTT'}")

    if not SOUNDDEVICE_OK:
        path = "/tmp/openpal_tx_live.wav"
        print(f"[TX] No sound card — saving to {path}")
        _save_wav(audio, path)
        return

    if ptt:
        ptt.transmit()

    try:
        sd.play(audio.astype(np.float32),
                samplerate=SAMPLE_RATE,
                device=device,
                blocking=True)
    finally:
        if ptt:
            ptt.receive()

    print("[TX] Done")


# ── RX: record from sound card ────────────────────────────────────────────────

class AudioReceiver:
    """
    Continuously records from the sound card into a rolling ring buffer.
    Detects OpenPal preamble tones and extracts complete transmissions.

    Usage:
        rx = AudioReceiver()
        rx.start()
        audio = rx.wait_for_transmission(timeout=300)
        rx.stop()
        # pass audio to openpal_modem.audio_to_packets()
    """

    def __init__(self, device=RX_DEVICE, chunk_s=0.1):
        self.device   = device
        self.chunk    = int(SAMPLE_RATE * chunk_s)
        self._buf     = []
        self._lock    = threading.Lock()
        self._stream  = None

    def _audio_callback(self, indata, frames, time_info, status):
        chunk = indata[:, 0].copy()
        with self._lock:
            self._buf.append(chunk)
            # Rolling 10-minute buffer max
            max_chunks = int(600 * SAMPLE_RATE / self.chunk)
            if len(self._buf) > max_chunks:
                self._buf.pop(0)

    def start(self):
        if not SOUNDDEVICE_OK:
            print("[RX] No sound card — file-based RX only")
            return
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype='float32',
            blocksize=self.chunk,
            device=self.device,
            callback=self._audio_callback
        )
        self._stream.start()
        print(f"[RX] Listening (device={self.device or 'default'}, "
              f"squelch={RX_SQUELCH})")

    def stop(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        print("[RX] Stopped")

    def get_buffer(self) -> np.ndarray:
        with self._lock:
            if not self._buf:
                return np.array([], dtype=np.float32)
            return np.concatenate(self._buf).astype(np.float32)

    def wait_for_transmission(self, timeout=RX_TIMEOUT_S):
        """
        Block until preamble is detected and transmission completes,
        or timeout expires. Returns audio ndarray or None.
        """
        from openpal_modem import detect_preamble
        print(f"[RX] Waiting for OpenPal signal (timeout={timeout}s)...")
        deadline         = time.time() + timeout
        preamble_found   = False
        preamble_pos_s   = 0.0

        while time.time() < deadline:
            audio = self.get_buffer()
            dur   = len(audio) / SAMPLE_RATE

            if dur > 1.0 and not preamble_found:
                pos = detect_preamble(audio)
                if pos > 0:
                    preamble_found = True
                    preamble_pos_s = pos / SAMPLE_RATE
                    print(f"[RX] Preamble at {preamble_pos_s:.1f}s — "
                          f"waiting for end of transmission...")

            if preamble_found:
                # Wait until 2s of silence after the preamble
                min_wait = preamble_pos_s + 10.0
                if dur > min_wait:
                    tail = audio[-int(SAMPLE_RATE * 2):]
                    rms  = float(np.sqrt(np.mean(tail ** 2)))
                    if rms < RX_SQUELCH:
                        print(f"[RX] Transmission complete ({dur:.1f}s captured)")
                        start = max(0, int((preamble_pos_s - 0.5) * SAMPLE_RATE))
                        return audio[start:]

            time.sleep(0.25)

        print("[RX] Timeout")
        return None

    def record_fixed(self, duration_s: float, path: str = None) -> np.ndarray:
        """Record for a fixed duration. Optionally save to WAV."""
        if not SOUNDDEVICE_OK:
            print("[RX] No sound card")
            return np.array([], dtype=np.float32)
        print(f"[RX] Recording {duration_s}s...")
        data = sd.rec(int(duration_s * SAMPLE_RATE),
                      samplerate=SAMPLE_RATE, channels=1,
                      dtype='float32', device=self.device)
        sd.wait()
        audio = data[:, 0]
        if path:
            _save_wav(audio, path)
            print(f"[RX] Saved: {path}")
        return audio


# ── Device listing ────────────────────────────────────────────────────────────

def list_audio_devices():
    if not SOUNDDEVICE_OK:
        print("[Soundcard] Not available")
        return
    print("\nAvailable audio devices:")
    print("─" * 65)
    for i, dev in enumerate(sd.query_devices()):
        markers = []
        if i == sd.default.device[0]: markers.append("DEFAULT-IN")
        if i == sd.default.device[1]: markers.append("DEFAULT-OUT")
        print(f"  [{i:2d}] {dev['name'][:42]:<42} "
              f"in={dev['max_input_channels']} "
              f"out={dev['max_output_channels']} "
              f"{' '.join(markers)}")
    print("─" * 65)
    print("Set TX_DEVICE / RX_DEVICE in openpal_soundcard.py\n")


if __name__ == '__main__':
    list_audio_devices()
