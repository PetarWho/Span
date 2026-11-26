// Everything the screens need: discovery, the receiver, the optional relay,
// the open browse session and the list of transfers.
import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'core/channel.dart';
import 'core/discovery.dart';
import 'core/receiver.dart';
import 'core/relay.dart';
import 'core/session.dart';
import 'core/transfer.dart';
import 'core/util.dart';

const _native = MethodChannel('span/native');

class Transfer {
  final int id;
  final String title;
  final bool toRemote; // data moves from this phone to the other computer
  final CancelToken? cancel;
  int done = 0, total = 0;
  String status = 'running'; // running | done | failed
  String message = 'Waiting...';
  final DateTime started = DateTime.now();
  DateTime? firstByte;

  Transfer(this.id, this.title, this.toRemote, [this.cancel]);

  void progress(int d, int t) {
    firstByte ??= DateTime.now();
    done = d;
    total = t;
    final secs = DateTime.now().difference(firstByte!).inMilliseconds / 1000;
    message = '${human(d)} of ${human(t)}${secs > 0.3 ? ', ${human(d / secs)}/s' : ''}';
  }
}

class IncomingAccess {
  final String sid, name;
  IncomingAccess(this.sid, this.name);
}

/// Asks the user something; the UI registers these.
typedef BrowsePrompt = Future<bool> Function(String sender, String ip, String code);
typedef PushPrompt = Future<bool> Function(String sender, List<IncomingFile> files, int total, String code);

class AppState extends ChangeNotifier {
  late SharedPreferences _prefs;
  late Discovery discovery;
  late Receiver receiver;
  RelayClient? relay;
  String name = 'Android phone';
  String shareDir = '';
  bool autoAccept = false;
  bool relayEnabled = false;
  String relayAddress = '', relayPassword = '', relayStatus = '';
  String? receiverError;
  List<String> ips = [];
  List<Peer> peers = [];

  // browse session on another computer
  RemoteSession? session;
  String remotePath = '';
  List<Map<String, dynamic>> entries = [];
  bool loading = false;
  String? pendingLabel, pendingCode;
  String? lastCode; // shown on the bridge while connected
  CancelToken? _pendingCancel;
  String? connectError;

  final List<Transfer> transfers = [];
  final Map<String, IncomingAccess> incoming = {};
  final Map<int, Transfer> _rx = {};
  String status = '';
  int _ids = 0;
  Timer? _timer;

  BrowsePrompt? browsePrompt;
  PushPrompt? pushPrompt;

  Future<void> init() async {
    _prefs = await SharedPreferences.getInstance();
    name = _prefs.getString('name') ?? await _deviceName();
    autoAccept = _prefs.getBool('auto_accept') ?? false;
    relayEnabled = _prefs.getBool('relay_enabled') ?? false;
    relayAddress = _prefs.getString('relay_address') ?? '';
    relayPassword = _prefs.getString('relay_password') ?? '';
    shareDir = await _pickShareDir();
    ips = await localIps();
    try {
      await _native.invokeMethod('acquireMulticastLock'); // lets broadcasts in while Span is open
    } catch (_) {}
    discovery = Discovery(name);
    await discovery.start();
    receiver = Receiver(
      getRoot: () => shareDir,
      getName: () => name,
      askPush: _askPush,
      askBrowse: _askBrowse,
      events: _onReceiverEvent,
    );
    try {
      await receiver.start();
    } catch (e) {
      receiverError = 'Other computers can\'t reach this phone right now (${friendly(e)}).';
    }
    _startRelay();
    _timer = Timer.periodic(const Duration(milliseconds: 1500), (_) => _refreshPeers());
    _refreshPeers();
    notifyListeners();
  }

  Future<String> _deviceName() async {
    try {
      final n = await _native.invokeMethod<String>('deviceName');
      if (n != null && n.trim().isNotEmpty) return n.trim();
    } catch (_) {}
    return 'Android phone';
  }

  /// Download/Span is visible in the Files app; fall back to app storage.
  Future<String> _pickShareDir() async {
    final candidates = <String>[];
    if (Platform.isAndroid) candidates.add('/storage/emulated/0/Download/Span');
    final ext = Platform.isAndroid ? await getExternalStorageDirectory() : await getDownloadsDirectory();
    if (ext != null) candidates.add(p.join(ext.path, 'Span'));
    candidates.add(p.join((await getApplicationDocumentsDirectory()).path, 'Span'));
    for (final c in candidates) {
      try {
        final d = Directory(c);
        await d.create(recursive: true);
        final probe = File(p.join(c, '.span-write-test'));
        await probe.writeAsString('ok');
        await probe.delete();
        return c;
      } catch (_) {}
    }
    return candidates.last;
  }

  @override
  void dispose() {
    _timer?.cancel();
    discovery.stop();
    receiver.stop();
    relay?.stop();
    session?.close();
    try {
      _native.invokeMethod('releaseMulticastLock');
    } catch (_) {}
    super.dispose();
  }

  void _say(String text) {
    status = text;
    notifyListeners();
  }

