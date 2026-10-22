# all_bind

Bind anything on any input device — mice, keyboards, controllers, touchpads — to keys,
combos, turbo / rapid fire, macros, commands or profile switches. Works on Wayland and X11
(it sits below the compositor, using evdev and uinput), with a GUI, a CLI and an optional
root system service.

- **Any trigger**: every key and button on any grabbed device, scroll-wheel directions and
  controller d-pad directions. A binding can apply to all devices or just one.
- **Actions**
  - **Remap** to a key, mouse button, wheel direction or combo (`Ctrl + C`).
  - **Turbo**: 0.1 to 20,000 presses per second, while held or toggled, with press length,
    random jitter and burst count.
  - **Macros**: tap / press / release keys, exact or randomised delays, mouse moves
    (instant or smoothed), scrolling, typed text and shell commands. Run once, N times,
    loop while held, or loop on a toggle. Can be recorded from real input in the GUI.
  - **Run command**, **disable**, **switch profile**, or **shift to a profile while held**
    (a layer, e.g. a low-sensitivity sniper button).
- **Movement settings** per profile: invert or swap X/Y, per-axis sensitivity, wheel inversion.
- **Profiles** with `all_bind next` / `all_bind prev` and a desktop notification.
- **Find device**: use a device and see which one it is.
- Controllers, touchpads and tablets keep working while grabbed (sticks, triggers and touch
  are passed through on a copy of the device with the same IDs). Caps lock and other
  keyboard lights still work.

---

## Install

```sh
git clone https://github.com/jack02134x/all_bind
cd all_bind
```

### Arch Linux (recommended)

Builds a real package, so pacman tracks every file and can remove it again:

```sh
sudo pacman -S --needed base-devel python python-evdev pyside6 libnotify
make pkg
sudo pacman -U packaging/all_bind-1.0.0-1-x86_64.pkg.tar.zst
```

To update later: `git pull && make pkg` and install the new package the same way.
Remove with `sudo pacman -R all_bind`. (An AUR package is planned.)

### Any distro

```sh
make
sudo make install          # PREFIX=/usr by default
sudo make uninstall        # removes it again
```

Needs: a C compiler, `make`, Python 3, `python-evdev`, `PySide6` (for the GUI),
`libnotify` (optional, notifications).

