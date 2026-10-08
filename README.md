# Control a Windows PC from a Fire TV remote (Android + ADB bridge)

A Fire TV remote does not work as a Windows keyboard. Windows never receives its buttons as normal keys.

This is for a Windows PC and an Android phone that already has that remote paired over Bluetooth.

The phone reads the remote. A tray app on the PC turns those presses into Windows input: arrows, Windows volume, seek, and a pointer.

You run it on your own machines. It is not a hosted service. Use it only on a private network you trust, such as home Wi-Fi.

## How it works

The PC does not pair with the Fire TV remote. The phone does. Button presses are not a long-lived `adb` stream from the PC. The phone opens a TCP connection to the PC.

1. The tray app listens on the PC.
2. Wireless debugging is used once, to start a small helper on the phone. It has to stay on while the remote controls the PC. `adb` starts that helper. It does not carry the buttons.
3. The helper waits until the Fire TV remote is connected over Bluetooth, then grabs it so the phone does not act on those presses.
4. The phone connects to the PC and sends the token stored in `bridge.txt`.
5. Key lines follow. The tray app injects them: Windows volume, seek, arrows, Tab, or pointer movement.
6. Turning the tray app off closes the connection. The phone lets go of the remote.

```mermaid
flowchart LR
  remote["Fire TV remote"] -->|Bluetooth| phone["Android phone"]
  phone -->|"TCP plus token in bridge.txt"| pc["Windows tray app"]
  pc -->|"Windows volume, seek, keys, pointer"| windows["Windows"]
```

## Requirements

