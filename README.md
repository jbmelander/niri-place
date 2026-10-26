# niri-place
Choose where a monitor goes relative to your main screen in [niri](https://github.com/YaLTeR/niri),
and have niri remember it. Saved monitors go back to their place whenever they are plugged in.

<img width="921" height="672" alt="image" src="https://github.com/user-attachments/assets/07424d1f-4281-4462-a9ec-4a0029b6d6fc" />


## Install

```sh
git clone <this repo> niri-place
cd niri-place
./install.sh
```

This installs PyQt6 if needed (`python-pyqt6` via pacman on Arch/CachyOS), the `niri-place`
command in `/usr/local/bin`, and a **Place Monitor** entry in the app menu. Use
`./install.sh --user` to install into `~/.local` without sudo, and `./install.sh --uninstall`
to remove it.

## Use

    niri-place                  open the GUI
    niri-place --monitor DP-2   open the GUI on that connector
    niri-place --watch          stay in the background; open the GUI when an unsaved monitor appears

Positions are written to `~/.config/niri/cfg/monitors.kdl`, which is included from your
`config.kdl` (a backup of `config.kdl` is made the first time).