Both ways install the engine, the GUI (with an app-menu entry), the CLI, the optional
systemd service, a udev rule and the `uinput` autoload. Log out and back in (or reboot)
afterwards so the udev rule and the `uinput` module take effect. Nothing is started or
enabled for you — see [Running](#running).

---

## Permissions

all_bind reads input devices (`/dev/input/event*`) and creates virtual ones (`/dev/uinput`).
The package loads the `uinput` module at boot and gives the logged-in user access to
`/dev/uinput` through a udev rule.

When it starts, all_bind checks whether you can read the input devices:

- **You can** (you're in the `input` group) — it just runs as you, no password.
  To get there, once:

  ```sh
  sudo usermod -aG input $USER     # then log out and back in
  ```

- **You can't** — it asks for your password (a polkit dialog from the GUI or autostart,
  `sudo` in a terminal) and runs as root, still with *your* config. Commands and
  notifications it starts still run as you. A polkit agent must be running for the dialog
  (most desktops have one; on Hyprland: `hyprpolkitagent`).

Or skip both and use the [root system service](#as-a-root-system-service), which starts at
boot and never asks.

Set `ALL_BIND_NO_ELEVATE=1` to never ask (it then runs without access and grabs nothing).

---

## Running

The engine (`all_bind`) does the work and keeps running in the background. The GUI and CLI
only edit the config; the engine notices every save and reloads by itself.

### As a root system service

```sh
sudo systemctl enable --now all_bind@$USER     # now and at every boot
sudo systemctl disable --now all_bind@$USER    # off again
systemctl status all_bind@$USER
```

It runs as root but uses **your** config (`~/.config/all_bind/`), starts at boot before
anyone logs in, and needs no group membership. Commands and notifications it triggers run
as you, not root. As a safety check it refuses a config that isn't owned by you or that
other users can write. Don't also start it from your session — only one engine runs at a
time.

In the GUI, Restart / Stop of the service go through `systemctl` and ask for your password
(needs `polkit`).

---

## GUI

Open **all_bind** from your app menu, or run `all_bind --gui` (or `all_bind-gui`).

- **Devices**: every input device is listed with its type. Tick one to grab it.
  **Find device…** tells you which device you just used.
- **Bindings**: *Quick add* — press the button you want, then choose what it does.
- **Movement**: inversion and sensitivity for the active profile.
- **Engine log**: tick *Show physical input events* to see exactly what each button sends.
- **Restart / Stop** in the top-right, plus the current profile and how many devices are grabbed.

---

## CLI

Everything the GUI does, from a terminal (no PySide6 needed):

```sh
all_bind --cli help
```

```text
Devices
  devices                       list every input device (* = grabbed)
  grab N|NAME ... / ungrab ...  grab devices by number or name
  find [--motion]               use a device and see which one it is
  capture                       press something and see its name
  keys [FILTER]                 list key / button names

Profiles
  profiles                      list profiles (> = active)
  profile use|new|copy|delete NAME
  profile rename OLD NEW
  next | prev                   switch the running engine's profile

Bindings  (add profile=NAME to work on another profile)
  binds                         list bindings in the active profile
  bind TRIGGER ACTION ... [from=DEVICE]
      remap KEY [KEY ...]
      turbo KEY [KEY ...] [rate=20] [press=0] [jitter=0] [burst=0] [mode=hold|toggle]
      macro "STEPS" [mode=once|repeat|while_held|toggle] [count=1] [gap=0]
      run COMMAND ...
      layer PROFILE [mode=hold|toggle]
      switch PROFILE
      disable
  unbind N | on N | off N
  move [invert-x=yes] [invert-y=no] [swap=no] [sens=1] [sens-x=] [sens-y=]
       [invert-wheel=] [invert-hwheel=]

Engine
  status | start | stop | restart | service
```

Key names are Linux input names (`KEY_A`, `BTN_SIDE`, `WHEEL_UP`, `DPAD_LEFT`…). The `KEY_`
prefix and letter case are optional, and common aliases work: `ctrl`, `shift`, `alt`,
`super`, `left-click`, `right-click`, `mouse4`, `mouse5`.

Macro steps are separated by `;`:
`tap KEY [HOLD_MS]`, `press KEY`, `release KEY`, `delay MS [RANDOM_MS]`,
`move DX DY [OVER_MS]`, `scroll N [h]`, `type TEXT`, `run COMMAND`.

### Examples

```sh
all_bind --cli devices
all_bind --cli grab 16                                      # by number from `devices`
all_bind --cli grab "HID 1bcf:08a0 Mouse"                   # or by name
all_bind --cli move invert-x=yes invert-y=yes

all_bind --cli bind mouse4 turbo left-click rate=100        # side button: 100 clicks/s while held
all_bind --cli bind mouse5 remap ctrl c                     # forward button: Ctrl+C
all_bind --cli bind f13 macro "tap a; delay 50; type hello; run notify-send done"
all_bind --cli bind KEY_F9 run hyprctl dispatch workspace +1

all_bind --cli profile new Sniper
all_bind --cli move sens=0.3 profile=Sniper
all_bind --cli profile use Default
all_bind --cli bind BTN_MIDDLE layer Sniper                 # hold middle click: sniper mode

all_bind --cli binds
all_bind --cli off 2                                        # disable binding 2
all_bind --cli unbind 2
```

---

## Switching profiles

```sh
all_bind next      # next profile, with a notification
all_bind prev      # previous profile
```

Bind them to keys, e.g. in Hyprland:

```ini
hl.bind(.. " + F9 ",         hl.dsp.exec_cmd("all_bind next"))
hl.bind(.. " + SHIFT + F9",  hl.dsp.exec_cmd("all_bind prev"))
```

…or to a mouse button inside all_bind itself: add a binding with **Run command** →
`all_bind next`.

Switching isn't saved: after a restart, or when you change something in the GUI / CLI,
all_bind returns to the profile selected there (`profile use NAME`).

---

## Files

| Path | What |
| --- | --- |
| `~/.config/all_bind/config.json` | Your setup. Written by the GUI and CLI. |
| `~/.config/all_bind/all_bind.conf` | Same setup converted for the engine. Regenerated on every save — don't edit. |
| `/usr/bin/all_bind` | The engine (C). Also `next`, `prev`, `reload`, `--cli`, `--gui`. |
| `/usr/bin/all_bind-gui` | GUI and CLI (Python). |
| `/usr/lib/systemd/system/all_bind@.service` | Optional root service. |
| `/usr/lib/udev/rules.d/60-all_bind-uinput.rules` | `/dev/uinput` access for the logged-in user. |
| `/usr/lib/modules-load.d/all_bind.conf` | Loads `uinput` at boot. |

Engine options (`all_bind --help`):

```text
all_bind [--user NAME] [--events] [CONFIG]   run the engine
all_bind next | prev | reload                talk to the running engine
all_bind --cli [COMMAND ...]                 configure from the terminal
all_bind --gui                               open the app
```

---

## How it works

all_bind grabs the devices you pick (`EVIOCGRAB`, so nothing else sees their raw input)
and re-emits their input through virtual devices:

- Keyboards and mice share one device, **all_bind virtual device**, so a modifier held on
  one device still applies to keys from another. Remaps and macros come out there too.
- Devices with absolute axes (controllers, touchpads, tablets) each get a copy,
  **"<name> (all_bind)"**, with identical capabilities and IDs, so games still recognise
  the controller.

Turbo and macros run on their own threads with absolute-time sleeps, so 1000 clicks per
second really is 1000.

---

## Troubleshooting

- **A device shows "⚠ busy"** — another program grabbed it first (another remapper, an old
  script…). all_bind picks it up as soon as it's free.
- **A mouse button doesn't show up** — many mice send extra buttons through a second
  "… Keyboard" device. Grab that one too. *Find device* or `all_bind --cli find` tells you
  which device a button belongs to.
- **Locked out after grabbing a keyboard** — `pkill all_bind` (or
  `sudo systemctl stop all_bind@$USER`) releases everything.
- **Controller in Steam games** — Steam Input and some games read controllers directly
  (hidraw), bypassing all_bind. Rumble isn't forwarded to a grabbed controller.
- **Outputs** can be keyboard keys, mouse buttons and the wheel — not controller buttons.
- **Typed text** in macros assumes a US keyboard layout.
- **Notifications from the root service** need you to be logged in (they go to your
  session's notification daemon).

---

## Development

```sh
make                  # builds ./all_bind
./all_bind_gui.py     # GUI from the source folder (rebuilds all_bind.c when it changes)
./all_bind --cli help
make pkg              # local Arch package from the working tree
make clean
```

Next to `all_bind.c` the GUI uses the `./all_bind` it builds; installed, it uses
`/usr/bin/all_bind`. Either way the config is `~/.config/all_bind/`.

## License

MIT — see [LICENSE](LICENSE).
