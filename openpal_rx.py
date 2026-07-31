"""
OpenPal - Phase 3c: Home RX Automation + Website Upload
========================================================
Runs on the HOME Pi (Raspberry Pi 4 at Retro St, Emerald).

Workflow (runs continuously as a service):
  1. Listen on USB audio adapter (Baofeng UV-5R → Kenwood K1 cable)
  2. Detect OpenPal preamble tone
  3. Record until end of transmission
  4. Decode audio → packets → image
  5. Upload image to chmac.asn.au/Images/fieldcam.jpg via SFTP/SCP
  6. Log result and loop

Run as a systemd service:
  sudo cp openpal-rx.service /etc/systemd/system/
  sudo systemctl enable --now openpal-rx

Greg VK4GDW / OpenPal Project 2026
In memory of Erik Sundstrup VK4AES (SK)
"""

import os
import sys
import time
import logging
import argparse
import subprocess
import shutil
from pathlib import Path
from datetime import datetime

import numpy as np

import openpal_packet    as op
import openpal_modem     as modem
import openpal_soundcard as sc

# ── Configuration ─────────────────────────────────────────────────────────────

CALLSIGN        = "VK4GDW"

# Website upload — SCP/SFTP to chmac.asn.au
# Requires SSH key auth set up: ssh-keygen, then ssh-copy-id to server
UPLOAD_ENABLED  = True
UPLOAD_HOST     = "chmac.asn.au"
UPLOAD_USER     = "your_username"          # SSH username for the web server
UPLOAD_KEY      = "/home/pi/.ssh/id_rsa"  # SSH private key path
UPLOAD_DEST     = "/var/www/html/Images/fieldcam.jpg"   # remote path

# Alternatively, use FTP:
FTP_ENABLED     = False
FTP_HOST        = "chmac.asn.au"
FTP_USER        = "your_ftp_user"
FTP_PASS        = "your_ftp_pass"          # consider an env variable instead
FTP_DEST_PATH   = "/Images/fieldcam.jpg"

# Local storage — keep last N received images
IMAGE_STORE     = Path("/home/pi/openpal/received")
IMAGE_STORE.mkdir(parents=True, exist_ok=True)
KEEP_IMAGES     = 48    # keep last 48 images (8 hours at 10-min intervals)

# Audio
RX_AUDIO_DEVICE = None  # None = system default
RX_TIMEOUT_S    =300
WAV_ARCHIVE     = Path("/home/pi/openpal/wav_archive")
WAV_ARCHIVE.mkdir(parents=True, exist_ok=True)
KEEP_WAVS       = 10    # keep last 10 received WAV files for debugging

# Logging
LOG_DIR         = Path("/home/pi/openpal/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s [RX] %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
    handlers= [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "rx.log"),
    ]
)
log = logging.getLogger("openpal_rx")


# ── Upload functions ──────────────────────────────────────────────────────────

