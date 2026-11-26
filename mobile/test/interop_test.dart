// Interop tests: the Dart protocol code against the real span.py, in both
// directions and through the relay. Needs `python` with `cryptography`.
//
//   flutter test test/interop_test.dart
import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:cryptography/cryptography.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:path/path.dart' as p;
import 'package:span/core/channel.dart';
import 'package:span/core/receiver.dart';
import 'package:span/core/relay.dart';
import 'package:span/core/session.dart';
import 'package:span/core/transfer.dart';
import 'package:span/core/util.dart';

final peerScript = p.join(Directory.current.path, 'test', 'interop', 'py_peer.py');

/// A Python helper process; collects its JSON output lines.
class Py {
  final Process proc;
  final lines = <Map<String, dynamic>>[];
  final _ready = Completer<void>();
  final _result = Completer<Map<String, dynamic>>();
  final _stderr = StringBuffer();
  bool _killed = false;

  Py._(this.proc) {
    // a helper we kill on purpose must not fail whichever test runs next
    _ready.future.ignore();
    _result.future.ignore();
    proc.stdout.transform(utf8.decoder).transform(const LineSplitter()).listen((l) {
      try {
        final m = jsonDecode(l) as Map<String, dynamic>;
        lines.add(m);
        if (m['ready'] == true && !_ready.isCompleted) _ready.complete();
        if (m.containsKey('result') && !_result.isCompleted) _result.complete(m['result']);
      } catch (_) {}
    });
    proc.stderr.transform(utf8.decoder).listen(_stderr.write);
    proc.exitCode.then((code) {
      if (_killed) return;
      if (!_result.isCompleted && code != 0) _result.completeError('python exited $code:\n$_stderr');
      if (!_ready.isCompleted && code != 0) _ready.completeError('python exited $code:\n$_stderr');
    });
  }

  static Future<Py> start(List<String> args) async =>
      Py._(await Process.start('python', ['-u', peerScript, ...args]));

  Future<void> ready() => _ready.future.timeout(const Duration(seconds: 30));
  Future<Map<String, dynamic>> result() => _result.future.timeout(const Duration(seconds: 120));
  void kill() {
    _killed = true;
    proc.kill();
  }
}

Future<String> sha(String path) async => hexEncode((await Sha256().hash(await File(path).readAsBytes())).bytes);

void writeRandom(String path, int size) {
  final r = Random(42);
  File(path)
    ..createSync(recursive: true)
    ..writeAsBytesSync(List.generate(size, (_) => r.nextInt(256)));
}

