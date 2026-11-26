// Accepts incoming connections: pushes, browse sessions on the shared folder,
// and uploads/downloads within an approved session. Mirrors Receiver in span.py.
import 'dart:async';
import 'dart:io';

import 'channel.dart';
import 'transfer.dart';
import 'util.dart';

typedef AskPush = Future<bool> Function(String sender, String ip, List<IncomingFile> files, int total, String code);
typedef AskBrowse = Future<bool> Function(String sender, String ip, String code);
typedef ReceiverEvent = void Function(String kind, Map<String, dynamic> info);

class BrowseSession {
  final String sid, token, name, peer;
  final Conn conn;
  BrowseSession(this.sid, this.token, this.name, this.peer, this.conn);
}

class Receiver {
  final String Function() getRoot;
  final String Function() getName;
  final AskPush askPush;
  final AskBrowse askBrowse;
  final ReceiverEvent events;
  final int port;
  ServerSocket? _server;
  final Map<String, BrowseSession> sessions = {};
  int _ids = 0;

  Receiver({
    required this.getRoot,
    required this.getName,
    required this.askPush,
    required this.askBrowse,
    required this.events,
    this.port = transferPort,
  });

  Future<void> start() async {
    _server = await ServerSocket.bind(InternetAddress.anyIPv4, port, shared: false);
    _server!.listen((s) => handleConnection(Conn(s)), onError: (_) {});
  }

  Future<void> stop() async {
    await _server?.close();
    for (final sid in sessions.keys.toList()) {
      closeSession(sid);
    }
  }

  void closeSession(String sid) => sessions.remove(sid)?.conn.destroy();

  String _root() {
    final d = Directory(getRoot());
    if (!d.existsSync()) d.createSync(recursive: true);
    return d.resolveSymbolicLinksSync();
  }

  /// Serves one connection (direct, or handed over by a relay).
  Future<void> handleConnection(Conn conn) async {
    final tid = ++_ids;
    var peer = conn.peerIp;
    SecureChannel ch;
    try {
      ch = await SecureChannel.handshake(conn, isClient: false);
    } catch (_) {
      conn.destroy(); // port scanners, old versions: drop silently
      return;
    }
    try {
      final hdr = await ch.recvJson(ioTimeout);
      if (hdr['magic'] != magic) return;
      final sender = '${hdr['sender'] ?? 'unknown'}';
      final name = sender.length > 64 ? sender.substring(0, 64) : sender;
      peer = '$name (${conn.peerIp})';
      switch (hdr['op'] ?? 'push') {
        case 'push':
          await _servePush(ch, hdr, name, conn.peerIp, peer, tid);
        case 'browse':
          await _serveBrowse(ch, name, conn.peerIp, peer, tid);
        case 'put':
        case 'get':
          final sess = await _checkAuth(hdr, ch);
          if (sess == null) {
            await ch.sendJson({'accept': false, 'ok': false, 'error': 'not allowed - open the folder again'});
            return;
          }
          if (hdr['op'] == 'put') {
            await _servePut(ch, hdr, sess, peer, tid);
          } else {
            await _serveGet(ch, hdr, sess, peer, tid);
          }
      }
    } catch (e) {
      try {
        await ch.sendJson({'ok': false, 'error': e.toString()});
      } catch (_) {}
      events('error', {'tid': tid, 'peer': peer, 'error': friendly(e)});
    } finally {
      conn.destroy();
    }
  }

  Progress _progress(int tid, String peer, String direction) => (d, t, n) =>
      events('progress', {'tid': tid, 'peer': peer, 'done': d, 'total': t, 'name': n, 'direction': direction});

  Future<void> _servePush(SecureChannel ch, Map hdr, String sender, String ip, String peer, int tid) async {
    final files = parseFileList(hdr['files'] as List);
    final total = files.fold<int>(0, (a, f) => a + f.size);
    if (!await askPush(sender, ip, files, total, ch.code)) {
      await ch.sendJson({'accept': false, 'reason': 'declined by user'});
      return;
    }
    final dest = _root();
    await ch.sendJson({'accept': true});
    events('start', {'tid': tid, 'peer': peer, 'count': files.length, 'total': total, 'direction': 'in'});
    final sw = Stopwatch()..start();
    await receiveFiles(ch, dest, files, _progress(tid, peer, 'in'));
    await ch.sendJson({'ok': true});
    events('done', {
      'tid': tid, 'peer': peer, 'count': files.length, 'total': total, 'dest': dest, //
      'seconds': sw.elapsedMilliseconds / 1000, 'direction': 'in',
    });
  }

