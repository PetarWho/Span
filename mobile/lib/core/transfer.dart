// Sending and receiving files over a SecureChannel, plus the shared-folder
// path rules. Mirrors the "sending" and "shared folder" sections of span.py.
import 'dart:io';

import 'package:path/path.dart' as p;

import 'channel.dart';
import 'util.dart';

/// Lets the UI cancel a transfer by closing its socket.
class CancelToken {
  bool cancelled = false;
  Conn? conn;

  void cancel() {
    cancelled = true;
    conn?.destroy();
  }
}

class LocalFile {
  final String path; // absolute path on this device
  final String rel; // '/'-separated name sent to the other side
  final int size;
  LocalFile(this.path, this.rel, this.size);
}

/// Expands files and folders into a flat list. Folder contents keep the
/// folder's name as the first path component, like span.py.
List<LocalFile> collectFiles(List<String> paths, {String? confineTo}) {
  final out = <LocalFile>[];
  for (final raw in paths) {
    final type = FileSystemEntity.typeSync(raw);
    if (type == FileSystemEntityType.file) {
      out.add(LocalFile(raw, p.basename(raw), File(raw).lengthSync()));
    } else if (type == FileSystemEntityType.directory) {
      final base = p.basename(raw);
      final entries = Directory(raw).listSync(recursive: true, followLinks: false).whereType<File>().toList()
        ..sort((a, b) => a.path.compareTo(b.path));
      for (final f in entries) {
        if (confineTo != null && !isInside(confineTo, f.resolveSymbolicLinksSync())) continue;
        final rel = p.posix.joinAll([base, ...p.split(p.relative(f.path, from: raw))]);
        out.add(LocalFile(f.path, rel, f.lengthSync()));
      }
    } else {
      throw SpanError('Not found: $raw');
    }
  }
  return out;
}

bool isInside(String root, String path) {
  final r = p.normalize(root), x = p.normalize(path);
  return x == r || p.isWithin(r, x);
}

Future<void> streamFiles(SecureChannel ch, List<LocalFile> files, Progress? progress) async {
  final total = files.fold<int>(0, (a, f) => a + f.size);
  var done = 0;
  for (final f in files) {
    final raf = await File(f.path).open();
    try {
      var left = f.size;
      while (left > 0) {
        final data = await raf.read(left < chunkSize ? left : chunkSize);
        if (data.isEmpty) throw SpanError('${f.rel} got smaller while it was being sent');
        await ch.send(data); // AES-GCM authenticates every chunk
        left -= data.length;
        done += data.length;
        progress?.call(done, total, f.rel);
      }
      if (f.size == 0) progress?.call(done, total, f.rel);
    } finally {
      await raf.close();
    }
  }
}

class IncomingFile {
  final List<String> parts;
  final int size;
  IncomingFile(this.parts, this.size);
  String get rel => parts.join('/');
}

List<IncomingFile> parseFileList(List items) => [
      for (final f in items)
        () {
          final size = (f['size'] as num).toInt();
          if (size < 0) throw const FormatException('negative file size');
          return IncomingFile(safeRelParts(f['path'] as String), size);
        }()
    ];

/// Receives announced files into [dest]; existing names get ' (1)' added.
Future<void> receiveFiles(SecureChannel ch, String dest, List<IncomingFile> files, Progress? progress) async {
  await Directory(dest).create(recursive: true);
  final total = files.fold<int>(0, (a, f) => a + f.size);
  final topMap = <String, String>{};
  for (final f in files) {
    topMap.putIfAbsent(f.parts.first, () => p.basename(uniquePath(p.join(dest, f.parts.first))));
  }
  final parts = <File>[];
  var done = 0;
  try {
    for (final f in files) {
      var finalPath = p.joinAll([dest, topMap[f.parts.first]!, ...f.parts.skip(1)]);
      if (FileSystemEntity.typeSync(finalPath) != FileSystemEntityType.notFound) {
        finalPath = uniquePath(finalPath); // duplicate path inside one transfer
      }
      await Directory(p.dirname(finalPath)).create(recursive: true);
      final part = File('$finalPath.part');
      parts.add(part);
      final sink = part.openWrite();
      try {
        var left = f.size;
        while (left > 0) {
          final data = await ch.recv(ioTimeout); // decrypts and verifies
          if (data.isEmpty || data.length > left) {
            throw TamperedError('the other computer sent more data than it announced');
          }
          sink.add(data);
          left -= data.length;
          done += data.length;
          progress?.call(done, total, f.rel);
        }
        if (f.size == 0) progress?.call(done, total, f.rel);
      } finally {
        await sink.close();
      }
      await part.rename(finalPath);
      parts.remove(part);
    }
  } catch (_) {
    for (final part in parts) {
      try {
        part.deleteSync();
      } catch (_) {}
    }
    rethrow;
  }
}

