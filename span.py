#!/usr/bin/env python3
"""Span - copy files and folders between your computers, encrypted.

Runs on Windows and Linux with Python 3.8+ and the 'cryptography' package
(the app offers to install it on first run).

    python span.py                         # open the window (GUI)
    python span.py receive                 # headless receiver (servers, SSH sessions)
    python span.py list                    # show computers found on the network
    python span.py send <name|ip> FILES... # send from the command line
    python span.py firewall                # allow the app through the firewall

How it works
  * Discovery: every running copy broadcasts a small UDP packet on port 50505
    every 2 seconds, so the others can list it by name.
  * Transfer: files are streamed over TCP port 50506, end-to-end encrypted
    (X25519 key exchange + AES-256-GCM). Both screens show a 6-digit
    verification code; if the codes match, nobody is intercepting. The
    receiver must accept each transfer (unless auto-accept is on), and incoming
    paths are sanitised so nothing can be written outside the download folder.
"""
import argparse
import hashlib
import hmac
import importlib
import ipaddress
import json
import os
import queue
import secrets
import shutil
import site
import socket
import struct
import subprocess
import sys
import threading
import time
import types
import uuid
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter.scrolledtext import ScrolledText
except ImportError:  # headless Linux without python3-tk: CLI still works
    tk = None

APP = "Span"
MAGIC = "SPAN1"
PROTO = b"SPAN1\n"  # first bytes of every encrypted connection
DISCOVERY_PORT = 50505
TRANSFER_PORT = 50506
ANNOUNCE_EVERY = 2.0
PEER_TIMEOUT = 7.0
CHUNK = 1024 * 1024
MAX_RECORD = 16 * 1024 * 1024
HANDSHAKE_TIMEOUT = 15
ACCEPT_WAIT = 300  # seconds the sender waits for the receiver to click Accept
IO_TIMEOUT = 60
CONFIG_PATH = Path.home() / ".span.json"
LEGACY_CONFIG_PATH = Path.home() / ".lanft.json"  # name before the rename to Span


# --------------------------------------------------------------------------- helpers

def default_download_dir():
    downloads = Path.home() / "Downloads"
    return (downloads if downloads.is_dir() else Path.home()) / APP


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fmt_secs(s):
    s = int(round(s))
    return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"


def local_ips():
    ips = set()
    try:  # finds the IP of the interface used for the default route; sends nothing
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.")))


def parse_target(text, default_port=TRANSFER_PORT):
    """'192.168.1.5' or '192.168.1.5:6000' or 'host' -> (host, port)."""
    text = text.strip()
    if not text:
        raise ValueError("No address given")
    host, port = text, default_port
    if text.count(":") == 1:
        host, p = text.split(":")
        if not p.isdigit() or not 0 < int(p) < 65536:
            raise ValueError(f"Invalid port in {text!r}")
        port = int(p)
    return host, port


