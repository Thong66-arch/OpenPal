"""
OpenPal - Phase 3b: Field TX Automation Script
===============================================
Runs on the FIELD Pi (Raspberry Pi 3B at Mosquito Creek Airfield).

Workflow (every 10 minutes via cron):
  1. Grab latest snapshot from Reolink RLC-511W camera
  2. Encode image → OpenPal packet stream
  3. Key PTT on SA828-U via GPIO
  4. Transmit audio through USB sound card → SA828-U → 70cm
  5. Release PTT
  6. Log result

Crontab entry (run as pi user):
  */10 * * * * /usr/bin/python3 /home/pi/openpal/openpal_tx.py >> /home/pi/openpal/tx.log 2>&1

Greg VK4GDW / OpenPal Project 2026
In memory of Erik Sundstrup VK4AES (SK)
"""

import os
import sys
import time
import logging
import argparse
import requests
import tempfile
from pathlib import Path
from datetime import datetime

import openpal_packet  as op
import openpal_modem   as modem
import openpal_soundcard as sc

# ── Configuration ─────────────────────────────────────────────────────────────

CALLSIGN        = "VK4GDW"
IMAGE_ID_FILE   = "/tmp/openpal_image_id.txt"   # persists between runs

# Reolink camera — adjust IP to your camera's address on local network
# Field network: camera and Pi share the same 12V-powered router/switch,
# or Pi connects directly to camera via ethernet with static IPs.
CAMERA_IP       = "192.168.1.100"    # Reolink RLC-511W field IP
CAMERA_USER     = "admin"
CAMERA_PASS     = "your_password"    # change this
CAMERA_SNAP_URL = f"http://{CAMERA_IP}/cgi-bin/api.cgi?cmd=Snap&channel=0&user={CAMERA_USER}&password={CAMERA_PASS}"

# Fallback: use a local image file if camera unreachable
FALLBACK_IMAGE  = "/home/pi/openpal/fallback.jpg"

# OpenPal encoding
MAX_DIMENSION   = 320   # px — keeps transmission under 3 minutes
JPEG_QUALITY    = 70    # lower = smaller = faster TX

# Sound card
TX_AUDIO_DEVICE = None  # None = system default; set to USB device index if needed

# Logging
LOG_DIR         = Path("/home/pi/openpal/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s [TX] %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
    handlers= [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "tx.log"),
    ]
)
log = logging.getLogger("openpal_tx")


# ── Image acquisition ─────────────────────────────────────────────────────────

def grab_camera_snapshot(output_path: str, timeout: int = 10) -> bool:
    """
    Fetch a snapshot from the Reolink camera.
    Returns True on success, False on failure.
    """
    try:
        log.info(f"Fetching snapshot from {CAMERA_IP}...")
        resp = requests.get(CAMERA_SNAP_URL, timeout=timeout, stream=True)
        if resp.status_code == 200 and resp.headers.get('content-type','').startswith('image'):
            with open(output_path, 'wb') as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            size = os.path.getsize(output_path)
            log.info(f"Snapshot saved: {output_path} ({size} bytes)")
            return True
        else:
            log.warning(f"Camera returned {resp.status_code}: {resp.text[:100]}")
            return False
    except requests.exceptions.ConnectTimeout:
        log.warning(f"Camera timeout (is {CAMERA_IP} reachable?)")
        return False
    except Exception as e:
        log.warning(f"Camera error: {e}")
        return False


def get_image(temp_dir: str) -> str | None:
    """
    Try camera first, fall back to local file.
    Returns path to image file, or None if nothing available.
    """
    snap_path = os.path.join(temp_dir, "snapshot.jpg")
    if grab_camera_snapshot(snap_path):
        return snap_path
    if os.path.exists(FALLBACK_IMAGE):
        log.warning(f"Using fallback image: {FALLBACK_IMAGE}")
        return FALLBACK_IMAGE
    log.error("No image available — skipping this cycle")
    return None


# ── Image ID persistence ──────────────────────────────────────────────────────

def get_next_image_id() -> int:
    """Increment and return a persistent image ID (0–65535 wrapping)."""
    try:
        with open(IMAGE_ID_FILE) as f:
            current = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        current = 0
    next_id = (current + 1) & 0xFFFF
    with open(IMAGE_ID_FILE, 'w') as f:
        f.write(str(next_id))
    return next_id


# ── Main TX cycle ─────────────────────────────────────────────────────────────

def run_tx_cycle(image_path: str = None, dry_run: bool = False):
    """
    Full transmit cycle: image → packets → audio → TX.
    If image_path given, use that; otherwise fetch from camera.
    dry_run: encode and measure but don't actually transmit.
    """
    cycle_start = time.time()
    log.info("=" * 50)
    log.info(f"OpenPal TX cycle — {CALLSIGN}")
    log.info("=" * 50)

    with tempfile.TemporaryDirectory() as tmp:

        # 1. Get image
        if image_path:
            img = image_path
        else:
            img = get_image(tmp)
        if not img:
            return False

        # 2. Encode to packets
        image_id = get_next_image_id()
        log.info(f"Image ID: {image_id:#06x}")
        try:
            packets = op.image_to_packets(
                img,
                callsign     = CALLSIGN,
                image_id     = image_id,
                max_dimension= MAX_DIMENSION,
                jpeg_quality = JPEG_QUALITY,
                interleave   = True
            )
        except Exception as e:
            log.error(f"Encode failed: {e}")
            return False

        # 3. Convert to audio
        audio_path = os.path.join(tmp, "tx.wav")
        try:
            audio = modem.packets_to_audio(packets, output_wav=audio_path)
        except Exception as e:
            log.error(f"Audio encode failed: {e}")
            return False

        duration = len(audio) / modem.SAMPLE_RATE
        log.info(f"TX audio: {duration:.1f}s ({len(packets)} packets)")

        if dry_run:
            log.info("DRY RUN — not transmitting")
            log.info(f"Audio saved to: {audio_path}")
            return True

        # 4. Transmit
        with sc.PTTController() as ptt:
            try:
                sc.transmit_audio(audio, device=TX_AUDIO_DEVICE, ptt=ptt)
                log.info(f"TX complete in {time.time()-cycle_start:.1f}s")
                return True
            except Exception as e:
                log.error(f"TX failed: {e}")
                return False


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="OpenPal TX — field transmitter")
    parser.add_argument('--image',   help="Transmit a specific image file")
    parser.add_argument('--dry-run', action='store_true',
                        help="Encode but don't transmit")
    parser.add_argument('--list-devices', action='store_true',
                        help="List audio devices and exit")
    args = parser.parse_args()

    if args.list_devices:
        sc.list_audio_devices()
        sys.exit(0)

    success = run_tx_cycle(
        image_path = args.image,
        dry_run    = args.dry_run
    )
    sys.exit(0 if success else 1)
