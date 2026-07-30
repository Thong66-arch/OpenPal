"""
OpenPal - Phase 2: OFDM Audio Modem
=====================================
Converts OpenPal packets to/from an audio waveform suitable for
transmission over a standard SSB/FM transceiver via sound card.

Modulation: OFDM (Orthogonal Frequency Division Multiplexing)
  - Same family as EasyPal/DRM but our own clean implementation
  - Multiple carriers across a narrow audio passband
  - Each carrier independently BPSK or QPSK modulated
  - Guard intervals between symbols to handle multipath

Passband: 300 Hz – 2700 Hz  (fits inside SSB or FM audio)
Sample rate: 8000 Hz         (works on any sound card, Pi included)
Symbol rate: configurable    (default gives ~300 bps/carrier)

Audio output: WAV file or direct to sound card via sounddevice
Audio input:  WAV file or direct from sound card

Greg VK4GDW / OpenPal Project 2026
In memory of Erik Sundstrup VK4AES (SK)
"""

import numpy as np
import struct
import wave
import io
import time
from scipy import signal as scipy_signal


# ── Modem Parameters ─────────────────────────────────────────────────────────

SAMPLE_RATE     = 8000       # Hz — universal sound card rate
FREQ_LOW        = 300        # Hz — lowest carrier
FREQ_HIGH       = 2700       # Hz — highest carrier
NUM_CARRIERS    = 16         # number of OFDM sub-carriers
SYMBOL_DURATION = 0.040      # seconds per OFDM symbol (40 ms)
GUARD_DURATION  = 0.008      # cyclic prefix guard interval (8 ms)
PREAMBLE_FREQ   = 1500       # Hz — single-tone sync preamble
PREAMBLE_DUR    = 0.500      # seconds of preamble tone
POSTAMBLE_DUR   = 0.200      # seconds of silence after data
AMPLITUDE       = 0.7        # output amplitude (0.0–1.0)
MODULATION      = 'QPSK'     # 'BPSK' (1 bit/carrier) or 'QPSK' (2 bits/carrier)

# Derived
SAMPLES_PER_SYMBOL = int(SAMPLE_RATE * SYMBOL_DURATION)
SAMPLES_GUARD      = int(SAMPLE_RATE * GUARD_DURATION)
SAMPLES_TOTAL      = SAMPLES_PER_SYMBOL + SAMPLES_GUARD
BITS_PER_SYMBOL    = 2 if MODULATION == 'QPSK' else 1
BITS_PER_OFDM_SYM  = NUM_CARRIERS * BITS_PER_SYMBOL
BYTES_PER_OFDM_SYM = BITS_PER_OFDM_SYM // 8

# Carrier frequencies — evenly spaced across passband
CARRIERS = np.linspace(FREQ_LOW, FREQ_HIGH, NUM_CARRIERS)

# Pre-compute carrier phases for each sample in a symbol
_t_sym  = np.arange(SAMPLES_PER_SYMBOL) / SAMPLE_RATE
_phases = np.array([2 * np.pi * f * _t_sym for f in CARRIERS])  # (N_carriers, N_samples)

# QPSK constellation: 2 bits → complex symbol
_QPSK_MAP = {
    0b00: complex( 1,  1) / np.sqrt(2),
    0b01: complex(-1,  1) / np.sqrt(2),
    0b10: complex( 1, -1) / np.sqrt(2),
    0b11: complex(-1, -1) / np.sqrt(2),
}
_QPSK_DEMAP = {v: k for k, v in _QPSK_MAP.items()}

_BPSK_MAP  = {0: complex(1, 0), 1: complex(-1, 0)}
_BPSK_DEMAP= {v: k for k, v in _BPSK_MAP.items()}


# ── Bit packing helpers ───────────────────────────────────────────────────────

def bytes_to_bits(data: bytes) -> list:
    bits = []
    for byte in data:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)
    return bits


def bits_to_bytes(bits: list) -> bytes:
    # Pad to multiple of 8
    while len(bits) % 8:
        bits.append(0)
    out = bytearray()
    for i in range(0, len(bits), 8):
        byte = 0
        for j in range(8):
            byte = (byte << 1) | bits[i + j]
        out.append(byte)
    return bytes(out)


# ── OFDM Symbol encode/decode ─────────────────────────────────────────────────

