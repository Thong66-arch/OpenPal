"""
OpenPal Phase 1 Test Harness
Tests the packet layer end-to-end with simulated channel impairments.

Usage:
    python3 test_openpal.py [image_path]

If no image is supplied a synthetic test image is generated.
Greg VK4GDW / OpenPal Project 2026
"""

import sys
import os
import random
from PIL import Image, ImageDraw, ImageFont
import openpal_packet as op

CALLSIGN   = "VK4GDW"
TEST_IMAGE = "/tmp/openpal_test_input.jpg"
OUT_CLEAN  = "/tmp/openpal_rx_clean.jpg"
OUT_LOSSY  = "/tmp/openpal_rx_lossy.jpg"


def make_test_image(path: str):
    """Generate a synthetic test image with callsign and grid."""
    img  = Image.new('RGB', (320, 240), color=(30, 60, 30))
    draw = ImageDraw.Draw(img)

    # Grid lines
    for x in range(0, 320, 40):
        draw.line([(x, 0), (x, 240)], fill=(60, 120, 60), width=1)
    for y in range(0, 240, 40):
        draw.line([(0, y), (320, y)], fill=(60, 120, 60), width=1)

    # Gradient blocks for compression testing
    for i in range(8):
        colour = (i * 30, 100, 200 - i * 20)
        draw.rectangle([i*40, 80, i*40+38, 160], fill=colour)

    # Text
    draw.rectangle([20, 10, 300, 70], fill=(0, 0, 0))
    draw.text((30, 15), "OpenPal v1.0",       fill=(0, 255, 0))
    draw.text((30, 35), f"TX: {CALLSIGN}",    fill=(255, 255, 0))
    draw.text((30, 55), "Digital Image TX",   fill=(200, 200, 200))

    draw.rectangle([20, 170, 300, 230], fill=(0, 0, 80))
    draw.text((30, 175), "Mosquito Creek Airfield", fill=(100, 200, 255))
    draw.text((30, 195), "Central Highlands MAC",   fill=(100, 200, 255))
    draw.text((30, 215), "434.500 MHz 70cm",        fill=(255, 150, 50))

    img.save(path, 'JPEG', quality=80)
    print(f"[Test] Generated test image: {path} ({img.size[0]}x{img.size[1]})")


def print_stats(label: str, stats: dict):
    print(f"\n{'='*50}")
    print(f"  {label}")
    print(f"{'='*50}")
    print(f"  Callsign   : {stats['callsign']}")
    print(f"  Image ID   : {stats['image_id']}")
    print(f"  Packets    : {stats['pkt_received']}/{stats['pkt_total']} received")
    print(f"  Missing    : {stats['pkt_missing']}")
    print(f"  RS corrected: {stats['pkt_corrected']}")
    print(f"  Complete   : {'YES' if stats['complete'] else 'NO'}")
    print(f"  Received   : {stats['pct_received']:.1f}%")
    if stats.get('image_saved'):
        print(f"  Image size : {stats['image_size']}")
        print(f"  Saved to   : ", end="")
    print(f"{'='*50}\n")