  Future<BrowseSession?> _checkAuth(Map hdr, SecureChannel ch) async {
    final s = sessions['${hdr['session'] ?? ''}'];
    if (s == null) return null;
    final expect = await sessionMac(s.token, ch.binding, 'client');
    return macEquals('${hdr['auth'] ?? ''}', expect) ? s : null;
  }

  Future<void> _serveBrowse(SecureChannel ch, String sender, String ip, String peer, int tid) async {
    if (!await askBrowse(sender, ip, ch.code)) {
      await ch.sendJson({'ok': false, 'error': 'the other computer declined'});
      return;
    }
    final sid = randomHex(8), token = randomHex(32);
    sessions[sid] = BrowseSession(sid, token, sender, peer, ch.conn);
    events('session_open', {'sid': sid, 'peer': peer, 'name': sender});
    try {
      final root = _root();
      await ch.sendJson({'ok': true, 'session': sid, 'token': token, 'name': getName(), 'root': root.split(RegExp(r'[\\/]')).last});
      while (true) {
        final req = await ch.recvJson(const Duration(seconds: 120)); // the client pings every 20 s
        final op = req['op'];
        if (op == 'bye') break;
        Map<String, dynamic> resp;
        try {
          if (op == 'list') {
            resp = {'ok': true, 'entries': listDir(_root(), '${req['path'] ?? ''}')};
          } else if (op == 'mkdir') {
            Directory(resolveInRoot(_root(), '${req['path'] ?? ''}', mustExist: false)).createSync();
            resp = {'ok': true};
          } else if (op == 'ping') {
            resp = {'ok': true};
          } else {
            resp = {'ok': false, 'error': 'unknown request'};
          }
        } catch (e) {
          resp = {'ok': false, 'error': friendly(e)};
        }
        await ch.sendJson(resp);
      }
    } catch (_) {
      // connection closed or disconnected by us
    } finally {
      sessions.remove(sid);
      events('session_closed', {'sid': sid, 'peer': peer, 'name': sender});
    }
  }

  Future<void> _servePut(SecureChannel ch, Map hdr, BrowseSession s, String peer, int tid) async {
    final dest = resolveInRoot(_root(), '${hdr['dest'] ?? ''}');
    if (!FileSystemEntity.isDirectorySync(dest)) throw SpanError('that folder no longer exists');
    final files = parseFileList(hdr['files'] as List);
    final total = files.fold<int>(0, (a, f) => a + f.size);
    await ch.sendJson({'accept': true, 'proof': await sessionMac(s.token, ch.binding, 'server')});
    events('start', {'tid': tid, 'peer': peer, 'count': files.length, 'total': total, 'direction': 'in'});
    final sw = Stopwatch()..start();
    await receiveFiles(ch, dest, files, _progress(tid, peer, 'in'));
    await ch.sendJson({'ok': true});
    events('done', {
      'tid': tid, 'peer': peer, 'count': files.length, 'total': total, 'dest': dest, //
      'seconds': sw.elapsedMilliseconds / 1000, 'direction': 'in',
    });
  }

  Future<void> _serveGet(SecureChannel ch, Map hdr, BrowseSession s, String peer, int tid) async {
    final root = _root();
    final items = [for (final rel in (hdr['paths'] as List? ?? [])) resolveInRoot(root, '$rel')];
    final files = collectFiles(items, confineTo: root); // skips symlinks pointing outside the share
    final total = files.fold<int>(0, (a, f) => a + f.size);
    await ch.sendJson({
      'ok': true,
      'proof': await sessionMac(s.token, ch.binding, 'server'),
      'total': total,
      'files': [for (final f in files) {'path': f.rel, 'size': f.size}],
    });
    if (files.isEmpty) return;
    events('start', {'tid': tid, 'peer': peer, 'count': files.length, 'total': total, 'direction': 'out'});
    final sw = Stopwatch()..start();
    await streamFiles(ch, files, _progress(tid, peer, 'out'));
    await ch.recvJson(ioTimeout);
    events('done', {
      'tid': tid, 'peer': peer, 'count': files.length, 'total': total, 'dest': '', //
      'seconds': sw.elapsedMilliseconds / 1000, 'direction': 'out',
    });
  }
}