def _bits_to_symbols(bits: list) -> list:
    """Convert bitstream to list of complex constellation symbols, one per carrier."""
    symbols = []
    if MODULATION == 'QPSK':
        for i in range(0, len(bits) - 1, 2):
            dibit = (bits[i] << 1) | bits[i + 1]
            symbols.append(_QPSK_MAP[dibit])
    else:
        for b in bits:
            symbols.append(_BPSK_MAP[b])
    return symbols


def _symbols_to_bits(symbols: list) -> list:
    """Decode complex symbols back to bits using nearest-neighbour decision."""
    bits = []
    if MODULATION == 'QPSK':
        for sym in symbols:
            # Find nearest QPSK point
            best_dibit = min(_QPSK_MAP, key=lambda d: abs(_QPSK_MAP[d] - sym))
            bits.append((best_dibit >> 1) & 1)
            bits.append(best_dibit & 1)
    else:
        for sym in symbols:
            bits.append(0 if sym.real >= 0 else 1)
    return bits


def modulate_ofdm_symbol(carrier_bits: list) -> np.ndarray:
    """
    Modulate one OFDM symbol from a list of bits (one dibit per carrier for QPSK).
    Returns audio samples including cyclic prefix guard interval.
    """
    assert len(carrier_bits) == NUM_CARRIERS * BITS_PER_SYMBOL

    # Map bits to constellation points
    symbols = _bits_to_symbols(carrier_bits)

    # Sum carriers: each carrier = Re(symbol * e^(j*2pi*f*t))
    audio = np.zeros(SAMPLES_PER_SYMBOL)
    for c, sym in enumerate(symbols):
        # I/Q modulation onto carrier frequency
        audio += sym.real * np.cos(_phases[c]) - sym.imag * np.sin(_phases[c])

    # Normalise
    peak = np.max(np.abs(audio))
    if peak > 0:
        audio /= peak
    audio *= AMPLITUDE

    # Cyclic prefix: copy end of symbol to front (guard against multipath)
    guard = audio[-SAMPLES_GUARD:]
    return np.concatenate([guard, audio])


def demodulate_ofdm_symbol(samples: np.ndarray) -> list:
    """
    Demodulate one OFDM symbol. `samples` should be SAMPLES_TOTAL long.
    Strip guard interval, correlate against each carrier, decode bits.
    """
    # Remove cyclic prefix
    audio = samples[SAMPLES_GUARD:SAMPLES_GUARD + SAMPLES_PER_SYMBOL]

    recovered_bits = []
    for c in range(NUM_CARRIERS):
        # Correlate with I and Q components
        I = 2 * np.sum(audio * np.cos(_phases[c])) / SAMPLES_PER_SYMBOL
        Q = 2 * np.sum(audio * (-np.sin(_phases[c]))) / SAMPLES_PER_SYMBOL
        sym = complex(I, Q)

        if MODULATION == 'QPSK':
            dibit = min(_QPSK_MAP, key=lambda d: abs(_QPSK_MAP[d] - sym))
            recovered_bits.append((dibit >> 1) & 1)
            recovered_bits.append(dibit & 1)
        else:
            recovered_bits.append(0 if sym.real >= 0 else 1)

    return recovered_bits


# ── Preamble / sync ───────────────────────────────────────────────────────────

def make_preamble() -> np.ndarray:
    """
    Preamble: known tone burst for AGC settling + sync detection.
    Structure: silence → tone ramp up → steady tone → known OFDM pilot
    """
    t_pre   = np.arange(int(SAMPLE_RATE * PREAMBLE_DUR)) / SAMPLE_RATE

    # Sine tone with smooth envelope
    tone    = np.sin(2 * np.pi * PREAMBLE_FREQ * t_pre)
    env     = np.ones_like(t_pre)
    ramp    = int(0.05 * SAMPLE_RATE)
    env[:ramp]  = np.linspace(0, 1, ramp)
    env[-ramp:] = np.linspace(1, 0, ramp)
    tone   *= env * AMPLITUDE

    # Pilot OFDM symbol: all carriers at known phase (all bits = 0)
    pilot_bits = [0] * (NUM_CARRIERS * BITS_PER_SYMBOL)
    pilot      = modulate_ofdm_symbol(pilot_bits)

    # Short silence lead-in
    silence = np.zeros(int(SAMPLE_RATE * 0.1))

    return np.concatenate([silence, tone, pilot])