def upload_scp(local_path: str) -> bool:
    """Upload image to web server via SCP."""
    if not UPLOAD_ENABLED:
        return True
    dest = f"{UPLOAD_USER}@{UPLOAD_HOST}:{UPLOAD_DEST}"
    cmd  = ["scp", "-i", UPLOAD_KEY, "-o", "StrictHostKeyChecking=no",
            local_path, dest]
    log.info(f"Uploading via SCP to {dest}...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            log.info("Upload OK")
            return True
        else:
            log.warning(f"SCP failed: {result.stderr.strip()}")
            return False
    except subprocess.TimeoutExpired:
        log.warning("SCP timeout")
        return False
    except Exception as e:
        log.warning(f"SCP error: {e}")
        return False


def upload_ftp(local_path: str) -> bool:
    """Upload image to web server via FTP (fallback)."""
    if not FTP_ENABLED:
        return True
    try:
        import ftplib
        log.info(f"Uploading via FTP to {FTP_HOST}{FTP_DEST_PATH}...")
        with ftplib.FTP(FTP_HOST, FTP_USER, FTP_PASS, timeout=30) as ftp:
            with open(local_path, 'rb') as f:
                ftp.storbinary(f"STOR {FTP_DEST_PATH}", f)
        log.info("FTP upload OK")
        return True
    except Exception as e:
        log.warning(f"FTP error: {e}")
        return False


def upload_image(local_path: str) -> bool:
    """Try SCP first, fall back to FTP."""
    if upload_scp(local_path):
        return True
    return upload_ftp(local_path)


# ── Image storage management ──────────────────────────────────────────────────

def save_received_image(image_data: bytes,
                        callsign: str,
                        image_id: int) -> str:
    """Save decoded image with timestamp, prune old files."""
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{ts}_{callsign}_id{image_id:04x}.jpg"
    path = IMAGE_STORE / name
    with open(path, 'wb') as f:
        f.write(image_data)
    log.info(f"Saved: {path}")

    # Also write as fieldcam.jpg (latest)
    latest = IMAGE_STORE / "fieldcam_latest.jpg"
    shutil.copy(path, latest)

    # Prune old images
    images = sorted(IMAGE_STORE.glob("20*.jpg"))
    for old in images[:-KEEP_IMAGES]:
        old.unlink()

    return str(path)


def archive_wav(wav_data: np.ndarray) -> str:
    """Archive received WAV for debugging."""
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = WAV_ARCHIVE / f"{ts}_rx.wav"
    modem._save_wav(wav_data, str(path))
    # Prune old WAVs
    wavs = sorted(WAV_ARCHIVE.glob("*.wav"))
    for old in wavs[:-KEEP_WAVS]:
        old.unlink()
    return str(path)


# ── Decode pipeline ───────────────────────────────────────────────────────────

def process_received_audio(audio: np.ndarray) -> bool:
    """
    Full decode pipeline: audio → packets → image → upload.
    Returns True if a usable image was recovered.
    """
    log.info(f"Processing {len(audio)/modem.SAMPLE_RATE:.1f}s of audio...")

    # Archive the raw WAV first (useful for debugging marginal signals)
    wav_path = archive_wav(audio)
    log.info(f"WAV archived: {wav_path}")

    # Decode audio to packets
    try:
        packets = modem.audio_to_packets(audio)
    except Exception as e:
        log.error(f"Audio decode failed: {e}")
        return False

    if not packets:
        log.warning("No packets decoded")
        return False

    # Decode packets to image
    import tempfile
    with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
        tmp_path = tmp.name

    try:
        stats = op.packets_to_image(packets, tmp_path, interleaved=True)
    except Exception as e:
        log.error(f"Packet decode failed: {e}")
        return False

    log.info(f"Packets: {stats['pkt_received']}/{stats['pkt_total']} "
             f"({stats['pct_received']:.0f}%) "
             f"RS corrections: {stats['pkt_corrected']} "
             f"from {stats['callsign']}")

    if not stats.get('image_saved'):
        log.warning(f"Image not recoverable: {stats.get('image_error')}")
        # Even a partial receive is worth logging
        return False

    # Save locally
    with open(tmp_path, 'rb') as f:
        image_bytes = f.read()
    os.unlink(tmp_path)

    local_path = save_received_image(
        image_bytes,
        stats['callsign'],
        stats.get('image_id', 0) or 0
    )

    # Upload to website
    latest = str(IMAGE_STORE / "fieldcam_latest.jpg")
    uploaded = upload_image(latest)
    if not uploaded:
        log.warning("Upload failed — image saved locally only")

    log.info(f"Cycle complete — {stats['pct_received']:.0f}% image quality, "
             f"upload={'OK' if uploaded else 'FAILED'}")
    return True


# ── Main RX loop ──────────────────────────────────────────────────────────────

def run_rx_loop():
    """
    Continuous receive loop — runs forever as a service.
    """
    log.info("=" * 50)
    log.info(f"OpenPal RX starting — {CALLSIGN}")
    log.info(f"Upload: {'SCP' if UPLOAD_ENABLED else 'FTP' if FTP_ENABLED else 'DISABLED'}")
    log.info("=" * 50)

    rx = sc.AudioReceiver(device=RX_AUDIO_DEVICE)
    rx.start()

    consecutive_errors = 0
    try:
        while True:
            try:
                audio = rx.wait_for_transmission(timeout=RX_TIMEOUT_S)
                if audio is not None and len(audio) > modem.SAMPLE_RATE:
                    ok = process_received_audio(audio)
                    consecutive_errors = 0 if ok else consecutive_errors + 1
                else:
                    log.info("No transmission — continuing to listen")

                if consecutive_errors > 5:
                    log.warning(f"{consecutive_errors} consecutive failures — "
                                f"check antenna and audio levels")

            except KeyboardInterrupt:
                raise
            except Exception as e:
                log.error(f"Unexpected error: {e}", exc_info=True)
                consecutive_errors += 1
                time.sleep(5)
    finally:
        rx.stop()
        log.info("OpenPal RX stopped")


def decode_wav_file(wav_path: str, output_path: str = None):
    """
    Decode a pre-recorded WAV file (useful for testing / replaying).
    """
    log.info(f"Decoding WAV file: {wav_path}")
    audio = modem.load_wav(wav_path)
    if output_path is None:
        output_path = wav_path.replace('.wav', '_decoded.jpg')
    packets = modem.audio_to_packets(audio)
    stats   = op.packets_to_image(packets, output_path, interleaved=True)
    log.info(f"Result: {stats['pkt_received']}/{stats['pkt_total']} packets, "
             f"{stats['pct_received']:.0f}%")
    if stats.get('image_saved'):
        log.info(f"Image saved: {output_path} {stats.get('image_size')}")
    return stats


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="OpenPal RX — home receiver")
    parser.add_argument('--decode-wav', metavar='FILE',
                        help="Decode a WAV file instead of listening live")
    parser.add_argument('--output',     metavar='FILE',
                        help="Output image path (with --decode-wav)")
    parser.add_argument('--list-devices', action='store_true',
                        help="List audio devices and exit")
    parser.add_argument('--no-upload',  action='store_true',
                        help="Disable website upload for this run")
    args = parser.parse_args()

    if args.list_devices:
        sc.list_audio_devices()
        sys.exit(0)

    if args.no_upload:
        UPLOAD_ENABLED = False
        FTP_ENABLED    = False

    if args.decode_wav:
        decode_wav_file(args.decode_wav, args.output)
    else:
        run_rx_loop()
