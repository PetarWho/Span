"""Dual-pane window for Span (PySide6).

Left: this computer's folders. Right: the other computer's shared folder.
Between them, the bridge: link state, verification code and transfers.
Drag items across to copy them in either direction.
"""
import importlib
import json
import os
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QDateTime, QDir, QLocale, QMimeData, QObject, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QColor, QDesktopServices, QDrag, QFont, QFontDatabase, QFontMetrics, QIcon,
                           QPainter, QPainterPath, QPen, QPixmap, QStandardItem, QStandardItemModel)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QDialog, QFileDialog,
                               QFileIconProvider, QFileSystemModel, QFormLayout, QFrame, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QSizePolicy, QStackedWidget, QStyle, QToolButton, QTreeView, QVBoxLayout,
                               QWidget)

core = None  # the span module, set in main()

REMOTE_MIME = "application/x-span-remote-items"

# Design tokens. Cobalt always means "this computer", jade "the other one".
C = {
    "paper": "#F2F4F6", "surface": "#FFFFFF", "ink": "#1A1E23", "muted": "#5F6873", "line": "#D9DEE3",
    "hover": "#F5F7F9", "local": "#2B59C3", "local_tint": "#E2E9F8", "remote": "#0E8A6E",
    "remote_tint": "#DCF0E9", "bridge": "#262B33", "bridge_ink": "#E8ECF0", "bridge_muted": "#9AA3AD",
    "bridge_line": "#3B424C", "danger": "#B3261E",
}


# ------------------------------------------------------------------- plumbing

class Bus(QObject):
    """Carries callables from worker threads to the GUI thread."""
    call = Signal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(lambda fn: fn())


BUS = None


def ui(fn):
    BUS.call.emit(fn)


def background(fn, done=None, fail=None):
    def run():
        try:
            result = fn()
        except Exception as e:  # noqa: BLE001
            if fail:
                ui(lambda e=e: fail(e))
            return
        if done:
            ui(lambda: done(result))
    threading.Thread(target=run, daemon=True).start()


def wait_for_ui(fn):
    """Run fn on the GUI thread from a worker thread and return its result."""
    done, box = threading.Event(), {}

    def wrapper():
        try:
            box["r"] = fn()
        finally:
            done.set()
    ui(wrapper)
    done.wait()
    return box.get("r")


def pick_font(candidates, fallback):
    available = set(QFontDatabase.families())
    return next((f for f in candidates if f in available), fallback)


def display_font(size, weight=QFont.Weight.DemiBold):
    f = QFont(FONTS["display"])
    f.setPointSizeF(size)
    f.setWeight(weight)
    return f


FONTS = {}


def stylesheet():
    t = dict(C, ui=FONTS["ui"], display=FONTS["display"])
    return """
    QWidget {{ font-family: "{ui}"; font-size: 10pt; color: {ink}; }}
    QMainWindow, #root {{ background: {paper}; }}
    QToolTip {{ background: {bridge}; color: {bridge_ink}; border: none; padding: 4px 6px; }}

    #pane {{ background: {surface}; border: 1px solid {line}; border-radius: 10px; }}
    #role, #hint {{ color: {muted}; }}
    #appName {{ font-family: "{display}"; font-size: 13pt; font-weight: 600; }}
    #machine {{ font-family: "{display}"; font-size: 16pt; font-weight: 600; }}
    #deviceName {{ font-family: "{display}"; font-size: 12.5pt; font-weight: 600; }}
    #sectionTitle {{ font-family: "{display}"; font-size: 12pt; font-weight: 600; }}
    #dialogTitle {{ font-family: "{display}"; font-size: 15pt; font-weight: 600; }}
    #dialogCode {{ font-family: "{display}"; font-size: 26pt; font-weight: 400; }}
    #bridgeState {{ font-family: "{display}"; font-size: 12pt; font-weight: 600; }}
    #code {{ font-family: "{display}"; font-size: 30pt; font-weight: 400; }}
    #status {{ color: {muted}; }}

    QLineEdit {{ background: {paper}; border: 1px solid {line}; border-radius: 6px; padding: 5px 8px;
                selection-background-color: {local}; }}
    QLineEdit:focus {{ border: 1px solid {local}; background: {surface}; }}

    QPushButton, QToolButton {{ background: {surface}; border: 1px solid {line}; border-radius: 6px;
                               padding: 5px 12px; }}
    QToolButton {{ padding: 4px 6px; }}
    QPushButton:hover, QToolButton:hover {{ background: {hover}; }}
    QPushButton:focus, QToolButton:focus {{ border: 1px solid {local}; }}
    QPushButton:disabled {{ color: #A3ABB4; }}
    QPushButton#primaryLocal {{ background: {local}; color: white; border: 1px solid {local}; }}
    QPushButton#primaryRemote {{ background: {remote}; color: white; border: 1px solid {remote}; }}
    QPushButton#primaryLocal:hover {{ background: #244CA8; }}
    QPushButton#primaryRemote:hover {{ background: #0B765E; }}
    QPushButton#warn {{ border: 1px solid #E3B4AF; color: {danger}; }}

    QTreeView, QListWidget {{ background: {surface}; border: 1px solid transparent; border-radius: 6px;
                             outline: 0; }}
    QTreeView::item {{ height: 30px; padding-left: 2px; }}
    QTreeView::item:hover, QListWidget::item:hover {{ background: {hover}; }}
    QTreeView#localView::item:selected {{ background: {local_tint}; color: {ink}; }}
    QTreeView#remoteView::item:selected, QListWidget::item:selected {{ background: {remote_tint}; color: {ink}; }}
    QTreeView#localView:focus {{ border: 1px solid {local}; }}
    QTreeView#remoteView:focus, QListWidget:focus {{ border: 1px solid {remote}; }}
    QTreeView[drop="true"] {{ border: 2px dashed {remote}; background: #F6FBF9; }}
    QTreeView#localView[drop="true"] {{ border: 2px dashed {local}; background: #F6F8FD; }}
    QListWidget::item {{ border-bottom: 1px solid {line}; }}
    QHeaderView::section {{ background: {surface}; color: {muted}; border: none;
                           border-bottom: 1px solid {line}; padding: 6px 6px; }}

    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: #C9D0D7; border-radius: 3px; min-height: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar:horizontal {{ height: 0; }}

    #bridge {{ background: {bridge}; border-radius: 10px; }}
    #bridge QLabel {{ color: {bridge_ink}; }}
    #bridge QLabel#bridgeMuted {{ color: {bridge_muted}; }}
    #bridge QPushButton, #bridge QToolButton {{ background: transparent; color: {bridge_ink};
        border: 1px solid {bridge_line}; padding: 3px 8px; }}
    #bridge QPushButton:hover, #bridge QToolButton:hover {{ background: #323843; }}
    #bridge QProgressBar {{ background: {bridge_line}; border: none; border-radius: 1px; }}
    #bridge QProgressBar#toRemote::chunk {{ background: #6E95F0; border-radius: 1px; }}
    #bridge QProgressBar#toLocal::chunk {{ background: #3FC4A3; border-radius: 1px; }}
    #bridge QProgressBar#failed::chunk {{ background: #E07A73; }}

    QDialog {{ background: {surface}; }}
    #codeBox {{ background: {paper}; border-radius: 8px; }}
    QPlainTextEdit {{ background: {surface}; border: 1px solid {line}; border-radius: 6px; }}
    """.format(**t)


