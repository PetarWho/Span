// The phone's side of an approved browse session on another computer's
// shared folder. Mirrors RemoteSession in span.py.
import 'dart:async';
import 'dart:io';

import 'channel.dart';
import 'transfer.dart';
import 'util.dart';

class RemoteSession {
  final String myName;
  final Future<Conn> Function() dial;
  SecureChannel? _ch;
  String sid = '', token = '', remoteName = '', rootName = '';
  bool closed = false;
  Timer? _pinger;
  Future<void> _queue = Future.value(); // one request at a time on the browse connection

  RemoteSession(this.myName, this.dial);

  factory RemoteSession.direct(String myName, String host, int port) =>
      RemoteSession(myName, () => Conn.connect(host, port));

  /// Connects and waits for the other side to tap Allow.
  Future<void> open({void Function(String code)? onCode, CancelToken? cancel}) async {
    try {
      final ch = await openChannel(dial, cancel: cancel, onCode: onCode);
      try {
        await ch.sendJson({'magic': magic, 'op': 'browse', 'sender': myName});
        final resp = await ch.recvJson(acceptWait);
        if (resp['ok'] != true) throw SpanError('${resp['error'] ?? 'the other computer declined'}');
        sid = '${resp['session']}';
        token = '${resp['token']}';
        remoteName = '${resp['name'] ?? '?'}';
        rootName = '${resp['root'] ?? ''}';
      } catch (_) {
        ch.conn.destroy();
        rethrow;
      }
      _ch = ch;
      _pinger = Timer.periodic(const Duration(seconds: 20), (_) {
        _request({'op': 'ping'}).then((_) {}, onError: (_) {}); // a failed ping marks the session closed
      });
    } catch (e) {
      if (cancel?.cancelled ?? false) throw Cancelled();
      rethrow;
    }
  }

  Future<Map<String, dynamic>> _request(Map<String, dynamic> obj) {
    final done = Completer<Map<String, dynamic>>();
    _queue = _queue.then((_) async {
      if (closed) {
        done.completeError(SpanError('disconnected from the other computer'));
        return;
      }
      try {
        await _ch!.sendJson(obj);
        final resp = await _ch!.recvJson(ioTimeout);
        if (resp['ok'] != true) {
          done.completeError(SpanError('${resp['error'] ?? 'request failed'}'));
        } else {
          done.complete(resp);
        }
      } catch (e) {
        _shutdown();
        done.completeError(e);
      }
    });
    return done.future;
  }

  Future<List<Map<String, dynamic>>> list([String rel = '']) async =>
      ((await _request({'op': 'list', 'path': rel}))['entries'] as List).cast<Map<String, dynamic>>();

  Future<void> mkdir(String rel) => _request({'op': 'mkdir', 'path': rel});

  void _shutdown() {
    closed = true;
    _pinger?.cancel();
    _ch?.conn.destroy();
  }

  Future<void> close() async {
    if (closed) return;
    try {
      await _ch?.sendJson({'op': 'bye'}).timeout(const Duration(seconds: 2));
    } catch (_) {}
    _shutdown();
  }

  /// Sends local files/folders into [destRel] of the other computer's share.
  Future<(int, int)> upload(List<String> paths, String destRel, {Progress? progress, CancelToken? cancel}) =>
      sendFiles(dial, paths, myName,
          progress: progress,
          cancel: cancel,
          extra: {'op': 'put', 'session': sid, 'dest': destRel},
          authToken: token);

  /// Fetches files/folders from the other computer's share into [localDir].
  Future<(int, int)> download(List<String> relPaths, String localDir,
      {Progress? progress, CancelToken? cancel}) async {
    try {
      final ch = await openChannel(dial, cancel: cancel);
      try {
        await ch.sendJson({
          'magic': magic, 'op': 'get', 'sender': myName, 'session': sid, 'paths': relPaths, //
          'auth': await sessionMac(token, ch.binding, 'client'),
        });
        final resp = await ch.recvJson(ioTimeout);
        if (resp['ok'] != true) throw SpanError('${resp['error'] ?? 'download refused'}');
        if (!macEquals('${resp['proof'] ?? ''}', await sessionMac(token, ch.binding, 'server'))) {
          throw TamperedError('could not verify the other computer');
        }
        final files = parseFileList(resp['files'] as List);
        if (files.isEmpty) throw SpanError('nothing to download (empty folder)');
        await Directory(localDir).create(recursive: true);
        await receiveFiles(ch, localDir, files, progress);
        await ch.sendJson({'ok': true});
        return (files.length, files.fold<int>(0, (a, f) => a + f.size));
      } finally {
        ch.conn.destroy();
      }
    } catch (e) {
      if (cancel?.cancelled ?? false) throw Cancelled();
      rethrow;
    }
  }
}
