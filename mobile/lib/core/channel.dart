// Connection + end-to-end encrypted channel. Byte-for-byte the same as
// SecureChannel in span.py:
//
//   client -> PROTO + SHA256(client_pub)
//   server -> PROTO + server_pub
//   client -> client_pub                 (server checks the hash)
//   HKDF-SHA256(shared, salt = c_pub + s_pub, info = "span1 session keys", 100 bytes)
//     [0:32] client->server key  [32:64] server->client key
//     [64:68] verification code  [68:100] channel binding
//   records: 4-byte big-endian length + AES-256-GCM(ciphertext || tag),
//   nonce = 12-byte big-endian message counter per direction.
import 'dart:async' as async;
import 'dart:convert';
import 'dart:io';
import 'dart:math';
import 'dart:typed_data';

import 'package:cryptography/cryptography.dart';

import 'util.dart';

/// Reads exact byte counts from a socket, keeping any extra bytes buffered so
/// the same reader can be handed from the relay handshake to the channel.
class ByteReader {
  final async.StreamIterator<Uint8List> _it;
  Uint8List _buf = Uint8List(0);
  int _pos = 0;

  ByteReader(Stream<Uint8List> s) : _it = async.StreamIterator(s);

  Future<Uint8List> read(int n, [Duration? timeout]) async {
    final out = BytesBuilder(copy: false);
    var need = n;
    while (need > 0) {
      if (_pos >= _buf.length) {
        final next = _it.moveNext();
        final has = timeout == null
            ? await next
            : await next.timeout(timeout, onTimeout: () => throw async.TimeoutException('timed out'));
        if (!has) throw const SocketException('connection closed unexpectedly');
        _buf = _it.current;
        _pos = 0;
        continue;
      }
      final take = min(need, _buf.length - _pos);
      out.add(Uint8List.sublistView(_buf, _pos, _pos + take));
      _pos += take;
      need -= take;
    }
    return out.takeBytes();
  }
}

class Conn {
  final Socket socket;
  final ByteReader reader;
  String peerIp; // shown to the user; 'via relay' for relayed connections

  Conn(this.socket, {String? label})
      : reader = ByteReader(socket),
        peerIp = label ?? socket.remoteAddress.address {
    try {
      socket.setOption(SocketOption.tcpNoDelay, true);
    } catch (_) {}
  }

  static Future<Conn> connect(String host, int port) async =>
      Conn(await Socket.connect(host, port, timeout: const Duration(seconds: 10)));

  void write(List<int> bytes) => socket.add(bytes);
  Future<void> flush() => socket.flush();
  Future<Uint8List> read(int n, [Duration? t]) => reader.read(n, t);

  /// One JSON line, read byte by byte so nothing after it is consumed (relay).
  Future<Map<String, dynamic>> readLine({int limit = 1 << 20, Duration? timeout}) async {
    final buf = BytesBuilder();
    while (true) {
      final b = await reader.read(1, timeout);
      if (b[0] == 10) break;
      buf.add(b);
      if (buf.length > limit) throw const FormatException('message too long');
    }
    final obj = jsonDecode(utf8.decode(buf.takeBytes()));
    if (obj is! Map<String, dynamic>) throw const FormatException('bad message');
    return obj;
  }

  void writeLine(Map<String, dynamic> obj) => write(utf8.encode('${jsonEncode(obj)}\n'));

  void destroy() {
    try {
      socket.destroy();
    } catch (_) {}
  }
}

class HandshakeError implements Exception {
  final String message;
  HandshakeError(this.message);
  @override
  String toString() => message;
}

class SecureChannel {
  static final _aes = AesGcm.with256bits();
  static final _x25519 = X25519();
  final Conn conn;
  final String code;
  final List<int> binding;
  final SecretKey _tx, _rx;
  int _txN = 0, _rxN = 0;

  SecureChannel._(this.conn, this.code, this.binding, this._tx, this._rx);

