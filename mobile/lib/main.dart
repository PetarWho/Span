import 'dart:async';

import 'package:flutter/material.dart';
import 'package:path/path.dart' as p;
import 'package:receive_sharing_intent/receive_sharing_intent.dart';

import 'app_state.dart';
import 'core/discovery.dart';
import 'core/util.dart';
import 'ui/home_screen.dart';
import 'ui/theme.dart';
import 'ui/widgets.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  // cryptography_flutter registers itself: AES-GCM and X25519 run natively on Android
  final state = AppState();
  runApp(AppScope(state: state, child: SpanApp(state: state)));
  await state.init();
}

class SpanApp extends StatefulWidget {
  final AppState state;
  const SpanApp({super.key, required this.state});

  @override
  State<SpanApp> createState() => _SpanAppState();
}

class _SpanAppState extends State<SpanApp> {
  final _nav = GlobalKey<NavigatorState>();
  StreamSubscription? _shareSub;

  @override
  void initState() {
    super.initState();
    final s = widget.state;
    s.browsePrompt = (sender, ip, code) async {
      final ctx = _nav.currentContext;
      if (ctx == null) return false;
      return askDialog(ctx,
          title: 'Let $sender open your Span folder?',
          body: '$sender ($ip) will be able to see, download and add files in this phone\'s Span folder.',
          code: code,
          yes: 'Allow',
          no: 'Deny');
    };
    s.pushPrompt = (sender, files, total, code) async {
      final ctx = _nav.currentContext;
      if (ctx == null) return false;
      return askDialog(ctx,
          title: '$sender wants to send you ${files.length} file(s)',
          body: '${human(total)} in total, saved to Download/Span.',
          details: filesSummary(files),
          code: code,
          yes: 'Accept',
          no: 'Decline');
    };
    // "Share -> Span" from other apps
    _shareSub = ReceiveSharingIntent.instance.getMediaStream().listen(_onShared, onError: (_) {});
    ReceiveSharingIntent.instance.getInitialMedia().then((files) {
      _onShared(files);
      ReceiveSharingIntent.instance.reset();
    });
  }

  @override
  void dispose() {
    _shareSub?.cancel();
    super.dispose();
  }

  void _onShared(List<SharedMediaFile> files) {
    final paths = files.map((f) => f.path).where((x) => x.isNotEmpty).toList();
    if (paths.isEmpty) return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      final ctx = _nav.currentContext;
      if (ctx != null) showSendSheet(ctx, widget.state, paths);
    });
  }

  @override
  Widget build(BuildContext context) => MaterialApp(
        title: appName,
        navigatorKey: _nav,
        debugShowCheckedModeBanner: false,
        theme: spanTheme(),
        home: const HomeScreen(),
      );
}

/// Where should shared files go? The open folder, or a computer nearby.
Future<void> showSendSheet(BuildContext context, AppState s, List<String> paths) => showModalBottomSheet(
      context: context,
      showDragHandle: true,
      backgroundColor: SpanColors.surface,
      builder: (ctx) => ListenableBuilder(
        listenable: s,
        builder: (ctx, _) => SafeArea(
          child: Padding(
            padding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
            child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(paths.length == 1 ? 'Send ${p.basename(paths.first)}' : 'Send ${paths.length} files',
                  style: display(22), maxLines: 1, overflow: TextOverflow.ellipsis),
              const SizedBox(height: 4),
              const Text('Choose where they go.', style: TextStyle(color: SpanColors.muted)),
              const SizedBox(height: 8),
              if (s.session != null)
                ListTile(
                  contentPadding: EdgeInsets.zero,
                  leading: const Icon(Icons.folder_open, color: SpanColors.remote),
                  title: Text('The open folder on ${s.session!.remoteName}', style: display(17)),
                  subtitle: Text(s.remotePath.isEmpty ? s.session!.rootName : s.remotePath),
                  onTap: () {
                    Navigator.pop(ctx);
                    s.upload(paths);
                  },
                ),
              for (final Peer peer in s.peers)
                ListTile(
                  contentPadding: EdgeInsets.zero,
                  leading: Icon(peer.via == 'relay' ? Icons.cloud_outlined : Icons.computer, color: SpanColors.remote),
                  title: Text(peer.name, style: display(17)),
                  subtitle: const Text('They\'ll be asked to accept'),
                  onTap: () {
                    Navigator.pop(ctx);
                    s.pushTo(peer, paths);
                  },
                ),
              if (s.session == null && s.peers.isEmpty)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 16),
                  child: Text('No computers found yet. Open Span on the computer, on the same Wi-Fi as this phone.',
                      style: TextStyle(color: SpanColors.muted)),
                ),
            ]),
          ),
        ),
      ),
    );
