import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../app_state.dart';
import '../core/util.dart';
import 'theme.dart';
import 'widgets.dart';

/// The other computer's shared folder: waiting for Allow, then browsing.
class RemoteScreen extends StatefulWidget {
  const RemoteScreen({super.key});

  @override
  State<RemoteScreen> createState() => _RemoteScreenState();
}

class _RemoteScreenState extends State<RemoteScreen> {
  final Set<String> _selected = {};

  Future<void> _sendFiles(AppState s) async {
    final picked = await FilePicker.pickFiles(dialogTitle: 'Choose files to send');
    final paths = picked.map((f) => f.path).whereType<String>().toList();
    if (paths.isNotEmpty) s.upload(paths);
  }

  Future<void> _newFolder(AppState s) async {
    final ctl = TextEditingController();
    final name = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('New folder', style: display(22)),
        content: TextField(
          controller: ctl,
          autofocus: true,
          decoration: const InputDecoration(hintText: 'Folder name'),
          onSubmitted: (v) => Navigator.pop(ctx, v),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(ctx, ctl.text), child: const Text('Create')),
        ],
      ),
    );
    if (name != null) s.remoteMkdir(name);
  }

  @override
  Widget build(BuildContext context) {
    final s = AppScope.of(context);
    final waiting = s.pendingLabel != null;
    final sess = s.session;
    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, _) {
        if (didPop) return;
        if (_selected.isNotEmpty) {
          setState(_selected.clear); // back first clears a selection,
        } else if (sess != null && s.remotePath.isNotEmpty) {
          s.remoteUp(); // then walks up folders,
        } else {
          if (waiting) s.cancelConnect(); // then leaves and disconnects
          if (sess != null) s.disconnect();
          Navigator.pop(context);
        }
      },
      child: Scaffold(
        appBar: AppBar(
          title: Row(children: [
            const Dot(SpanColors.remote, size: 11),
            const SizedBox(width: 10),
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(sess?.remoteName ?? s.pendingLabel ?? 'Other computer',
                    style: display(20), maxLines: 1, overflow: TextOverflow.ellipsis),
                Text(
                  _selected.isNotEmpty
                      ? '${_selected.length} selected'
                      : sess != null
                          ? 'Shared folder'
                          : waiting
                              ? 'Waiting for permission'
                              : 'Not connected',
                  style: const TextStyle(fontSize: 13, color: SpanColors.muted),
                ),
              ]),
            ),
          ]),
          actions: [
            if (sess != null && _selected.isNotEmpty)
              IconButton(
                tooltip: 'Download to this phone',
                icon: const Icon(Icons.download),
                onPressed: () {
                  s.download(_selected.toList());
                  setState(_selected.clear);
                },
              )
            else if (sess != null) ...[
              IconButton(tooltip: 'New folder', icon: const Icon(Icons.create_new_folder_outlined), onPressed: () => _newFolder(s)),
              IconButton(tooltip: 'Disconnect', icon: const Icon(Icons.link_off), onPressed: () {
                s.disconnect();
                Navigator.pop(context);
              }),
            ],
          ],
        ),
        floatingActionButton: sess == null
            ? null
            : FloatingActionButton.extended(
                backgroundColor: SpanColors.local,
                foregroundColor: Colors.white,
                onPressed: () => _sendFiles(s),
                icon: const Icon(Icons.upload),
                label: const Text('Send files here'),
              ),
        body: SafeArea(
          child: waiting
              ? _waiting(s)
              : sess == null
                  ? _failed(s)
                  : _browse(s),
        ),
      ),
    );
  }

  Widget _waiting(AppState s) => ListView(padding: const EdgeInsets.all(16), children: [
        Bridge(
          state: 'Waiting for ${s.pendingLabel}',
          extra: [
            const SizedBox(height: 8),
            const Text('Verification code', style: TextStyle(color: SpanColors.bridgeMuted)),
            Text(s.pendingCode ?? '...', style: display(52, weight: FontWeight.w400, color: SpanColors.bridgeInk)),
            const SizedBox(height: 4),
            Text(
              '${s.pendingLabel} has been asked to let this phone open its shared folder. '
              'Its screen shows a code too. It should match this one.',
              style: const TextStyle(color: SpanColors.bridgeMuted),
            ),
            const SizedBox(height: 10),
          ],
        ),
        const SizedBox(height: 16),
        Align(
          alignment: Alignment.centerLeft,
          child: OutlinedButton(onPressed: () {
            s.cancelConnect();
            Navigator.pop(context);
          }, child: const Text('Cancel')),
        ),
      ]);

  Widget _failed(AppState s) => ListView(padding: const EdgeInsets.all(16), children: [
        Pane(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(s.connectError ?? s.status, style: const TextStyle(fontSize: 15)),
            const SizedBox(height: 12),
            OutlinedButton(onPressed: () => Navigator.pop(context), child: const Text('Back')),
          ]),
        ),
      ]);

  Widget _browse(AppState s) {
    final sess = s.session!;
    final crumbs = [sess.rootName, ...s.remotePath.split('/').where((x) => x.isNotEmpty)];
    final active = s.transfers.where((t) => t.status == 'running' || DateTime.now().difference(t.started).inSeconds < 8).take(3).toList();
    return RefreshIndicator(
      onRefresh: () => s.remoteGo(s.remotePath),
      child: ListView(padding: const EdgeInsets.fromLTRB(16, 4, 16, 96), children: [
        Bridge(state: 'Encrypted link', code: s.lastCode, connected: true, transfers: active),
        const SizedBox(height: 12),
        Pane(
          padding: const EdgeInsets.fromLTRB(8, 4, 8, 4),
          child: Column(children: [
            Row(children: [
              IconButton(
                tooltip: 'Up one folder',
                onPressed: s.remotePath.isEmpty ? null : s.remoteUp,
                icon: const Icon(Icons.arrow_upward),
              ),
              Expanded(
                child: Text(crumbs.join(' / '),
                    maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(color: SpanColors.muted)),
              ),
              if (s.loading) const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2)),
              const SizedBox(width: 8),
            ]),
            const Divider(),
            if (s.entries.isEmpty && !s.loading)
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 32, horizontal: 16),
                child: Text('This folder is empty. Tap "Send files here" to copy files from this phone.',
                    textAlign: TextAlign.center, style: TextStyle(color: SpanColors.muted)),
              ),
            for (final e in s.entries) _entry(s, e),
          ]),
        ),
      ]),
    );
  }

  Widget _entry(AppState s, Map<String, dynamic> e) {
    final isDir = e['dir'] == true;
    final rel = s.remoteJoin(e['name']);
    final selected = _selected.contains(rel);
    final when = DateTime.fromMillisecondsSinceEpoch((e['mtime'] as num).toInt() * 1000);
    return ListTile(
      selected: selected,
      selectedTileColor: SpanColors.remoteTint,
      selectedColor: SpanColors.ink,
      leading: Icon(isDir ? Icons.folder : Icons.insert_drive_file_outlined,
          color: isDir ? const Color(0xFFE8B54A) : SpanColors.muted),
      title: Text(e['name'], maxLines: 1, overflow: TextOverflow.ellipsis),
      subtitle: Text(
        '${isDir ? 'Folder' : human(e['size'] as num)}, '
        '${MaterialLocalizations.of(context).formatMediumDate(when)}',
        style: const TextStyle(color: SpanColors.muted, fontSize: 12.5),
      ),
      trailing: isDir ? const Icon(Icons.chevron_right) : const Icon(Icons.download_outlined, size: 20),
      onLongPress: () {
        HapticFeedback.selectionClick();
        setState(() => selected ? _selected.remove(rel) : _selected.add(rel));
      },
      onTap: () {
        if (_selected.isNotEmpty) {
          setState(() => selected ? _selected.remove(rel) : _selected.add(rel));
        } else if (isDir) {
          s.remoteGo(rel);
        } else {
          s.download([rel]);
          ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Downloading ${e['name']} to Download/Span')));
        }
      },
    );
  }
}
