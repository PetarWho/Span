// Optional relay client: keeps this device registered on a relay server so
// computers elsewhere can reach it. The relay only forwards encrypted bytes.
// Mirrors RelayClient / relay_connect in span.py.
import 'dart:async';

import 'channel.dart';
import 'discovery.dart';
import 'util.dart';

/// Asks the relay for a byte pipe to another registered device.
Future<Conn> relayConnect(String host, int port, String password, String targetId) async {
  final c = await Conn.connect(host, port);
  try {
    c.writeLine({'op': 'connect', 'password': password, 'target': targetId});
    await c.flush();
    final resp = await c.readLine(timeout: const Duration(seconds: 30));
    if (resp['ok'] != true) throw SpanError('relay: ${resp['error'] ?? 'refused'}');
    return c; // the same reader continues, so nothing after "ok" is lost
  } catch (_) {
    c.destroy();
    rethrow;
  }
}

class RelayClient {
  final String address, password, ident;
  final String Function() getName;
  final void Function(Conn conn) onIncoming;
  final void Function(String status) onStatus;
  late final String host;
  late final int port;
  List<Peer> peers = [];
  bool _stopped = false;
  Conn? _conn;

  RelayClient(this.address, this.password, this.ident, this.getName, this.onIncoming, this.onStatus) {
    final (h, p) = parseTarget(address, relayPort);
    host = h;
    port = p;
  }

  void start() => _loop();

  void stop() {
    _stopped = true;
    _conn?.destroy();
  }

  Future<Conn> connectTo(String targetId) => relayConnect(host, port, password, targetId);

  Future<void> _loop() async {
    var delay = 2;
    while (!_stopped) {
      onStatus('connecting...');
      Timer? pinger;
      try {
        final c = await Conn.connect(host, port);
        _conn = c;
        c.writeLine({'op': 'register', 'password': password, 'id': ident, 'name': getName()});
        await c.flush();
        final resp = await c.readLine(timeout: const Duration(seconds: 20));
        if (resp['ok'] != true) throw SpanError('${resp['error'] ?? 'refused'}');
        delay = 2;
        onStatus('connected');
        pinger = Timer.periodic(const Duration(seconds: 25), (_) {
          try {
            c.writeLine({'op': 'ping', 'name': getName()});
          } catch (_) {}
        });
        while (!_stopped) {
          final msg = await c.readLine(timeout: const Duration(seconds: 90));
          if (msg['op'] == 'peers') {
            peers = [
              for (final p in (msg['peers'] as List? ?? []))
                if (p['id'] != ident) Peer(id: '${p['id']}', name: '${p['name'] ?? '?'}', via: 'relay')
            ];
          } else if (msg['op'] == 'incoming') {
            _attach('${msg['session']}');
          }
        }
      } catch (e) {
        if (_stopped) break;
        onStatus('not connected (${friendly(e)}), retrying');
      } finally {
        pinger?.cancel();
        peers = [];
        _conn?.destroy();
      }
      await Future.delayed(Duration(seconds: delay));
      delay = delay * 2 > 30 ? 30 : delay * 2;
    }
  }

  Future<void> _attach(String session) async {
    try {
      final c = await Conn.connect(host, port);
      c.writeLine({'op': 'attach', 'password': password, 'session': session});
      await c.flush();
      final resp = await c.readLine(timeout: const Duration(seconds: 20));
      if (resp['ok'] == true) {
        c.peerIp = 'via relay'; // same Conn, so bytes already buffered are kept
        onIncoming(c);
      } else {
        c.destroy();
      }
    } catch (_) {}
  }
}