def repolish(w):
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


def drag_pixmap(text, color):
    f = QFont(FONTS["ui"])
    f.setPointSizeF(9.5)
    fm = QFontMetrics(f)
    w, h = fm.horizontalAdvance(text) + 28, fm.height() + 14
    pm = QPixmap(w, h)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawRoundedRect(QRectF(0, 0, w, h), 7, 7)
    p.setPen(QColor("white"))
    p.setFont(f)
    p.drawText(QRectF(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, text)
    p.end()
    return pm


def items_text(names):
    return names[0] if len(names) == 1 else f"{len(names)} items"


# -------------------------------------------------------------------- widgets

class Dot(QWidget):
    def __init__(self, color, size=10):
        super().__init__()
        self.color = QColor(color)
        self.setFixedSize(size, size)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.color)
        p.drawEllipse(self.rect())


class PaneHeader(QWidget):
    def __init__(self, color, name, role):
        super().__init__()
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        h.addWidget(Dot(color, 11), 0, Qt.AlignmentFlag.AlignVCenter)
        v = QVBoxLayout()
        v.setSpacing(0)
        self.name = QLabel(name)
        self.name.setObjectName("machine")
        self.role = QLabel(role)
        self.role.setObjectName("role")
        v.addWidget(self.name)
        v.addWidget(self.role)
        h.addLayout(v, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(6)
        h.addLayout(self.actions)


def tool(text, tip, slot):
    b = QPushButton(text)
    b.setToolTip(tip)
    b.clicked.connect(slot)
    return b


class LinkLine(QWidget):
    """Horizontal link from this computer (left) to the other one (right)."""

    def __init__(self):
        super().__init__()
        self.state = "idle"
        self.setFixedHeight(36)

    def set_state(self, state):
        self.state = state
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r, y = 5, self.height() / 2
        x0, x1 = r + 1, self.width() - r - 1
        pen = QPen(QColor(C["bridge_ink"] if self.state == "connected" else C["bridge_muted"]), 1.5)
        if self.state != "connected":
            pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawLine(int(x0 + r), int(y), int(x1 - r), int(y))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(C["local"]).lighter(135))
        p.drawEllipse(QRectF(x0 - r, y - r, 2 * r, 2 * r))
        p.setBrush(QColor(C["remote"]).lighter(135) if self.state == "connected" else QColor(C["bridge_line"]))
        p.drawEllipse(QRectF(x1 - r, y - r, 2 * r, 2 * r))
        if self.state == "connected":  # a small padlock in the middle of the link
            cx = self.width() / 2
            p.setBrush(QColor(C["bridge"]))
            p.drawRect(QRectF(cx - 12, y - 12, 24, 24))
            p.setBrush(QColor(C["bridge_ink"]))
            p.drawRoundedRect(QRectF(cx - 6, y - 2, 12, 9), 2, 2)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(C["bridge_ink"]), 1.6))
            path = QPainterPath()
            path.moveTo(cx - 3.5, y - 2)
            path.lineTo(cx - 3.5, y - 5)
            path.arcTo(QRectF(cx - 3.5, y - 8.5, 7, 7), 180, -180)
            path.lineTo(cx + 3.5, y - 2)
            p.drawPath(path)


class TransferRow(QFrame):
    """One transfer in the bridge. to_remote: data moves left to right."""

    def __init__(self, title, to_remote, ctl=None):
        super().__init__()
        self.t0 = None
        self.last = 0.0
        self.finished = False
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(8)
        arrow = QLabel("→" if to_remote else "←")
        arrow.setStyleSheet(f"color: {'#6E95F0' if to_remote else '#3FC4A3'}; "
                            f"font-family: '{FONTS['display']}'; font-size: 15pt;")
        arrow.setAccessibleName("to the other computer" if to_remote else "to this computer")
        h.addWidget(arrow, 0, Qt.AlignmentFlag.AlignTop)
        v = QVBoxLayout()
        v.setSpacing(3)
        top = QHBoxLayout()
        self.title = QLabel()
        self.title.setToolTip(title)
        self._full_title = title
        top.addWidget(self.title, 1)
        self.cancel_btn = None
        if ctl:
            self.cancel_btn = QToolButton()
            self.cancel_btn.setText("×")
            self.cancel_btn.setToolTip("Cancel this transfer")
            self.cancel_btn.setAccessibleName("Cancel this transfer")
            self.cancel_btn.clicked.connect(ctl.cancel)
            top.addWidget(self.cancel_btn)
        v.addLayout(top)
        self.bar = QProgressBar()
        self.bar.setObjectName("toRemote" if to_remote else "toLocal")
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(3)
        v.addWidget(self.bar)
        self.detail = QLabel("Waiting...")
        self.detail.setObjectName("bridgeMuted")
        self.detail.setWordWrap(True)
        v.addWidget(self.detail)
        h.addLayout(v, 1)
        QTimer.singleShot(0, self._elide)

    def _elide(self):
        fm = QFontMetrics(self.title.font())
        self.title.setText(fm.elidedText(self._full_title, Qt.TextElideMode.ElideMiddle, max(60, self.title.width())))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def progress(self, done, total):
        now = time.time()
        self.t0 = self.t0 or now
        self.bar.setValue(int(1000 * done / total) if total else 1000)
        speed = done / max(now - self.t0, 1e-3)
        self.detail.setText(f"{core.human(done)} of {core.human(total)}, {core.human(speed)}/s")

    def finish(self, ok, text):
        self.finished = True
        if ok:
            self.bar.setValue(1000)
        else:
            self.bar.setObjectName("failed")
            repolish(self.bar)
        self.detail.setText(text)
        if self.cancel_btn:
            self.cancel_btn.hide()