def make_postamble() -> np.ndarray:
    return np.zeros(int(SAMPLE_RATE * POSTAMBLE_DUR))


def detect_preamble(audio: np.ndarray, threshold: float = 0.3) -> int:
    """
    Scan audio for preamble tone using sliding correlation.
    Returns sample index of preamble end (= start of data), or -1.
    """
    t_pre   = np.arange(int(SAMPLE_RATE * PREAMBLE_DUR * 0.5)) / SAMPLE_RATE
    ref     = np.sin(2 * np.pi * PREAMBLE_FREQ * t_pre)
    ref    /= np.linalg.norm(ref)

    step    = int(SAMPLE_RATE * 0.010)   # 10 ms steps
    best_pos= -1
    best_val= 0.0

    for pos in range(0, len(audio) - len(ref), step):
        chunk = audio[pos:pos + len(ref)]
        norm  = np.linalg.norm(chunk)
        if norm < 1e-6:
            continue
        corr = abs(np.dot(chunk / norm, ref))
        if corr > best_val:
            best_val = corr
            best_pos = pos

    if best_val < threshold:
        return -1

    # Return position after preamble
    return best_pos + len(ref) + SAMPLES_TOTAL   # +1 pilot symbol


# ── Length header symbol ──────────────────────────────────────────────────────