/// Opens a connection, runs the handshake and reports the verification code.
Future<SecureChannel> openChannel(Future<Conn> Function() dial,
    {CancelToken? cancel, void Function(String code)? onCode}) async {
  final conn = await dial();
  cancel?.conn = conn;
  if (cancel?.cancelled ?? false) {
    conn.destroy();
    throw Cancelled();
  }
  try {
    final ch = await SecureChannel.handshake(conn, isClient: true);
    onCode?.call(ch.code);
    return ch;
  } catch (_) {
    conn.destroy();
    rethrow;
  }
}

/// Sends files. Without [extra] it's a push the receiver must Accept; with an
/// approved session's token it becomes an upload ("put") into its share.
Future<(int, int)> sendFiles(
  Future<Conn> Function() dial,
  List<String> paths,
  String senderName, {
  Progress? progress,
  CancelToken? cancel,
  void Function(String code)? onCode,
  Map<String, dynamic>? extra,
  String? authToken,
}) async {
  final files = collectFiles(paths);
  if (files.isEmpty) throw SpanError('nothing to send (the folder is empty)');
  final total = files.fold<int>(0, (a, f) => a + f.size);
  final header = <String, dynamic>{
    'magic': magic,
    'sender': senderName,
    'total': total,
    'files': [for (final f in files) {'path': f.rel, 'size': f.size}],
    ...?extra,
  };
  try {
    final ch = await openChannel(dial, cancel: cancel, onCode: onCode);
    try {
      if (authToken != null) header['auth'] = await sessionMac(authToken, ch.binding, 'client');
      await ch.sendJson(header);
      final resp = await ch.recvJson(acceptWait);
      if (resp['accept'] != true) {
        final reason = resp['reason'] ?? resp['error'];
        throw SpanError('the other computer declined${reason != null ? ' ($reason)' : ''}');
      }
      if (authToken != null &&
          !macEquals('${resp['proof'] ?? ''}', await sessionMac(authToken, ch.binding, 'server'))) {
        throw TamperedError('could not verify the other computer');
      }
      await streamFiles(ch, files, progress);
      final result = await ch.recvJson(ioTimeout);
      if (result['ok'] != true) throw SpanError('receiver error: ${result['error'] ?? 'unknown'}');
    } finally {
      ch.conn.destroy();
    }
  } catch (e) {
    if (cancel?.cancelled ?? false) throw Cancelled();
    rethrow;
  }
  return (files.length, total);
}

// ------------------------------------------------------------- shared folder

/// Maps a '/'-path from the network to a real path inside [root], or throws.
String resolveInRoot(String root, String rel, {bool mustExist = true}) {
  final r = Directory(root).resolveSymbolicLinksSync();
  rel = rel.trim();
  var path = rel.replaceAll(RegExp(r'[/. ]'), '').isEmpty ? r : p.joinAll([r, ...safeRelParts(rel)]);
  final exists = FileSystemEntity.typeSync(path) != FileSystemEntityType.notFound;
  if (exists) {
    path = File(path).resolveSymbolicLinksSync();
  } else if (!mustExist) {
    path = p.join(Directory(p.dirname(path)).resolveSymbolicLinksSync(), p.basename(path));
  }
  if (!isInside(r, path)) throw SpanError('that is outside the shared folder');
  if (mustExist && !exists) throw SpanError('it no longer exists');
  return path;
}

List<Map<String, dynamic>> listDir(String root, String rel) {
  final d = resolveInRoot(root, rel);
  if (!FileSystemEntity.isDirectorySync(d)) throw SpanError('not a folder');
  final out = <Map<String, dynamic>>[];
  for (final e in Directory(d).listSync(followLinks: false)) {
    try {
      final st = e.statSync(); // follows links, like os.scandir().stat()
      final isDir = st.type == FileSystemEntityType.directory;
      out.add({
        'name': p.basename(e.path),
        'dir': isDir,
        'size': isDir ? 0 : st.size,
        'mtime': st.modified.millisecondsSinceEpoch ~/ 1000,
      });
    } catch (_) {
      continue;
    }
    if (out.length >= 20000) break;
  }
  out.sort((a, b) {
    if (a['dir'] != b['dir']) return a['dir'] == true ? -1 : 1;
    return (a['name'] as String).toLowerCase().compareTo((b['name'] as String).toLowerCase());
  });
  return out;
}
