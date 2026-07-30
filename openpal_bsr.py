"""
OpenPal - Phase 4: Bad Segment Request (BSR) Protocol
======================================================
After a primary transmission the receiving station lists missing
packet numbers in a short BSR burst. The TX retransmits only those
packets. Up to MAX_BSR_ROUNDS cycles until the image is complete.

Key design decision for retransmit:
  Retransmitted packets are sent WITHOUT interleaving and are decoded
  directly using their embedded pkt_num field. The receiver slots them
  into the correct position by packet number, not by sequence order.
  This sidesteps the interleave/deinterleave mismatch on subsets.

BSR frame:
  [BSR_SYNC 4][VER 1][CS_RX 8][CS_TX 8][IMG_ID 2][ROUND 1]
  [N_MISSING 2][MISSING_LIST N×2 bytes][CRC 4]

Greg VK4GDW / OpenPal Project 2026
In memory of Erik Sundstrup VK4AES (SK)
"""

import struct
import zlib
import time
import logging
import io
import numpy as np
from PIL import Image

import openpal_packet as op
import openpal_modem  as modem

log = logging.getLogger("openpal_bsr")

BSR_SYNC       = b'\xBB\x44\xBB\x44'
BSR_VERSION    = 1
MAX_BSR_ROUNDS = 3
BSR_WAIT_S     = 3.0
BSR_LISTEN_S   = 8.0
RETX_GAP_S     = 1.0


# ── BSR frame encode / decode ─────────────────────────────────────────────────

def encode_bsr(callsign_rx, callsign_tx, image_id, missing, bsr_round=1):
    cs_rx    = callsign_rx.upper().encode('ascii')[:8].ljust(8)
    cs_tx    = callsign_tx.upper().encode('ascii')[:8].ljust(8)
    n_miss   = len(missing)
    header   = (BSR_SYNC
                + struct.pack('B', BSR_VERSION)
                + cs_rx + cs_tx
                + struct.pack('>H', image_id & 0xFFFF)
                + struct.pack('B',  bsr_round & 0xFF)
                + struct.pack('>H', n_miss))
    miss_b   = struct.pack(f'>{n_miss}H', *missing) if missing else b''
    body     = header + miss_b
    crc      = zlib.crc32(body) & 0xFFFFFFFF
    return body + struct.pack('>I', crc)


class BSRDecodeError(Exception):
    pass


def decode_bsr(raw):
    min_len = 4+1+8+8+2+1+2+4
    if len(raw) < min_len:
        raise BSRDecodeError(f"Too short: {len(raw)}")
    idx = raw.find(BSR_SYNC)
    if idx < 0:
        raise BSRDecodeError("Sync not found")
    raw    = raw[idx:]
    off    = 4
    ver    = raw[off];                         off += 1
    cs_rx  = raw[off:off+8].decode('ascii').strip(); off += 8
    cs_tx  = raw[off:off+8].decode('ascii').strip(); off += 8
    img_id = struct.unpack('>H', raw[off:off+2])[0]; off += 2
    rnd    = raw[off];                         off += 1
    n_miss = struct.unpack('>H', raw[off:off+2])[0]; off += 2
    if len(raw) < off + n_miss*2 + 4:
        raise BSRDecodeError("Truncated")
    missing  = list(struct.unpack(f'>{n_miss}H', raw[off:off+n_miss*2]))
    off     += n_miss * 2
    crc_rx   = struct.unpack('>I', raw[off:off+4])[0]
    crc_calc = zlib.crc32(raw[:off]) & 0xFFFFFFFF
    if crc_rx != crc_calc:
        raise BSRDecodeError(f"CRC fail")
    return dict(version=ver, callsign_rx=cs_rx, callsign_tx=cs_tx,
                image_id=img_id, bsr_round=rnd,
                missing=missing, n_missing=n_miss)


# ── BSR audio wrappers ────────────────────────────────────────────────────────

def bsr_to_audio(callsign_rx, callsign_tx, image_id, missing, bsr_round=1):
    bsr_bytes = encode_bsr(callsign_rx, callsign_tx,
                           image_id, missing, bsr_round)
    pkt  = op.encode_packet(callsign_rx, image_id, 0, 1,
                             bsr_bytes[:op.PAYLOAD_SIZE],
                             flags=op.FLAG_CTRL_PKT)
    audio = modem.packets_to_audio([pkt])
    log.info(f"[BSR] Encoded: {len(missing)} missing, "
             f"{len(audio)/modem.SAMPLE_RATE:.1f}s audio")
    return audio


