import 'dart:io';

import 'package:flutter/material.dart';
import 'package:open_filex/open_filex.dart';
import 'package:path/path.dart' as p;

import '../core/util.dart';
import 'theme.dart';
import 'widgets.dart';

/// This phone's Span folder: where received files land, and what computers
/// can open after you tap Allow.
class PhoneFolderScreen extends StatefulWidget {
  const PhoneFolderScreen({super.key});

  @override
  State<PhoneFolderScreen> createState() => _PhoneFolderScreenState();
}

class _PhoneFolderScreenState extends State<PhoneFolderScreen> {
  String _rel = '';

  List<FileSystemEntity> _list(String root) {
    try {
      final items = Directory(p.join(root, _rel)).listSync()
        ..removeWhere((e) => p.basename(e.path).startsWith('.') || e.path.endsWith('.part'));
      items.sort((a, b) {
        final ad = a is Directory, bd = b is Directory;
        if (ad != bd) return ad ? -1 : 1;
        return p.basename(a.path).toLowerCase().compareTo(p.basename(b.path).toLowerCase());
      });
      return items;
    } catch (_) {
      return [];
    }
  }

  Future<void> _delete(FileSystemEntity e) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('Delete ${p.basename(e.path)}?', style: display(20)),
        content: const Text('It will be removed from this phone. This can\'t be undone.'),
        actions: [
          TextButton(autofocus: true, onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: SpanColors.danger),
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (ok == true) {
      await e.delete(recursive: true);
      setState(() {});
    }
  }

  @override
  Widget build(BuildContext context) {
    final s = AppScope.of(context);
    final items = _list(s.shareDir);
    return PopScope(
      canPop: _rel.isEmpty,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop) setState(() => _rel = p.dirname(_rel) == '.' ? '' : p.dirname(_rel));
      },
      child: Scaffold(
        appBar: AppBar(
          title: Row(children: [
            const Dot(SpanColors.local, size: 11),
            const SizedBox(width: 10),
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(_rel.isEmpty ? 'Span folder' : p.basename(_rel), style: display(20)),
                const Text('On this phone', style: TextStyle(fontSize: 13, color: SpanColors.muted)),
              ]),
            ),
          ]),
        ),
        body: SafeArea(
          child: RefreshIndicator(
            onRefresh: () async => setState(() {}),
            child: ListView(padding: const EdgeInsets.fromLTRB(16, 4, 16, 24), children: [
              Text(
                'Received files are saved here (${s.shareDir.replaceFirst('/storage/emulated/0/', '')}). '
                'A computer can open this folder only after you tap Allow.',
                style: const TextStyle(color: SpanColors.muted),
              ),
              const SizedBox(height: 12),
              Pane(
                padding: const EdgeInsets.symmetric(vertical: 4, horizontal: 4),
                child: Column(children: [
                  if (items.isEmpty)
                    const Padding(
                      padding: EdgeInsets.symmetric(vertical: 32, horizontal: 16),
                      child: Text('Nothing here yet. Files sent to this phone will appear here.',
                          textAlign: TextAlign.center, style: TextStyle(color: SpanColors.muted)),
                    ),
                  for (final e in items) _tile(e),
                ]),
              ),
            ]),
          ),
        ),
      ),
    );
  }

  Widget _tile(FileSystemEntity e) {
    final isDir = e is Directory;
    final name = p.basename(e.path);
    final st = e.statSync();
    return ListTile(
      leading: Icon(isDir ? Icons.folder : Icons.insert_drive_file_outlined,
          color: isDir ? const Color(0xFFE8B54A) : SpanColors.muted),
      title: Text(name, maxLines: 1, overflow: TextOverflow.ellipsis),
      subtitle: Text(
        '${isDir ? 'Folder' : human(st.size)}, ${MaterialLocalizations.of(context).formatMediumDate(st.modified)}',
        style: const TextStyle(color: SpanColors.muted, fontSize: 12.5),
      ),
      trailing: IconButton(
        tooltip: 'Delete $name',
        icon: const Icon(Icons.delete_outline, size: 20),
        onPressed: () => _delete(e),
      ),
      onTap: () async {
        if (isDir) {
          setState(() => _rel = p.join(_rel, name));
        } else {
          final r = await OpenFilex.open(e.path);
          if (r.type != ResultType.done && mounted) {
            ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('No app can open $name.')));
          }
        }
      },
    );
  }
}