  static Future<SecureChannel> handshake(Conn c, {required bool isClient}) async {
    final kp = await _x25519.newKeyPair();
    final pub = (await kp.extractPublicKey()).bytes;
    final helloLen = proto.length + 32;
    List<int> peerPub, cPub, sPub;
    if (isClient) {
      c.write([...proto, ...(await Sha256().hash(pub)).bytes]);
      await c.flush();
      final hello = await c.read(helloLen, handshakeTimeout);
      _checkProto(hello);
      peerPub = hello.sublist(proto.length);
      c.write(pub);
      await c.flush();
      cPub = pub;
      sPub = peerPub;
    } else {
      final hello = await c.read(helloLen, handshakeTimeout);
      _checkProto(hello);
      final commitment = hello.sublist(proto.length);
      c.write([...proto, ...pub]);
      await c.flush();
      peerPub = await c.read(32, handshakeTimeout);
      if (!_equal((await Sha256().hash(peerPub)).bytes, commitment)) {
        throw HandshakeError('handshake failed (key commitment mismatch)');
      }
      cPub = peerPub;
      sPub = pub;
    }
    final shared = await _x25519.sharedSecretKey(
        keyPair: kp, remotePublicKey: SimplePublicKey(peerPub, type: KeyPairType.x25519));
    final sharedBytes = await shared.extractBytes();
    if (sharedBytes.every((b) => b == 0)) throw HandshakeError('handshake failed (invalid key)');
    final okm = await (await Hkdf(hmac: Hmac.sha256(), outputLength: 100).deriveKey(
      secretKey: SecretKey(sharedBytes),
      nonce: [...cPub, ...sPub],
      info: utf8.encode('span1 session keys'),
    ))
        .extractBytes();
    final c2s = SecretKey(okm.sublist(0, 32)), s2c = SecretKey(okm.sublist(32, 64));
    final n = ByteData.sublistView(Uint8List.fromList(okm.sublist(64, 68))).getUint32(0) % 1000000;
    final code = '${(n ~/ 1000).toString().padLeft(3, '0')} ${(n % 1000).toString().padLeft(3, '0')}';
    return SecureChannel._(c, code, okm.sublist(68, 100), isClient ? c2s : s2c, isClient ? s2c : c2s);
  }

  static void _checkProto(List<int> hello) {
    if (!_equal(hello.sublist(0, proto.length), proto)) {
      throw HandshakeError('the other side is not Span, or runs an incompatible version');
    }
  }

  static bool _equal(List<int> a, List<int> b) {
    if (a.length != b.length) return false;
    var diff = 0;
    for (var i = 0; i < a.length; i++) {
      diff |= a[i] ^ b[i];
    }
    return diff == 0;
  }

  static List<int> _nonce(int n) {
    final b = ByteData(12)..setUint64(4, n);
    return b.buffer.asUint8List();
  }

  Future<void> send(List<int> data) async {
    final box = await _aes.encrypt(data, secretKey: _tx, nonce: _nonce(_txN++));
    final len = box.cipherText.length + box.mac.bytes.length;
    final header = ByteData(4)..setUint32(0, len);
    conn.write([...header.buffer.asUint8List(), ...box.cipherText, ...box.mac.bytes]);
    await conn.flush();
  }

  Future<Uint8List> recv([Duration? timeout]) async {
    final n = ByteData.sublistView(await conn.read(4, timeout)).getUint32(0);
    if (n < 16 || n > maxRecord + 16) throw TamperedError('invalid encrypted message size');
    final ct = await conn.read(n, timeout);
    try {
      final clear = await _aes.decrypt(
        SecretBox(Uint8List.sublistView(ct, 0, n - 16),
            nonce: _nonce(_rxN++), mac: Mac(Uint8List.sublistView(ct, n - 16))),
        secretKey: _rx,
      );
      return Uint8List.fromList(clear);
    } on SecretBoxAuthenticationError {
      throw TamperedError('data failed authentication - corrupted or tampered with');
    }
  }

  Future<void> sendJson(Map<String, dynamic> obj) => send(utf8.encode(jsonEncode(obj)));

  Future<Map<String, dynamic>> recvJson([Duration? timeout]) async {
    final obj = jsonDecode(utf8.decode(await recv(timeout)));
    if (obj is! Map<String, dynamic>) throw const FormatException('unexpected message');
    return obj;
  }
}

/// HMAC-SHA256(token, role + binding) as hex: proves a connection belongs to
/// an approved browse session without sending the token again.
Future<String> sessionMac(String token, List<int> binding, String role) async {
  final mac = await Hmac.sha256()
      .calculateMac([...utf8.encode(role), ...binding], secretKey: SecretKey(hexDecode(token)));
  return hexEncode(mac.bytes);
}

bool macEquals(String a, String b) {
  if (a.length != b.length) return false;
  var diff = 0;
  for (var i = 0; i < a.length; i++) {
    diff |= a.codeUnitAt(i) ^ b.codeUnitAt(i);
  }
  return diff == 0;
}