def throttled(row):
    """A progress callback for worker threads that updates a TransferRow ~10x/s."""
    state = {"last": 0.0}

    def cb(done, total, _name):
        now = time.time()
        if now - state["last"] >= 0.1 or done >= total:
            state["last"] = now
            ui(lambda: row.progress(done, total))
    return cb


class Bridge(QFrame):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setObjectName("bridge")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(208)
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 18, 16, 16)
        v.setSpacing(10)
        self.state = QLabel("Not connected")
        self.state.setObjectName("bridgeState")
        v.addWidget(self.state)
        self.link = LinkLine()
        v.addWidget(self.link)
        self.code_box = QWidget()
        cb = QVBoxLayout(self.code_box)
        cb.setContentsMargins(0, 4, 0, 4)
        cb.setSpacing(2)
        cap = QLabel("Verification code")
        cap.setObjectName("bridgeMuted")
        cb.addWidget(cap)
        self.code = QLabel()
        self.code.setObjectName("code")
        self.code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cb.addWidget(self.code)
        hint = QLabel("It should match the code on the other screen.")
        hint.setObjectName("bridgeMuted")
        hint.setWordWrap(True)
        cb.addWidget(hint)
        v.addWidget(self.code_box)
        self.code_box.hide()
        self.idle_hint = QLabel("Pick a computer on the right, then drag files across to copy them.")
        self.idle_hint.setObjectName("bridgeMuted")
        self.idle_hint.setWordWrap(True)
        v.addWidget(self.idle_hint)
        self.tx = QVBoxLayout()
        self.tx.setSpacing(10)
        v.addSpacing(6)
        v.addLayout(self.tx)
        v.addStretch(1)
        self.access = QVBoxLayout()
        self.access.setSpacing(8)
        v.addLayout(self.access)
        self.access_rows = {}

    def set_link(self, state, code=None, name=""):
        self.link.set_state(state)
        self.state.setText({"idle": "Not connected", "waiting": f"Waiting for {name}",
                            "connected": "Encrypted link"}[state])
        self.code.setText(code or "")
        self.code_box.setVisible(bool(code) and state != "idle")
        self.idle_hint.setVisible(state == "idle")

    def add_transfer(self, title, to_remote, ctl=None):
        row = TransferRow(title, to_remote, ctl)
        self.tx.addWidget(row)
        # keep the bridge tidy: at most five rows, oldest finished ones go first
        rows = [self.tx.itemAt(i).widget() for i in range(self.tx.count())]
        for old in [r for r in rows if r.finished][: max(0, len(rows) - 5)]:
            old.setParent(None)
            old.deleteLater()
        return row

    def add_access(self, sid, name):
        w = QWidget()
        h = QVBoxLayout(w)
        h.setContentsMargins(0, 8, 0, 0)
        h.setSpacing(4)
        lbl = QLabel(f"{name} has your shared folder open")
        lbl.setWordWrap(True)
        h.addWidget(lbl)
        b = QPushButton("Disconnect")
        b.setAccessibleName(f"Disconnect {name}")
        b.clicked.connect(lambda: self.win.recv.close_session(sid))
        h.addWidget(b, 0, Qt.AlignmentFlag.AlignLeft)
        self.access.addWidget(w)
        self.access_rows[sid] = w

    def remove_access(self, sid):
        w = self.access_rows.pop(sid, None)
        if w:
            w.setParent(None)
            w.deleteLater()


# ---------------------------------------------------------------------- panes

class LocalView(QTreeView):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.path = ""
        self.setObjectName("localView")
        self.fs = QFileSystemModel(self)
        self.fs.setReadOnly(True)
        self.fs.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot | QDir.Filter.AllDirs)
        self.fs.setRootPath("")
        self.setModel(self.fs)
        self.setColumnHidden(2, True)
        self.setRootIsDecorated(False)
        self.setItemsExpandable(False)
        self.setUniformRowHeights(True)
        self.setSortingEnabled(True)
        self.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        hdr = self.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setStretchLastSection(False)
        hdr.resizeSection(1, 90)
        hdr.resizeSection(3, 150)
        self.doubleClicked.connect(self._activate)

    def set_path(self, path):
        self.path = path
        self.fs.setRootPath(path)
        self.setRootIndex(self.fs.index(path))

    def _activate(self, idx):
        path = self.fs.filePath(idx)
        if self.fs.isDir(idx):
            self.win.local_go(path)
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def startDrag(self, actions):
        paths = [self.fs.filePath(i) for i in self.selectionModel().selectedRows(0)]
        if not paths:
            return
        drag = QDrag(self)
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(p) for p in paths])
        drag.setMimeData(md)
        drag.setPixmap(drag_pixmap(items_text([Path(p).name for p in paths]), C["local"]))
        drag.exec(Qt.DropAction.CopyAction)

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(REMOTE_MIME):
            e.acceptProposedAction()
            self.setProperty("drop", True)
            repolish(self)
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if e.mimeData().hasFormat(REMOTE_MIME):
            e.setDropAction(Qt.DropAction.CopyAction)
            e.accept()
        else:
            e.ignore()

    def dragLeaveEvent(self, e):
        self.setProperty("drop", False)
        repolish(self)

    def dropEvent(self, e):
        self.setProperty("drop", False)
        repolish(self)
        if not e.mimeData().hasFormat(REMOTE_MIME):
            return e.ignore()
        paths = json.loads(bytes(e.mimeData().data(REMOTE_MIME)).decode("utf-8"))["paths"]
        idx = self.indexAt(e.position().toPoint())
        dest = self.fs.filePath(idx) if idx.isValid() and self.fs.isDir(idx) else self.path
        if not dest or not os.path.isdir(dest):
            self.win.set_status("Open a folder on the left first, then drop the files there.")
            return e.ignore()
        e.acceptProposedAction()
        self.win.download(paths, dest)