def run_tests(image_path: str):

    print("\n" + "="*60)
    print("  OpenPal Phase 1 — Packet Layer Test")
    print("  VK4GDW / OpenPal Project 2026")
    print("="*60 + "\n")

    # ── Encode ───────────────────────────────────────────────────────────────
    print("[Test 1] Encoding image to packets...")
    packets = op.image_to_packets(
        image_path,
        callsign   = CALLSIGN,
        image_id   = 0x0042,
        interleave = True
    )
    print(f"[Test 1] Total packets produced: {len(packets)}")
    print(f"[Test 1] Bytes per packet: {len(packets[0])}")
    print(f"[Test 1] Total bytes on air: {len(packets) * len(packets[0])}")

    # ── Test 1: Perfect channel ───────────────────────────────────────────────
    print("\n[Test 1] Perfect channel (no loss)...")
    stats = op.packets_to_image(
        list(packets), OUT_CLEAN, interleaved=True
    )
    print_stats("PERFECT CHANNEL", stats)
    assert stats['complete'], "Perfect channel should deliver complete image"
    assert stats['image_saved'], "Image should decode successfully"
    print("[Test 1] PASS ✓")

    # ── Test 2: 10% random loss + burst ──────────────────────────────────────
    print("\n[Test 2] 10% random loss + burst of 3...")
    lossy = op.simulate_channel(
        list(packets),
        loss_pct   = 10.0,
        burst_len  = 3,
        corrupt_pct= 2.0
    )
    stats = op.packets_to_image(lossy, OUT_LOSSY, interleaved=True)
    print_stats("10% LOSS + BURST", stats)
    if stats['image_saved']:
        print("[Test 2] PASS ✓ — image recovered despite losses")
    else:
        print("[Test 2] PARTIAL — image data recovered but JPEG incomplete")

    # ── Test 3: Single packet RS correction ──────────────────────────────────
    print("\n[Test 3] Single packet RS error correction...")
    # Use a non-interleaved packet for this test (cleaner to reason about)
    test_pkts_raw = op.image_to_packets(
        image_path, callsign=CALLSIGN, image_id=0x0099, interleave=False
    )
    test_pkt  = test_pkts_raw[5]   # pick a middle packet
    body_len  = op.HEADER_SIZE + op.PAYLOAD_SIZE

    # Corrupt exactly 10 bytes in the body — RS(32) corrects up to 16
    rng       = random.Random(12345)   # fixed seed for reproducibility
    corrupted = bytearray(test_pkt)
    positions = rng.sample(range(body_len), 10)
    for pos in positions:
        corrupted[pos] ^= 0x55
    corrupted = bytes(corrupted)

    try:
        decoded  = op.decode_packet(corrupted)
        original = op.decode_packet(test_pkt)
        if decoded['payload'] == original['payload']:
            print(f"[Test 3] RS corrected {len(positions)} byte errors — PASS ✓")
        else:
            print("[Test 3] FAIL — payload mismatch after RS correction")
    except op.PacketDecodeError as e:
        print(f"[Test 3] FAIL — {e}")

    # ── Test 4: Exceed RS capacity ────────────────────────────────────────────
    print("\n[Test 4] Exceed RS capacity (20 errors, expect failure)...")
    corrupted = bytearray(test_pkt)
    body_len  = op.HEADER_SIZE + op.PAYLOAD_SIZE
    positions = random.sample(range(body_len), 20)
    for pos in positions:
        corrupted[pos] ^= 0xFF
    corrupted = bytes(corrupted)

    try:
        decoded = op.decode_packet(corrupted)
        print("[Test 4] RS recovered despite 20 errors (lucky alignment)")
    except op.PacketDecodeError as e:
        print(f"[Test 4] PASS ✓ — correctly rejected unrecoverable packet: {e}")

    # ── Test 5: Heavy loss stress test ───────────────────────────────────────
    print("\n[Test 5] Heavy loss stress test (25% loss, burst=6)...")
    lossy = op.simulate_channel(
        list(packets),
        loss_pct   = 25.0,
        burst_len  = 6,
        corrupt_pct= 5.0
    )
    stats = op.packets_to_image(lossy, "/tmp/openpal_rx_heavy.jpg",
                                interleaved=True)
    print_stats("25% LOSS + BURST 6", stats)
    print(f"[Test 5] {stats['pct_received']:.0f}% of image data recovered")
    if stats['pct_received'] > 70:
        print("[Test 5] PASS ✓ — usable image despite heavy loss")
    else:
        print("[Test 5] INFO — significant loss, increase RS depth for this channel")

    print("\n" + "="*60)
    print("  Phase 1 Tests Complete")
    print(f"  Output files:")
    print(f"    Perfect channel : {OUT_CLEAN}")
    print(f"    10% loss        : {OUT_LOSSY}")
    print(f"    25% loss        : /tmp/openpal_rx_heavy.jpg")
    print("="*60 + "\n")


if __name__ == '__main__':
    if len(sys.argv) > 1:
        img = sys.argv[1]
        if not os.path.exists(img):
            print(f"Image not found: {img}")
            sys.exit(1)
    else:
        make_test_image(TEST_IMAGE)
        img = TEST_IMAGE

    run_tests(img)
