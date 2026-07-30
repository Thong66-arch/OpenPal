"""
OpenPal Phase 2 — Modem Test
Tests the full pipeline: image → packets → audio → decode → image

Greg VK4GDW / OpenPal Project 2026
"""

import sys, os, time, random
import numpy as np
from PIL import Image, ImageDraw
import openpal_packet as op
import openpal_modem  as modem

CALLSIGN  = "VK4GDW"
TEST_IMG  = "/tmp/openpal_test_input.jpg"


def make_test_image():
    img  = Image.new('RGB', (320, 240), (30, 60, 30))
    draw = ImageDraw.Draw(img)
    for x in range(0, 320, 40):
        draw.line([(x,0),(x,240)], fill=(60,120,60))
    for y in range(0, 240, 40):
        draw.line([(0,y),(320,y)], fill=(60,120,60))
    for i in range(8):
        draw.rectangle([i*40, 80, i*40+38, 160], fill=(i*30, 100, 200-i*20))
    draw.rectangle([20,10,300,70],   fill=(0,0,0))
    draw.text((30,15), "OpenPal v2.0",           fill=(0,255,0))
    draw.text((30,35), f"TX: {CALLSIGN}",        fill=(255,255,0))
    draw.text((30,55), "Phase 2 — Audio Modem",  fill=(200,200,200))
    draw.rectangle([20,170,300,230], fill=(0,0,80))
    draw.text((30,175), "Mosquito Creek Airfield",fill=(100,200,255))
    draw.text((30,195), "VK4GDW  434.500 MHz",   fill=(100,200,255))
    draw.text((30,215), "In memory VK4AES SK",   fill=(255,150,50))
    img.save(TEST_IMG, 'JPEG', quality=80)
    print(f"[Test] Test image: {img.size[0]}x{img.size[1]}px")


def section(title):
    print(f"\n{'='*55}\n  {title}\n{'='*55}")


def run():
    make_test_image()
    modem.print_modem_info()

    # ── Encode image → packets ────────────────────────────────────────────────
    section("Step 1: Image → Packets")
    packets = op.image_to_packets(
        TEST_IMG, CALLSIGN, image_id=0x0100, interleave=True
    )
    print(f"  {len(packets)} packets × {len(packets[0])} bytes "
          f"= {len(packets)*len(packets[0])} bytes total")

    # ── Encode packets → audio ────────────────────────────────────────────────
    section("Step 2: Packets → Audio")
    t0    = time.time()
    audio = modem.packets_to_audio(packets, output_wav="/tmp/openpal_tx.wav")
    enc_t = time.time() - t0
    dur   = len(audio) / modem.SAMPLE_RATE
    print(f"  Encoded in {enc_t:.2f}s (audio is {dur:.1f}s long)")
    print(f"  WAV saved: /tmp/openpal_tx.wav")

    # ── Perfect channel decode ────────────────────────────────────────────────
    section("Step 3: Perfect Channel Decode")
    rx_audio = modem.load_wav("/tmp/openpal_tx.wav")
    rx_pkts  = modem.audio_to_packets(rx_audio, expected_packets=len(packets))
    stats    = op.packets_to_image(
        rx_pkts, "/tmp/openpal_rx_modem_perfect.jpg", interleaved=True
    )
    print(f"  Received: {stats['pkt_received']}/{stats['pkt_total']} packets")
    print(f"  Complete: {'YES ✓' if stats['complete'] else 'NO'}")
    print(f"  RS corrections: {stats['pkt_corrected']}")
    if stats.get('image_saved'):
        print(f"  Image: {stats['image_size']} → /tmp/openpal_rx_modem_perfect.jpg")
    result1 = stats['pct_received']

    # ── Noisy channel decode (SNR 20 dB) ─────────────────────────────────────
    section("Step 4: Noisy Channel (SNR=20dB)")
    noisy = modem.add_channel_noise(audio, snr_db=20.0)
    modem._save_wav(noisy, "/tmp/openpal_tx_noisy.wav")
    rx_pkts2 = modem.audio_to_packets(noisy, expected_packets=len(packets))
    stats2   = op.packets_to_image(
        rx_pkts2, "/tmp/openpal_rx_modem_noisy.jpg", interleaved=True
    )
    print(f"  Received: {stats2['pkt_received']}/{stats2['pkt_total']} packets")
    print(f"  Complete: {'YES ✓' if stats2['complete'] else 'NO'}")
    print(f"  RS corrections: {stats2['pkt_corrected']}")
    if stats2.get('image_saved'):
        print(f"  Image: {stats2['image_size']} → /tmp/openpal_rx_modem_noisy.jpg")
    result2 = stats2['pct_received']

    # ── Very noisy channel (SNR 10 dB) ───────────────────────────────────────
    section("Step 5: Very Noisy Channel (SNR=10dB)")
    very_noisy = modem.add_channel_noise(audio, snr_db=10.0)
    modem._save_wav(very_noisy, "/tmp/openpal_tx_vnoisy.wav")
    rx_pkts3  = modem.audio_to_packets(very_noisy, expected_packets=len(packets))
    stats3    = op.packets_to_image(
        rx_pkts3, "/tmp/openpal_rx_modem_vnoisy.jpg", interleaved=True
    )
    print(f"  Received: {stats3['pkt_received']}/{stats3['pkt_total']} packets")
    print(f"  Complete: {'YES ✓' if stats3['complete'] else 'NO'}")
    print(f"  RS corrections: {stats3['pkt_corrected']}")
    if stats3.get('image_saved'):
        print(f"  Image: {stats3['image_size']} → /tmp/openpal_rx_modem_vnoisy.jpg")
    result3 = stats3['pct_received']

    # ── Summary ───────────────────────────────────────────────────────────────
    section("Phase 2 Summary")
    print(f"  Perfect channel  : {result1:.0f}% recovered")
    print(f"  SNR 20 dB        : {result2:.0f}% recovered")
    print(f"  SNR 10 dB        : {result3:.0f}% recovered")
    print(f"\n  TX audio duration: {dur:.1f}s for {len(packets)} packets")
    print(f"  ({dur/60:.1f} minutes per image)")
    print(f"\n  Output files:")
    print(f"    TX audio (clean) : /tmp/openpal_tx.wav")
    print(f"    TX audio (noisy) : /tmp/openpal_tx_noisy.wav")
    print(f"    RX perfect       : /tmp/openpal_rx_modem_perfect.jpg")
    print(f"    RX 20dB noisy    : /tmp/openpal_rx_modem_noisy.jpg")
    print(f"    RX 10dB noisy    : /tmp/openpal_rx_modem_vnoisy.jpg")
    print(f"{'='*55}\n")


if __name__ == '__main__':
    run()