class RemoteView(QTreeView):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setObjectName("remoteView")
        self.icons = QFileIconProvider()
        self.items = QStandardItemModel(0, 3, self)
        self.items.setHorizontalHeaderLabels(["Name", "Size", "Date Modified"])
        self.setModel(self.items)
        self.setRootIsDecorated(False)
        self.setItemsExpandable(False)
        self.setUniformRowHeights(True)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        hdr = self.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setStretchLastSection(False)
        hdr.resizeSection(1, 90)
        hdr.resizeSection(2, 150)
        hdr.setSectionsClickable(False)
        self.empty_text = ""
        self.doubleClicked.connect(self._activate)

    def populate(self, entries):
        self.items.removeRows(0, self.items.rowCount())
        folder = self.icons.icon(QFileIconProvider.IconType.Folder)
        file_ = self.icons.icon(QFileIconProvider.IconType.File)
        for e in entries:
            name = QStandardItem(folder if e["dir"] else file_, e["name"])
            name.setData(e, Qt.ItemDataRole.UserRole)
            size = QStandardItem("" if e["dir"] else core.human(e["size"]))
            size.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            date = QStandardItem(QLocale().toString(QDateTime.fromSecsSinceEpoch(int(e["mtime"])),
                                                    QLocale.FormatType.ShortFormat))
            for it in (name, size, date):
                it.setForeground(QColor(C["ink"] if it is name else C["muted"]))
            self.items.appendRow([name, size, date])
        self.empty_text = "This folder is empty.\nDrag files here from the left to copy them over."
        self.viewport().update()

    def entry(self, idx):
        return self.items.item(idx.row(), 0).data(Qt.ItemDataRole.UserRole) if idx.isValid() else None

    def selected_entries(self):
        return [self.entry(i) for i in self.selectionModel().selectedRows(0)]

    def _activate(self, idx):
        e = self.entry(idx)
        if e and e["dir"]:
            self.win.remote_go(self.win.remote_join(e["name"]))
        elif e:
            self.win.download([self.win.remote_join(e["name"])], self.win.local.path or str(Path.home()))

    def startDrag(self, actions):
        ents = self.selected_entries()
        if not ents:
            return
        md = QMimeData()
        md.setData(REMOTE_MIME, json.dumps({"paths": [self.win.remote_join(e["name"]) for e in ents]}).encode())
        drag = QDrag(self)
        drag.setMimeData(md)
        drag.setPixmap(drag_pixmap(items_text([e["name"] for e in ents]), C["remote"]))
        drag.exec(Qt.DropAction.CopyAction)

    @staticmethod
    def _local_paths(md):
        return [u.toLocalFile() for u in md.urls() if u.isLocalFile()] if md.hasUrls() else []

    def dragEnterEvent(self, e):
        if self._local_paths(e.mimeData()):
            e.acceptProposedAction()
            self.setProperty("drop", True)
            repolish(self)
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.setDropAction(Qt.DropAction.CopyAction)
            e.accept()
        else:
            e.ignore()

    def dragLeaveEvent(self, e):
        self.setProperty("drop", False)
        repolish(self)

    def dropEvent(self, e):
        self.setProperty("drop", False)
        repolish(self)
        paths = self._local_paths(e.mimeData())
        if not paths:
            return e.ignore()
        target = self.entry(self.indexAt(e.position().toPoint()))
        dest = self.win.remote_join(target["name"]) if target and target["dir"] else self.win.remote_path
        e.acceptProposedAction()
        self.win.upload(paths, dest)

    def paintEvent(self, e):
        super().paintEvent(e)
        if self.items.rowCount() == 0 and self.empty_text:
            p = QPainter(self.viewport())
            p.setPen(QColor(C["muted"]))
            p.drawText(self.viewport().rect().adjusted(24, 24, -24, -24),
                       Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.empty_text)


class DeviceItem(QWidget):
    def __init__(self, name, where):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 9, 12, 9)
        v.setSpacing(1)
        n = QLabel(name)
        n.setObjectName("deviceName")
        w = QLabel(where)
        w.setObjectName("role")
        v.addWidget(n)
        v.addWidget(w)


# -------------------------------------------------------------------- dialogs

class AskDialog(QDialog):
    """Approval prompt with the verification code front and centre."""

    def __init__(self, parent, title, body, code, yes, no, details=None, path=None):
        super().__init__(parent)
        self.setWindowTitle(core.APP)
        self.setMinimumWidth(440)
        v = QVBoxLayout(self)
        v.setContentsMargins(24, 22, 24, 18)
        v.setSpacing(12)
        t = QLabel(title)
        t.setObjectName("dialogTitle")
        t.setWordWrap(True)
        v.addWidget(t)
        b = QLabel(body)
        b.setWordWrap(True)
        b.setTextFormat(Qt.TextFormat.PlainText)
        v.addWidget(b)
        if path:  # own line, shortened in the middle so the drive and folder name stay visible
            pl = QLabel(QFontMetrics(self.font()).elidedText(path, Qt.TextElideMode.ElideMiddle, 390))
            pl.setToolTip(path)
            pl.setStyleSheet("font-weight: 600;")
            v.addWidget(pl)
        if details:
            d = QLabel(details)
            d.setObjectName("role")
            d.setTextFormat(Qt.TextFormat.PlainText)
            v.addWidget(d)
        if code:
            box = QFrame()
            box.setObjectName("codeBox")
            bl = QVBoxLayout(box)
            bl.setContentsMargins(16, 12, 16, 12)
            cap = QLabel("Verification code")
            cap.setObjectName("role")
            c = QLabel(code)
            c.setObjectName("dialogCode")
            hint = QLabel("Allow only if it matches the code on the other computer's screen.")
            hint.setObjectName("role")
            hint.setWordWrap(True)
            for w in (cap, c, hint):
                bl.addWidget(w)
            v.addWidget(box)
        row = QHBoxLayout()
        row.addStretch(1)
        nb = QPushButton(no)
        yb = QPushButton(yes)
        yb.setObjectName("primaryRemote")
        nb.clicked.connect(self.reject)
        yb.clicked.connect(self.accept)
        row.addWidget(nb)
        row.addWidget(yb)
        v.addLayout(row)
        nb.setDefault(True)  # Enter must not grant access by accident
        nb.setFocus()