  // ---- peers
  void _refreshPeers() {
    final local = discovery.peers();
    final ids = local.map((p) => p.id).toSet();
    final remote = (relay?.peers ?? []).where((p) => !ids.contains(p.id)).toList()
      ..sort((a, b) => a.name.toLowerCase().compareTo(b.name.toLowerCase()));
    final next = [...local, ...remote];
    String key(List<Peer> l) => l.map((p) => '${p.via}|${p.id}|${p.name}|${p.ip}').join(',');
    if (key(next) != key(peers)) {
      peers = next;
      notifyListeners();
    }
    if (session != null && session!.closed) {
      final n = session!.remoteName;
      session = null;
      entries = [];
      _say('The connection to $n closed.');
    }
  }

  Future<Conn> Function() _dialFor(Peer peer) {
    if (peer.via == 'relay') {
      final r = relay!;
      return () => r.connectTo(peer.id);
    }
    return () => Conn.connect(peer.ip, peer.port);
  }

  // ---- browsing another computer
  Future<void> connect({Peer? peer, String? address}) async {
    await disconnect(quiet: true);
    late Future<Conn> Function() dial;
    late String label;
    try {
      if (peer != null) {
        dial = _dialFor(peer);
        label = peer.name;
      } else {
        final (host, port) = parseTarget(address ?? '');
        dial = () => Conn.connect(host, port);
        label = address!.trim();
      }
    } catch (e) {
      connectError = friendly(e);
      notifyListeners();
      return;
    }
    final cancel = CancelToken();
    _pendingCancel = cancel;
    pendingLabel = label;
    pendingCode = null;
    connectError = null;
    notifyListeners();
    final s = RemoteSession(name, dial);
    try {
      await s.open(
          cancel: cancel,
          onCode: (c) {
            pendingCode = c;
            notifyListeners();
          });
      if (_pendingCancel != cancel) {
        await s.close();
        return;
      }
      session = s;
      lastCode = pendingCode;
      remotePath = '';
      _say('Connected to ${s.remoteName}.');
      await remoteGo('');
    } catch (e) {
      if (_pendingCancel == cancel) connectError = e is Cancelled ? null : 'Couldn\'t open $label: ${friendly(e)}';
    } finally {
      if (_pendingCancel == cancel) {
        _pendingCancel = null;
        pendingLabel = null;
      }
      notifyListeners();
    }
  }

  void cancelConnect() {
    _pendingCancel?.cancel();
    _pendingCancel = null;
    pendingLabel = null;
    notifyListeners();
  }

  Future<void> disconnect({bool quiet = false}) async {
    final s = session;
    session = null;
    entries = [];
    remotePath = '';
    if (s != null) {
      await s.close();
      if (!quiet) _say('Disconnected from ${s.remoteName}.');
    }
    notifyListeners();
  }

  String remoteJoin(String n) => remotePath.isEmpty ? n : '$remotePath/$n';

  Future<void> remoteGo(String rel) async {
    final s = session;
    if (s == null) return;
    loading = true;
    notifyListeners();
    try {
      final list = await s.list(rel);
      if (s == session) {
        remotePath = rel;
        entries = list;
      }
    } catch (e) {
      _say('Couldn\'t open that folder: ${friendly(e)}');
    } finally {
      loading = false;
      notifyListeners();
    }
  }

  Future<void> remoteUp() async {
    if (remotePath.isEmpty) return;
    final i = remotePath.lastIndexOf('/');
    await remoteGo(i < 0 ? '' : remotePath.substring(0, i));
  }

  Future<void> remoteMkdir(String folder) async {
    final s = session;
    if (s == null || folder.trim().isEmpty) return;
    try {
      await s.mkdir(remoteJoin(folder.trim()));
      await remoteGo(remotePath);
    } catch (e) {
      _say('Couldn\'t create the folder: ${friendly(e)}');
    }
  }

  // ---- transfers
  String _itemsTitle(List<String> names) => names.length == 1 ? names.first : '${names.length} items';

  Transfer _newTransfer(String title, bool toRemote, [CancelToken? c]) {
    final t = Transfer(++_ids, title, toRemote, c);
    transfers.insert(0, t);
    if (transfers.length > 20) transfers.removeLast();
    notifyListeners();
    return t;
  }

  Progress _progressFor(Transfer t) {
    var last = DateTime.fromMillisecondsSinceEpoch(0);
    return (d, total, _) {
      t.progress(d, total);
      final now = DateTime.now();
      if (now.difference(last).inMilliseconds > 120 || d >= total) {
        last = now;
        notifyListeners();
      }
    };
  }

  void _finish(Transfer t, bool ok, String message) {
    t.status = ok ? 'done' : 'failed';
    t.message = message;
    _say(ok ? '${t.title}: $message' : '${t.title} failed: $message');
  }

