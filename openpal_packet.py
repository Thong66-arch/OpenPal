"""
OpenPal - Open source EasyPal successor
Phase 1: Packet layer with Reed-Solomon error correction

Packet structure (per packet):
  [SYNC 4 bytes][VERSION 1][CALLSIGN 8][IMAGE_ID 2][PKT_NUM 2][PKT_TOTAL 2]
  [FLAGS 1][PAYLOAD_LEN 2][PAYLOAD 200 bytes][RS_ECC 32 bytes][CRC 4 bytes]

Greg VK4GDW / OpenPal Project 2026
In memory of Erik Sundstrup VK4AES (SK) — inventor of EasyPal
"""

import struct
import zlib
import io
import os
import random
import time
from PIL import Image
import reedsolo

# ── Constants ─────────────────────────────────────────────────────────────────

SYNC_WORD      = b'\xAA\x55\xAA\x55'
VERSION        = 1
RS_ECC_SYMBOLS = 32          # corrects up to 16 byte errors per packet
PAYLOAD_SIZE   = 200         # image data bytes per packet
HEADER_FMT     = '>4sB8sHHHBH'
HEADER_SIZE    = struct.calcsize(HEADER_FMT)   # 22 bytes
TRAILER_SIZE   = RS_ECC_SYMBOLS + 4            # ECC + CRC32
PACKET_SIZE    = HEADER_SIZE + PAYLOAD_SIZE + TRAILER_SIZE

FLAG_LAST_PKT  = 0x01
FLAG_IMAGE_PKT = 0x02
FLAG_CTRL_PKT  = 0x04

INTERLEAVE_DEPTH = 8

_rsc = reedsolo.RSCodec(RS_ECC_SYMBOLS)


# ── Packet encode/decode ───────────────────────────────────────────────────────

def _pad_callsign(cs: str) -> bytes:
    return cs.upper().encode('ascii')[:8].ljust(8)


def encode_packet(callsign, image_id, pkt_num, pkt_total, payload, flags=FLAG_IMAGE_PKT):
    if len(payload) > PAYLOAD_SIZE:
        raise ValueError(f"Payload too large: {len(payload)}")
    padded  = payload.ljust(PAYLOAD_SIZE, b'\x00')
    header  = struct.pack(HEADER_FMT,
                          SYNC_WORD, VERSION, _pad_callsign(callsign),
                          image_id & 0xFFFF, pkt_num & 0xFFFF,
                          pkt_total & 0xFFFF, flags, len(payload))
    body    = header + padded
    encoded = bytes(_rsc.encode(body))
    ecc     = encoded[len(body):]
    crc     = zlib.crc32(body + ecc) & 0xFFFFFFFF
    return body + ecc + struct.pack('>I', crc)


class PacketDecodeError(Exception):
    pass


def decode_packet(raw: bytes) -> dict:
    if len(raw) < PACKET_SIZE:
        raise PacketDecodeError(f"Too short: {len(raw)}")
    raw     = raw[:PACKET_SIZE]
    body    = raw[:HEADER_SIZE + PAYLOAD_SIZE]
    ecc     = raw[HEADER_SIZE + PAYLOAD_SIZE:HEADER_SIZE + PAYLOAD_SIZE + RS_ECC_SYMBOLS]
    crc_rx  = struct.unpack('>I', raw[-4:])[0]
    crc_ok  = zlib.crc32(body + ecc) & 0xFFFFFFFF == crc_rx
    try:
        body   = bytes(_rsc.decode(body + ecc)[0])
        rs_ok  = True
    except reedsolo.ReedSolomonError:
        if not crc_ok:
            raise PacketDecodeError("RS failed and CRC mismatch")
        rs_ok  = False
    fields = struct.unpack_from(HEADER_FMT, body)
    sync, ver, cs, img_id, pkt_num, pkt_tot, flags, pay_len = fields
    if sync != SYNC_WORD:
        raise PacketDecodeError("Bad sync")
    offset  = HEADER_SIZE
    payload = body[offset:offset + pay_len]
    return dict(version=ver, callsign=cs.decode('ascii').strip(),
                image_id=img_id, pkt_num=pkt_num, pkt_total=pkt_tot,
                flags=flags, payload=payload,
                rs_ok=rs_ok, crc_ok=crc_ok, corrected=rs_ok and not crc_ok)


# ── Interleave (matrix transpose, proven round-trip) ──────────────────────────

def interleave_packets(packets: list, depth: int = INTERLEAVE_DEPTH) -> list:
    """
    Write original packets as rows of a matrix, transmit as columns.
    A burst fade wiping `depth` consecutive TX packets removes only
    1 byte per original packet row — RS corrects up to 16 such erasures.
    """
    if not packets:
        return packets
    pkt_len = len(packets[0])
    n       = len(packets)
    out     = []
    for gs in range(0, n, depth):
        grp = [p for p in packets[gs:gs + depth]]
        g   = len(grp)
        # pad to full depth
        while len(grp) < depth:
            grp.append(bytes(pkt_len))
        # flatten row-major then read column-major
        flat = bytearray()
        for p in grp:
            flat.extend(p)
        col_major = bytearray(g * pkt_len)
        for r in range(g):
            for c in range(pkt_len):
                col_major[c * g + r] = flat[r * pkt_len + c]
        for i in range(g):
            out.append(bytes(col_major[i * pkt_len:(i + 1) * pkt_len]))
    return out[:n]


