# OpenPal

**Open source digital image transfer for amateur radio.**

Spiritual successor to EasyPal, originally developed by Erik Sundstrup VK4AES (SK).  
This project is dedicated to Erik's memory and his contribution to the amateur radio community.

---

## What it does

OpenPal transmits images over any narrow-band radio link (HF, VHF, UHF) using:
- **OFDM audio modulation** — 16 carriers across 300–2700 Hz, fits inside any SSB or FM audio passband
- **Reed-Solomon error correction** — recovers corrupted packets automatically
- **Packet interleaving** — survives burst fades that wipe consecutive packets
- **Automated TX/RX** — cron-driven TX, continuous RX daemon with website upload

Designed for the **VK4GDW CHMAC airfield camera project** at Mosquito Creek Airfield,  
Emerald, Central Queensland — 434.500 MHz, 70cm band, 16km path.

---

## Files

| File | Purpose |
|---|---|
| `openpal_packet.py` | Packet encoder/decoder, RS error correction, interleave |
| `openpal_modem.py` | OFDM audio modem — encode/decode packets to/from audio |
| `openpal_soundcard.py` | Sound card TX/RX, GPIO PTT control |
| `openpal_tx.py` | Field TX automation — camera grab → encode → transmit |
| `openpal_rx.py` | Home RX daemon — listen → decode → upload to website |
| `test_openpal.py` | Phase 1 packet layer tests |
| `test_modem.py` | Phase 2 end-to-end audio tests |
| `openpal-rx.service` | systemd service for RX daemon |

---

## Installation

### Both Pis
```bash
pip3 install reedsolo Pillow numpy scipy sounddevice requests --break-system-packages
sudo apt install libportaudio2
mkdir -p ~/openpal && cd ~/openpal
# copy all .py files here
```

### Field Pi (TX) — Mosquito Creek Airfield
1. Edit `openpal_tx.py` — set `CAMERA_IP`, `CAMERA_USER`, `CAMERA_PASS`
2. Wire GPIO 17 → SA828-U PTT pin (1kΩ series resistor)
3. Wire USB audio adapter output → SA828-U audio input
4. Run `python3 openpal_tx.py --list-devices` to find your audio device index
5. Set `TX_AUDIO_DEVICE` in `openpal_soundcard.py`
6. Test: `python3 openpal_tx.py --dry-run`
7. Add to crontab: `*/10 * * * * python3 /home/pi/openpal/openpal_tx.py`

### Home Pi (RX) — Retro St, Emerald
1. Edit `openpal_rx.py` — set `UPLOAD_HOST`, `UPLOAD_USER`, `UPLOAD_DEST`
2. Set up SSH key auth to the web server: `ssh-keygen && ssh-copy-id user@chmac.asn.au`
3. Wire Baofeng UV-5R → Kenwood K1 cable → USB audio adapter line in
4. Run `python3 openpal_rx.py --list-devices` to find your audio device
5. Set `RX_AUDIO_DEVICE` in `openpal_soundcard.py`
6. Test decode: `python3 openpal_rx.py --decode-wav /tmp/openpal_tx.wav`
7. Install service:
```bash
sudo cp openpal-rx.service /etc/systemd/system/
sudo systemctl enable --now openpal-rx
sudo systemctl status openpal-rx
```

---

## Testing (no radio needed)

```bash
# Phase 1: packet layer
python3 test_openpal.py

# Phase 2: full audio encode/decode loop
python3 test_modem.py

# Decode a specific WAV
python3 openpal_rx.py --decode-wav /tmp/openpal_tx.wav --no-upload

# Encode a specific image (dry run)
python3 openpal_tx.py --image myimage.jpg --dry-run
```

---

## Modem parameters

| Parameter | Value |
|---|---|
| Sample rate | 8000 Hz |
| Passband | 300–2700 Hz |
| Carriers | 16 |
| Modulation | QPSK (2 bits/carrier) |
| Symbol duration | 40 ms + 8 ms guard |
| Gross data rate | ~667 bps |
| RS ECC symbols | 32 (corrects up to 16 byte errors/packet) |
| Interleave depth | 8 packets |
| Image size | 320×240 px JPEG |
| TX time | ~2.5 minutes per image |

---

## System diagram

```
FIELD (Mosquito Creek Airfield, ~190m ASL)
┌─────────────────────────────────────────────┐
│  Reolink RLC-511W IP camera                 │
│         │ RTSP/snapshot                     │
│  Raspberry Pi 3B                            │
│    openpal_tx.py (cron, every 10 min)       │
│         │ USB audio out                     │
│  USB audio adapter                          │
│         │ audio in                          │
│  SA828-U UHF module ←── GPIO 17 (PTT)      │
│         │ RF out                            │
│  7-el Yagi @ 24.5° NNE                     │
└─────────────────────────────────────────────┘
              │ 434.500 MHz 70cm
              │ 16.18 km
              ▼
HOME (Retro St, Emerald)
┌─────────────────────────────────────────────┐
│  Homebrew 70cm antenna                      │
│         │ RF in                             │
│  Baofeng UV-5R (RX only)                   │
│         │ Kenwood K1 cable (audio out)      │
│  USB audio adapter (line in)               │
│         │                                  │
│  Raspberry Pi 4                             │
│    openpal_rx.py (systemd service)          │
│         │ SCP/SFTP                          │
│  chmac.asn.au/Images/fieldcam.jpg          │
└─────────────────────────────────────────────┘
```

---

## Licence

GNU General Public Licence v3 — same as Dream DRM, which this project builds upon.

## Contributors

- VK4GDW — Greg, project lead
- VK4AES (SK) — Erik Sundstrup, inventor of EasyPal. This project exists because of his work.