def _encode_length_symbol(n_packets: int) -> np.ndarray:
    """Encode packet count into a dedicated OFDM symbol before data."""
    # Use 16 bits for packet count (max 65535 packets)
    count_bits = [(n_packets >> (15 - i)) & 1 for i in range(16)]
    # Repeat to fill all carrier bits
    all_bits   = (count_bits * ((NUM_CARRIERS * BITS_PER_SYMBOL // 16) + 1))
    all_bits   = all_bits[:NUM_CARRIERS * BITS_PER_SYMBOL]
    return modulate_ofdm_symbol(all_bits)


def _decode_length_symbol(samples: np.ndarray) -> int:
    bits = demodulate_ofdm_symbol(samples)[:16]
    n    = 0
    for b in bits:
        n = (n << 1) | b
    return n


# ── Main encode / decode ──────────────────────────────────────────────────────

def packets_to_audio(packets: list, output_wav: str = None) -> np.ndarray:
    """
    Convert a list of raw packet bytes to an audio waveform.
    Optionally saves to WAV file.
    Returns numpy float32 array of audio samples.
    """
    print(f"[OpenPal Modem] Encoding {len(packets)} packets to audio...")
    print(f"[OpenPal Modem] {NUM_CARRIERS} carriers, {MODULATION}, "
          f"{SAMPLE_RATE} Hz, symbol={SYMBOL_DURATION*1000:.0f}ms")

    segments = [make_preamble(), _encode_length_symbol(len(packets))]

    for pkt_idx, pkt in enumerate(packets):
        # Convert packet bytes to bits
        bits = bytes_to_bits(pkt)
        # Pad to multiple of BITS_PER_OFDM_SYM
        while len(bits) % BITS_PER_OFDM_SYM:
            bits.append(0)
        # Encode as OFDM symbols
        for sym_start in range(0, len(bits), BITS_PER_OFDM_SYM):
            sym_bits = bits[sym_start:sym_start + BITS_PER_OFDM_SYM]
            segments.append(modulate_ofdm_symbol(sym_bits))

    segments.append(make_postamble())
    audio = np.concatenate(segments).astype(np.float32)

    duration = len(audio) / SAMPLE_RATE
    bitrate  = (len(packets) * len(packets[0]) * 8) / duration
    print(f"[OpenPal Modem] Audio: {duration:.1f}s, "
          f"effective rate: {bitrate:.0f} bps, "
          f"{len(audio)} samples")

    if output_wav:
        _save_wav(audio, output_wav)
        print(f"[OpenPal Modem] Saved: {output_wav}")

    return audio


def audio_to_packets(audio: np.ndarray,
                     expected_packets: int = None) -> list:
    """
    Decode audio back to packet bytes.
    Returns list of bytes objects (one per packet).
    """
    from openpal_packet import PACKET_SIZE
    print(f"[OpenPal Modem] Decoding {len(audio)/SAMPLE_RATE:.1f}s of audio...")

    # Structural offsets (deterministic — no preamble search needed for same-
    # machine loopback; real RX will use detect_preamble for coarse sync)
    preamble_samples = len(make_preamble())   # silence + tone + pilot symbol
    length_sym_start = preamble_samples       # length symbol immediately follows
    data_start       = preamble_samples + SAMPLES_TOTAL   # data follows length sym

    # Read length symbol
    if length_sym_start + SAMPLES_TOTAL <= len(audio):
        n_packets = _decode_length_symbol(
            audio[length_sym_start:length_sym_start + SAMPLES_TOTAL])
    else:
        n_packets = 0

    if expected_packets is not None:
        n_packets = expected_packets
    print(f"[OpenPal Modem] Expecting {n_packets} packets")

    bits_per_pkt = PACKET_SIZE * 8
    syms_per_pkt = -(-bits_per_pkt // BITS_PER_OFDM_SYM)   # ceiling division

    packets = []
    for pkt_idx in range(n_packets):
        pkt_bits = []
        for sym_i in range(syms_per_pkt):
            start = data_start + (pkt_idx * syms_per_pkt + sym_i) * SAMPLES_TOTAL
            end   = start + SAMPLES_TOTAL
            if end > len(audio):
                pkt_bits.extend([0] * BITS_PER_OFDM_SYM)
            else:
                pkt_bits.extend(demodulate_ofdm_symbol(audio[start:end]))
        raw = bits_to_bytes(pkt_bits[:PACKET_SIZE * 8])[:PACKET_SIZE]
        packets.append(raw)

    print(f"[OpenPal Modem] Decoded {len(packets)} packets")
    return packets


# ── WAV file I/O ──────────────────────────────────────────────────────────────

def _save_wav(audio: np.ndarray, path: str):
    """Save float32 audio array as 16-bit WAV."""
    pcm = np.clip(audio, -1.0, 1.0)
    pcm = (pcm * 32767).astype(np.int16)
    with wave.open(path, 'w') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())


def load_wav(path: str) -> np.ndarray:
    """Load a WAV file as float32 numpy array."""
    with wave.open(path, 'r') as wf:
        assert wf.getnchannels() == 1,  "Mono only"
        assert wf.getframerate() == SAMPLE_RATE, \
            f"Need {SAMPLE_RATE} Hz, got {wf.getframerate()}"
        raw = wf.readframes(wf.getnframes())
        pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    return pcm / 32768.0


def add_channel_noise(audio: np.ndarray, snr_db: float = 20.0) -> np.ndarray:
    """Add AWGN noise at specified SNR for testing."""
    sig_power   = np.mean(audio ** 2)
    noise_power = sig_power / (10 ** (snr_db / 10))
    noise       = np.random.normal(0, np.sqrt(noise_power), len(audio))
    return (audio + noise).astype(np.float32)


# ── Modem info ────────────────────────────────────────────────────────────────

def print_modem_info():
    from openpal_packet import PACKET_SIZE
    bits_per_pkt   = PACKET_SIZE * 8
    syms_per_pkt   = -(-bits_per_pkt // BITS_PER_OFDM_SYM)
    secs_per_pkt   = syms_per_pkt * SAMPLES_TOTAL / SAMPLE_RATE
    gross_bps      = BITS_PER_OFDM_SYM / SAMPLES_TOTAL * SAMPLE_RATE
    print(f"""
OpenPal Modem — Phase 2 Parameters
─────────────────────────────────────────
  Sample rate    : {SAMPLE_RATE} Hz
  Passband       : {FREQ_LOW}–{FREQ_HIGH} Hz
  Carriers       : {NUM_CARRIERS}
  Spacing        : {(FREQ_HIGH-FREQ_LOW)/(NUM_CARRIERS-1):.0f} Hz
  Modulation     : {MODULATION} ({BITS_PER_SYMBOL} bits/carrier)
  Symbol duration: {SYMBOL_DURATION*1000:.0f} ms + {GUARD_DURATION*1000:.0f} ms guard
  Gross data rate: {gross_bps:.0f} bps
  Bits/OFDM sym  : {BITS_PER_OFDM_SYM}
  Packet size    : {PACKET_SIZE} bytes ({bits_per_pkt} bits)
  Symbols/packet : {syms_per_pkt}
  Time/packet    : {secs_per_pkt:.2f}s
─────────────────────────────────────────""")
