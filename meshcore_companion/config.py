"""Settings of the service, and the Cap LoRa-1262 wiring."""

import os
from pathlib import Path
from typing import Any, Dict

from . import statefile

STATE_DIR = Path(os.environ.get("MESHCORE_COMPANION_STATE", "/var/lib/meshcore-companion"))
RUNTIME_DIR = Path(os.environ.get("MESHCORE_COMPANION_RUNTIME", "/run/meshcore-companion"))
CONTROL_SOCKET = RUNTIME_DIR / "control.sock"
TCP_PORT = 5000  # what MeshCore Wi-Fi companions use; the app's default

DEFAULTS: Dict[str, Any] = {
    "background": True,  # run the node while the tile is closed, and at boot
    "bluetooth": True,
    "wifi": False,  # TCP on the network as well as loopback; no password, so off by default
    "pairing": "code",  # "code" (passkey on the screen) or "button" (Just Works)
}

#: The pins and switches of the Cap LoRa-1262 on the CardputerZero (as MeshTerm 0.13.0
#: drives it): SPI0 CS1, reset/busy/IRQ on gpiochip0, the EXT 5V rail and pin routing as
#: LED-class switches, the RF switch on the PI4IOE5V6408 at 0x43 on I2C-1, DIO2 as RF
#: switch, DIO3 powering the TCXO, and the GPS on the serial port.
CAP_LORA_1262 = {
    "bus_id": 0,
    "cs_id": 1,
    "cs_pin": -1,
    "gpio_chip": 0,
    "use_gpiod_backend": False,
    "reset_pin": 26,
    "busy_pin": 22,
    "irq_pin": 23,
    "txen_pin": -1,
    "rxen_pin": -1,
    "en_pins": None,
    "leds": ["ext_5v_out=1", "ext_usb_gpio_fun=0"],
    "pi4io_bus": 1,
    "pi4io_address": 0x43,
    "pi4io_high": [0],
    "use_dio2_rf": True,
    "use_dio3_tcxo": True,
    "is_waveshare": False,
    "gps_port": "/dev/serial0",
    "gps_baud": 115200,
}

#: Values a brand-new node starts with (US/Canada preset); the MeshCore app changes them.
SEED = {
    "node_name": "CardputerZero",
    "frequency_hz": 910_525_000,
    "bandwidth_hz": 62_500,
    "spreading_factor": 7,
    "coding_rate": 5,
    "tx_power_dbm": 22,
}


def load(path: Path = None) -> Dict[str, Any]:
    """The saved settings over the defaults; unknown keys and bad types are ignored.

    A file that does not parse is moved to ``config.json.bad`` (one warning) and the
    defaults apply.
    """
    path = path or STATE_DIR / "config.json"
    out = dict(DEFAULTS)
    saved = statefile.read_json(path, dict)
    if isinstance(saved, dict):
        for key, default in DEFAULTS.items():
            if key in saved and isinstance(saved[key], type(default)):
                out[key] = saved[key]
    if out["pairing"] not in ("code", "button"):
        out["pairing"] = DEFAULTS["pairing"]
    return out


def save(settings: Dict[str, Any], path: Path = None) -> None:
    """Write the settings in one rename and fsync them: a switch the user set must survive
    a power loss (the file changes only when the user changes a setting)."""
    path = path or STATE_DIR / "config.json"
    statefile.write_atomic(path, statefile.dumps({k: settings[k] for k in DEFAULTS}), fsync=True)
