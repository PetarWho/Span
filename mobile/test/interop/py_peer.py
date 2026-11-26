"""Python side of the Dart <-> Python interop tests (uses ../../../span.py).

    py_peer.py receiver PORT SHARE              span.py Receiver that allows everything
    py_peer.py relay RELAY_PORT PORT SHARE      relay server + a receiver registered on it as "py-peer"
    py_peer.py relayserver RELAY_PORT           relay server only
    py_peer.py client PORT WORKDIR              span.py client against a (Dart) receiver
    py_peer.py relayclient RELAY_PORT TARGET WORKDIR   same, through the relay

Each mode prints JSON lines on stdout; servers print READY when listening.
"""
import hashlib
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
import span  # noqa: E402

span.load_crypto()


def say(**kw):
    print(json.dumps(kw), flush=True)


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def receiver(port, share):
    def ask_browse(sender, ip, code):
        say(event="browse", sender=sender, code=code)
        return True

    def ask_push(sender, ip, files, total, code):
        say(event="push", sender=sender, code=code, files=[f.as_posix() for f, _ in files])
        return True

    return span.Receiver(lambda: share, ask_push, lambda k, **i: None, port=port,
                         ask_browse=ask_browse, get_name=lambda: "PyPeer").start()


def client_flow(session, workdir, push=None):
    out = {"code": session_code[0], "root": session.root_name, "remote": session.remote_name,
           "list": [e["name"] for e in session.list("")]}
    session.mkdir("FromPy")
    src = os.path.join(workdir, "py_upload.bin")
    with open(src, "wb") as f:
        f.write(os.urandom(2_500_000))
    out["upload"] = list(session.upload([src], "FromPy"))
    out["upload_sha"] = sha(src)
    out["list_frompy"] = [e["name"] for e in session.list("FromPy")]
    dl = os.path.join(workdir, "py_dl")
    os.makedirs(dl, exist_ok=True)
    out["download"] = list(session.download(["phone.bin", "PhoneFolder"], dl))
    out["dl_sha"] = sha(os.path.join(dl, "phone.bin"))
    out["dl_nested"] = os.path.exists(os.path.join(dl, "PhoneFolder", "inner", "note.txt"))
    for bad in ["../outside", "PhoneFolder/../../x"]:
        try:
            session.list(bad)
            out.setdefault("escape", []).append("NOT BLOCKED")
        except Exception as e:  # noqa: BLE001
            out.setdefault("escape", []).append("refused: " + str(e))
    if push:
        out["push"] = list(push(src))
    session.close()
    say(result=out)


session_code = [None]

mode = sys.argv[1]
if mode == "receiver":
    receiver(int(sys.argv[2]), sys.argv[3])
    say(ready=True)
    time.sleep(120)
elif mode in ("relay", "relayserver"):
    rport = int(sys.argv[2])
    threading.Thread(target=span.RelayServer(rport, "pw").serve, daemon=True).start()
    time.sleep(0.3)
    if mode == "relay":
        recv = receiver(int(sys.argv[3]), sys.argv[4])
        rc = span.RelayClient(f"127.0.0.1:{rport}", "pw", "py-peer", lambda: "PyRelayPeer",
                              lambda s: recv.handle_socket(s, "via relay")).start()
        while not rc.connected:
            time.sleep(0.05)
    say(ready=True)
    time.sleep(120)
elif mode == "client":
    port, workdir = int(sys.argv[2]), sys.argv[3]
    s = span.RemoteSession("PyClient", "127.0.0.1", port).open(on_code=lambda c: session_code.__setitem__(0, c))
    client_flow(s, workdir, push=lambda f: span.send_files("127.0.0.1", port, [f], "PyClient"))
elif mode == "relayclient":
    rport, target, workdir = int(sys.argv[2]), sys.argv[3], sys.argv[4]
    dial = lambda: span.relay_connect("127.0.0.1", rport, "pw", target)  # noqa: E731
    s = span.RemoteSession("PyClient", connect=dial).open(on_code=lambda c: session_code.__setitem__(0, c))
    client_flow(s, workdir)
