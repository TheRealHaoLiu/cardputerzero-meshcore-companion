# MeshCore Companion for the CardputerZero

Turns an M5Stack CardputerZero with the Cap LoRa-1262 into a MeshCore companion radio
that runs in the background. Connect to it from the official MeshCore apps over
Bluetooth (phone on the go), Wi-Fi, or from MeshTerm on the device itself.

Status: under construction.

- A background service runs the MeshCore node (openHop core) on the Cap and serves the
  companion protocol over Bluetooth LE (Nordic UART, like MeshCore firmware), TCP on
  `127.0.0.1:5000`, and optionally Wi-Fi on port 5000.
- A small launcher tile switches the service, Bluetooth and Wi-Fi on and off and pairs
  new phones (the pairing code shows on the CardputerZero's screen).
- Everything about the node itself (name, radio preset, channels, contacts) is set from
  the MeshCore app, as with a firmware companion.

Only one app can drive the LoRa radio at a time: turn "Run in background" off to free the
Cap for other radio apps. With it on, a start that fails (the radio is held by another
app, or Bluetooth is not up yet at boot) is retried after 30 seconds, then less often, up
to every 10 minutes.

The node runs only while the Cap LoRa-1262 is attached, and the service keeps away from
any other Cap (they use the same pins for other things):

- When the service starts (at boot, or when the tile is opened) it looks for the Cap
  once: it switches the 5 V output of the header (`ext_5v_out`) on for a tenth of a
  second and makes one I2C read at the address of the Cap's expander (0x43). Nothing
  else on the header is touched until the Cap LoRa-1262 has answered.
- Without it nothing starts and nothing is retried: the radio library is not loaded,
  the tile shows "No Cap", and the service exits after 20 seconds without a tile. It
  never looks on a timer, because a look powers whatever Cap is attached. After putting
  the Cap on with the device running, open the tile (it looks when it opens) or press
  "Check" in it.
- While the node runs, one I2C read a second checks that the Cap is still there. When
  it comes off, the node stops: the radio pins and the serial port are released, the
  header power goes back off, and Bluetooth stops advertising. Messages queued for the
  app are lost, as at any restart of the node. Give it a few seconds before putting a
  different Cap on.

M5Stack says not to swap Caps with the CardputerZero powered on. A Cap with the same
expander chip at the same address would be taken for the Cap LoRa-1262.

## Security

The companion protocol has no login. What a client can do depends on how it connects:

- **This device (TCP `127.0.0.1:5000`).** Any program or user on the CardputerZero can
  connect and has full control of the node: read contacts and messages, send messages as
  the node, change its name, radio settings, channels and contacts, and export or import
  its private key (the node's identity on the mesh).
- **Wi-Fi (TCP port 5000 on every interface, only while "Wi-Fi access" is on).** There is
  no password. Anyone on the same network can do everything a local client can, except
  export or import the private key, which the service refuses over the network. That still
  includes reading messages, sending as the node, changing its settings, and taking the
  single client slot away from your phone. Turn Wi-Fi access on only on a network you
  trust, and off when you are done.
- **Bluetooth.** The companion service needs an encrypted link, so only a bonded phone
  can use it, and a new phone can pair only while the tile's pairing window is open (two
  minutes, at most five). A bonded phone has full control, including key export and
  import. The two pairing methods differ in what they protect against while the window
  is open:
  - **Code** (the default): the phone must enter the 6-digit code shown on the
    CardputerZero's screen. Someone nearby who cannot see the screen cannot pair, and
    the pairing cannot be intercepted (it is authenticated, MITM-protected).
  - **Button** ("Just Works"): no code. Anyone in Bluetooth range can pair while the
    window is open, and a nearby attacker could intercept the pairing. The link is still
    encrypted afterwards.

The tile talks to the service over `/run/meshcore-companion/control.sock`, which only
members of group `input` can use: whoever can read the keyboard may use the tile. The
tile must run as a user in group `input` (it reads the keys from `/dev/input` anyway).
"Forget all" in the tile forgets only the phones that paired through the tile's pairing
window; other paired devices (a keyboard, a headset, or a phone paired with an earlier
version that did not keep this list) are shown as paired elsewhere and left alone. Remove
those with `bluetoothctl remove <address>`.

## Battery and SD card

- The node's state (identity, prefs, channels, contacts) lives in
  `/var/lib/meshcore-companion/node` on the SD card. The contact list, which a busy mesh
  changes every few seconds, is written at most every 5 minutes and at shutdown;
  contacts you add, remove or import in the app are written within a few seconds. Logs
  are lifecycle lines only (set `MESHCORE_COMPANION_LOG=DEBUG` in the unit for more).
- Messages that arrive while no app is connected are queued in memory, as the library
  keeps them, and are lost when the service or the node restarts.
- The radio library's SX1262 receive task wakes every 10 ms whether or not a packet has
  arrived, which costs about 4% of one core around the clock (measured on the device
  over 15 hours); an upstream fix (openhop-dev/openhop_core#177) is not merged yet.

## Building the package

`packaging/build_deb.py` builds `meshcore-companion_<version>_arm64.deb` on Linux or
macOS, without dpkg (Python 3.12 or later):

```sh
python3 packaging/build_deb.py --openhop ../forks/openhop_core --out dist
```

- `--openhop` is a git checkout of the openHop core fork
  (https://github.com/TheRealHaoLiu/openhop_core, branch `feat/companion-ble-transport-1.1.3`). The build
  bundles its committed HEAD and refuses a checkout with uncommitted or untracked
  changes; the commit is recorded in `/usr/lib/meshcore-companion/VENDORED`.
- The Python dependencies are pinned in `WHEELS` in the script and downloaded from PyPI
  as CPython 3.13 aarch64 wheels with `pip download` (so the Python that runs the script
  needs pip), unless `--wheels <dir>` points at a directory with exactly those wheels.
- The archive is reproducible: timestamps are `SOURCE_DATE_EPOCH`, or the commit date of
  this repository's HEAD.

Install it with `sudo apt install ./dist/meshcore-companion_<version>_arm64.deb`. The
first install enables and starts the service; an upgrade restarts it only if it was
running and keeps it disabled if you disabled it. `apt remove` keeps the node's state.
**`apt purge` deletes `/var/lib/meshcore-companion`, including the node identity
(`identity.key`)**: a reinstalled node gets a new identity, and the contacts that knew
the old one see a stranger. Export the private key from the app first (over Bluetooth)
if you want to keep it.

License: Apache-2.0 (see `NOTICE` for included work). Not affiliated with the MeshCore
project or M5Stack.