def audio_to_bsr(audio):
    try:
        pkts = modem.audio_to_packets(audio, expected_packets=1)
        if not pkts:
            return None
        pkt = op.decode_packet(pkts[0])
        if not (pkt['flags'] & op.FLAG_CTRL_PKT):
            return None
        b = decode_bsr(pkt['payload'])
        log.info(f"[BSR] Decoded: {b['n_missing']} missing from "
                 f"{b['callsign_rx']} round={b['bsr_round']}")
        return b
    except (op.PacketDecodeError, BSRDecodeError) as e:
        log.debug(f"[BSR] Decode failed: {e}")
        return None


# ── Packet helpers ────────────────────────────────────────────────────────────

def decode_received(raw_list):
    """
    Decode a list of raw packets (may contain None).
    Returns {pkt_num: payload} dict.
    """
    received = {}
    for raw in raw_list:
        if raw is None:
            continue
        try:
            pkt = op.decode_packet(raw)
            received[pkt['pkt_num']] = pkt['payload']
        except op.PacketDecodeError:
            pass
    return received


def reconstruct_image(received, pkt_total, output_path):
    """Rebuild image from {pkt_num: payload} dict. Returns stats dict."""
    missing = [i for i in range(pkt_total) if i not in received]
    data    = b''.join(received.get(i, b'\x00'*op.PAYLOAD_SIZE)
                       for i in range(pkt_total))
    pct     = 100 * len(received) / pkt_total
    stats   = dict(pkt_total=pkt_total,
                   pkt_received=len(received),
                   pkt_missing=len(missing),
                   pct_received=pct,
                   complete=not missing)
    try:
        img = Image.open(io.BytesIO(data))
        img.save(output_path)
        stats.update(image_saved=True, image_size=img.size)
        log.info(f"[BSR] Saved {output_path} {img.size} — {pct:.0f}%")
    except Exception as e:
        stats.update(image_saved=False, image_error=str(e))
        log.warning(f"[BSR] Image decode failed: {e}")
    return stats


# ── BSR simulation ────────────────────────────────────────────────────────────

def simulate_bsr_session(original_packets, image_id, callsign,
                         output_path,
                         initial_loss_pct=20.0,
                         retx_loss_pct=5.0):
    """
    Simulate a full BSR session without real radio.
    original_packets: list of raw packet bytes (NOT interleaved).
    """
    pkt_total = len(original_packets)
    log.info("=" * 55)
    log.info(f"BSR simulation: {pkt_total} pkts  "
             f"initial={initial_loss_pct}%  retx={retx_loss_pct}%")

    # ── Initial TX (with interleave) ──────────────────────────────────────────
    tx_il    = op.interleave_packets(list(original_packets))
    lossy    = op.simulate_channel(tx_il,
                                   loss_pct=initial_loss_pct,
                                   burst_len=4,
                                   corrupt_pct=2.0)
    # Deinterleave and decode
    rx_di    = op.deinterleave_packets(lossy)
    received = decode_received(rx_di)
    log.info(f"Initial: {len(received)}/{pkt_total} packets")

    # ── BSR rounds ────────────────────────────────────────────────────────────
    for rnd in range(1, MAX_BSR_ROUNDS + 1):
        missing = [i for i in range(pkt_total) if i not in received]
        if not missing:
            log.info(f"Complete after {rnd-1} BSR round(s)")
            break

        log.info(f"Round {rnd}: {len(missing)} missing → retransmit "
                 f"(no interleave on retx)")

        # TX sends missing packets RAW (no interleave)
        # Each packet carries its own pkt_num so RX can slot them correctly
        retx_raw   = [original_packets[i] for i in missing
                      if i < pkt_total]
        lossy_retx = op.simulate_channel(retx_raw,
                                         loss_pct=retx_loss_pct,
                                         burst_len=2,
                                         corrupt_pct=1.0)
        # Decode directly — no deinterleave needed
        new_pkts   = decode_received(lossy_retx)
        added = 0
        for num, payload in new_pkts.items():
            if num not in received:
                received[num] = payload
                added += 1
        log.info(f"  Round {rnd}: +{added} → {len(received)}/{pkt_total}")

    stats = reconstruct_image(received, pkt_total, output_path)
    stats['bsr_rounds'] = rnd
    return stats