void main() {
  late Directory tmp;
  setUp(() => tmp = Directory.systemTemp.createTempSync('span_interop'));
  tearDown(() {
    try {
      tmp.deleteSync(recursive: true);
    } catch (_) {}
  });

  Future<void> checkDartClient(RemoteSession s, String share, List<String> codes) async {
    expect(s.remoteName, anyOf('PyPeer', 'PyRelayPeer'));
    expect(s.rootName, 'share');
    expect((await s.list()).map((e) => e['name']), containsAll(['Photos', 'notes.txt']));
    await s.mkdir('FromPhone');
    final src = p.join(tmp.path, 'phone_upload.bin');
    writeRandom(src, 3000000);
    final up = await s.upload([src], 'FromPhone');
    expect(up.$1, 1);
    expect(await sha(p.join(share, 'FromPhone', 'phone_upload.bin')), await sha(src));
    final dl = p.join(tmp.path, 'dl');
    final down = await s.download(['Photos'], dl);
    expect(down.$1, 2);
    expect(await sha(p.join(dl, 'Photos', 'a.bin')), await sha(p.join(share, 'Photos', 'a.bin')));
    expect(File(p.join(dl, 'Photos', 'deep', 'b.txt')).readAsStringSync(), 'deep');
    await expectLater(s.list('../'), completes); // '..' alone just means the share root
    await expectLater(s.list('Photos/../../x'), throwsA(isA<SpanError>()));
    await expectLater(s.download(['../outside.txt'], dl), throwsA(isA<SpanError>()));
    await s.close();
  }

  void makePcShare(String share) {
    writeRandom(p.join(share, 'Photos', 'a.bin'), 1500000);
    File(p.join(share, 'Photos', 'deep', 'b.txt'))
      ..createSync(recursive: true)
      ..writeAsStringSync('deep');
    File(p.join(share, 'notes.txt')).writeAsStringSync('hello');
  }

  test('Dart phone opens a Python PC: browse, upload, download, push', () async {
    final share = p.join(tmp.path, 'share');
    makePcShare(share);
    final py = await Py.start(['receiver', '51606', share]);
    try {
      await py.ready();
      String? myCode;
      final s = RemoteSession.direct('DartPhone', '127.0.0.1', 51606);
      await s.open(onCode: (c) => myCode = c);
      final pyCode = py.lines.firstWhere((l) => l['event'] == 'browse')['code'];
      expect(myCode, pyCode, reason: 'both sides must show the same verification code');
      await checkDartClient(s, share, []);

      // push (send without a session): the PC sees the files and the same code
      final f = p.join(tmp.path, 'pushed.txt');
      File(f).writeAsStringSync('pushed from phone');
      String? pushCode;
      final r = await sendFiles(() => Conn.connect('127.0.0.1', 51606), [f], 'DartPhone', onCode: (c) => pushCode = c);
      expect(r.$1, 1);
      final ev = py.lines.lastWhere((l) => l['event'] == 'push');
      expect(ev['code'], pushCode);
      expect(File(p.join(share, 'pushed.txt')).readAsStringSync(), 'pushed from phone');
    } finally {
      py.kill();
    }
  }, timeout: const Timeout(Duration(minutes: 3)));

  Future<(Receiver, List<String>, String)> startDartReceiver(int port, {String? share}) async {
    share ??= p.join(tmp.path, 'phone_share');
    writeRandom(p.join(share, 'phone.bin'), 2000000);
    File(p.join(share, 'PhoneFolder', 'inner', 'note.txt'))
      ..createSync(recursive: true)
      ..writeAsStringSync('from phone');
    final codes = <String>[];
    final r = Receiver(
      getRoot: () => share!,
      getName: () => 'DartPhone',
      askPush: (s, ip, files, total, code) async {
        codes.add(code);
        return true;
      },
      askBrowse: (s, ip, code) async {
        codes.add(code);
        return true;
      },
      events: (k, i) {},
      port: port,
    );
    await r.start();
    return (r, codes, share);
  }

  void checkPythonClient(Map<String, dynamic> res, List<String> codes, String share) {
    expect(codes, contains(res['code']), reason: 'Python and Dart must derive the same code');
    expect(res['remote'], 'DartPhone');
    expect(res['root'], 'phone_share');
    expect(res['list'], containsAll(['PhoneFolder', 'phone.bin']));
    expect(res['upload'][0], 1);
    expect(res['list_frompy'], ['py_upload.bin']);
    expect(res['download'][0], 2);
    expect(res['dl_nested'], isTrue);
    expect((res['escape'] as List).every((e) => '$e'.startsWith('refused')), isTrue, reason: '${res['escape']}');
  }

  test('Python PC opens the Dart phone: browse, upload, download, push', () async {
    final (r, codes, share) = await startDartReceiver(51616);
    try {
      final py = await Py.start(['client', '51616', tmp.path]);
      final res = await py.result();
      checkPythonClient(res, codes, share);
      expect(res['upload_sha'], await sha(p.join(share, 'FromPy', 'py_upload.bin')));
      expect(res['dl_sha'], await sha(p.join(share, 'phone.bin')));
      expect(res['push'][0], 1);
      expect(File(p.join(share, 'py_upload.bin')).existsSync(), isTrue);
    } finally {
      await r.stop();
    }
  }, timeout: const Timeout(Duration(minutes: 3)));

  test('Dart phone reaches a Python PC through the relay', () async {
    final share = p.join(tmp.path, 'share');
    makePcShare(share);
    final py = await Py.start(['relay', '51627', '51626', share]);
    try {
      await py.ready();
      final s = RemoteSession('DartPhone', () => relayConnect('127.0.0.1', 51627, 'pw', 'py-peer'));
      String? myCode;
      await s.open(onCode: (c) => myCode = c);
      expect(myCode, py.lines.firstWhere((l) => l['event'] == 'browse')['code']);
      await checkDartClient(s, share, []);
    } finally {
      py.kill();
    }
  }, timeout: const Timeout(Duration(minutes: 3)));

  test('Python PC reaches the Dart phone through the relay', () async {
    final relay = await Py.start(['relayserver', '51637']);
    final (r, codes, share) = await startDartReceiver(51636);
    RelayClient? rc;
    try {
      await relay.ready();
      final connected = Completer<void>();
      rc = RelayClient('127.0.0.1:51637', 'pw', 'dart-peer', () => 'DartPhone', r.handleConnection, (st) {
        if (st == 'connected' && !connected.isCompleted) connected.complete();
      })
        ..start();
      await connected.future.timeout(const Duration(seconds: 10));
      final py = await Py.start(['relayclient', '51637', 'dart-peer', tmp.path]);
      final res = await py.result();
      checkPythonClient(res, codes, share);
    } finally {
      rc?.stop();
      await r.stop();
      relay.kill();
    }
  }, timeout: const Timeout(Duration(minutes: 3)));

  test('a modified byte is rejected (tamper detection)', () async {
    final (r, codes, share) = await startDartReceiver(51646);
    // proxy that flips one byte after the handshake
    final proxy = await ServerSocket.bind(InternetAddress.loopbackIPv4, 51647);
    proxy.listen((client) async {
      final up = await Socket.connect('127.0.0.1', 51646);
      var seen = 0;
      client.listen((d) {
        final b = List<int>.from(d);
        if (seen <= 3000 && seen + b.length > 3000) b[3000 - seen] ^= 1;
        seen += b.length;
        up.add(b);
      }, onDone: () => up.destroy());
      up.listen(client.add, onDone: () => client.destroy());
    });
    try {
      final f = p.join(tmp.path, 'big.bin');
      writeRandom(f, 200000);
      await expectLater(sendFiles(() => Conn.connect('127.0.0.1', 51647), [f], 'X'), throwsA(anything));
      expect(File(p.join(share, 'big.bin')).existsSync(), isFalse);
      expect(Directory(share).listSync().where((e) => e.path.endsWith('.part')), isEmpty);
    } finally {
      await proxy.close();
      await r.stop();
    }
  }, timeout: const Timeout(Duration(minutes: 2)));
}
