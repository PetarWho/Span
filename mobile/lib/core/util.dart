// Shared constants and helpers. Mirrors the top of span.py so both speak the
// same protocol: keep the two in sync.
import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:path/path.dart' as p;

const appName = 'Span';
const magic = 'SPAN1';
final List<int> proto = utf8.encode('SPAN1\n'); // first bytes of every encrypted connection
const discoveryPort = 50505;
const transferPort = 50506;
const relayPort = 50507;
const chunkSize = 1024 * 1024;
const maxRecord = 16 * 1024 * 1024;
const handshakeTimeout = Duration(seconds: 15);
const ioTimeout = Duration(seconds: 60);
const acceptWait = Duration(seconds: 300); // how long a sender waits for Accept/Allow

typedef Progress = void Function(int done, int total, String name);

String human(num n) {
  var v = n.toDouble();
  for (final unit in ['B', 'KB', 'MB', 'GB', 'TB']) {
    if (v < 1024 || unit == 'TB') {
      return unit == 'B' ? '${v.toStringAsFixed(0)} B' : '${v.toStringAsFixed(1)} $unit';
    }
    v /= 1024;
  }
  return '$n B';
}

String randomHex(int bytes) {
  final r = Random.secure();
  return List.generate(bytes, (_) => r.nextInt(256).toRadixString(16).padLeft(2, '0')).join();
}

List<int> hexDecode(String hex) =>
    List.generate(hex.length ~/ 2, (i) => int.parse(hex.substring(i * 2, i * 2 + 2), radix: 16));

String hexEncode(List<int> bytes) => bytes.map((b) => b.toRadixString(16).padLeft(2, '0')).join();

const _reserved = {
  'CON', 'PRN', 'AUX', 'NUL', //
  'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
  'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9',
};

/// Turns a '/'-separated path from the network into safe relative parts.
/// Rejects '..' and strips anything that could escape the target folder.
List<String> safeRelParts(String path) {
  final parts = <String>[];
  for (var part in path.split('/')) {
    if (part == '..') throw FormatException("unsafe path from sender: '$path'");
    if (part.isEmpty || part == '.') continue;
    final b = StringBuffer();
    for (final ch in part.runes) {
      final c = String.fromCharCode(ch);
      b.write(ch < 32 || '<>:"|?*\\'.contains(c) ? '_' : c);
    }
    part = b.toString().replaceFirst(RegExp(r'[ .]+$'), '');
    if (part.isEmpty) part = '_';
    if (_reserved.contains(part.split('.').first.toUpperCase())) part = '_$part';
    parts.add(part);
  }
  if (parts.isEmpty) throw FormatException("empty path from sender: '$path'");
  return parts;
}

bool _taken(String path) =>
    FileSystemEntity.typeSync(path, followLinks: false) != FileSystemEntityType.notFound ||
    File('$path.part').existsSync();

/// 'photo.jpg' -> 'photo (1).jpg' if the name is already used.
String uniquePath(String path) {
  if (!_taken(path)) return path;
  final isDir = FileSystemEntity.isDirectorySync(path);
  final ext = isDir ? '' : p.extension(path);
  final stem = isDir ? p.basename(path) : p.basenameWithoutExtension(path);
  for (var i = 1;; i++) {
    final cand = p.join(p.dirname(path), '$stem ($i)$ext');
    if (!_taken(cand)) return cand;
  }
}

(String, int) parseTarget(String text, [int defaultPort = transferPort]) {
  text = text.trim();
  if (text.isEmpty) throw const FormatException('No address given');
  if (':'.allMatches(text).length == 1) {
    final i = text.indexOf(':');
    final port = int.tryParse(text.substring(i + 1));
    if (port == null || port <= 0 || port > 65535) throw FormatException('Invalid port in "$text"');
    return (text.substring(0, i), port);
  }
  return (text, defaultPort);
}

class Cancelled implements Exception {
  @override
  String toString() => 'Cancelled';
}

class TamperedError implements Exception {
  final String message;
  TamperedError(this.message);
  @override
  String toString() => message;
}

class SpanError implements Exception {
  final String message;
  SpanError(this.message);
  @override
  String toString() => message;
}

/// Plain-language error text for the UI.
String friendly(Object e) {
  if (e is Cancelled) return 'Cancelled';
  if (e is SocketException) {
    final code = e.osError?.errorCode;
    final msg = '${e.message} ${e.osError?.message ?? ''}'.toLowerCase();
    if (msg.contains('refused') || code == 111 || code == 10061) {
      return 'connection refused - is Span open on the other computer and allowed through its firewall?';
    }
    if (msg.contains('timed out') || code == 110) {
      return 'timed out - check the address and the other computer\'s firewall';
    }
    if (msg.contains('unreachable') || code == 113 || code == 101) {
      return 'can\'t reach the other computer - check that the phone is on the same Wi-Fi';
    }
    if (msg.contains('reset') || msg.contains('closed') || msg.contains('broken pipe')) {
      return 'the connection was closed by the other computer';
    }
    if (msg.contains('lookup') || msg.contains('host')) return 'unknown computer name or address';
    return e.message;
  }
  if (e is TimeoutException) return 'timed out waiting for the other computer';
  if (e is FormatException) return e.message;
  return e.toString().replaceFirst('Exception: ', '');
}
