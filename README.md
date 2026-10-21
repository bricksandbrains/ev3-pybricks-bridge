# EV3 Pybricks serial bridge

This script runs a Pybricks MicroPython program on a LEGO® MINDSTORMS® EV3 from the terminal, over USB, and prints back whatever the program prints. It's what let Claude Code send programs to an EV3 and read the results without going through the browser.

> **Update (October 21, 2026): you probably don't need this any more.** Pybricks has released a new official command-line tool, [`pb`](https://github.com/pybricks/pybricks-tools) (`pybricks-cli` 1.0.0b1, beta). Tested on the same Windows PC and EV3 (firmware 4.1.0), it found the brick over USB and ran programs with no bridge and no driver changes, and the program's printed output arrived live. To use it:
>
> ```powershell
> uv tool install pybricks-cli
> pb run hello.py --wait
> ```
>
> `pb` keeps the brick connected through a background service. Run `pb service stop` before using Pybricks Code or this bridge, or the port stays busy (`Access is denied`). This repo stays up as the companion to the video and for its notes on how the serial messages are packaged.

> **Unofficial.** It isn't made or supported by the Pybricks team. It's only been tested on **Windows** with **Pybricks EV3 firmware 4.1.0** and **pybricksdev 2.3.2** (October 2026). If the official tool adds support for this, use the official tool instead. Tracking issue: [pybricks/support#2869](https://github.com/pybricks/support/issues/2869).

## Why it exists

`pybricksdev run usb` (pybricksdev 2.3.2) only looks for hubs that show up as a raw-USB Pybricks device, with a product name that ends in `Pybricks`. An EV3 running Pybricks firmware 4.1.0 doesn't show up like that. It shows up as a plain USB serial port (CDC-ACM) with a name like `<hub name> (MINDSTORMS EV3)`. So pybricksdev reports `Pybricks Hub not found`, even though the brick is fine and the Pybricks Code web app can talk to it.

The web app ([code.pybricks.com](https://code.pybricks.com)) connects to the EV3 over that serial port, using Web Serial. This bridge does the same thing from Python:

- **Kept from pybricksdev:** its command layer, which compiles the program, downloads it, starts it and collects its output.
- **Swapped in:** only the connection. The bridge uses the serial port, with the same framing and messages the live web app uses.

## What you need

- An EV3 with Pybricks firmware 4.1.x, flashed from [code.pybricks.com](https://code.pybricks.com). It's connected by its USB cable.
- Python 3.10 or newer.
- pybricksdev 2.3.2 and pyserial (see `requirements.txt`).

## Install (Windows, the way it was tested)

```powershell
# 1. Python 3.12 (per-user)
winget install --id Python.Python.3.12 --scope user --exact

# 2. pipx, then pybricksdev in its own environment
py -3.12 -m pip install --user --upgrade pipx
py -3.12 -m pipx install "pybricksdev==2.3.2"

# 3. Add pyserial to that same environment
py -3.12 -m pipx inject pybricksdev pyserial
```

Then download `ev3_serial_run.py` from this repo.

A plain virtual environment works too: `python -m venv .venv`, then `.venv\Scripts\pip install -r requirements.txt`.

## Run a program

Close any Pybricks Code browser tabs first, because the web app holds onto the serial port. Then run the bridge with the Python that has pybricksdev installed:

```powershell
& "$env:LOCALAPPDATA\pipx\pipx\venvs\pybricksdev\Scripts\python.exe" -I -X utf8 .\ev3_serial_run.py examples\hello.py
```

The bridge finds the EV3's port by its USB IDs (`0694:0005`). To choose the port yourself, add `--port COM6`.

Expected output:

```
[bridge] connected to <hub name> on COM6 (firmware 4.1.0, protocol 1.7.0)
hello from EV3
```

## Using it with Claude Code

Put a short `CLAUDE.md` in your project folder, so Claude Code knows how to run programs on the brick. For example:

```markdown
Programs run on a LEGO EV3 with Pybricks firmware 4.1, connected by USB.
To run a program on the brick, use:
  & "$env:LOCALAPPDATA\pipx\pipx\venvs\pybricksdev\Scripts\python.exe" -I -X utf8 .\ev3_serial_run.py <file.py>
Do not use `pybricksdev run usb`: it doesn't detect this firmware.
Save program files as UTF-8 without a BOM.
Do not change USB drivers.
```

After that, Claude Code can write a program, run it, read what the brick prints and decide what to try next, without anyone copying and pasting in between.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Could not connect on COM6: ... Access is denied` | Another app has the port open: usually a Pybricks Code tab, or `pb`'s background service | Close the tab (or the browser), or run `pb service stop`, and try again |
| `EV3 serial port not found` | The brick is off, unplugged, or doesn't show up as a serial port | Turn it on and check Device Manager under **Ports (COM & LPT)** |
| The EV3 no longer shows under Ports, and Pybricks Code can't connect | Its driver was swapped to WinUSB (for example with Zadig) | In Device Manager, uninstall the device, tick **Delete the driver software**, then unplug it and plug it back in. **Don't** swap drivers on a Pybricks EV3 |
| `NameError` on line 1 of your program | The file was saved with a UTF-8 BOM (Windows PowerShell 5 `Set-Content -Encoding utf8` adds one) | Save it as UTF-8 without a BOM |
| `Pybricks Hub not found` | You ran `pybricksdev run usb` | Use this bridge instead |

**Known limitation:** on the tested setup, the program's printed lines arrived all at once, when the program ended, rather than line by line as it ran.

## How the messages are packaged

This section describes what the live Pybricks Code web app does (as of October 2026), which the bridge copies.

**Port settings:** 115200 baud, over the EV3's USB serial port.

**Framing (a COBS variant).** Every message is encoded as one frame:

- Byte values 0, 1 and 2 are escaped, so they never appear inside a frame.
- A frame is made of blocks of at most 84 bytes. Each block starts with a code byte saying how long it is and which escaped value (if any) ends it.
- Every encoded byte is XORed with `0x03`.
- Each frame ends with the delimiter byte `0x02`.

`encode_frame()` and `decode_frame()` in the script implement this.

**Message types.** The first byte of each decoded message says what it is:

| Direction | Type | Layout | Purpose |
|---|---|---|---|
| PC → hub | `0x01` subscribe | `[0x01, 1 or 0]` | Turn hub events on or off |
| PC → hub | `0x02` command | `[0x02, seq, Pybricks command…]` | A standard Pybricks command (download, start, stop…) |
| PC → hub | `0x03` read | `[0x03, service, char id (LE16)]` | Read a value, such as the firmware version |
| hub → PC | `0x01` response | `[0x01, seq, status (LE32)]` | Result of a command; 0 means OK |
| hub → PC | `0x02` event | `[0x02, Pybricks event…]` | Hub events, including the program's printed output |
| hub → PC | `0x03` read reply | `[0x03, service, char id (LE16), value…]` | The value that was asked for |

The service is `0x01` for the standard device information and `0x02` for Pybricks.

**Connection handshake:**

1. Open the port and send a single `0x02`, to flush any half-received frame on the hub side.
2. Read the firmware version, the hub name and the protocol version (service `0x01`).
3. Read the Pybricks hub capabilities (service `0x02`): maximum write size, feature flags, maximum program size and number of slots.
4. Subscribe to events (`[0x01, 1]`). Like the web app, the bridge doesn't wait for a reply to this.

After that, pybricksdev's normal `run()` sends the usual Pybricks commands (download the compiled program, start it), and its printed output comes back as events.

## Files

| File | What it is |
|---|---|
| `ev3_serial_run.py` | The bridge |
| `examples/hello.py` | A one-line test program |
| `requirements.txt` | Python packages, with the tested pybricksdev version pinned |

## Credits

- [pybricksdev](https://github.com/pybricks/pybricksdev) (MIT License, © The Pybricks Authors). The bridge imports and reuses its `PybricksHub` command layer and constants.
- [Pybricks Code](https://github.com/pybricks/pybricks-code) (MIT License, © The Pybricks Authors). The serial framing and message layout were worked out from the live web app and re-implemented here in Python.
- [Pybricks](https://pybricks.com), for the EV3 firmware.

LEGO® and MINDSTORMS® are trademarks of the LEGO Group, which doesn't sponsor, authorize or endorse this project. It isn't affiliated with the Pybricks team or Anthropic either.

## License

MIT. See [LICENSE](LICENSE).
