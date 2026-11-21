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
Cap for other radio apps.

License: Apache-2.0 (see `NOTICE` for included work). Not affiliated with the MeshCore
project or M5Stack.