def friendly(e):
    if isinstance(e, ConnectionRefusedError):
        return ("connection refused - is the app open on the other computer "
                "and allowed through its firewall?")
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "timed out - check the address and the other computer's firewall"
    if isinstance(e, socket.gaierror):
        return "unknown computer name or address"
    if isinstance(e, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
        return "the connection was closed by the other computer"
    return str(e) or type(e).__name__


def load_config():
    for path in (CONFIG_PATH, LEGACY_CONFIG_PATH):  # settings saved before the rename still load
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return {}


def save_config(cfg):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass


def asset(name):
    """Path to a bundled file in assets/ (also inside a PyInstaller build)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "assets" / name


def open_folder(path):
    path = str(path)
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa: S606 - local folder chosen by the user
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# ------------------------------------------------------------------ path safety

_BAD_CHARS = set('<>:"|?*\\') | {chr(i) for i in range(32)}
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def safe_rel_path(p):
    """Turn a '/'-separated path from the network into a safe relative Path.

    Rejects '..', drops absolute prefixes and replaces characters that are
    invalid on Windows, so a malicious sender cannot escape the target folder.
    """
    parts = []
    for part in str(p).split("/"):
        if part == "..":
            raise ValueError(f"unsafe path from sender: {p!r}")
        if part in ("", "."):
            continue
        part = "".join("_" if c in _BAD_CHARS else c for c in part).rstrip(" .") or "_"
        if part.split(".")[0].upper() in _RESERVED:
            part = "_" + part
        parts.append(part)
    if not parts:
        raise ValueError(f"empty path from sender: {p!r}")
    return Path(*parts)


def unique_path(path):
    if not path.exists() and not path.with_name(path.name + ".part").exists():
        return path
    stem, suffix = (path.stem, path.suffix) if path.is_file() or path.suffix else (path.name, "")
    i = 1
    while True:
        cand = path.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists() and not cand.with_name(cand.name + ".part").exists():
            return cand
        i += 1


def collect_files(paths):
    """Expand files/folders into [(absolute Path, relative '/' path)]."""
    out = []
    for p in map(Path, paths):
        p = p.resolve()
        if p.is_file():
            out.append((p, p.name))
        elif p.is_dir():
            for root, dirs, files in os.walk(p):
                dirs.sort()
                for f in sorted(files):
                    fp = Path(root) / f
                    if fp.is_file():
                        out.append((fp, (Path(p.name) / fp.relative_to(p)).as_posix()))
        else:
            raise FileNotFoundError(f"Not found: {p}")
    return out


def _no_window():
    """subprocess kwargs that stop a console window flashing up on Windows."""
    return {"creationflags": 0x08000000} if os.name == "nt" else {}


# ------------------------------------------------------------------- encryption

_crypto = None


def load_crypto():
    """Import the 'cryptography' package. Returns True if available."""
    global _crypto
    if _crypto is not None:
        return True
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError:
        return False
    _crypto = types.SimpleNamespace(
        InvalidTag=InvalidTag, hashes=hashes, serialization=serialization, AESGCM=AESGCM,
        HKDF=HKDF, X25519PrivateKey=X25519PrivateKey, X25519PublicKey=X25519PublicKey)
    return True


def have_qt():
    import importlib.util
    return importlib.util.find_spec("PySide6") is not None


def install_crypto(packages=("cryptography",)):
    """pip-install packages for the running Python. Returns (ok, message)."""
    if getattr(sys, "frozen", False):
        return False, "this build is missing a package - rebuild it with PyInstaller"
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *packages]
    if sys.prefix == sys.base_prefix:  # not in a virtualenv
        cmd.insert(4, "--user")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, **_no_window())
    except OSError as e:
        return False, str(e)
    if r.returncode != 0:
        out = r.stdout + r.stderr
        if "externally-managed" in out or "No module named pip" in out:
            return False, ("Install them with your package manager instead:\n"
                           "  Debian/Ubuntu: sudo apt install python3-cryptography python3-pyside6.qtwidgets\n"
                           "  Fedora:        sudo dnf install python3-cryptography python3-pyside6\n"
                           "  Arch:          sudo pacman -S python-cryptography pyside6\n"
                           "or use a virtual environment: python3 -m venv ~/.span-venv && "
                           "~/.span-venv/bin/pip install cryptography PySide6")
        return False, out.strip()[-800:] or f"pip exited with code {r.returncode}"
    importlib.invalidate_caches()
    try:  # a fresh --user site-packages folder is not on sys.path yet
        site.addsitedir(site.getusersitepackages())
    except Exception:  # noqa: BLE001
        pass
    if load_crypto() and ("PySide6" not in packages or have_qt()):
        return True, ""
    return False, "it was installed but could not be loaded - please restart the app"


class HandshakeError(Exception):
    pass


class TamperedError(Exception):
    pass


def recv_exact(sock, n):
    buf = bytearray(n)
    view, got = memoryview(buf), 0
    while got < n:
        k = sock.recv_into(view[got:])
        if k == 0:
            raise ConnectionError("connection closed unexpectedly")
        got += k
    return bytes(buf)


class SecureChannel:
    """End-to-end encrypted, authenticated message stream over a TCP socket.

    Handshake (fresh X25519 keys per connection, with a hash commitment so a
    man-in-the-middle cannot grind keys until the verification codes match):
        client -> PROTO + SHA256(client_pub)
        server -> PROTO + server_pub
        client -> client_pub              (server checks it matches the hash)
    HKDF-SHA256 over the shared secret and both public keys gives one key per
    direction plus the 6-digit verification code. Every message is AES-256-GCM
    with a counter nonce, so modified, replayed or reordered data is rejected.
    Because only the two endpoints hold the keys, any relay or router in
    between sees only ciphertext.
    """

    def __init__(self, sock, is_client):
        c = _crypto
        if c is None:
            raise RuntimeError("the cryptography package is not installed")
        self.sock = sock
        priv = c.X25519PrivateKey.generate()
        pub = priv.public_key().public_bytes(c.serialization.Encoding.Raw,
                                             c.serialization.PublicFormat.Raw)
        hello_len = len(PROTO) + 32
        if is_client:
            sock.sendall(PROTO + hashlib.sha256(pub).digest())
            hello = recv_exact(sock, hello_len)
            self._check_proto(hello)
            peer_pub = hello[len(PROTO):]
            sock.sendall(pub)
            c_pub, s_pub = pub, peer_pub
        else:
            hello = recv_exact(sock, hello_len)
            self._check_proto(hello)
            commitment = hello[len(PROTO):]
            sock.sendall(PROTO + pub)
            peer_pub = recv_exact(sock, 32)
            if not hmac.compare_digest(hashlib.sha256(peer_pub).digest(), commitment):
                raise HandshakeError("handshake failed (key commitment mismatch)")
            c_pub, s_pub = peer_pub, pub
        try:
            shared = priv.exchange(c.X25519PublicKey.from_public_bytes(peer_pub))
        except ValueError as e:
            raise HandshakeError(f"handshake failed ({e})") from e
        okm = c.HKDF(algorithm=c.hashes.SHA256(), length=100, salt=c_pub + s_pub,
                     info=b"span1 session keys").derive(shared)
        k_c2s, k_s2c = okm[:32], okm[32:64]
        n = int.from_bytes(okm[64:68], "big") % 1_000_000
        self.code = f"{n // 1000:03d} {n % 1000:03d}"
        self.binding = okm[68:100]  # unique per connection; used to tie session auth to it
        self._tx = c.AESGCM(k_c2s if is_client else k_s2c)
        self._rx = c.AESGCM(k_s2c if is_client else k_c2s)
        self._tx_n = self._rx_n = 0

    @staticmethod
    def _check_proto(hello):
        if hello[:len(PROTO)] != PROTO:
            raise HandshakeError("the other side is not Span, or runs an incompatible version")

    def send(self, data):
        nonce = self._tx_n.to_bytes(12, "big")
        self._tx_n += 1
        ct = self._tx.encrypt(nonce, bytes(data), None)
        self.sock.sendall(struct.pack(">I", len(ct)) + ct)

    def recv(self):
        (n,) = struct.unpack(">I", recv_exact(self.sock, 4))
        if n < 16 or n > MAX_RECORD + 16:
            raise TamperedError("invalid encrypted message size")
        ct = recv_exact(self.sock, n)
        nonce = self._rx_n.to_bytes(12, "big")
        self._rx_n += 1
        try:
            return self._rx.decrypt(nonce, ct, None)
        except _crypto.InvalidTag:
            raise TamperedError("data failed authentication - corrupted or tampered with") from None

    def send_json(self, obj):
        self.send(json.dumps(obj).encode("utf-8"))

    def recv_json(self):
        obj = json.loads(self.recv().decode("utf-8"))
        if not isinstance(obj, dict):
            raise ValueError("unexpected message")
        return obj


# --------------------------------------------------------------------- firewall

FW_NAME = "Span"
FW_PORTS = (("UDP", DISCOVERY_PORT), ("TCP", TRANSFER_PORT))
# Only computers on the local network, plus Tailscale/CGNAT addresses.
FW_REMOTE = "localsubnet,100.64.0.0/10"


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, **_no_window())


def _linux_firewall():
    if shutil.which("firewall-cmd") and _run(["firewall-cmd", "--state"]).returncode == 0:
        return "firewalld"
    try:
        if "ENABLED=yes" in Path("/etc/ufw/ufw.conf").read_text():
            return "ufw"
    except OSError:
        pass
    return None


def firewall_status(cfg=None):
    """'ok', 'missing', 'none' (no firewall to configure) or 'unknown'."""
    try:
        if os.name == "nt":
            for proto, _ in FW_PORTS:
                r = _run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={FW_NAME} ({proto})"])
                if r.returncode != 0:
                    return "missing"
            return "ok"
        if sys.platform.startswith("linux"):
            kind = _linux_firewall()
            if kind == "firewalld":
                ok = all(_run(["firewall-cmd", f"--query-port={port}/{proto.lower()}"]).returncode == 0
                         for proto, port in FW_PORTS)
                return "ok" if ok else "missing"
            if kind == "ufw":  # ufw's rule list needs root to read; remember what we did
                return "ok" if (cfg or {}).get("ufw_rules_added") else "missing"
            return "none"
    except OSError:
        pass
    return "unknown"


def _run_elevated_windows(exe, params):
    """Run a program as administrator (shows the UAC prompt) and wait.

    Returns its exit code, or None if the user declined the prompt.
    """
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
                    ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                    ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
                    ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]

    shell32, kernel32 = ctypes.WinDLL("shell32"), ctypes.WinDLL("kernel32")
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    sei = SHELLEXECUTEINFOW(cbSize=ctypes.sizeof(SHELLEXECUTEINFOW), fMask=0x40,  # NOCLOSEPROCESS
                            lpVerb="runas", lpFile=exe, lpParameters=params, nShow=0)
    if not shell32.ShellExecuteExW(ctypes.byref(sei)) or not sei.hProcess:
        return None
    kernel32.WaitForSingleObject(sei.hProcess, 120_000)
    code = wintypes.DWORD()
    kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
    kernel32.CloseHandle(sei.hProcess)
    return code.value


def firewall_allow(cfg, use_terminal=False):
    """Open the app's ports, asking for admin rights. Returns (ok, message)."""
    if os.name == "nt":
        cmds = []
        for proto, port in FW_PORTS:
            name = f"{FW_NAME} ({proto})"
            cmds.append(f'netsh advfirewall firewall delete rule name="{name}"')
            cmds.append(f'netsh advfirewall firewall add rule name="{name}" dir=in action=allow '
                        f'protocol={proto} localport={port} remoteip={FW_REMOTE} profile=any')
        rc = _run_elevated_windows("cmd.exe", "/c " + " & ".join(cmds))
        if rc is None:
            return False, "administrator permission was not given"
        if firewall_status() == "ok":
            return True, "Allowed through Windows Firewall (local network only)."
        return False, f"netsh failed (exit code {rc})"

    kind = _linux_firewall()
    if kind == "firewalld":
        cmd = (f"firewall-cmd --permanent --add-port={DISCOVERY_PORT}/udp --add-port={TRANSFER_PORT}/tcp"
               " && firewall-cmd --reload")
    elif kind == "ufw":
        cmd = (f"ufw allow {DISCOVERY_PORT}/udp comment '{FW_NAME}'"
               f" && ufw allow {TRANSFER_PORT}/tcp comment '{FW_NAME}'")
    else:
        return True, "No active firewall found - nothing to change."
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        argv = ["sh", "-c", cmd]
    elif use_terminal and shutil.which("sudo"):
        argv = ["sudo", "sh", "-c", cmd]
    elif shutil.which("pkexec"):
        argv = ["pkexec", "sh", "-c", cmd]
    else:
        return False, f"run this as root:\n  {cmd}"
    try:
        r = subprocess.run(argv, capture_output=not use_terminal, text=True)
    except OSError as e:
        return False, str(e)
    if r.returncode == 0:
        if kind == "ufw":
            cfg["ufw_rules_added"] = True
        return True, f"Allowed through {kind}."
    if argv[0] == "pkexec" and r.returncode in (126, 127):
        return False, "administrator permission was not given"
    return False, (r.stderr or "").strip()[-400:] or f"exit code {r.returncode}"


# -------------------------------------------------------------------- discovery

class Discovery:
    """Announces this computer and listens for others via UDP broadcast."""

    def __init__(self, name, port=TRANSFER_PORT, announce=True):
        self.id = uuid.uuid4().hex
        self.name = name
        self.port = port
        self.announce = announce
        self.peers = {}
        self.error = None
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._listen, daemon=True).start()
        if self.announce:
            threading.Thread(target=self._announce_loop, daemon=True).start()
        return self

    def stop(self):
        self._stop.set()

    def _announce_loop(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while not self._stop.is_set():
            pkt = json.dumps({"magic": MAGIC, "id": self.id, "name": self.name,
                              "port": self.port}).encode()
            # Global broadcast plus a /24 directed broadcast per interface, so
            # machines with several adapters (VPN, Hyper-V, WSL) still announce
            # on the real LAN.
            targets = {"255.255.255.255"} | {ip.rsplit(".", 1)[0] + ".255" for ip in local_ips()}
            for t in targets:
                try:
                    s.sendto(pkt, (t, DISCOVERY_PORT))
                except OSError:
                    pass
            self._stop.wait(ANNOUNCE_EVERY)
        s.close()

    def _listen(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", DISCOVERY_PORT))
        except OSError as e:
            self.error = str(e)
            return
        s.settimeout(1.0)
        while not self._stop.is_set():
            try:
                data, addr = s.recvfrom(4096)
                msg = json.loads(data.decode("utf-8"))
                if msg.get("magic") != MAGIC or msg.get("id") == self.id:
                    continue
                with self._lock:
                    self.peers[str(msg["id"])] = {
                        "id": str(msg["id"]),
                        "name": str(msg.get("name", "?"))[:64],
                        "ip": addr[0],
                        "port": int(msg.get("port", TRANSFER_PORT)),
                        "seen": time.time(),
                    }
            except socket.timeout:
                continue
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
        s.close()

    def list_peers(self):
        now = time.time()
        with self._lock:
            for k in [k for k, p in self.peers.items() if now - p["seen"] > PEER_TIMEOUT]:
                del self.peers[k]
            return sorted(self.peers.values(), key=lambda p: (p["name"].lower(), p["ip"]))


# --------------------------------------------------------------------- sending
#
# Requests (first encrypted message on a connection):
#   push   {"magic","sender","files"}                 receiver must click Accept
#   browse {"magic","op":"browse","sender"}           receiver must click Allow;
#          then on the same connection: list / mkdir / ping / bye requests
#   put    {"magic","op":"put","session","auth","dest","files"}   upload into share
#   get    {"magic","op":"get","session","auth","paths"}          download from share
# put/get ride on an approved browse session. "auth" is an HMAC of the session
# token over this connection's channel binding, and replies carry a matching
# "proof", so the token is never exposed and cannot be relayed by a MITM.

class Cancelled(Exception):
    pass


class SendControl:
    """Lets another thread cancel a running send."""

    def __init__(self):
        self.cancelled = False
        self.sock = None

    def cancel(self):
        self.cancelled = True
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def session_mac(token, binding, role):
    return hmac.new(bytes.fromhex(token), role + binding, hashlib.sha256).hexdigest()


def _open_channel(dial, ctl=None, on_code=None):
    sock = dial()
    if ctl is not None:
        ctl.sock = sock
        if ctl.cancelled:
            sock.close()
            raise Cancelled()
    try:
        sock.settimeout(HANDSHAKE_TIMEOUT)
        ch = SecureChannel(sock, is_client=True)
    except BaseException:
        sock.close()
        raise
    if on_code:
        on_code(ch.code)
    return sock, ch


def _stream_files(ch, files, sizes, total, progress=None):
    done = 0
    for (fp, rel), size in zip(files, sizes):
        left = size
        with open(fp, "rb") as f:
            while left > 0:
                chunk = f.read(min(CHUNK, left))
                if not chunk:
                    raise IOError(f"{rel} got smaller while it was being sent")
                ch.send(chunk)  # AES-GCM authenticates every chunk
                left -= len(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total, rel)
        if progress and size == 0:
            progress(done, total, rel)


def _parse_file_list(items):
    files = []
    for f in items:
        size = int(f["size"])
        if size < 0:
            raise ValueError("negative file size")
        files.append((safe_rel_path(f["path"]), size))
    return files


def _receive_files(ch, dest, files, progress=None):
    """Receive announced files into dest. Existing names get ' (1)' added.

    Returns the top-level paths that were created.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    total = sum(s for _, s in files)
    # Give each top-level item a fresh name if it already exists, so
    # re-sending a folder creates "Photos (1)" instead of mixing contents.
    top_map = {}
    for rel, _ in files:
        if rel.parts[0] not in top_map:
            top_map[rel.parts[0]] = unique_path(dest / rel.parts[0]).name
    parts, done = [], 0
    try:
        for rel, size in files:
            final = dest.joinpath(top_map[rel.parts[0]], *rel.parts[1:])
            if final.exists():  # duplicate path inside one transfer
                final = unique_path(final)
            final.parent.mkdir(parents=True, exist_ok=True)
            part = final.with_name(final.name + ".part")
            parts.append(part)
            left = size
            with open(part, "wb") as out:
                while left > 0:
                    chunk = ch.recv()  # decrypts and verifies; raises if tampered
                    if not chunk or len(chunk) > left:
                        raise TamperedError("the other computer sent more data than it announced")
                    out.write(chunk)
                    left -= len(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total, rel.as_posix())
            if progress and size == 0:
                progress(done, total, rel.as_posix())
            os.replace(part, final)
            parts.remove(part)
    except BaseException:
        for p in parts:
            try:
                p.unlink()
            except OSError:
                pass
        raise
    return [dest / t for t in top_map.values()]


def send_files(host, port, paths, sender_name, progress=None, ctl=None, on_code=None, connect=None,
               extra=None, auth_token=None):
    """Send files/folders to a receiver. Returns (file_count, total_bytes).

    on_code(code) is called with the verification code once the encrypted
    connection is up, before the receiver has accepted. connect() may supply
    an already-connected socket (used for relay connections) instead of
    dialling host:port directly. extra/auth_token turn the push into an
    upload on an approved browse session.
    """
    if not load_crypto():
        raise RuntimeError("the cryptography package is not installed")
    ctl = ctl or SendControl()
    files = collect_files(paths)
    if not files:
        raise ValueError("nothing to send (the folder is empty)")
    sizes = [fp.stat().st_size for fp, _ in files]
    total = sum(sizes)
    header = {"magic": MAGIC, "sender": sender_name, "total": total,
              "files": [{"path": rel, "size": sz} for (_, rel), sz in zip(files, sizes)]}
    header.update(extra or {})
    dial = connect or (lambda: socket.create_connection((host, port), timeout=10))
    try:
        sock, ch = _open_channel(dial, ctl, on_code)
        with sock:
            if auth_token:
                header["auth"] = session_mac(auth_token, ch.binding, b"client")
            ch.send_json(header)
            sock.settimeout(ACCEPT_WAIT)
            resp = ch.recv_json()
            if not resp.get("accept"):
                raise RuntimeError("the other computer declined"
                                   + (f" ({resp['reason']})" if resp.get("reason") else ""))
            if auth_token and not hmac.compare_digest(str(resp.get("proof", "")),
                                                      session_mac(auth_token, ch.binding, b"server")):
                raise TamperedError("could not verify the other computer")
            sock.settimeout(IO_TIMEOUT)
            _stream_files(ch, files, sizes, total, progress)
            result = ch.recv_json()
            if not result.get("ok"):
                raise RuntimeError("receiver error: " + str(result.get("error", "unknown")))
    except Cancelled:
        raise
    except Exception:
        if ctl.cancelled:
            raise Cancelled()
        raise
    return len(files), total


# --------------------------------------------------------------- shared folder

def resolve_in_root(root, rel, must_exist=True):
    """Map a '/'-path from the network to a real path inside root, or raise."""
    root = Path(root).resolve()
    rel = str(rel or "").strip()
    p = root.joinpath(safe_rel_path(rel)) if rel.strip("/. ") else root
    p = p.resolve() if (must_exist or p.exists()) else p.parent.resolve() / p.name
    if p != root and root not in p.parents:
        raise PermissionError("that is outside the shared folder")
    if must_exist and not p.exists():
        raise FileNotFoundError("it no longer exists")
    return p


def list_dir(root, rel):
    d = resolve_in_root(root, rel)
    if not d.is_dir():
        raise NotADirectoryError("not a folder")
    out = []
    with os.scandir(d) as it:
        for e in it:
            try:
                is_dir = e.is_dir()
                st = e.stat()
            except OSError:
                continue
            out.append({"name": e.name, "dir": is_dir, "size": 0 if is_dir else st.st_size,
                        "mtime": int(st.st_mtime)})
            if len(out) >= 20000:
                break
    out.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return out


class RemoteSession:
    """An approved browse session on another computer's shared folder."""

    def __init__(self, my_name, host=None, port=TRANSFER_PORT, connect=None):
        self.my_name = my_name
        self._dial = connect or (lambda: socket.create_connection((host, port), timeout=10))
        self.sock = self.ch = None
        self.sid = self.token = None
        self.remote_name = self.root_name = ""
        self.closed = False
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def open(self, on_code=None, ctl=None):
        """Connect and wait for the other side to click Allow."""
        if not load_crypto():
            raise RuntimeError("the cryptography package is not installed")
        ctl = ctl or SendControl()
        try:
            sock, ch = _open_channel(self._dial, ctl, on_code)
            try:
                ch.send_json({"magic": MAGIC, "op": "browse", "sender": self.my_name})
                sock.settimeout(ACCEPT_WAIT)
                resp = ch.recv_json()
                if not resp.get("ok"):
                    raise RuntimeError(str(resp.get("error", "the other computer declined")))
            except BaseException:
                sock.close()
                raise
        except Exception:
            if ctl.cancelled:
                raise Cancelled()
            raise
        sock.settimeout(IO_TIMEOUT)
        self.sock, self.ch = sock, ch
        self.sid, self.token = str(resp["session"]), str(resp["token"])
        self.remote_name = str(resp.get("name", "?"))[:64]
        self.root_name = str(resp.get("root", ""))[:255]
        threading.Thread(target=self._pinger, daemon=True).start()
        return self

    def _request(self, obj):
        with self._lock:
            if self.closed:
                raise ConnectionError("disconnected from the other computer")
            try:
                self.ch.send_json(obj)
                resp = self.ch.recv_json()
            except Exception:
                self._close_socket()
                raise
        if not resp.get("ok"):
            raise RuntimeError(str(resp.get("error", "request failed")))
        return resp

    def list(self, rel=""):
        return self._request({"op": "list", "path": rel})["entries"]

    def mkdir(self, rel):
        self._request({"op": "mkdir", "path": rel})

    def alive(self):
        return not self.closed

    def _pinger(self):
        while not self._stop.wait(20):
            try:
                self._request({"op": "ping"})
            except Exception:  # noqa: BLE001
                return

    def _close_socket(self):
        self.closed = True
        self._stop.set()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass

    def close(self):
        if self.closed:
            return
        if self._lock.acquire(timeout=2):
            try:
                self.ch.send_json({"op": "bye"})
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._lock.release()
        self._close_socket()

    def upload(self, paths, dest_rel, progress=None, ctl=None):
        """Send local files/folders into dest_rel of the other computer's share."""
        return send_files(None, None, paths, self.my_name, progress, ctl, connect=self._dial,
                          extra={"op": "put", "session": self.sid, "dest": dest_rel}, auth_token=self.token)

    def download(self, rel_paths, local_dir, progress=None, ctl=None):
        """Fetch files/folders from the other computer's share into local_dir."""
        ctl = ctl or SendControl()
        try:
            sock, ch = _open_channel(self._dial, ctl)
            with sock:
                ch.send_json({"magic": MAGIC, "op": "get", "sender": self.my_name, "session": self.sid,
                              "paths": list(rel_paths), "auth": session_mac(self.token, ch.binding, b"client")})
                sock.settimeout(IO_TIMEOUT)
                resp = ch.recv_json()
                if not resp.get("ok"):
                    raise RuntimeError(str(resp.get("error", "download refused")))
                if not hmac.compare_digest(str(resp.get("proof", "")),
                                           session_mac(self.token, ch.binding, b"server")):
                    raise TamperedError("could not verify the other computer")
                files = _parse_file_list(resp["files"])
                if not files:
                    raise ValueError("nothing to download (empty folder)")
                total = sum(s for _, s in files)
                if shutil.disk_usage(local_dir).free < total:
                    raise IOError("not enough free disk space here")
                _receive_files(ch, local_dir, files, progress)
                ch.send_json({"ok": True})
            return len(files), total
        except Cancelled:
            raise
        except Exception:
            if ctl.cancelled:
                raise Cancelled()
            raise


# ------------------------------------------------------------------- receiving

class Receiver:
    """TCP server that accepts incoming transfers and browse sessions.

    get_dest_dir() is the shared folder: pushes land there and browse
    sessions can see only it.
    ask_accept(sender, ip, files, total, code) -> bool   (files: [(Path, size)])
    ask_browse(sender, ip, code) -> bool                 (default: refuse)
    events(kind, **info): start/progress/done/error (with direction "in" or
    "out"), session_open/session_closed.
    """

    def __init__(self, get_dest_dir, ask_accept, events=None, port=TRANSFER_PORT, ask_browse=None,
                 get_name=None):
        self.get_dest_dir = get_dest_dir
        self.ask_accept = ask_accept
        self.ask_browse = ask_browse or (lambda sender, ip, code: False)
        self.get_name = get_name or socket.gethostname
        self.events = events or (lambda kind, **info: None)
        self.port = port
        self.srv = None
        self.sessions = {}
        self._ids = 0
        self._lock = threading.Lock()

    def start(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":  # on Windows SO_REUSEADDR would allow port hijacking
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("", self.port))
        srv.listen(16)
        self.srv = srv
        threading.Thread(target=self._accept_loop, daemon=True).start()
        return self

    def stop(self):
        if self.srv:
            try:
                self.srv.close()
            except OSError:
                pass
        for sid in list(self.sessions):
            self.close_session(sid)

    def close_session(self, sid):
        """Disconnect a computer that is browsing the shared folder."""
        with self._lock:
            s = self.sessions.pop(sid, None)
        if s:
            try:
                s["conn"].shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _root(self):
        root = Path(self.get_dest_dir())
        root.mkdir(parents=True, exist_ok=True)
        return root.resolve()

    def _accept_loop(self):
        while True:
            try:
                conn, addr = self.srv.accept()
            except OSError:
                return
            self.handle_socket(conn, addr[0])

    def handle_socket(self, conn, label):
        """Serve one incoming connection (direct, or handed over by a relay)."""
        with self._lock:
            self._ids += 1
            tid = self._ids
        threading.Thread(target=self._handle, args=(conn, (label, 0), tid), daemon=True).start()

    def _handle(self, conn, addr, tid):
        peer = addr[0]
        with conn:
            conn.settimeout(HANDSHAKE_TIMEOUT)
            try:
                ch = SecureChannel(conn, is_client=False)
            except Exception:  # noqa: BLE001 - port scanners, old versions: drop silently
                return
            conn.settimeout(IO_TIMEOUT)
            try:
                hdr = ch.recv_json()
                if hdr.get("magic") != MAGIC:
                    return
                sender = str(hdr.get("sender", "unknown"))[:64]
                peer = f"{sender} ({addr[0]})"
                op = hdr.get("op", "push")
                if op == "push":
                    self._serve_push(ch, hdr, sender, addr[0], peer, tid)
                elif op == "browse":
                    self._serve_browse(ch, conn, sender, addr[0], peer, tid)
                elif op in ("put", "get"):
                    sess = self._check_auth(hdr, ch)
                    if not sess:
                        ch.send_json({"accept": False, "ok": False,
                                      "error": "not allowed - open the folder again"})
                        return
                    (self._serve_put if op == "put" else self._serve_get)(ch, hdr, sess, peer, tid)
            except Exception as e:  # noqa: BLE001 - report every failure to the user
                try:
                    ch.send_json({"ok": False, "error": str(e)})
                except Exception:  # noqa: BLE001
                    pass
                self.events("error", tid=tid, peer=peer, error=friendly(e))

    def _progress(self, tid, peer, direction):
        return lambda d, t, n: self.events("progress", tid=tid, peer=peer, done=d, total=t, name=n,
                                           direction=direction)

    def _serve_push(self, ch, hdr, sender, ip, peer, tid):
        files = _parse_file_list(hdr["files"])
        total = sum(s for _, s in files)
        if not self.ask_accept(sender, ip, files, total, ch.code):
            ch.send_json({"accept": False, "reason": "declined by user"})
            return
        dest = self._root()
        if shutil.disk_usage(dest).free < total:
            ch.send_json({"accept": False, "reason": "not enough free disk space"})
            self.events("error", tid=tid, peer=peer, error="not enough free disk space")
            return
        ch.send_json({"accept": True})
        self.events("start", tid=tid, peer=peer, count=len(files), total=total, code=ch.code, direction="in")
        start = time.time()
        _receive_files(ch, dest, files, self._progress(tid, peer, "in"))
        ch.send_json({"ok": True})
        self.events("done", tid=tid, peer=peer, count=len(files), total=total, dest=str(dest),
                    seconds=time.time() - start, direction="in")

    def _check_auth(self, hdr, ch):
        with self._lock:
            s = self.sessions.get(str(hdr.get("session", "")))
        if s and hmac.compare_digest(str(hdr.get("auth", "")), session_mac(s["token"], ch.binding, b"client")):
            return s
        return None

    def _serve_browse(self, ch, conn, sender, ip, peer, tid):
        if not self.ask_browse(sender, ip, ch.code):
            ch.send_json({"ok": False, "error": "the other computer declined"})
            return
        sid, token = secrets.token_hex(8), secrets.token_hex(32)
        with self._lock:
            self.sessions[sid] = {"sid": sid, "token": token, "name": sender, "conn": conn, "peer": peer}
        self.events("session_open", sid=sid, peer=peer, name=sender)
        try:
            ch.send_json({"ok": True, "session": sid, "token": token, "name": self.get_name(),
                          "root": self._root().name})
            conn.settimeout(120)  # the client pings every 20 s
            while True:
                req = ch.recv_json()
                op = req.get("op")
                if op == "bye":
                    break
                try:
                    if op == "list":
                        resp = {"ok": True, "entries": list_dir(self._root(), req.get("path", ""))}
                    elif op == "mkdir":
                        resolve_in_root(self._root(), req.get("path", ""), must_exist=False).mkdir()
                        resp = {"ok": True}
                    elif op == "ping":
                        resp = {"ok": True}
                    else:
                        resp = {"ok": False, "error": "unknown request"}
                except (OSError, ValueError) as e:
                    resp = {"ok": False, "error": friendly(e)}
                ch.send_json(resp)
        except (OSError, ValueError, ConnectionError, TamperedError):
            pass
        finally:
            with self._lock:
                self.sessions.pop(sid, None)
            self.events("session_closed", sid=sid, peer=peer, name=sender)

    def _serve_put(self, ch, hdr, sess, peer, tid):
        dest = resolve_in_root(self._root(), hdr.get("dest", ""))
        if not dest.is_dir():
            raise NotADirectoryError("that folder no longer exists")
        files = _parse_file_list(hdr["files"])
        total = sum(s for _, s in files)
        if shutil.disk_usage(dest).free < total:
            ch.send_json({"accept": False, "reason": "not enough free disk space"})
            return
        ch.send_json({"accept": True, "proof": session_mac(sess["token"], ch.binding, b"server")})
        self.events("start", tid=tid, peer=peer, count=len(files), total=total, code=None, direction="in")
        start = time.time()
        _receive_files(ch, dest, files, self._progress(tid, peer, "in"))
        ch.send_json({"ok": True})
        self.events("done", tid=tid, peer=peer, count=len(files), total=total, dest=str(dest),
                    seconds=time.time() - start, direction="in")

    def _serve_get(self, ch, hdr, sess, peer, tid):
        root = self._root()
        items = [resolve_in_root(root, p) for p in hdr.get("paths", [])]
        files = []
        for fp, rel in collect_files(items):
            real = fp.resolve()  # skip symlinks that point outside the share
            if real == root or root in real.parents:
                files.append((fp, rel))
        sizes = [fp.stat().st_size for fp, _ in files]
        total = sum(sizes)
        ch.send_json({"ok": True, "proof": session_mac(sess["token"], ch.binding, b"server"), "total": total,
                      "files": [{"path": rel, "size": sz} for (_, rel), sz in zip(files, sizes)]})
        if not files:
            return
        self.events("start", tid=tid, peer=peer, count=len(files), total=total, code=None, direction="out")
        start = time.time()
        _stream_files(ch, files, sizes, total, self._progress(tid, peer, "out"))
        ch.recv_json()
        self.events("done", tid=tid, peer=peer, count=len(files), total=total, dest="",
                    seconds=time.time() - start, direction="out")


# ------------------------------------------------------------------------ relay
#
# Optional. A relay is a small server (this same file, run with `relay`) that
# computers in different places connect OUT to, so no port forwarding or
# firewall changes are needed on the laptops. It only forwards bytes: the
# SecureChannel handshake runs end-to-end through it, so the relay never sees
# keys or file contents. Local-network transfers keep going direct.
#
#   client -> relay  {"op":"register", "password", "id", "name"}   long-lived
#   relay  -> client {"op":"peers", ...} / {"op":"incoming","session"}
#   sender -> relay  {"op":"connect", "password", "target"}       new socket
#   target -> relay  {"op":"attach",  "password", "session"}      new socket
#   relay answers {"ok":true} on both and then pipes raw bytes between them.

RELAY_PORT = 50507


def _send_line(sock, obj):
    sock.sendall(json.dumps(obj).encode("utf-8") + b"\n")


def _recv_line(sock, limit=1 << 20):
    """Read one JSON line, byte by byte so nothing after it is consumed."""
    buf = bytearray()
    while True:
        b = sock.recv(1)
        if not b:
            raise ConnectionError("relay connection closed")
        if b == b"\n":
            obj = json.loads(buf.decode("utf-8"))
            if not isinstance(obj, dict):
                raise ValueError("bad relay message")
            return obj
        buf += b
        if len(buf) > limit:
            raise ValueError("relay message too long")


def relay_connect(host, port, password, target_id):
    """Ask the relay for a byte pipe to another registered computer."""
    s = socket.create_connection((host, port), timeout=10)
    try:
        _send_line(s, {"op": "connect", "password": password, "target": target_id})
        s.settimeout(30)
        resp = _recv_line(s)
        if not resp.get("ok"):
            raise RuntimeError("relay: " + str(resp.get("error", "refused")))
        return s
    except BaseException:
        s.close()
        raise


def relay_list(host, port, password):
    with socket.create_connection((host, port), timeout=10) as s:
        _send_line(s, {"op": "list", "password": password})
        resp = _recv_line(s)
    if not resp.get("ok"):
        raise RuntimeError("relay: " + str(resp.get("error", "refused")))
    return resp.get("peers", [])


class RelayClient:
    """Keeps this computer registered on a relay and accepts relayed transfers."""

    def __init__(self, address, password, ident, get_name, on_incoming, on_status=None):
        self.host, self.port = parse_target(address, RELAY_PORT)
        self.address = address
        self.password = password
        self.ident = ident
        self.get_name = get_name
        self.on_incoming = on_incoming
        self.on_status = on_status or (lambda s: None)
        self.peers = []
        self.connected = False
        self._stop = threading.Event()
        self._sock = None

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        return self

    def stop(self):
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    def connect_to(self, target_id):
        return relay_connect(self.host, self.port, self.password, target_id)

    def _loop(self):
        delay = 2
        while not self._stop.is_set():
            self.on_status("connecting...")
            try:
                s = socket.create_connection((self.host, self.port), timeout=10)
                self._sock = s
                _send_line(s, {"op": "register", "password": self.password, "id": self.ident,
                               "name": self.get_name()})
                resp = _recv_line(s)
                if not resp.get("ok"):
                    raise RuntimeError(str(resp.get("error", "refused")))
                self.connected, delay = True, 2
                self.on_status("connected")
                s.settimeout(90)
                threading.Thread(target=self._pinger, args=(s,), daemon=True).start()
                while not self._stop.is_set():
                    msg = _recv_line(s)
                    if msg.get("op") == "peers":
                        self.peers = [{"id": str(p["id"]), "name": str(p.get("name", "?"))[:64]}
                                      for p in msg.get("peers", []) if p.get("id") != self.ident]
                    elif msg.get("op") == "incoming":
                        threading.Thread(target=self._attach, args=(str(msg.get("session")),),
                                         daemon=True).start()
            except Exception as e:  # noqa: BLE001
                if self._stop.is_set():
                    break
                self.on_status(f"not connected ({friendly(e)}) - retrying")
            finally:
                self.connected, self.peers = False, []
                if self._sock:
                    try:
                        self._sock.close()
                    except OSError:
                        pass
            self._stop.wait(delay)
            delay = min(delay * 2, 30)

    def _pinger(self, s):
        # keeps NAT mappings alive and lets the relay pick up name changes
        while not self._stop.wait(25):
            try:
                _send_line(s, {"op": "ping", "name": self.get_name()})
            except OSError:
                return

    def _attach(self, session):
        try:
            s = socket.create_connection((self.host, self.port), timeout=10)
            _send_line(s, {"op": "attach", "password": self.password, "session": session})
            if _recv_line(s).get("ok"):
                self.on_incoming(s)
            else:
                s.close()
        except (OSError, ValueError):
            pass


class RelayServer:
    """The relay itself. Needs only the standard library - no cryptography."""

    def __init__(self, port=RELAY_PORT, password="", max_clients=500):
        self.port, self.password, self.max_clients = port, password, max_clients
        self.clients = {}   # id -> {"name", "sock", "wlock"}
        self.pending = {}   # session -> {"event", "sock"}
        self.lock = threading.Lock()

    @staticmethod
    def log(text):
        print(time.strftime("%Y-%m-%d %H:%M:%S ") + text, flush=True)

    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("", self.port))
        srv.listen(128)
        self.log(f"Relay listening on port {self.port}"
                 + ("" if self.password else " - WARNING: no password, anyone can use this relay "
                                             "(they still cannot read transfers)"))
        while True:
            conn, addr = srv.accept()
            threading.Thread(target=self._handle, args=(conn, addr), daemon=True).start()

    def _handle(self, conn, addr):
        keep = False
        try:
            conn.settimeout(20)
            msg = _recv_line(conn, limit=4096)
            if self.password and not hmac.compare_digest(str(msg.get("password", "")).encode(),
                                                         self.password.encode()):
                time.sleep(1)  # slow down password guessing
                _send_line(conn, {"ok": False, "error": "wrong relay password"})
                return
            op = msg.get("op")
            if op == "register":
                self._register(conn, addr, msg)
            elif op == "list":
                _send_line(conn, {"ok": True, "peers": self._peer_list()})
            elif op == "connect":
                self._connect(conn, msg)
            elif op == "attach":
                keep = self._attach(conn, msg)  # socket is taken over by the connect thread
        except Exception as e:  # noqa: BLE001
            if not isinstance(e, (ConnectionError, socket.timeout)):
                self.log(f"{addr[0]}: {friendly(e)}")
        finally:
            if not keep:
                try:
                    conn.close()
                except OSError:
                    pass

    def _peer_list(self):
        with self.lock:
            return [{"id": cid, "name": c["name"]} for cid, c in self.clients.items()]

    def _push(self, client, obj):
        try:
            with client["wlock"]:
                _send_line(client["sock"], obj)
        except OSError:
            pass

    def _broadcast_peers(self):
        peers = self._peer_list()
        with self.lock:
            clients = list(self.clients.values())
        for c in clients:
            self._push(c, {"op": "peers", "peers": peers})

    def _register(self, conn, addr, msg):
        cid, name = str(msg.get("id", ""))[:64], str(msg.get("name", "?"))[:64]
        if not cid:
            return
        client = {"name": name, "sock": conn, "wlock": threading.Lock()}
        with self.lock:
            if len(self.clients) >= self.max_clients and cid not in self.clients:
                _send_line(conn, {"ok": False, "error": "relay is full"})
                return
            old = self.clients.get(cid)
            self.clients[cid] = client
        if old:
            try:
                old["sock"].close()
            except OSError:
                pass
        self._push(client, {"ok": True})
        self.log(f"+ {name} ({addr[0]})")
        self._broadcast_peers()
        try:
            conn.settimeout(90)  # clients ping every 25 s
            while True:
                m = _recv_line(conn, limit=4096)
                new_name = str(m.get("name", name))[:64]
                if new_name != client["name"]:
                    client["name"] = new_name
                    self._broadcast_peers()
        except (OSError, ValueError):
            pass
        finally:
            with self.lock:
                if self.clients.get(cid) is client:
                    del self.clients[cid]
            self.log(f"- {client['name']} ({addr[0]})")
            self._broadcast_peers()

    def _connect(self, conn, msg):
        with self.lock:
            target = self.clients.get(str(msg.get("target", "")))
        if not target:
            _send_line(conn, {"ok": False, "error": "that computer is not connected to the relay"})
            return
        session = uuid.uuid4().hex
        slot = {"event": threading.Event(), "sock": None}
        with self.lock:
            self.pending[session] = slot
        self._push(target, {"op": "incoming", "session": session})
        slot["event"].wait(20)
        with self.lock:
            self.pending.pop(session, None)
            other = slot["sock"]
        if other is None:
            _send_line(conn, {"ok": False, "error": "the other computer did not answer"})
            return
        try:
            _send_line(conn, {"ok": True})
            _send_line(other, {"ok": True})
            self.log(f"  session {session[:8]} -> {target['name']}")
            sent = self._pipe(conn, other)
            self.log(f"  session {session[:8]} closed, {human(sent)} relayed")
        finally:
            try:
                other.close()
            except OSError:
                pass

    def _attach(self, conn, msg):
        with self.lock:
            slot = self.pending.get(str(msg.get("session", "")))
            if not slot or slot["sock"] is not None:
                return False
            slot["sock"] = conn
        slot["event"].set()
        return True

    @staticmethod
    def _pipe(a, b):
        """Copy bytes both ways until either side closes. Returns bytes copied a->b."""
        counted = [0]

        def pump(src, dst, count):
            try:
                while True:
                    data = src.recv(256 * 1024)
                    if not data:
                        break
                    dst.sendall(data)
                    if count:
                        counted[0] += len(data)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        for s in (a, b):
            s.settimeout(ACCEPT_WAIT + 60)  # long enough for the receiver to click Accept
        t = threading.Thread(target=pump, args=(b, a, False), daemon=True)
        t.start()
        pump(a, b, True)
        t.join()
        return counted[0]


# ------------------------------------------------------------------------- GUI

class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.name_var = tk.StringVar(value=self.cfg.get("name") or socket.gethostname())
        self.dest = self.cfg.get("dest") or str(default_download_dir())
        self.dest_var = tk.StringVar(value=self.dest)
        self.auto_var = tk.BooleanVar(value=bool(self.cfg.get("auto_accept", False)))
        self.target_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready.")
        self.fw_var = tk.StringVar(value="checking...")
        self.relay_var = tk.StringVar()
        self.q = queue.Queue()
        self.peers = []
        self.sending = None
        self.rx_state = {}
        self.relay = None
        self.relay_choice = None  # (text shown in Address, relay peer id)

        root.title(APP)
        root.minsize(660, 620)
        self._build()

        self.disc = Discovery(self.name_var.get()).start()
        self.recv = Receiver(lambda: self.dest, self._ask_accept, self._recv_event)
        try:
            self.recv.start()
            self.log(f"Ready to receive (encrypted). This computer's IP: {', '.join(local_ips()) or 'unknown'}")
        except OSError as e:
            self.log(f"Could not listen on port {TRANSFER_PORT} ({e}). Is another copy already "
                     "running? Direct receiving is disabled in this window.")
        self._start_relay()
        root.protocol("WM_DELETE_WINDOW", self._close)
        root.after(100, self._pump)
        root.after(300, self._refresh_peers)
        root.after(1500, self._check_discovery)
        threading.Thread(target=self._firewall_check, daemon=True).start()

    # ---- layout
    def _build(self):
        pad = {"padx": 10, "pady": 5}
        me = ttk.LabelFrame(self.root, text="This computer")
        me.pack(fill="x", **pad)
        g = {"sticky": "w", "padx": 6, "pady": 3}
        ttk.Label(me, text="Name:").grid(row=0, column=0, **g)
        name_entry = ttk.Entry(me, textvariable=self.name_var)
        name_entry.grid(row=0, column=1, columnspan=2, sticky="ew", padx=6)
        name_entry.bind("<FocusOut>", lambda _e: self._save_name())
        name_entry.bind("<Return>", lambda _e: self._save_name())
        ttk.Label(me, text="IP address:").grid(row=1, column=0, **g)
        ttk.Label(me, text=", ".join(local_ips()) or "unknown").grid(row=1, column=1, **g)
        ttk.Label(me, text="Save files to:").grid(row=2, column=0, **g)
        ttk.Label(me, textvariable=self.dest_var).grid(row=2, column=1, **g)
        btns = ttk.Frame(me)
        btns.grid(row=2, column=2, sticky="e", padx=6)
        ttk.Button(btns, text="Change...", command=self._choose_dest).pack(side="left")
        ttk.Button(btns, text="Open", command=self._open_dest).pack(side="left", padx=(4, 0))
        ttk.Label(me, text="Firewall:").grid(row=3, column=0, **g)
        ttk.Label(me, textvariable=self.fw_var).grid(row=3, column=1, **g)
        self.fw_btn = ttk.Button(me, text="Allow...", command=self._firewall_allow)
        self.fw_btn.grid(row=3, column=2, sticky="e", padx=6)
        self.fw_btn.grid_remove()
        ttk.Label(me, text="Relay:").grid(row=4, column=0, **g)
        ttk.Label(me, textvariable=self.relay_var).grid(row=4, column=1, **g)
        ttk.Button(me, text="Relay settings...", command=self._relay_dialog).grid(row=4, column=2, sticky="e",
                                                                                padx=6)
        ttk.Checkbutton(me, text="Accept incoming files without asking", variable=self.auto_var,
                        command=self._save_cfg).grid(row=5, column=0, columnspan=3, **g)
        me.columnconfigure(1, weight=1)

        send = ttk.LabelFrame(self.root, text="Send to another computer")
        send.pack(fill="both", expand=True, **pad)
        ttk.Label(send, text="Computers found (click one):").pack(anchor="w", padx=6, pady=(4, 0))
        self.peer_box = tk.Listbox(send, height=6, activestyle="none", exportselection=False)
        self.peer_box.pack(fill="both", expand=True, padx=6, pady=4)
        self.peer_box.bind("<<ListboxSelect>>", self._on_peer_select)
        row = ttk.Frame(send)
        row.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(row, text="Address:").pack(side="left")
        ttk.Entry(row, textvariable=self.target_var, width=22).pack(side="left", padx=4)
        self.btn_files = ttk.Button(row, text="Send files...", command=self._send_files_dialog)
        self.btn_files.pack(side="left", padx=2)
        self.btn_folder = ttk.Button(row, text="Send folder...", command=self._send_folder_dialog)
        self.btn_folder.pack(side="left", padx=2)
        self.btn_cancel = ttk.Button(row, text="Cancel", command=self._cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=2)

        prog = ttk.Frame(self.root)
        prog.pack(fill="x", **pad)
        self.bar = ttk.Progressbar(prog, maximum=1000)
        self.bar.pack(fill="x")
        ttk.Label(prog, textvariable=self.status_var).pack(anchor="w", pady=(3, 0))

        self.log_box = ScrolledText(self.root, height=8, state="disabled", wrap="word")
        self.log_box.pack(fill="both", expand=False, padx=10, pady=(0, 10))

    # ---- thread plumbing: worker threads hand callables to the Tk thread
    def _pump(self):
        try:
            while True:
                self.q.get_nowait()()
        except queue.Empty:
            pass
        self.root.after(50, self._pump)

    def _call_main(self, fn):
        done, box = threading.Event(), {}

        def wrapper():
            try:
                box["r"] = fn()
            finally:
                done.set()
        self.q.put(wrapper)
        done.wait()
        return box.get("r")

    def log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", time.strftime("%H:%M:%S  ") + text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _progress(self, verb, done, total, name, state):
        now = time.time()
        if "t0" not in state:
            state["t0"], state["last"] = now, 0.0
        if now - state["last"] < 0.1 and done < total:
            return
        state["last"] = now
        speed = done / max(now - state["t0"], 1e-3)
        frac = done / total if total else 1.0
        text = f"{verb}: {name} - {human(done)} of {human(total)} ({human(speed)}/s)"
        self.q.put(lambda: (self.bar.configure(value=frac * 1000), self.status_var.set(text)))

    # ---- settings
    def _save_cfg(self):
        self.cfg.update(name=self.name_var.get().strip(), dest=self.dest,
                        auto_accept=bool(self.auto_var.get()))
        save_config(self.cfg)

    def _save_name(self):
        name = self.name_var.get().strip() or socket.gethostname()
        self.name_var.set(name)
        self.disc.name = name
        self._save_cfg()

    def _choose_dest(self):
        d = filedialog.askdirectory(initialdir=self.dest, title="Save received files to")
        if d:
            self.dest = str(Path(d))
            self.dest_var.set(self.dest)
            self._save_cfg()

    def _open_dest(self):
        Path(self.dest).mkdir(parents=True, exist_ok=True)
        try:
            open_folder(self.dest)
        except OSError as e:
            messagebox.showerror(APP, f"Could not open folder: {e}")

    def _check_discovery(self):
        if self.disc.error:
            self.log(f"Network discovery unavailable ({self.disc.error}). You can still type an IP address.")

    # ---- firewall
    def _firewall_check(self, offer=True):
        status = firewall_status(self.cfg)
        self.q.put(lambda: self._show_firewall(status, offer))

    def _show_firewall(self, status, offer):
        self.fw_var.set({"ok": "Allowed (local network only)",
                         "missing": "Not allowed yet - other computers can't send to you directly",
                         "none": "No firewall active",
                         "unknown": "Unknown"}[status])
        if status == "missing":
            self.fw_btn.grid()
        else:
            self.fw_btn.grid_remove()
        if status == "missing" and offer and not self.cfg.get("firewall_declined"):
            if messagebox.askyesno(APP, "To receive files from other computers, Span must be "
                                        "allowed through the firewall (ports 50505-50506, local network "
                                        "only).\n\nAllow it now? You'll be asked for administrator "
                                        "permission.", parent=self.root):
                self._firewall_allow()
            else:
                self.cfg["firewall_declined"] = True
                self._save_cfg()
                self.log("Firewall not changed. Click 'Allow...' next to Firewall any time.")

    def _firewall_allow(self):
        self.fw_var.set("Waiting for administrator permission...")
        self.fw_btn.configure(state="disabled")

        def work():
            ok, msg = firewall_allow(self.cfg)
            if ok:
                self.cfg.pop("firewall_declined", None)

            def done():
                self._save_cfg()
                self.fw_btn.configure(state="normal")
                self.log(msg if ok else f"Firewall not changed: {msg}")
                threading.Thread(target=self._firewall_check, args=(False,), daemon=True).start()
            self.q.put(done)
        threading.Thread(target=work, daemon=True).start()

    # ---- relay
    def _start_relay(self):
        if self.relay:
            self.relay.stop()
            self.relay = None
        address = self.cfg.get("relay_address", "")
        if not self.cfg.get("relay_enabled") or not address:
            self.relay_var.set("Off - local network only")
            return

        def status(s):
            self.q.put(lambda: self.relay_var.set(f"{address} - {s}"))
        try:
            self.relay = RelayClient(address, self.cfg.get("relay_password", ""), self.disc.id,
                                     lambda: self.disc.name, self._relay_incoming, status).start()
        except ValueError as e:
            self.relay_var.set(f"Invalid relay address: {e}")

    def _relay_incoming(self, sock):
        self.recv.handle_socket(sock, "via relay")

    def _relay_dialog(self):
        win = tk.Toplevel(self.root)
        win.title("Relay server")
        win.transient(self.root)
        win.resizable(False, False)
        enabled = tk.BooleanVar(value=bool(self.cfg.get("relay_enabled")))
        addr = tk.StringVar(value=self.cfg.get("relay_address", ""))
        pw = tk.StringVar(value=self.cfg.get("relay_password", ""))
        f = ttk.Frame(win, padding=14)
        f.pack(fill="both")
        ttk.Label(f, justify="left", text=(
            "A relay server lets you send files to computers outside this network.\n"
            "Transfers stay end-to-end encrypted: the relay cannot read them.\n"
            "Computers on your local network are still reached directly.\n\n"
            "Run the relay on your server with:  python3 span.py relay --password ...")
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        ttk.Checkbutton(f, text="Use a relay server", variable=enabled).grid(row=1, column=0, columnspan=2,
                                                                             sticky="w", pady=(0, 6))
        ttk.Label(f, text="Address (host:port):").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Entry(f, textvariable=addr, width=34).grid(row=2, column=1, sticky="ew")
        ttk.Label(f, text="Relay password:").grid(row=3, column=0, sticky="w", pady=3)
        ttk.Entry(f, textvariable=pw, show="*", width=34).grid(row=3, column=1, sticky="ew")

        def save():
            a = addr.get().strip()
            if enabled.get():
                try:
                    parse_target(a, RELAY_PORT)
                except ValueError as e:
                    messagebox.showerror(APP, f"Relay address: {e}", parent=win)
                    return
            self.cfg.update(relay_enabled=bool(enabled.get()), relay_address=a, relay_password=pw.get())
            self._save_cfg()
            win.destroy()
            self._start_relay()
            self.log("Relay turned on." if enabled.get() else "Relay turned off - local network only.")

        b = ttk.Frame(f)
        b.grid(row=4, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(b, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(b, text="Save", command=save).pack(side="right", padx=4)
        win.grab_set()

    # ---- peers
    def _current_peers(self):
        local = [dict(p, via="lan") for p in self.disc.list_peers()]
        local_ids = {p["id"] for p in local}
        remote = [dict(p, via="relay") for p in (self.relay.peers if self.relay else [])
                  if p["id"] not in local_ids]  # same computer on both: prefer the direct route
        return local + sorted(remote, key=lambda p: p["name"].lower())

    @staticmethod
    def _peer_key(peers):
        return [(p["via"], p["id"], p["name"], p.get("ip")) for p in peers]

    def _refresh_peers(self):
        peers = self._current_peers()
        if self._peer_key(peers) != self._peer_key(self.peers) or self.peer_box.size() == 0:
            sel = self.peer_box.curselection()
            sel_id = self.peers[sel[0]]["id"] if sel and sel[0] < len(self.peers) else None
            self.peers = peers
            self.peer_box.delete(0, "end")
            for i, p in enumerate(peers):
                where = p["ip"] if p["via"] == "lan" else "via relay"
                self.peer_box.insert("end", f"  {p['name']}    {where}")
                if p["id"] == sel_id:
                    self.peer_box.selection_set(i)
            if not peers:
                self.peer_box.insert("end", "  Searching... open this app on the other computer too.")
                self.peer_box.itemconfig(0, foreground="gray")
        self.root.after(1000, self._refresh_peers)

    def _on_peer_select(self, _e=None):
        sel = self.peer_box.curselection()
        if sel and sel[0] < len(self.peers):
            p = self.peers[sel[0]]
            if p["via"] == "lan":
                self.relay_choice = None
                self.target_var.set(p["ip"] if p["port"] == TRANSFER_PORT else f"{p['ip']}:{p['port']}")
            else:
                text = f"relay: {p['name']}"
                self.relay_choice = (text, p["id"])
                self.target_var.set(text)
        elif sel:
            self.peer_box.selection_clear(0, "end")

    # ---- sending
    def _send_files_dialog(self):
        paths = filedialog.askopenfilenames(title="Choose files to send")
        if paths:
            self._start_send(list(paths))

    def _send_folder_dialog(self):
        path = filedialog.askdirectory(title="Choose a folder to send")
        if path:
            self._start_send([path])

    def _set_busy(self, busy):
        self.btn_files.configure(state="disabled" if busy else "normal")
        self.btn_folder.configure(state="disabled" if busy else "normal")
        self.btn_cancel.configure(state="normal" if busy else "disabled")

    def _start_send(self, paths):
        if self.sending:
            return
        target = self.target_var.get().strip()
        if not target:
            messagebox.showwarning(APP, "Click a computer in the list, or type its IP address.")
            return
        host = port = connect = None
        if self.relay_choice and target == self.relay_choice[0]:
            if not self.relay:
                messagebox.showerror(APP, "The relay is turned off.")
                return
            relay, rid = self.relay, self.relay_choice[1]
            connect = lambda: relay.connect_to(rid)  # noqa: E731
        else:
            try:
                host, port = parse_target(target)
            except ValueError as e:
                messagebox.showerror(APP, str(e))
                return
        self._save_name()
        ctl, name, state = SendControl(), self.name_var.get(), {}
        self.sending = ctl
        self._set_busy(True)
        self.bar.configure(value=0)
        self.status_var.set(f"Connecting to {target}...")
        self.log(f"Sending to {target}...")
        t0 = time.time()

        def on_code(code):
            def show():
                self.status_var.set(f"Waiting for {target} to accept...   Verification code: {code}")
                self.log(f"Encrypted connection up. Verification code: {code} "
                         "(it should match the code on the other computer)")
            self.q.put(show)

        def work():
            try:
                n, total = send_files(host, port, paths, name,
                                      lambda d, t, r: self._progress("Sending", d, t, r, state), ctl,
                                      on_code=on_code, connect=connect)
                secs = time.time() - state.get("t0", t0)
                msg, ok = f"Sent {n} file(s), {human(total)} to {target} in {fmt_secs(secs)}.", True
            except Cancelled:
                msg, ok = "Send cancelled.", False
            except Exception as e:  # noqa: BLE001
                msg, ok = f"Send failed: {friendly(e)}", False
            self.q.put(lambda: self._send_finished(msg, ok))

        threading.Thread(target=work, daemon=True).start()

    def _send_finished(self, msg, ok):
        self.sending = None
        self._set_busy(False)
        self.bar.configure(value=1000 if ok else 0)
        self.status_var.set(msg)
        self.log(msg)

    def _cancel(self):
        if self.sending:
            self.sending.cancel()

    # ---- receiving (called from receiver threads)
    def _ask_accept(self, sender, ip, files, total, code):
        def ask():
            if self.auto_var.get():
                return True
            listing = "\n".join(f"   {r.as_posix()}  ({human(s)})" for r, s in files[:8])
            if len(files) > 8:
                listing += f"\n   ...and {len(files) - 8} more"
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(800, lambda: self.root.attributes("-topmost", False))
            return messagebox.askyesno(
                APP, f"{sender} ({ip}) wants to send you {len(files)} file(s), "
                     f"{human(total)} in total:\n\n{listing}\n\n"
                     f"Verification code:  {code}\n"
                     f"(it should match the code on the sender's screen)\n\nAccept?", parent=self.root)
        return bool(self._call_main(ask))

    def _recv_event(self, kind, **i):
        if kind == "progress":
            st = self.rx_state.setdefault(i["tid"], {})
            self._progress(f"Receiving from {i['peer']}", i["done"], i["total"], i["name"], st)
            return
        if kind in ("done", "error"):
            self.rx_state.pop(i["tid"], None)
        if kind == "start":
            msg = (f"Receiving {i['count']} file(s), {human(i['total'])} from {i['peer']} "
                   f"(verification code {i['code']})...")
        elif kind == "done":
            msg = (f"Received {i['count']} file(s), {human(i['total'])} from {i['peer']} "
                   f"in {fmt_secs(i['seconds'])}. Saved to {i['dest']}")
        else:
            msg = f"Receive from {i['peer']} failed: {i['error']}"

        def show():
            self.log(msg)
            if kind != "start":
                self.status_var.set(msg)
                self.bar.configure(value=1000 if kind == "done" else 0)
        self.q.put(show)

    def _close(self):
        self._save_cfg()
        self.disc.stop()
        self.recv.stop()
        if self.relay:
            self.relay.stop()
        if self.sending:
            self.sending.cancel()
        self.root.destroy()


def ensure_deps_gui(root):
    """Offer to pip-install missing packages with a small progress window.

    Returns True when encryption is available (the window may still lack Qt).
    """
    missing = [p for p, ok in (("cryptography", load_crypto()), ("PySide6", have_qt())) if not ok]
    if not missing:
        return True
    what = {"cryptography": "cryptography (encrypts every transfer)",
            "PySide6": "PySide6 (the app's window, about 70 MB)"}
    if not messagebox.askyesno(APP, "Span needs these free Python packages:\n\n  "
                                    + "\n  ".join(what[m] for m in missing)
                                    + "\n\nDownload and install them now via pip?"):
        return load_crypto()
    win = tk.Toplevel(root)
    win.title(APP)
    ttk.Label(win, text="Installing, this can take a minute...").pack(padx=24, pady=(18, 8))
    bar = ttk.Progressbar(win, mode="indeterminate", length=280)
    bar.pack(padx=24, pady=(0, 18))
    bar.start(12)
    result = {}
    threading.Thread(target=lambda: result.update(r=install_crypto(tuple(missing))), daemon=True).start()
    while "r" not in result:
        root.update()
        time.sleep(0.03)
    win.destroy()
    ok, msg = result["r"]
    if not ok:
        messagebox.showerror(APP, f"Could not install automatically:\n\n{msg}")
    return load_crypto()


def run_gui(classic=False):
    if not classic and load_crypto() and have_qt():
        import span_qt  # the dual-pane window
        sys.exit(span_qt.main(sys.modules[__name__]))
    if tk is None:
        sys.exit("No window toolkit available. Install PySide6 (pip install PySide6 cryptography) or "
                 "tkinter (sudo apt install python3-tk), or use the command line: python span.py --help")
    root = tk.Tk()
    try:
        ttk.Style(root).theme_use("vista" if sys.platform.startswith("win") else "clam")
    except tk.TclError:
        pass
    try:
        root.iconphoto(True, tk.PhotoImage(file=str(asset("icon.png"))))
    except tk.TclError:
        pass
    root.withdraw()
    if not ensure_deps_gui(root):
        root.destroy()
        return
    if not classic and have_qt():
        root.destroy()
        import span_qt
        sys.exit(span_qt.main(sys.modules[__name__]))
    root.deiconify()  # fallback: the classic single-window app
    App(root)
    root.mainloop()


# ------------------------------------------------------------------------- CLI

class CliProgress:
    def __init__(self, verb):
        self.verb, self.t0, self.last = verb, None, 0.0

    def __call__(self, done, total, name):
        now = time.time()
        self.t0 = self.t0 or now
        if now - self.last < 0.3 and done < total:
            return
        self.last = now
        pct = 100 * done / total if total else 100
        speed = done / max(now - self.t0, 1e-3)
        line = f"\r{self.verb} {pct:5.1f}%  {human(done)} / {human(total)}  {human(speed)}/s  {name[-40:]}"
        print(line.ljust(110), end="", flush=True)


def ensure_crypto_cli():
    if load_crypto():
        return
    print("The 'cryptography' package is needed for encryption.")
    if not sys.stdin.isatty() or input("Install it now with pip? [Y/n] ").strip().lower() not in ("", "y", "yes"):
        sys.exit("Install it with: python -m pip install cryptography")
    print("Installing...")
    ok, msg = install_crypto()
    if not ok:
        sys.exit(f"Could not install it: {msg}")


def relay_settings(args):
    """(address, password) from command-line flags or the saved config, or None."""
    if getattr(args, "no_relay", False):
        return None
    cfg = load_config()
    if getattr(args, "relay", None):
        pw = args.relay_password or os.environ.get("SPAN_RELAY_PASSWORD") or cfg.get("relay_password", "")
        return args.relay, pw
    if cfg.get("relay_enabled") and cfg.get("relay_address"):
        return cfg["relay_address"], cfg.get("relay_password", "")
    return None


def cli_receive(args):
    ensure_crypto_cli()
    name = args.name or socket.gethostname()
    dest = Path(args.dir).expanduser().resolve() if args.dir else default_download_dir()
    prompt_lock = threading.Lock()
    bars = {}

    def ask(sender, ip, files, total, code):
        if args.yes:
            return True
        with prompt_lock:
            print(f"\n{sender} ({ip}) wants to send {len(files)} file(s), {human(total)}:")
            for rel, size in files[:10]:
                print(f"   {rel.as_posix()}  ({human(size)})")
            if len(files) > 10:
                print(f"   ...and {len(files) - 10} more")
            print(f"Verification code: {code}  (should match the sender's screen)")
            try:
                return input("Accept? [y/N] ").strip().lower() in ("y", "yes")
            except EOFError:
                return False

    def events(kind, **i):
        if kind == "start":
            print(f"Receiving {i['count']} file(s), {human(i['total'])} from {i['peer']} (code {i['code']})")
        elif kind == "progress":
            bars.setdefault(i["tid"], CliProgress("Receiving"))(i["done"], i["total"], i["name"])
        elif kind == "done":
            bars.pop(i["tid"], None)
            print(f"\nDone: saved {i['count']} file(s) to {i['dest']} in {fmt_secs(i['seconds'])}")
        elif kind == "error":
            bars.pop(i["tid"], None)
            print(f"\nTransfer from {i['peer']} failed: {i['error']}")

    disc = Discovery(name, port=args.port).start()
    recv = Receiver(lambda: dest, ask, events, port=args.port)
    try:
        recv.start()
    except OSError as e:
        sys.exit(f"Cannot listen on port {args.port}: {e}")
    relay = None
    rs = relay_settings(args)
    if rs:
        relay = RelayClient(rs[0], rs[1], disc.id, lambda: name,
                            lambda s: recv.handle_socket(s, "via relay"),
                            lambda s: print(f"[relay {rs[0]}] {s}")).start()
    print(f"Receiving as '{name}' on {', '.join(local_ips()) or '?'} (port {args.port}), encrypted.")
    print(f"Files are saved to {dest}. {'Auto-accepting. ' if args.yes else ''}Press Ctrl+C to stop.")
    if firewall_status(load_config()) == "missing":
        print("Note: the firewall may block other computers. Run:  python span.py firewall")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        disc.stop()
        if relay:
            relay.stop()
        print("\nStopped.")


def discover(wait):
    disc = Discovery("cli", announce=False).start()
    time.sleep(wait)
    disc.stop()
    if disc.error:
        print(f"(discovery unavailable: {disc.error})", file=sys.stderr)
    return disc.list_peers()


def discover_relay(rs):
    try:
        host, port = parse_target(rs[0], RELAY_PORT)
        return relay_list(host, port, rs[1])
    except Exception as e:  # noqa: BLE001
        print(f"(relay {rs[0]} unavailable: {friendly(e)})", file=sys.stderr)
        return []


def cli_list(args):
    print(f"Looking for computers for {args.wait:g} seconds...")
    peers = discover(args.wait)
    rs = relay_settings(args)
    remote = discover_relay(rs) if rs else []
    if not peers and not remote:
        print("None found. Make sure the app is running on the other computer and the firewall allows it.")
    for p in peers:
        print(f"  {p['name']:<30} {p['ip']}:{p['port']}")
    for p in remote:
        print(f"  {p['name']:<30} via relay")


def cli_send(args):
    ensure_crypto_cli()
    target, connect = args.target, None
    try:
        host, port = parse_target(target)
        ipaddress.ip_address(host)
    except ValueError:
        # Not an IP: look for that name on the local network, then on the relay.
        match = [p for p in discover(args.wait) if p["name"].lower() == target.lower()]
        rs = relay_settings(args)
        if match:
            host, port = match[0]["ip"], match[0]["port"]
            print(f"Found {match[0]['name']} on the local network at {host}")
        elif rs and (rmatch := [p for p in discover_relay(rs) if p["name"].lower() == target.lower()]):
            rhost, rport = parse_target(rs[0], RELAY_PORT)
            rid = rmatch[0]["id"]
            connect = lambda: relay_connect(rhost, rport, rs[1], rid)  # noqa: E731
            host, port = rs[0], None
            print(f"Found {rmatch[0]['name']} via relay {rs[0]}")
        else:
            host, port = parse_target(target)  # fall back to DNS / hostname
    if args.port and not connect:
        port = args.port
    print(f"Connecting to {target}...")

    def on_code(code):
        print(f"Encrypted. Verification code: {code}  (should match the receiver's screen)")
        print("Waiting for the other side to accept...")
    try:
        n, total = send_files(host, port, args.paths, args.name or socket.gethostname(),
                              CliProgress("Sending"), on_code=on_code, connect=connect)
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
    except Exception as e:  # noqa: BLE001
        sys.exit(f"\nSend failed: {friendly(e)}")
    print(f"\nSent {n} file(s), {human(total)}.")


def cli_relay(args):
    password = args.password or os.environ.get("SPAN_RELAY_PASSWORD", "")
    try:
        RelayServer(args.port, password).serve()
    except KeyboardInterrupt:
        print("\nStopped.")
    except OSError as e:
        sys.exit(f"Cannot listen on port {args.port}: {e}")


def cli_firewall(_args):
    cfg = load_config()
    status = firewall_status(cfg)
    if status == "ok":
        print("Already allowed through the firewall.")
        return
    if status == "none":
        print("No active firewall found - nothing to do.")
        return
    print("Allowing Span through the firewall (you may be asked for admin rights)...")
    ok, msg = firewall_allow(cfg, use_terminal=True)
    save_config(cfg)
    print(msg if ok else f"Failed: {msg}")
    if not ok:
        sys.exit(1)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="span", description=f"{APP}: send files over your network, encrypted.")
    sub = ap.add_subparsers(dest="cmd")
    g = sub.add_parser("gui", help="open the window (default)")
    g.add_argument("--classic", action="store_true", help="use the simple tkinter window")

    def relay_opts(p):
        p.add_argument("--relay", metavar="HOST:PORT", help="also use this relay server (default: saved setting)")
        p.add_argument("--relay-password", help="relay password (or set SPAN_RELAY_PASSWORD)")
        p.add_argument("--no-relay", action="store_true", help="local network only, ignore saved relay")

    r = sub.add_parser("receive", help="wait for incoming files (no window)")
    r.add_argument("-d", "--dir", help=f"folder to save into (default: {default_download_dir()})")
    r.add_argument("-y", "--yes", action="store_true", help="accept every transfer without asking")
    r.add_argument("-n", "--name", help="name shown to other computers (default: hostname)")
    r.add_argument("-p", "--port", type=int, default=TRANSFER_PORT)
    relay_opts(r)

    s = sub.add_parser("send", help="send files or folders")
    s.add_argument("target", help="computer name (as shown by 'list'), IP, or IP:port")
    s.add_argument("paths", nargs="+", help="files and/or folders")
    s.add_argument("-n", "--name", help="your name shown to the receiver")
    s.add_argument("-p", "--port", type=int, help="override port")
    s.add_argument("-w", "--wait", type=float, default=3.0, help="seconds to search the local network")
    relay_opts(s)

    li = sub.add_parser("list", help="show computers running the app")
    li.add_argument("-w", "--wait", type=float, default=3.0)
    relay_opts(li)

    rl = sub.add_parser("relay", help="run a relay server (on a machine reachable from the internet)")
    rl.add_argument("-p", "--port", type=int, default=RELAY_PORT)
    rl.add_argument("--password", help="clients must know this to use the relay (or set SPAN_RELAY_PASSWORD)")

    sub.add_parser("firewall", help="allow the app through this computer's firewall")

    args = ap.parse_args(argv)
    {"receive": cli_receive, "send": cli_send, "list": cli_list, "relay": cli_relay,
     "firewall": cli_firewall}.get(args.cmd, lambda a: run_gui(getattr(a, "classic", False)))(args)


if __name__ == "__main__":
    main()
