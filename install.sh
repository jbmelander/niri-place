#!/bin/sh
# Install niri-place as a `niri-place` command and a "Place Monitor" app-menu entry.
#
#     ./install.sh               install to /usr/local (asks for sudo)
#     ./install.sh --user        install to ~/.local (no sudo; ~/.local/bin must be on PATH)
#     ./install.sh --uninstall   remove it again (add --user if you installed with --user)
set -eu
here=$(cd "$(dirname "$0")" && pwd)

prefix=/usr/local
action=install
for arg in "$@"; do
    case "$arg" in
        --user) prefix="$HOME/.local" ;;
        --uninstall) action=uninstall ;;
        -h | --help) sed -n '2,7s/^# \{0,1\}//p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

bin="$prefix/bin/niri-place"
desktop="$prefix/share/applications/niri-place.desktop"

# Use sudo only when the prefix is not writable by us.
sudo=
if [ "$(id -u)" -ne 0 ]; then
    probe="$prefix"
    while [ ! -e "$probe" ]; do probe=$(dirname "$probe"); done
    [ -w "$probe" ] || sudo=sudo
fi

if [ "$action" = uninstall ]; then
    $sudo rm -f "$bin" "$desktop"
    echo "Removed $bin and $desktop."
    echo "Your saved monitors are kept in ~/.config/niri-place and ~/.config/niri/cfg/monitors.kdl."
    exit 0
fi

command -v niri >/dev/null || echo "Warning: niri was not found on PATH; niri-place needs it to run." >&2

# Dependency: PyQt6 for the system python3.
if ! python3 -c 'import PyQt6.QtWidgets' 2>/dev/null; then
    if command -v pacman >/dev/null; then
        echo "Installing PyQt6 (python-pyqt6) with pacman..."
        sudo pacman -S --needed python-pyqt6
    elif command -v apt-get >/dev/null; then
        sudo apt-get install -y python3-pyqt6
    elif command -v dnf >/dev/null; then
        sudo dnf install -y python3-pyqt6
    else
        echo "PyQt6 for python3 is missing; install it with your package manager and rerun." >&2
        exit 1
    fi
fi

$sudo install -Dm755 "$here/niri_place.py" "$bin"

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
cat > "$tmp" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Place Monitor
GenericName=Monitor layout
Comment=Choose where a monitor goes relative to the main screen, and remember it
Exec=$bin
Icon=video-display
Terminal=false
Categories=Settings;HardwareSettings;
Keywords=monitor;display;screen;output;niri;
DESKTOP
$sudo install -Dm644 "$tmp" "$desktop"
command -v update-desktop-database >/dev/null && $sudo update-desktop-database -q "$(dirname "$desktop")" || true

echo "Installed. Run 'niri-place' in a terminal, or open 'Place Monitor' from the app menu."
case ":$PATH:" in
    *":$prefix/bin:"*) ;;
    *) echo "Note: $prefix/bin is not on your PATH (fish: fish_add_path $prefix/bin)." ;;
esac