- Windows 10 or 11
- Python 3.11 or newer, from [python.org](https://www.python.org/downloads/). During setup, turn on **Add python.exe to PATH**. Skip the Microsoft Store alias if Windows offers one.
- An Android phone on the same private Wi-Fi as the PC
- The Fire TV remote paired to that phone in Bluetooth settings
- Wireless debugging left on while you want the tray app to receive buttons

`adb` is downloaded for you the first time you run setup. You do not install platform-tools yourself.

## Setup

Install Python 3 from [python.org](https://www.python.org/downloads/) and turn on **Add python.exe to PATH**. Skip the Microsoft Store alias if Windows offers one.

On your home Wi-Fi, clone the repo and double-click `setup.bat`:

```bat
git clone https://github.com/Param2596/firestick-pc-remote.git
cd firestick-pc-remote
```

`setup.bat` installs the tray libraries, downloads `adb` if it is missing, and puts a **Fire Remote** shortcut on the desktop and in Startup. The Startup shortcut comes up off, so the phone keeps the remote until you click the icon. First-time setup asks for a pairing code. After a reboot it only needs the main Wireless debugging IP and port.

On the phone, before you type `YES`:

1. Turn on Developer options and **Wireless debugging**. Leave it on.
2. Pair the Fire TV remote in Bluetooth settings.
3. Tap **Pair device with pairing code** and leave the popup open.
4. Type the popup's IP, port, and 6-digit code into the setup window.

The code expires quickly. If pairing fails, open the popup again. If the PC still cannot see the phone, close the popup and type the **IP address & Port** from the main Wireless debugging page.

Click the round icon by the clock. Gray is off, green is on. That tray app is the switch from then on.

After a phone reboot, turn Wireless debugging on and run `python ar_remote.py --connect`. Use **IP address & Port** from the main Wireless debugging page. You do not need a pairing code unless this PC was never paired, or you revoked USB debugging. First-time pairing is `python ar_remote.py --pair`.

If nothing connects, run `python ar_remote.py` in a window and read the error. `python ar_remote.py --list` prints the phone's input devices. The remote usually shows up as `AR Keyboard`.

Leave these on while you want the Fire TV remote on the PC:

- **Wireless debugging.** Turning it off drops the connection.
- Phone Bluetooth, with the remote connected
- Phone and PC on the same home Wi-Fi
- The VPN off, if it blocks the local network

Click the tray icon to turn forwarding on or off. Off means the remote goes back to the phone. Wireless debugging can stay on while the icon is gray.

Right-click the icon:

| Item | What it does |
| --- | --- |
| Use remote on this PC | Same as a left click. The check mark is on when forwarding is on. |
| Mode sound | Plays a short sound when Alexa changes mode. |
| Quit | Turns forwarding off and exits. |

After the phone reboots, turn Wireless debugging back on, then run `python ar_remote.py --connect` if the tray cannot find the phone.

When you are done, or when you leave home, turn Wireless debugging off.

`run.bat` opens the tray app again with no console window. `setup.bat` is only for the first install.

## Button map

Alexa cycles three modes: **Controls**, **Volume**, **Cursor**. A label appears at the top of the screen. On the direction buttons and Menu, a tap does one step and holding repeats faster and faster. Other buttons stay down while held. Nothing uses a separate long-press action. The remote's power, sleep, and mic buttons are ignored. The mic audio never leaves the remote.

### Controls

| Remote | Windows |
| --- | --- |
| Up, Down, Left, Right | Arrow keys. A tap is one step. Holding speeds it up. |
| Center | Enter |
| Back | Escape |
| Home | Windows key |
| Menu | Tab. A tap is one step. Holding speeds it up. |
| Play / Pause | Play / Pause |
| Rewind, Fast forward | Previous track, Next track |
| App shortcut (three lines) | Task View (Win+Tab) |
| Alexa | Next mode |

### Volume

| Remote | Windows |
| --- | --- |
| Up, Down | Windows volume up, Windows volume down. A tap is one step. Holding speeds it up. |
| Left, Right | Left and Right, which seek in a player such as VLC. A tap is one step. Holding speeds it up. |
| Center | Mute |
| Alexa | Next mode |

Home, Menu, Play / Pause, Rewind, and Fast forward still do what they do in Controls. Back is Escape.

### Cursor

| Remote | Windows |
| --- | --- |
| Up, Down, Left, Right | Move the pointer. Holding a direction speeds it up. |
| Center | Left click. Hold it to drag. |
| Menu | Right click |
| Alexa | Next mode |

## Security / network warning

**Do this only on a private network you trust, such as your home Wi-Fi.** A cafe, hotel, airport, school, or guest network is not safe.

Wireless debugging is not a harmless switch. While it is on, a computer on that network can run commands on the phone: install and remove apps, read files, and control the screen. This project uses that channel to start the helper that reads the Fire TV remote. Anyone else who pairs with the phone on the same network can do the same things.

The button channel itself is a TCP connection from the phone to the PC. The phone must send the token in `bridge.txt` before any keys are accepted. That token stays on your PC and is not in this repo.

Turn **Wireless debugging** off when you are finished, and leave it off away from home. It has to stay on while the remote is controlling this PC. `setup.bat` stops and asks you to type `YES` before it will pair.

A VPN that blocks the local network will stop the PC from reaching the phone. Turn the VPN off while you use the remote.

## Limitations

- Windows only. The Fire TV remote stays paired to the Android phone, not to the PC's Bluetooth.
- Wireless debugging has to stay on the whole time the tray app is receiving buttons. Turning it off stops the helper. There is no separate Android app in this repo.
- After a phone reboot, turn Wireless debugging back on. If the tray cannot find the phone, run `python ar_remote.py --connect`.
- Netflix, Prime Video, and the other shortcut buttons never show up as keys, so they cannot be mapped.
- The remote microphone cannot be streamed to the PC. Alexa only changes mode.
- Power is ignored, so the remote cannot sleep the PC.
- Holding a direction repeats that same press faster. It does not become a different command.
- A VPN that hides the local network blocks the phone from reaching the tray app.

## Files that stay on your PC

These are created locally and are not in the repo:

| File | What it stores |
| --- | --- |
| `endpoint.txt` | The phone's `ip:port` after the first connection |
| `sound.txt` | Whether the mode sound is on |
| `bridge.txt` | The private token the phone sends when it opens TCP to the PC |

## When it stops working

- **No phone found after a reboot.** Wireless debugging is off, or the port changed. Turn it on and run `python ar_remote.py --connect` with the main-page IP and port. Do not use the pairing-popup port. First-time pairing is still `python ar_remote.py --pair`. A VPN that blocks LAN traffic will also fail.
- **Not authorized.** Unlock the phone and tap Allow.
- **The remote still drives the phone.** The tray app is off, or the grabber on the phone is not running. Turn the icon off and on.
- **Buttons do nothing on the PC.** Bluetooth dropped. Reconnect the Fire TV remote to the phone, then toggle the tray icon.
- **Several command windows open.** Start with `pythonw`, not `python`. `pythonw` has no console, and the script hides `adb`'s windows.

## Rebuild the phone helper

`grabevent` is a small arm64 program already included in the repo. It holds the remote so Android does not act on the buttons. You only rebuild it if you change that program:

```bat
pip install keystone-engine
python build_grabevent.py
```

## Sound

The mode chime is `bong_001` from [Kenney Interface Sounds](https://kenney.nl/assets/interface-sounds). Thanks to Kenney for making that pack and letting people use it.