class SettingsDialog(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        cfg = win.cfg
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)
        v = QVBoxLayout(self)
        v.setContentsMargins(22, 20, 22, 16)
        v.setSpacing(14)
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        self.name = QLineEdit(win.disc.name)
        form.addRow("Computer name", self.name)
        share_row = QHBoxLayout()
        self.share = QLineEdit(win.share)
        self.share.setReadOnly(True)
        pick = QPushButton("Change...")
        pick.clicked.connect(self._pick_share)
        share_row.addWidget(self.share, 1)
        share_row.addWidget(pick)
        form.addRow("Shared folder", share_row)
        note = QLabel("Computers you allow can see, download and add files in this folder only. "
                      "Files sent to you land here too.")
        note.setObjectName("hint")
        note.setWordWrap(True)
        form.addRow("", note)
        self.auto = QCheckBox("Accept files sent to me without asking")
        self.auto.setChecked(bool(cfg.get("auto_accept")))
        form.addRow("", self.auto)
        v.addLayout(form)

        relay_title = QLabel("Relay server")
        relay_title.setObjectName("sectionTitle")
        v.addWidget(relay_title)
        rnote = QLabel("Optional. A relay lets you reach computers outside this network. Transfers stay "
                       "end-to-end encrypted, and computers on this network are still reached directly.")
        rnote.setObjectName("hint")
        rnote.setWordWrap(True)
        v.addWidget(rnote)
        rform = QFormLayout()
        rform.setHorizontalSpacing(14)
        self.relay_on = QCheckBox("Use a relay server")
        self.relay_on.setChecked(bool(cfg.get("relay_enabled")))
        self.relay_addr = QLineEdit(cfg.get("relay_address", ""))
        self.relay_addr.setPlaceholderText("myserver.com:50507")
        self.relay_pw = QLineEdit(cfg.get("relay_password", ""))
        self.relay_pw.setEchoMode(QLineEdit.EchoMode.Password)
        rform.addRow("", self.relay_on)
        rform.addRow("Address", self.relay_addr)
        rform.addRow("Password", self.relay_pw)
        v.addLayout(rform)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        save = QPushButton("Save")
        save.setObjectName("primaryLocal")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self._save)
        row.addWidget(cancel)
        row.addWidget(save)
        v.addLayout(row)

    def _pick_share(self):
        d = QFileDialog.getExistingDirectory(self, "Choose the folder to share", self.share.text())
        if d:
            self.share.setText(str(Path(d)))

    def _save(self):
        addr = self.relay_addr.text().strip()
        if self.relay_on.isChecked():
            try:
                core.parse_target(addr, core.RELAY_PORT)
            except ValueError as e:
                QMessageBox.warning(self, "Relay server", f"Check the relay address: {e}")
                return
        self.win.apply_settings(name=self.name.text().strip(), share=self.share.text(),
                                auto=self.auto.isChecked(), relay_on=self.relay_on.isChecked(),
                                relay_addr=addr, relay_pw=self.relay_pw.text())
        self.accept()


