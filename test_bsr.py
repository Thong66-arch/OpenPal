"""
OpenPal Phase 4 — BSR Test
Tests the full Bad Segment Request retransmit protocol end-to-end.

Greg VK4GDW / OpenPal Project 2026
"""

import sys, logging
import numpy as np
from PIL import Image, ImageDraw

import openpal_packet as op
import openpal_modem  as modem
import openpal_bsr    as bsr

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s %(message)s",
    datefmt= "%H:%M:%S"
)

CALLSIGN = "VK4GDW"
TEST_IMG = "/tmp/openpal_test_input.jpg"


def make_test_image():
    img  = Image.new('RGB', (320, 240), (30, 60, 30))
    draw = ImageDraw.Draw(img)
    for x in range(0, 320, 40):
        draw.line([(x,0),(x,240)], fill=(60,120,60))
    for y in range(0, 240, 40):
        draw.line([(0,y),(320,y)], fill=(60,120,60))
    for i in range(8):
        draw.rectangle([i*40,80,i*40+38,160], fill=(i*30,100,200-i*20))
    draw.rectangle([20,10,300,70],   fill=(0,0,0))
    draw.text((30,15), "OpenPal v1.0 Phase 4",   fill=(0,255,0))
    draw.text((30,35), f"BSR Protocol Test",      fill=(255,255,0))
    draw.text((30,55), f"TX: {CALLSIGN}",         fill=(200,200,200))
    draw.rectangle([20,170,300,230], fill=(0,0,80))
    draw.text((30,175), "Mosquito Creek Airfield", fill=(100,200,255))
    draw.text((30,195), "In memory VK4AES SK",    fill=(255,150,50))
    img.save(TEST_IMG, 'JPEG', quality=80)


def section(title):
    print(f"\n{'='*55}\n  {title}\n{'='*55}")


def run():
    make_test_image()

    # ── Encode image ──────────────────────────────────────────────────────────
    section("Step 1: Encode image to packets")
    packets_raw = op.image_to_packets(
        TEST_IMG, CALLSIGN, image_id=0x0200, interleave=False
    )
    pkt_total = len(packets_raw)
    print(f"  {pkt_total} for transmission")

    # ── Test 1: BSR frame round-trip ──────────────────────────────────────────
    section("Test 1: BSR frame encode/decode")
    missing_test = [3, 7, 12, 25, 31, 44]
    raw_bsr = bsr.encode_bsr("VK4GDW", "VK4GDW", 0x0200,
                              missing_test, bsr_round=1)
    decoded = bsr.decode_bsr(raw_bsr)
    assert decoded['missing']   == missing_test, "Missing list mismatch"
    assert decoded['image_id']  == 0x0200,        "Image ID mismatch"
    assert decoded['bsr_round'] == 1,              "Round mismatch"
    print(f"  BSR frame: {len(raw_bsr)} bytes")
    print(f"  Missing list round-trip: PASS ✓")
    print(f"  CRC verified: PASS ✓")

    # ── Test 2: BSR audio round-trip ──────────────────────────────────────────
    section("Test 2: BSR audio encode/decode")
    audio = bsr.bsr_to_audio("VK4GDW", "VK4GDW", 0x0200,
                              missing_test, bsr_round=1)
    print(f"  BSR audio: {len(audio)/modem.SAMPLE_RATE:.1f}s")
    decoded_audio = bsr.audio_to_bsr(audio)
    assert decoded_audio is not None, "BSR not found in audio"
    assert decoded_audio['missing'] == missing_test, "Audio BSR missing list wrong"
    print(f"  BSR audio round-trip: PASS ✓")
    print(f"  Missing list: {decoded_audio['missing']}")

    # ── Test 3: No losses (BSR should not be needed) ──────────────────────────
    section("Test 3: Perfect channel — BSR not triggered")
    stats = bsr.simulate_bsr_session(
        packets_raw, 0x0200, CALLSIGN,
        "/tmp/openpal_bsr_perfect.jpg",
        initial_loss_pct = 0.0,
        retx_loss_pct    = 0.0
    )
    print(f"\n  Received  : {stats['pkt_received']}/{stats['pkt_total']}")
    print(f"  Complete  : {'YES ✓' if stats['complete'] else 'NO'}")
    print(f"  BSR rounds: {stats['bsr_rounds']}")
    if stats.get('image_saved'):
        print(f"  Image     : {stats['image_size']}")

    # ── Test 4: 20% initial loss — BSR fills the gaps ─────────────────────────
    section("Test 4: 20% loss — BSR recovery")
    stats2 = bsr.simulate_bsr_session(
        packets_raw, 0x0200, CALLSIGN,
        "/tmp/openpal_bsr_20pct.jpg",
        initial_loss_pct = 20.0,
        retx_loss_pct    = 5.0
    )
    print(f"\n  Received  : {stats2['pkt_received']}/{stats2['pkt_total']}")
    print(f"  Complete  : {'YES ✓' if stats2['complete'] else 'NO'}")
    print(f"  BSR rounds: {stats2['bsr_rounds']}")
    print(f"  Recovery  : {stats2['pct_received']:.0f}%")
    if stats2.get('image_saved'):
        print(f"  Image     : {stats2['image_size']}")

    # ── Test 5: 40% initial loss — tough conditions ───────────────────────────
    section("Test 5: 40% loss — tough conditions")
    stats3 = bsr.simulate_bsr_session(
        packets_raw, 0x0200, CALLSIGN,
        "/tmp/openpal_bsr_40pct.jpg",
        initial_loss_pct = 40.0,
        retx_loss_pct    = 10.0
    )
    print(f"\n  Received  : {stats3['pkt_received']}/{stats3['pkt_total']}")
    print(f"  Complete  : {'YES ✓' if stats3['complete'] else 'NO'}")
    print(f"  BSR rounds: {stats3['bsr_rounds']}")
    print(f"  Recovery  : {stats3['pct_received']:.0f}%")
    if stats3.get('image_saved'):
        print(f"  Image     : {stats3['image_size']}")

    # ── Summary ───────────────────────────────────────────────────────────────
    section("Phase 4 Summary")
    print(f"  BSR frame encode/decode : PASS ✓")
    print(f"  BSR audio round-trip    : PASS ✓")
    print(f"  Perfect channel         : {stats['pct_received']:.0f}% in {stats['bsr_rounds']} round(s)")
    print(f"  20% loss + BSR          : {stats2['pct_received']:.0f}% in {stats2['bsr_rounds']} round(s)")
    print(f"  40% loss + BSR          : {stats3['pct_received']:.0f}% in {stats3['bsr_rounds']} round(s)")
    print(f"\n  Output files:")
    print(f"    Perfect : /tmp/openpal_bsr_perfect.jpg")
    print(f"    20% loss: /tmp/openpal_bsr_20pct.jpg")
    print(f"    40% loss: /tmp/openpal_bsr_40pct.jpg")
    print(f"{'='*55}\n")


if __name__ == '__main__':
    run()