  /// Phone -> the open folder on the other computer.
  Future<void> upload(List<String> paths, [String? destRel]) async {
    final s = session;
    if (s == null || paths.isEmpty) return;
    final c = CancelToken();
    final t = _newTransfer(_itemsTitle(paths.map(p.basename).toList()), true, c);
    final dest = destRel ?? remotePath;
    try {
      final (n, total) = await s.upload(paths, dest, progress: _progressFor(t), cancel: c);
      _finish(t, true, 'Copied $n file(s), ${human(total)} to ${s.remoteName}');
      if (s == session && dest == remotePath) await remoteGo(remotePath);
    } catch (e) {
      _finish(t, false, friendly(e));
    }
  }

  /// The other computer -> this phone's Span folder.
  Future<void> download(List<String> relPaths) async {
    final s = session;
    if (s == null || relPaths.isEmpty) return;
    final c = CancelToken();
    final t = _newTransfer(_itemsTitle(relPaths.map((r) => r.split('/').last).toList()), false, c);
    try {
      final (n, total) = await s.download(relPaths, shareDir, progress: _progressFor(t), cancel: c);
      _finish(t, true, 'Saved $n file(s), ${human(total)} to Download/Span');
    } catch (e) {
      _finish(t, false, friendly(e));
    }
  }

  /// Send to a computer without opening its folder (it must tap Accept).
  Future<void> pushTo(Peer peer, List<String> paths) async {
    final c = CancelToken();
    final t = _newTransfer(_itemsTitle(paths.map(p.basename).toList()), true, c)..message = 'Waiting for ${peer.name} to accept...';
    try {
      final (n, total) = await sendFiles(_dialFor(peer), paths, name,
          progress: _progressFor(t), cancel: c, onCode: (code) {
        t.message = 'Waiting for ${peer.name} to accept. Code $code';
        notifyListeners();
      });
      _finish(t, true, 'Sent $n file(s), ${human(total)} to ${peer.name}');
    } catch (e) {
      _finish(t, false, friendly(e));
    }
  }

  void clearFinished() {
    transfers.removeWhere((t) => t.status != 'running');
    notifyListeners();
  }

  // ---- requests from other computers
  Future<bool> _askBrowse(String sender, String ip, String code) async =>
      await browsePrompt?.call(sender, ip, code) ?? false;

  Future<bool> _askPush(String sender, String ip, List<IncomingFile> files, int total, String code) async {
    if (autoAccept) return true;
    return await pushPrompt?.call(sender, files, total, code) ?? false;
  }

  void _onReceiverEvent(String kind, Map<String, dynamic> i) {
    final who = '${i['peer'] ?? i['name'] ?? ''}'.split(' (').first;
    switch (kind) {
      case 'session_open':
        incoming[i['sid']] = IncomingAccess(i['sid'], i['name']);
        _say('${i['name']} opened your Span folder.');
      case 'session_closed':
        incoming.remove(i['sid']);
        _say('${i['name']} closed your Span folder.');
      case 'start':
        final incomingData = i['direction'] == 'in';
        _rx[i['tid']] = _newTransfer('${i['count']} file(s) ${incomingData ? 'from' : 'to'} $who', !incomingData);
      case 'progress':
        final t = _rx[i['tid']];
        if (t != null) _progressFor(t)(i['done'], i['total'], '');
      case 'done':
        final t = _rx.remove(i['tid']);
        if (t != null) {
          _finish(t, true, i['direction'] == 'out' ? '$who copied ${human(i['total'])}' : 'Saved ${human(i['total'])} to Download/Span');
        }
      case 'error':
        final t = _rx.remove(i['tid']);
        if (t != null) {
          _finish(t, false, '${i['error']}');
        } else {
          _say('Transfer with $who failed: ${i['error']}');
        }
    }
  }

  void revoke(String sid) => receiver.closeSession(sid);

  // ---- settings
  Future<void> saveSettings({
    required String newName,
    required bool auto,
    required bool relayOn,
    required String relayAddr,
    required String relayPw,
  }) async {
    name = newName.trim().isEmpty ? name : newName.trim();
    discovery.name = name;
    autoAccept = auto;
    final relayChanged = relayOn != relayEnabled || relayAddr != relayAddress || relayPw != relayPassword;
    relayEnabled = relayOn;
    relayAddress = relayAddr.trim();
    relayPassword = relayPw;
    await _prefs.setString('name', name);
    await _prefs.setBool('auto_accept', autoAccept);
    await _prefs.setBool('relay_enabled', relayEnabled);
    await _prefs.setString('relay_address', relayAddress);
    await _prefs.setString('relay_password', relayPassword);
    if (relayChanged) _startRelay();
    _say('Settings saved.');
  }

  void _startRelay() {
    relay?.stop();
    relay = null;
    if (!relayEnabled || relayAddress.isEmpty) {
      relayStatus = '';
      return;
    }
    try {
      relay = RelayClient(relayAddress, relayPassword, discovery.id, () => name, receiver.handleConnection, (s) {
        relayStatus = s;
        notifyListeners();
      })
        ..start();
    } catch (e) {
      relayStatus = 'address is invalid (${friendly(e)})';
    }
  }
}