# ---------------------------------------------------------------- main window

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = core.load_config()
        self.share = self.cfg.get("dest") or str(core.default_download_dir())
        Path(self.share).mkdir(parents=True, exist_ok=True)
        self.session = None
        self.pending = None      # SendControl for a connection waiting on approval
        self.remote_path = ""
        self.peers = []
        self.relay = None
        self.log_lines = []
        self.rx_rows = {}

        self.disc = core.Discovery(self.cfg.get("name") or core.socket.gethostname()).start()
        self.recv = core.Receiver(lambda: self.share, self._ask_push, self._rx_event,
                                  ask_browse=self._ask_browse, get_name=lambda: self.disc.name)

        self.setWindowTitle(core.APP)
        self.resize(1220, 740)
        self.setMinimumSize(940, 560)
        self._build()
        self.local_go(self.cfg.get("last_local") or str(Path.home()))

        try:
            self.recv.start()
            self.log(f"Ready. Others on your network can find this computer as {self.disc.name}.")
        except OSError as e:
            self.log(f"Could not listen on port {core.TRANSFER_PORT} ({e}). Is another copy running? "
                     "Other computers can't reach this one directly.")
        self._start_relay()
        self.peer_timer = QTimer(self, interval=1500, timeout=self._refresh_peers)
        self.peer_timer.start()
        self.watch_timer = QTimer(self, interval=2000, timeout=self._watch_session)
        self.watch_timer.start()
        QTimer.singleShot(300, self._refresh_peers)
        background(lambda: core.firewall_status(self.cfg), lambda s: self._show_firewall(s, offer=True))

    # ---- layout
    def _build(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(16, 12, 16, 10)
        outer.setSpacing(10)

        top = QHBoxLayout()
        name = QLabel(core.APP)
        name.setObjectName("appName")
        top.addWidget(name)
        top.addStretch(1)
        self.fw_btn = QPushButton("Allow through firewall")
        self.fw_btn.setObjectName("warn")
        self.fw_btn.setToolTip("Other computers can't reach this one until the firewall allows it.")
        self.fw_btn.clicked.connect(self._firewall_allow)
        self.fw_btn.hide()
        top.addWidget(self.fw_btn)
        self.relay_label = QLabel()
        self.relay_label.setObjectName("role")
        top.addWidget(self.relay_label)
        settings = QPushButton("Settings")
        settings.clicked.connect(lambda: SettingsDialog(self).exec())
        top.addWidget(settings)
        outer.addLayout(top)

        body = QHBoxLayout()
        body.setSpacing(10)
        body.addWidget(self._build_local(), 1)
        self.bridge = Bridge(self)
        body.addWidget(self.bridge)
        body.addWidget(self._build_remote(), 1)
        outer.addLayout(body, 1)

        bottom = QHBoxLayout()
        self.status = QLabel()
        self.status.setObjectName("status")
        bottom.addWidget(self.status, 1)
        act = QPushButton("Activity")
        act.clicked.connect(self._show_activity)
        bottom.addWidget(act)
        outer.addLayout(bottom)

    def _build_local(self):
        pane = QFrame()
        pane.setObjectName("pane")
        v = QVBoxLayout(pane)
        v.setContentsMargins(16, 14, 16, 12)
        v.setSpacing(10)
        self.local_header = PaneHeader(C["local"], self.disc.name, "This computer")
        share_btn = QPushButton("Shared folder")
        share_btn.setToolTip("Show the folder other computers can open")
        share_btn.clicked.connect(lambda: self.local_go(self.share))
        self.local_header.actions.addWidget(share_btn)
        v.addWidget(self.local_header)
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(tool("Up", "Go to the parent folder", self.local_up))
        self.local_path = QLineEdit()
        self.local_path.setAccessibleName("Folder on this computer")
        self.local_path.returnPressed.connect(lambda: self.local_go(self.local_path.text().strip()))
        row.addWidget(self.local_path, 1)
        v.addLayout(row)
        self.local = LocalView(self)
        v.addWidget(self.local, 1)
        return pane

    def _build_remote(self):
        pane = QFrame()
        pane.setObjectName("pane")
        v = QVBoxLayout(pane)
        v.setContentsMargins(16, 14, 16, 12)
        self.remote_stack = QStackedWidget()
        v.addWidget(self.remote_stack)

        # page 0: choose a computer
        pick = QWidget()
        pv = QVBoxLayout(pick)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.setSpacing(10)
        pv.addWidget(PaneHeader(C["remote"], "Other computer", "Choose one to open its shared folder"))
        self.devices = QListWidget()
        self.devices.setAccessibleName("Computers found")
        self.devices.itemDoubleClicked.connect(lambda _i: self._connect_selected())
        self.devices.itemActivated.connect(lambda _i: self._connect_selected())
        pv.addWidget(self.devices, 1)
        self.no_devices = QLabel("No computers found yet. Open Span on the other computer, "
                                 "or type its address below.")
        self.no_devices.setObjectName("hint")
        self.no_devices.setWordWrap(True)
        pv.addWidget(self.no_devices)
        row = QHBoxLayout()
        self.addr = QLineEdit()
        self.addr.setPlaceholderText("IP address, for example 192.168.1.20")
        self.addr.setAccessibleName("Address of the other computer")
        self.addr.returnPressed.connect(self._connect_typed)
        row.addWidget(self.addr, 1)
        self.open_btn = QPushButton("Open")
        self.open_btn.setObjectName("primaryRemote")
        self.open_btn.clicked.connect(self._connect_selected_or_typed)
        row.addWidget(self.open_btn)
        pv.addLayout(row)
        self.remote_stack.addWidget(pick)

        # page 1: waiting for approval
        wait = QWidget()
        wv = QVBoxLayout(wait)
        wv.setContentsMargins(0, 0, 0, 0)
        self.wait_header = PaneHeader(C["remote"], "", "Waiting for permission")
        wv.addWidget(self.wait_header)
        wv.addStretch(1)
        self.wait_text = QLabel()
        self.wait_text.setWordWrap(True)
        self.wait_text.setAlignment(Qt.AlignmentFlag.AlignLeft)
        wv.addWidget(self.wait_text)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self._cancel_connect)
        wv.addWidget(cancel, 0, Qt.AlignmentFlag.AlignLeft)
        wv.addStretch(2)
        self.remote_stack.addWidget(wait)

        # page 2: browsing
        browse = QWidget()
        bv = QVBoxLayout(browse)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(10)
        self.remote_header = PaneHeader(C["remote"], "", "Shared folder")
        disc = QPushButton("Disconnect")
        disc.clicked.connect(self.disconnect)
        self.remote_header.actions.addWidget(disc)
        bv.addWidget(self.remote_header)
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(tool("Up", "Go to the parent folder", self.remote_up))
        self.crumb = QLineEdit()
        self.crumb.setReadOnly(True)
        self.crumb.setAccessibleName("Folder on the other computer")
        row.addWidget(self.crumb, 1)
        row.addWidget(tool("New folder", "Create a folder on the other computer", self.remote_mkdir))
        row.addWidget(tool("Refresh", "Reload this folder", lambda: self.remote_go(self.remote_path)))
        bv.addLayout(row)
        self.remote = RemoteView(self)
        bv.addWidget(self.remote, 1)
        self.remote_stack.addWidget(browse)
        return pane

    # ---- small helpers
    def log(self, text):
        self.log_lines.append(time.strftime("%H:%M:%S  ") + text)
        self.log_lines = self.log_lines[-500:]
        self.set_status(text)

    def set_status(self, text):
        self.status.setText(text)

    def _show_activity(self):
        d = QDialog(self)
        d.setWindowTitle("Activity")
        d.resize(640, 380)
        v = QVBoxLayout(d)
        t = QPlainTextEdit("\n".join(self.log_lines))
        t.setReadOnly(True)
        v.addWidget(t)
        t.moveCursor(t.textCursor().MoveOperation.End)
        d.exec()

    # ---- local pane
    def local_go(self, path):
        if path and not os.path.isdir(path):
            self.set_status(f"{path} is not a folder on this computer.")
            self.local_path.setText(self.local.path)
            return
        self.local.set_path(path)
        self.local_path.setText(path or "This PC")

    def local_up(self):
        p = self.local.path
        if not p:
            return
        parent = str(Path(p).parent)
        self.local_go("" if parent == p else parent)

    # ---- remote pane: connecting
    def _refresh_peers(self):
        local = [dict(p, via="lan") for p in self.disc.list_peers()]
        ids = {p["id"] for p in local}
        remote = [dict(p, via="relay") for p in (self.relay.peers if self.relay else []) if p["id"] not in ids]
        peers = local + sorted(remote, key=lambda p: p["name"].lower())
        key = [(p["via"], p["id"], p["name"], p.get("ip")) for p in peers]
        if key == [(p["via"], p["id"], p["name"], p.get("ip")) for p in self.peers]:
            return
        sel = self.devices.currentItem()
        sel_id = sel.data(Qt.ItemDataRole.UserRole)["id"] if sel else None
        self.peers = peers
        self.devices.clear()
        for p in peers:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, p)
            w = DeviceItem(p["name"], p["ip"] if p["via"] == "lan" else "Through your relay server")
            item.setSizeHint(w.sizeHint())
            item.setText("")
            item.setToolTip(f"Double-click to open {p['name']}'s shared folder")
            self.devices.addItem(item)
            self.devices.setItemWidget(item, w)
            if p["id"] == sel_id:
                self.devices.setCurrentItem(item)
        self.no_devices.setVisible(not peers)

    def _connect_selected_or_typed(self):
        if self.addr.text().strip():
            self._connect_typed()
        else:
            self._connect_selected()

    def _connect_selected(self):
        item = self.devices.currentItem()
        if not item:
            self.set_status("Choose a computer in the list, or type its address.")
            return
        p = item.data(Qt.ItemDataRole.UserRole)
        if p["via"] == "lan":
            self._connect(p["name"], host=p["ip"], port=p["port"])
        elif self.relay:
            relay, rid = self.relay, p["id"]
            self._connect(p["name"], connect=lambda: relay.connect_to(rid))

    def _connect_typed(self):
        try:
            host, port = core.parse_target(self.addr.text())
        except ValueError as e:
            self.set_status(str(e))
            return
        self._connect(self.addr.text().strip(), host=host, port=port)

    def _connect(self, label, host=None, port=None, connect=None):
        if self.session:
            self.disconnect()
        ctl = core.SendControl()
        self.pending = ctl
        sess = core.RemoteSession(self.disc.name, host, port, connect)
        self.wait_header.name.setText(label)
        self.wait_text.setText(f"Connecting to {label}...")
        self.remote_stack.setCurrentIndex(1)
        self.bridge.set_link("waiting", name=label)

        def on_code(code):
            def show():
                self.bridge.set_link("waiting", code, label)
                self.wait_text.setText(f"{label} has been asked to let you open their shared folder.\n\n"
                                       f"Their screen shows the verification code {code}. It should match "
                                       "the code in the middle of this window.")
            ui(show)

        def done(s):
            if self.pending is not ctl:
                s.close()
                return
            self.pending = None
            self.session = s
            self.remote_header.name.setText(s.remote_name)
            self.bridge.set_link("connected", self.bridge.code.text(), s.remote_name)
            self.remote_stack.setCurrentIndex(2)
            self.log(f"Connected to {s.remote_name}. Drag files across to copy them.")
            self.remote_go("")

        def fail(e):
            if self.pending is not ctl:
                return
            self.pending = None
            self.remote_stack.setCurrentIndex(0)
            self.bridge.set_link("idle")
            msg = "Cancelled." if isinstance(e, core.Cancelled) else f"Couldn't open {label}: {core.friendly(e)}"
            self.log(msg)

        background(lambda: sess.open(on_code, ctl), done, fail)

    def _cancel_connect(self):
        if self.pending:
            self.pending.cancel()

    def disconnect(self):
        if self.session:
            s, self.session = self.session, None
            background(s.close)
            self.log(f"Disconnected from {s.remote_name}.")
        self.remote.populate([])
        self.remote_stack.setCurrentIndex(0)
        self.bridge.set_link("idle")

    def _watch_session(self):
        if self.session and not self.session.alive():
            name = self.session.remote_name
            self.session = None
            self.remote_stack.setCurrentIndex(0)
            self.bridge.set_link("idle")
            self.log(f"The connection to {name} closed.")

    # ---- remote pane: browsing
    def remote_join(self, name):
        return f"{self.remote_path}/{name}".lstrip("/")

    def remote_go(self, rel):
        s = self.session
        if not s:
            return

        def done(entries):
            if s is not self.session:
                return
            self.remote_path = rel
            self.crumb.setText(" / ".join([s.root_name] + [p for p in rel.split("/") if p]))
            self.remote.populate(entries)

        background(lambda: s.list(rel), done, lambda e: self.log(f"Couldn't open that folder: {core.friendly(e)}"))

    def remote_up(self):
        if self.remote_path:
            self.remote_go(self.remote_path.rpartition("/")[0])

    def remote_mkdir(self):
        if not self.session:
            return
        name, ok = QInputDialog.getText(self, "New folder", "Folder name on the other computer:")
        name = name.strip()
        if not ok or not name:
            return
        s, rel = self.session, self.remote_join(name)
        background(lambda: s.mkdir(rel), lambda _r: self.remote_go(self.remote_path),
                   lambda e: self.log(f"Couldn't create the folder: {core.friendly(e)}"))

    # ---- transfers
    def upload(self, paths, dest_rel):
        s = self.session
        if not s:
            return
        title = items_text([Path(p).name for p in paths])
        ctl = core.SendControl()
        row = self.bridge.add_transfer(title, to_remote=True, ctl=ctl)
        where = " / ".join([s.root_name] + [p for p in dest_rel.split("/") if p])
        self.log(f"Copying {title} to {s.remote_name} ({where})...")

        def done(r):
            n, total = r
            row.finish(True, f"Copied {n} file(s), {core.human(total)}")
            self.log(f"Copied {title} to {s.remote_name}.")
            if s is self.session and dest_rel == self.remote_path:
                self.remote_go(self.remote_path)

        def fail(e):
            msg = "Cancelled" if isinstance(e, core.Cancelled) else core.friendly(e)
            row.finish(False, msg)
            self.log(f"Copying {title} failed: {msg}")

        background(lambda: s.upload(paths, dest_rel, throttled(row), ctl), done, fail)

    def download(self, rel_paths, local_dir):
        s = self.session
        if not s:
            return
        title = items_text([p.rpartition("/")[2] for p in rel_paths])
        ctl = core.SendControl()
        row = self.bridge.add_transfer(title, to_remote=False, ctl=ctl)
        self.log(f"Copying {title} from {s.remote_name} to {local_dir}...")

        def done(r):
            n, total = r
            row.finish(True, f"Copied {n} file(s), {core.human(total)}")
            self.log(f"Copied {title} from {s.remote_name} to {local_dir}.")

        def fail(e):
            msg = "Cancelled" if isinstance(e, core.Cancelled) else core.friendly(e)
            row.finish(False, msg)
            self.log(f"Copying {title} failed: {msg}")

        background(lambda: s.download(rel_paths, local_dir, throttled(row), ctl), done, fail)

    # ---- requests from other computers (called on receiver threads)
    def _ask_browse(self, sender, ip, code):
        def ask():
            self._raise()
            d = AskDialog(self, f"Let {sender} open your shared folder?",
                          f"{sender} ({ip}) will be able to see, download and add files in this folder:",
                          code, "Allow", "Deny", path=self.share)
            return d.exec() == QDialog.DialogCode.Accepted
        return bool(wait_for_ui(ask))

    def _ask_push(self, sender, ip, files, total, code):
        def ask():
            if self.cfg.get("auto_accept"):
                return True
            self._raise()
            listing = "\n".join(f"{r.as_posix()}  ({core.human(s)})" for r, s in files[:8])
            if len(files) > 8:
                listing += f"\n...and {len(files) - 8} more"
            d = AskDialog(self, f"{sender} wants to send you {len(files)} file(s)",
                          f"{core.human(total)} in total, saved to {self.share}", code, "Accept", "Decline",
                          details=listing)
            return d.exec() == QDialog.DialogCode.Accepted
        return bool(wait_for_ui(ask))

    def _raise(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _rx_event(self, kind, **i):
        def handle():
            tid = i.get("tid")
            if kind == "session_open":
                self.bridge.add_access(i["sid"], i["name"])
                self.log(f"{i['name']} opened your shared folder.")
            elif kind == "session_closed":
                self.bridge.remove_access(i["sid"])
                self.log(f"{i['name']} closed your shared folder.")
            elif kind == "start":
                incoming = i.get("direction", "in") == "in"
                row = self.bridge.add_transfer(f"{i['count']} file(s) {'from' if incoming else 'to'} "
                                               f"{i['peer'].split(' (')[0]}", to_remote=not incoming)
                self.rx_rows[tid] = row
            elif kind == "progress" and tid in self.rx_rows:
                self.rx_rows[tid].progress(i["done"], i["total"])
            elif kind == "done":
                row = self.rx_rows.pop(tid, None)
                who = i["peer"].split(" (")[0]
                if i.get("direction") == "out":
                    text = f"{who} copied {i['count']} file(s), {core.human(i['total'])} from your shared folder."
                else:
                    text = f"{who} sent {i['count']} file(s), {core.human(i['total'])} to {i['dest']}."
                if row:
                    row.finish(True, f"{core.human(i['total'])} done")
                self.log(text)
            elif kind == "error":
                row = self.rx_rows.pop(tid, None)
                if row:
                    row.finish(False, i["error"])
                self.log(f"Transfer with {i['peer']} failed: {i['error']}")
        if kind == "progress":
            # throttle: progress events arrive per megabyte
            now = time.time()
            last = self._last_rx.get(i.get("tid"), 0.0) if hasattr(self, "_last_rx") else 0.0
            if now - last < 0.1 and i["done"] < i["total"]:
                return
            self._last_rx = getattr(self, "_last_rx", {})
            self._last_rx[i.get("tid")] = now
        ui(handle)

    # ---- settings, firewall, relay
    def _save_cfg(self):
        self.cfg.update(name=self.disc.name, dest=self.share, last_local=self.local.path)
        core.save_config(self.cfg)

    def apply_settings(self, name, share, auto, relay_on, relay_addr, relay_pw):
        self.disc.name = name or core.socket.gethostname()
        self.local_header.name.setText(self.disc.name)
        self.share = share
        Path(share).mkdir(parents=True, exist_ok=True)
        relay_changed = (relay_on, relay_addr, relay_pw) != (bool(self.cfg.get("relay_enabled")),
                                                            self.cfg.get("relay_address", ""),
                                                            self.cfg.get("relay_password", ""))
        self.cfg.update(auto_accept=auto, relay_enabled=relay_on, relay_address=relay_addr, relay_password=relay_pw)
        self._save_cfg()
        if relay_changed:
            self._start_relay()
        self.log("Settings saved.")

    def _start_relay(self):
        if self.relay:
            self.relay.stop()
            self.relay = None
        address = self.cfg.get("relay_address", "")
        if not self.cfg.get("relay_enabled") or not address:
            self.relay_label.setText("Local network only")
            return

        def status(s):
            ui(lambda: self.relay_label.setText(f"Relay {s}"))
        try:
            self.relay = core.RelayClient(address, self.cfg.get("relay_password", ""), self.disc.id,
                                          lambda: self.disc.name,
                                          lambda sock: self.recv.handle_socket(sock, "via relay"), status).start()
        except ValueError as e:
            self.relay_label.setText(f"Relay address is invalid: {e}")

    def _show_firewall(self, status, offer):
        self.fw_btn.setVisible(status == "missing")
        if status == "missing" and offer and not self.cfg.get("firewall_declined"):
            r = QMessageBox.question(
                self, core.APP, "Other computers can't reach this one until the firewall allows "
                                "Span (ports 50505-50506, local network only).\n\n"
                                "Allow it now? You'll be asked for administrator permission.")
            if r == QMessageBox.StandardButton.Yes:
                self._firewall_allow()
            else:
                self.cfg["firewall_declined"] = True
                self._save_cfg()
                self.log("Firewall not changed. Use “Allow through firewall” at the top any time.")

    def _firewall_allow(self):
        self.fw_btn.setEnabled(False)
        self.fw_btn.setText("Waiting for permission...")

        def done(r):
            ok, msg = r
            self.fw_btn.setEnabled(True)
            self.fw_btn.setText("Allow through firewall")
            if ok:
                self.cfg.pop("firewall_declined", None)
            self._save_cfg()
            self.log(msg if ok else f"Firewall not changed: {msg}")
            background(lambda: core.firewall_status(self.cfg), lambda s: self._show_firewall(s, offer=False))
        background(lambda: core.firewall_allow(self.cfg), done)

    def closeEvent(self, e):
        self._save_cfg()
        if self.session:
            self.session.close()
        self.disc.stop()
        self.recv.stop()
        if self.relay:
            self.relay.stop()
        e.accept()


def main(core_module=None):
    global core, BUS
    core = core_module or importlib.import_module("span")
    if not core.load_crypto():
        raise RuntimeError("the cryptography package is missing")
    if sys.platform.startswith("win"):
        # give the app its own taskbar identity, so Windows shows our icon instead of Python's
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Span.App")
        except (AttributeError, OSError):
            pass
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(core.APP)
    app.setDesktopFileName("span")  # matches the .desktop file on Linux
    icon_file = core.asset("icon.ico" if sys.platform.startswith("win") else "icon.png")
    if icon_file.exists():
        app.setWindowIcon(QIcon(str(icon_file)))
    FONTS["ui"] = pick_font(["Segoe UI Variable Text", "Segoe UI", "Inter", "Cantarell", "Noto Sans", "Ubuntu"],
                            app.font().family())
    FONTS["display"] = pick_font(["Bahnschrift", "Barlow", "Barlow Semi Condensed", "Noto Sans Display",
                                  "DejaVu Sans Condensed"], FONTS["ui"])
    app.setStyle("Fusion")  # consistent base on Windows and Linux; the stylesheet does the rest
    app.setStyleSheet(stylesheet())
    BUS = Bus()
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
