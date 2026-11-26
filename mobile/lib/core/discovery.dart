// Finds other Span devices on the local network: every copy broadcasts a
// small UDP packet on port 50505 every 2 seconds. Mirrors Discovery in span.py.
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'util.dart';

class Peer {
  final String id, name, ip, via; // via: 'lan' or 'relay'
  final int port;
  DateTime seen;
  Peer({required this.id, required this.name, this.ip = '', this.port = transferPort, this.via = 'lan'})
      : seen = DateTime.now();
}

class Discovery {
  final String id = randomHex(16);
  String name;
  final int port;
  final Map<String, Peer> _peers = {};
  RawDatagramSocket? _socket;
  Timer? _timer;
  String? error;

  Discovery(this.name, {this.port = transferPort});

  Future<void> start() async {
    try {
      _socket = await RawDatagramSocket.bind(InternetAddress.anyIPv4, discoveryPort, reuseAddress: true);
      _socket!.broadcastEnabled = true;
      _socket!.listen((event) {
        if (event != RawSocketEvent.read) return;
        final dg = _socket!.receive();
        if (dg == null) return;
        try {
          final msg = jsonDecode(utf8.decode(dg.data));
          if (msg is! Map || msg['magic'] != magic || msg['id'] == id) return;
          final pid = '${msg['id']}';
          final n = '${msg['name'] ?? '?'}';
          _peers[pid] = Peer(
            id: pid,
            name: n.length > 64 ? n.substring(0, 64) : n,
            ip: dg.address.address,
            port: (msg['port'] as num?)?.toInt() ?? transferPort,
          );
        } catch (_) {}
      });
    } catch (e) {
      error = e.toString();
      return;
    }
    _announce();
    _timer = Timer.periodic(const Duration(seconds: 2), (_) => _announce());
  }

  Future<void> _announce() async {
    final s = _socket;
    if (s == null) return;
    final pkt = utf8.encode(jsonEncode({'magic': magic, 'id': id, 'name': name, 'port': port}));
    final targets = <String>{'255.255.255.255', ...(await localIps()).map((ip) => '${ip.substring(0, ip.lastIndexOf('.'))}.255')};
    for (final t in targets) {
      try {
        s.send(pkt, InternetAddress(t), discoveryPort);
      } catch (_) {}
    }
  }

  List<Peer> peers() {
    final now = DateTime.now();
    _peers.removeWhere((_, p) => now.difference(p.seen).inSeconds > 7);
    return _peers.values.toList()..sort((a, b) => a.name.toLowerCase().compareTo(b.name.toLowerCase()));
  }

  void stop() {
    _timer?.cancel();
    _socket?.close();
  }
}

Future<List<String>> localIps() async {
  try {
    final ifs = await NetworkInterface.list(type: InternetAddressType.IPv4, includeLoopback: false);
    return [
      for (final i in ifs)
        for (final a in i.addresses)
          if (!a.address.startsWith('169.254.')) a.address
    ];
  } catch (_) {
    return [];
  }
}
