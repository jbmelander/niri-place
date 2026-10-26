#!/usr/bin/env python3
"""niri-place: choose where a monitor goes relative to your main screen, and remember it.

Saved monitors are written as `output "<make> <model> <serial>" { position ... }` blocks into
~/.config/niri/cfg/monitors.kdl (included from config.kdl). niri matches those blocks by make,
model and serial, so a saved monitor goes back to its place whenever it is plugged in, on any
port, with or without this program running.

The main screen (the laptop panel, by default) is pinned at 0,0 and every saved monitor's
position is stored in that frame. ~/.config/niri-place/monitors.json is the source of truth;
monitors.kdl is regenerated from it on every save.

    niri-place                  open the GUI (preselects a connected monitor that is not saved yet)
    niri-place --monitor DP-2   open the GUI on that connector
    niri-place --watch          stay in the background; open the GUI when an unsaved monitor appears
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
NIRI_DIR = CONFIG_HOME / "niri"
NIRI_CONFIG = NIRI_DIR / "config.kdl"
MANAGED_KDL = NIRI_DIR / "cfg" / "monitors.kdl"
INCLUDE_LINE = 'include "./cfg/monitors.kdl"'
STATE_FILE = CONFIG_HOME / "niri-place" / "monitors.json"

INTERNAL_PREFIXES = ("eDP", "LVDS", "DSI")
SIDES = ("left", "right", "above", "below")
ALIGNS = ("start", "center", "end")
SIDE_PHRASE = {"left": "left of", "right": "right of", "above": "above", "below": "below"}
TRANSFORMS = {
    "Normal": "normal",
    "90": "90",
    "180": "180",
    "270": "270",
    "Flipped": "flipped",
    "Flipped90": "flipped-90",
    "Flipped180": "flipped-180",
    "Flipped270": "flipped-270",
}


# ── niri IPC ────────────────────────────────────────────────────────────────


@dataclass
class Output:
    connector: str  # e.g. "HDMI-A-1"
    key: str  # "<make> <model> <serial>", what the config block is matched by
    make: str
    model: str
    x: int
    y: int
    width: int  # logical
    height: int
    scale: float
    transform: str  # KDL spelling
    mode: str | None  # "WxH@R.RRR", only when not the preferred mode

    @property
    def internal(self) -> bool:
        return self.connector.startswith(INTERNAL_PREFIXES)

    @property
    def label(self) -> str:
        return f"{self.make} {self.model} ({self.connector})"


def niri_request(request) -> dict:
    """One request over $NIRI_SOCKET; falls back to `niri msg --json` if the socket is missing."""
    path = os.environ.get("NIRI_SOCKET")
    if path:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(path)
            s.sendall((json.dumps(request) + "\n").encode())
            s.shutdown(socket.SHUT_WR)
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        reply = json.loads(buf)
        if "Err" in reply:
            raise RuntimeError(reply["Err"])
        return reply["Ok"]
    if request == "Outputs":
        out = subprocess.run(
            ["niri", "msg", "--json", "outputs"], check=True, capture_output=True, text=True
        ).stdout
        return {"Outputs": json.loads(out)}
    raise RuntimeError("NIRI_SOCKET is not set; is niri running?")


def connected_outputs() -> list[Output]:
    outputs = []
    for o in niri_request("Outputs")["Outputs"].values():
        logical = o.get("logical")
        if logical is None:  # connected but turned off
            continue
        mode = None
        cur = o.get("current_mode")
        if cur is not None and not o["modes"][cur]["is_preferred"]:
            m = o["modes"][cur]
            mode = f"{m['width']}x{m['height']}@{m['refresh_rate'] / 1000:.3f}"
        outputs.append(
            Output(
                connector=o["name"],
                key=f"{o['make']} {o['model']} {o.get('serial') or 'Unknown'}",
                make=o["make"],
                model=o["model"],
                x=logical["x"],
                y=logical["y"],
                width=logical["width"],
                height=logical["height"],
                scale=logical["scale"],
                transform=TRANSFORMS.get(logical["transform"], "normal"),
                mode=mode,
            )
        )
    outputs.sort(key=lambda o: (not o.internal, o.x, o.y))
    return outputs


# ── saved state ─────────────────────────────────────────────────────────────


def load_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("main", None)  # {"key", "name", "width", "height", "scale", ...}
    state.setdefault("monitors", {})  # key -> {"name", "x", "y", "width", "height", "side", ...}
    return state


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(STATE_FILE)


def device_settings(out: Output) -> dict:
    return {
        "width": out.width,
        "height": out.height,
        "scale": out.scale,
        "transform": out.transform,
        "mode": out.mode,
    }


def place(anchor: tuple[int, int, int, int], size: tuple[int, int], side: str, align: str):
    """Top-left corner of a `size` monitor on `side` of the `anchor` (x, y, w, h) rectangle."""
    ax, ay, aw, ah = anchor
    w, h = size
    if side in ("left", "right"):
        x = ax - w if side == "left" else ax + aw
        y = {"start": ay, "center": ay + (ah - h) // 2, "end": ay + ah - h}[align]
    else:
        y = ay - h if side == "above" else ay + ah
        x = {"start": ax, "center": ax + (aw - w) // 2, "end": ax + aw - w}[align]
    return x, y


def kdl_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def fmt_scale(scale: float) -> str:
    return f"{scale:g}"


def render_kdl(state: dict) -> str:
    lines = [
        "// Generated by niri-place: change it with the niri-place GUI. Hand edits are lost.",
        "// The main screen sits at 0,0; every saved monitor is positioned relative to it.",
        "",
    ]

    def block(key: str, entry: dict, x: int, y: int, comment: str) -> None:
        lines.append(f"// {comment}")
        lines.append(f"output {kdl_string(key)} {{")
        if entry.get("mode"):
            lines.append(f"    mode {kdl_string(entry['mode'])}")
        lines.append(f"    scale {fmt_scale(entry.get('scale', 1.0))}")
        if entry.get("transform", "normal") != "normal":
            lines.append(f"    transform {kdl_string(entry['transform'])}")
        lines.append(f"    position x={x} y={y}")
        lines.append("}")
        lines.append("")

    main = state.get("main")
    if main:
        block(main["key"], main, 0, 0, f"{main['name']} (main screen)")
    for key, m in sorted(state["monitors"].items(), key=lambda kv: kv[1]["name"].lower()):
        block(key, m, m["x"], m["y"], f"{m['name']}: {SIDE_PHRASE[m['side']]} {m['relative_name']}")
    return "\n".join(lines)


# ── config files ────────────────────────────────────────────────────────────

OUTPUT_LINE = re.compile(r'^\s*output\s+"((?:[^"\\]|\\.)*)"')
INCLUDE_RE = re.compile(r'^\s*include\s+"((?:[^"\\]|\\.)*)"')


def config_files(root: Path = NIRI_CONFIG, seen: set[Path] | None = None) -> list[Path]:
    """config.kdl and everything it includes, recursively (ignores `/-`-commented includes)."""
    seen = set() if seen is None else seen
    root = root.resolve()
    if root in seen or not root.exists():
        return []
    seen.add(root)
    files = [root]
    for line in root.read_text().splitlines():
        m = INCLUDE_RE.match(line)
        if m:
            inc = Path(os.path.expanduser(m.group(1)))
            files += config_files(inc if inc.is_absolute() else root.parent / inc, seen)
    return files


def conflicting_blocks(outputs: list[Output], keys: list[str]) -> list[str]:
    """Live `output` blocks in other config files that name one of these monitors.

    niri does not merge output blocks across includes, so a second block for the same monitor
    would fight with ours. niri matches names case-insensitively, by connector or by key.
    """
    names = {k.lower(): k for k in keys}
    for o in outputs:
        if o.key in keys:
            names[o.connector.lower()] = o.key
    found = []
    managed = MANAGED_KDL.resolve()
    for path in config_files():
        if path == managed:
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            m = OUTPUT_LINE.match(line)
            if m and m.group(1).lower() in names:
                found.append(f'{path}:{n}: output "{m.group(1)}"')
    return found


def ensure_included() -> None:
    text = NIRI_CONFIG.read_text() if NIRI_CONFIG.exists() else ""
    if any(INCLUDE_RE.match(line) and "monitors.kdl" in line for line in text.splitlines()):
        return
    backup = NIRI_CONFIG.with_name("config.kdl.bak-niri-place")
    if NIRI_CONFIG.exists() and not backup.exists():
        shutil.copy2(NIRI_CONFIG, backup)
    # Last, so it overrides anything before it.
    NIRI_CONFIG.write_text(text.rstrip("\n") + ("\n" if text else "") + INCLUDE_LINE + "\n")


def write_config(state: dict) -> None:
    """Write monitors.kdl, validate the whole config, and roll back if niri rejects it."""
    MANAGED_KDL.parent.mkdir(parents=True, exist_ok=True)
    old = MANAGED_KDL.read_text() if MANAGED_KDL.exists() else None
    MANAGED_KDL.write_text(render_kdl(state))
    ensure_included()
    check = subprocess.run(
        ["niri", "validate", "-c", str(NIRI_CONFIG)], capture_output=True, text=True
    )
    if check.returncode != 0:
        if old is None:
            MANAGED_KDL.unlink()
        else:
            MANAGED_KDL.write_text(old)
        msg = re.sub(r"\x1b\[[0-9;]*m", "", check.stderr or check.stdout).strip()
        raise RuntimeError(f"niri rejected the config, nothing was changed:\n\n{msg}")
    subprocess.run(["niri", "msg", "action", "load-config-file"], capture_output=True)


# ── GUI ─────────────────────────────────────────────────────────────────────


def run_gui(preselect: str | None) -> int:
    from PyQt6.QtCore import QRectF, Qt
    from PyQt6.QtGui import QColor, QFont, QPainter, QPen
    from PyQt6.QtWidgets import (
        QApplication,
        QButtonGroup,
        QComboBox,
        QFormLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMessageBox,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )

    app = QApplication(sys.argv)
    app.setApplicationName("niri-place")
    app.setDesktopFileName("niri-place")
    app.setStyleSheet(
        "QPushButton:checked { background: palette(highlight); color: palette(highlighted-text); }"
    )

    class Preview(QWidget):
        """Monitors drawn to scale; the one being placed is highlighted."""

        def __init__(self):
            super().__init__()
            self.setMinimumSize(420, 260)
            self.rects: list[tuple[str, tuple[int, int, int, int], str]] = []  # label, rect, role

        def set_rects(self, rects):
            self.rects = rects
            self.update()

        def paintEvent(self, _event):
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            pal = self.palette()
            p.fillRect(self.rect(), pal.color(pal.ColorRole.Base))
            if not self.rects:
                return
            x0 = min(r[0] for _, r, _ in self.rects)
            y0 = min(r[1] for _, r, _ in self.rects)
            x1 = max(r[0] + r[2] for _, r, _ in self.rects)
            y1 = max(r[1] + r[3] for _, r, _ in self.rects)
            pad = 16
            k = min((self.width() - 2 * pad) / (x1 - x0), (self.height() - 2 * pad) / (y1 - y0))
            ox = (self.width() - (x1 - x0) * k) / 2
            oy = (self.height() - (y1 - y0) * k) / 2
            colors = {
                "main": QColor(110, 110, 120),
                "saved": QColor(140, 140, 150),
                "target": QColor(52, 120, 220),
            }
            text = pal.color(pal.ColorRole.Text)
            font = QFont(self.font())
            for label, (x, y, w, h), role in sorted(self.rects, key=lambda r: r[2] == "target"):
                rect = QRectF(ox + (x - x0) * k, oy + (y - y0) * k, w * k, h * k).adjusted(
                    1, 1, -1, -1
                )
                fill = QColor(colors[role])
                fill.setAlpha(170 if role == "target" else 70)
                p.setBrush(fill)
                p.setPen(QPen(colors[role], 2))
                p.drawRoundedRect(rect, 4, 4)
                font.setBold(role != "saved")
                p.setFont(font)
                p.setPen(Qt.GlobalColor.white if role == "target" else text)
                p.drawText(
                    rect.adjusted(4, 4, -4, -4),
                    Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                    f"{label}\n{w}×{h}",
                )

    class Window(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Place monitor")
            self.state = load_state()
            self.outputs: list[Output] = []

            self.main_combo = QComboBox()
            self.target_combo = QComboBox()
            self.anchor_combo = QComboBox()
            self.name_edit = QLineEdit()
            self.name_edit.setPlaceholderText("e.g. Office Dell")

            self.side_group = QButtonGroup(self)
            side_row = QHBoxLayout()
            for i, text in enumerate(("← Left", "Right →", "↑ Above", "↓ Below")):
                b = QPushButton(text)
                b.setCheckable(True)
                self.side_group.addButton(b, i)
                side_row.addWidget(b)
            self.align_combo = QComboBox()

            form = QFormLayout()
            form.addRow("Main screen", self.main_combo)
            form.addRow("Monitor to place", self.target_combo)
            form.addRow("Next to", self.anchor_combo)
            form.addRow("Side", side_row)
            form.addRow("Align", self.align_combo)
            form.addRow("Save as", self.name_edit)

            self.preview = Preview()
            self.status = QLabel()
            self.status.setWordWrap(True)

            self.save_button = QPushButton("Apply and save")
            self.save_button.setDefault(True)
            close_button = QPushButton("Close")
            buttons = QHBoxLayout()
            buttons.addStretch()
            buttons.addWidget(close_button)
            buttons.addWidget(self.save_button)

            self.saved_list = QListWidget()
            self.saved_list.setMaximumHeight(110)
            forget = QPushButton("Forget selected")
            saved_box = QGroupBox("Saved monitors")
            sl = QVBoxLayout(saved_box)
            sl.addWidget(self.saved_list)
            fr = QHBoxLayout()
            fr.addStretch()
            fr.addWidget(forget)
            sl.addLayout(fr)

            layout = QVBoxLayout(self)
            layout.addLayout(form)
            layout.addWidget(self.preview, 1)
            layout.addWidget(self.status)
            layout.addLayout(buttons)
            layout.addWidget(saved_box)

            self.main_combo.currentIndexChanged.connect(self.on_main_changed)
            self.target_combo.currentIndexChanged.connect(self.on_target_changed)
            self.anchor_combo.currentIndexChanged.connect(self.refresh_preview)
            self.side_group.idToggled.connect(self.on_side_changed)
            self.align_combo.currentIndexChanged.connect(self.refresh_preview)
            self.save_button.clicked.connect(self.save)
            close_button.clicked.connect(self.close)
            forget.clicked.connect(self.forget)

            self.reload(preselect)

        # -- data

        def reload(self, preselect: str | None = None):
            try:
                self.outputs = connected_outputs()
            except Exception as e:  # noqa: BLE001
                QMessageBox.critical(self, "niri-place", f"Could not ask niri for outputs:\n{e}")
                self.outputs = []
            by_key = {o.key: o for o in self.outputs}
            main = self.state.get("main")
            if main and main["key"] in by_key:
                main_key = main["key"]
            elif self.outputs:
                main_key = self.outputs[0].key  # internal panels sort first
            else:
                main_key = None

            self.main_combo.blockSignals(True)
            self.main_combo.clear()
            for o in self.outputs:
                self.main_combo.addItem(o.label, o.key)
            if main and main["key"] not in by_key:
                self.main_combo.addItem(f"{main['name']} (not connected)", main["key"])
            self.main_combo.setCurrentIndex(max(0, self.main_combo.findData(main_key)))
            self.main_combo.blockSignals(False)
            self.on_main_changed(preselect=preselect)
            self.refresh_saved_list()

        def main_key(self) -> str | None:
            return self.main_combo.currentData()

        def main_entry(self) -> dict | None:
            key = self.main_key()
            out = next((o for o in self.outputs if o.key == key), None)
            if out:
                return {"key": key, "name": out.label, **device_settings(out)}
            main = self.state.get("main")
            return main if main and main["key"] == key else None

        def target(self) -> Output | None:
            key = self.target_combo.currentData()
            return next((o for o in self.outputs if o.key == key), None)

        def anchor_rect(self) -> tuple[int, int, int, int] | None:
            key = self.anchor_combo.currentData()
            if key is None:
                return None
            if key == self.main_key():
                m = self.main_entry()
                return (0, 0, m["width"], m["height"]) if m else None
            s = self.state["monitors"].get(key)
            return (s["x"], s["y"], s["width"], s["height"]) if s else None

        def side(self) -> str:
            return SIDES[max(0, self.side_group.checkedId())]

        def proposed(self) -> tuple[int, int] | None:
            t, a = self.target(), self.anchor_rect()
            if t is None or a is None:
                return None
            return place(
                a, (t.width, t.height), self.side(), ALIGNS[self.align_combo.currentIndex()]
            )

        # -- reactions

        def on_main_changed(self, *_args, preselect: str | None = None):
            main_key = self.main_key()
            current = preselect or self.target_combo.currentData()
            self.target_combo.blockSignals(True)
            self.target_combo.clear()
            for o in self.outputs:
                if o.key != main_key:
                    saved = self.state["monitors"].get(o.key)
                    suffix = f"  [saved: {saved['name']}]" if saved else "  [new]"
                    self.target_combo.addItem(o.label + suffix, o.key)
            # Preselect: the named connector, else the first monitor that is not saved yet.
            idx = -1
            for i in range(self.target_combo.count()):
                o = next(o for o in self.outputs if o.key == self.target_combo.itemData(i))
                if current in (o.connector, o.key):
                    idx = i
                    break
            if idx < 0:
                idx = next(
                    (
                        i
                        for i in range(self.target_combo.count())
                        if self.target_combo.itemData(i) not in self.state["monitors"]
                    ),
                    0,
                )
            self.target_combo.setCurrentIndex(idx)
            self.target_combo.blockSignals(False)
            self.on_target_changed()

        def on_target_changed(self, *_args):
            t = self.target()
            main_key = self.main_key()
            saved = self.state["monitors"].get(t.key) if t else None

            # Anchors: the main screen and every other saved monitor (their positions are fixed).
            self.anchor_combo.blockSignals(True)
            self.anchor_combo.clear()
            main = self.main_entry()
            if main:
                self.anchor_combo.addItem(f"{main['name']} (main screen)", main_key)
            for key, s in sorted(self.state["monitors"].items(), key=lambda kv: kv[1]["name"]):
                if t is None or key != t.key:
                    self.anchor_combo.addItem(s["name"], key)
            anchor = saved.get("relative_to") if saved else main_key
            self.anchor_combo.setCurrentIndex(max(0, self.anchor_combo.findData(anchor)))
            self.anchor_combo.blockSignals(False)

            self.side_group.blockSignals(True)
            side = saved["side"] if saved else "right"
            self.side_group.button(SIDES.index(side)).setChecked(True)
            self.side_group.blockSignals(False)
            self.update_align_labels()
            self.align_combo.blockSignals(True)
            self.align_combo.setCurrentIndex(ALIGNS.index(saved["align"]) if saved else 1)
            self.align_combo.blockSignals(False)

            self.name_edit.setText(saved["name"] if saved else (f"{t.make} {t.model}" if t else ""))
            self.save_button.setEnabled(t is not None and main is not None)
            self.refresh_preview()

        def on_side_changed(self, _id, checked):
            if checked:
                self.update_align_labels()
                self.refresh_preview()

        def update_align_labels(self):
            i = self.align_combo.currentIndex()
            labels = (
                ("Top edges", "Centered", "Bottom edges")
                if self.side() in ("left", "right")
                else ("Left edges", "Centered", "Right edges")
            )
            self.align_combo.blockSignals(True)
            self.align_combo.clear()
            self.align_combo.addItems(labels)
            self.align_combo.setCurrentIndex(i if i >= 0 else 1)
            self.align_combo.blockSignals(False)

        def refresh_preview(self, *_args):
            rects = []
            main = self.main_entry()
            t = self.target()
            connected = {o.key for o in self.outputs}
            if main:
                rects.append((main["name"], (0, 0, main["width"], main["height"]), "main"))
            for key, s in self.state["monitors"].items():
                if key in connected and (t is None or key != t.key):
                    rects.append((s["name"], (s["x"], s["y"], s["width"], s["height"]), "saved"))
            pos = self.proposed()
            if t and pos:
                rects.append(
                    (self.name_edit.text() or t.model, (*pos, t.width, t.height), "target")
                )
            self.preview.set_rects(rects)

            if not self.outputs:
                self.status.setText("niri reports no connected outputs.")
            elif t is None:
                self.status.setText(
                    "Only the main screen is connected. Plug in a monitor and reopen."
                )
            elif pos is None:
                self.status.setText("Pick what to place it next to.")
            else:
                clash = conflicting_blocks(self.outputs, [k for k in (t.key, self.main_key()) if k])
                self.status.setText(
                    (
                        "⚠ These monitors are also configured elsewhere; niri does not merge "
                        "output blocks, so remove or comment out (/-) these:\n" + "\n".join(clash)
                    )
                    if clash
                    else f"{t.connector} will go to x={pos[0]}, y={pos[1]} (main screen at 0, 0)."
                )

        def refresh_saved_list(self):
            self.saved_list.clear()
            connected = {o.key for o in self.outputs}
            for key, s in sorted(self.state["monitors"].items(), key=lambda kv: kv[1]["name"]):
                mark = "●" if key in connected else "○"
                item = QListWidgetItem(
                    f"{mark} {s['name']}: {SIDE_PHRASE[s['side']]} {s['relative_name']}"
                )
                item.setToolTip(f"{key}\n{'connected' if key in connected else 'not connected'}")
                item.setData(Qt.ItemDataRole.UserRole, key)
                self.saved_list.addItem(item)

        # -- actions

        def commit(self, state: dict) -> bool:
            try:
                write_config(state)
            except Exception as e:  # noqa: BLE001
                QMessageBox.critical(self, "niri-place", str(e))
                return False
            save_state(state)
            self.state = state
            return True

        def save(self):
            t, pos, main = self.target(), self.proposed(), self.main_entry()
            if t is None or pos is None or main is None:
                return
            name = self.name_edit.text().strip() or f"{t.make} {t.model}"
            state = json.loads(json.dumps(self.state))
            if state["main"] and state["main"]["key"] != main["key"]:
                ok = QMessageBox.question(
                    self,
                    "niri-place",
                    f"The main screen was {state['main']['name']}. Saved monitors are positioned "
                    f"relative to the main screen, so they will now be relative to {main['name']}.",
                )
                if ok != QMessageBox.StandardButton.Yes:
                    return
            if state["main"] and state["main"]["key"] == main["key"]:
                main["name"] = state["main"]["name"]
            state["main"] = main
            anchor_key = self.anchor_combo.currentData()
            state["monitors"][t.key] = {
                "name": name,
                "x": pos[0],
                "y": pos[1],
                "side": self.side(),
                "align": ALIGNS[self.align_combo.currentIndex()],
                "relative_to": anchor_key,
                "relative_name": self.anchor_combo.currentText().removesuffix(" (main screen)"),
                **device_settings(t),
            }
            if self.commit(state):
                self.reload(t.key)
                self.status.setText(f"Saved “{name}”. It will go here whenever it is plugged in.")

        def forget(self):
            item = self.saved_list.currentItem()
            if item is None:
                return
            key = item.data(Qt.ItemDataRole.UserRole)
            name = self.state["monitors"][key]["name"]
            dependents = [
                s["name"] for s in self.state["monitors"].values() if s.get("relative_to") == key
            ]
            note = (
                f"\n\nPlaced next to it: {', '.join(dependents)}. They keep their positions."
                if dependents
                else ""
            )
            if (
                QMessageBox.question(self, "niri-place", f"Forget “{name}”?{note}")
                != QMessageBox.StandardButton.Yes
            ):
                return
            state = json.loads(json.dumps(self.state))
            del state["monitors"][key]
            if self.commit(state):
                self.reload()
                self.status.setText(f"Forgot “{name}”. It will be placed automatically next time.")

    w = Window()
    w.resize(560, 640)
    w.show()
    return app.exec()


# ── watch mode ──────────────────────────────────────────────────────────────


def watch(interval: float) -> int:
    """Open the GUI whenever a monitor appears that is neither saved nor the main screen."""
    script = [sys.executable, os.path.abspath(__file__)]
    seen: set[str] | None = None
    while True:
        try:
            outputs = connected_outputs()
        except Exception:  # noqa: BLE001 - niri restarting, socket gone, ...
            time.sleep(interval)
            continue
        keys = {o.key for o in outputs}
        if seen is not None:
            state = load_state()
            known = set(state["monitors"]) | ({state["main"]["key"]} if state["main"] else set())
            for o in outputs:
                if o.key not in seen and o.key not in known:
                    subprocess.Popen([*script, "--monitor", o.connector])
                    break
        seen = keys
        time.sleep(interval)


def main() -> int:
    ap = argparse.ArgumentParser(prog="niri-place", description=__doc__.split("\n\n")[0])
    ap.add_argument("--monitor", help="connector or make/model/serial to preselect")
    ap.add_argument(
        "--watch", action="store_true", help="open the GUI when an unsaved monitor is connected"
    )
    ap.add_argument("--interval", type=float, default=2.0, help="watch poll interval, seconds")
    ap.add_argument(
        "--print", action="store_true", help="print the generated monitors.kdl and exit"
    )
    args = ap.parse_args()
    if args.print:
        print(render_kdl(load_state()), end="")
        return 0
    if args.watch:
        return watch(args.interval)
    return run_gui(args.monitor)


if __name__ == "__main__":
    sys.exit(main())