def deinterleave_packets(packets: list, depth: int = INTERLEAVE_DEPTH) -> list:
    """
    Reverse the matrix transpose. None entries (lost packets) are
    zero-filled; the packet is still returned so RS can attempt
    correction using the ECC bytes that arrived in other packets.
    Slots that were None are flagged so the decoder can track losses.
    """
    if not packets:
        return packets
    pkt_len  = next((len(p) for p in packets if p is not None), 0)
    if not pkt_len:
        return packets
    n        = len(packets)
    none_set = set(i for i, p in enumerate(packets) if p is None)
    out      = []
    for gs in range(0, n, depth):
        grp_in = packets[gs:gs + depth]
        g      = len(grp_in)
        # Replace None with zeros
        grp = [p if p is not None else bytes(pkt_len) for p in grp_in]
        while len(grp) < depth:
            grp.append(bytes(pkt_len))
        # flatten column-major then read row-major (inverse transpose)
        flat = bytearray()
        for p in grp:
            flat.extend(p)
        row_major = bytearray(g * pkt_len)
        for r in range(g):
            for c in range(pkt_len):
                row_major[r * pkt_len + c] = flat[c * g + r]
        for i in range(g):
            slot = gs + i
            if slot < n:
                # Mark as None only if the transmitted slot itself was None
                # AND none of the other depth packets in the group arrived
                group_none = all((gs + j) in none_set for j in range(g))
                if group_none:
                    out.append(None)
                else:
                    out.append(bytes(row_major[i * pkt_len:(i + 1) * pkt_len]))
    return out[:n]


# ── Image encode / decode ──────────────────────────────────────────────────────

def image_to_packets(image_path, callsign, image_id=None,
                     max_dimension=640, jpeg_quality=75,
                     interleave=True):
    if image_id is None:
        image_id = int(time.time()) & 0xFFFF
    img = Image.open(image_path).convert('RGB')
    img.thumbnail((max_dimension, max_dimension), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=jpeg_quality, optimize=True)
    data      = buf.getvalue()
    chunks    = [data[i:i + PAYLOAD_SIZE] for i in range(0, len(data), PAYLOAD_SIZE)]
    pkt_total = len(chunks)
    packets   = []
    for i, chunk in enumerate(chunks):
        flags = FLAG_IMAGE_PKT | (FLAG_LAST_PKT if i == pkt_total - 1 else 0)
        packets.append(encode_packet(callsign, image_id, i, pkt_total, chunk, flags))
    print(f"[OpenPal] {img.size[0]}x{img.size[1]}px → {len(data)} bytes "
          f"→ {pkt_total} packets ({PACKET_SIZE} bytes each)")
    if interleave:
        packets = interleave_packets(packets)
        print(f"[OpenPal] Interleaved depth={INTERLEAVE_DEPTH}")
    return packets


def packets_to_image(packets, output_path, interleaved=True):
    if interleaved:
        packets = deinterleave_packets(packets)
    received  = {}
    pkt_total = None
    errors = corrected = missing = 0
    callsign = "UNKNOWN"
    image_id = None
    for raw in packets:
        if raw is None:
            missing += 1
            continue
        try:
            pkt = decode_packet(raw)
            received[pkt['pkt_num']] = pkt['payload']
            pkt_total = pkt['pkt_total']
            callsign  = pkt['callsign']
            image_id  = pkt['image_id']
            if pkt['corrected']:
                corrected += 1
        except PacketDecodeError:
            errors  += 1
            missing += 1
    if not received:
        print("[OpenPal] No valid packets — cannot recover image")
        return dict(callsign='UNKNOWN', image_id=None, pkt_total=0,
                    pkt_received=0, pkt_missing=len(packets),
                    pkt_errors=errors, pkt_corrected=0,
                    pct_received=0.0, complete=False,
                    image_saved=False, image_error='No valid packets')
    gaps  = sum(1 for i in range(pkt_total) if i not in received)
    data  = b''.join(received.get(i, b'\x00' * PAYLOAD_SIZE)
                     for i in range(pkt_total))
    pct   = 100 * (pkt_total - gaps) / pkt_total
    stats = dict(callsign=callsign, image_id=image_id,
                 pkt_total=pkt_total,
                 pkt_received=pkt_total - gaps,
                 pkt_missing=gaps, pkt_errors=errors,
                 pkt_corrected=corrected,
                 pct_received=pct, complete=gaps == 0)
    try:
        img = Image.open(io.BytesIO(data))
        img.save(output_path)
        stats.update(image_saved=True, image_size=img.size)
        print(f"[OpenPal] Saved {output_path} ({img.size[0]}x{img.size[1]}) "
              f"— {pct:.0f}% complete")
    except Exception as e:
        stats.update(image_saved=False, image_error=str(e))
        print(f"[OpenPal] Image decode failed: {e}")
    return stats


# ── Channel simulator ──────────────────────────────────────────────────────────

def simulate_channel(packets, loss_pct=10.0, burst_len=3, corrupt_pct=2.0):
    result = list(packets)
    n      = len(result)
    for i in range(n):
        if random.random() < loss_pct / 100:
            result[i] = None
    bs = random.randint(0, max(0, n - burst_len))
    for i in range(bs, min(bs + burst_len, n)):
        result[i] = None
    for i in range(n):
        if result[i] and random.random() < corrupt_pct / 100:
            ba = bytearray(result[i])
            ba[random.randint(0, len(ba) - 1)] ^= random.randint(1, 255)
            result[i] = bytes(ba)
    lost = sum(1 for p in result if p is None)
    print(f"[Channel] {lost}/{n} lost (burst {bs}-{bs+burst_len-1}) "
          f"corrupt={corrupt_pct}%")
    return result
