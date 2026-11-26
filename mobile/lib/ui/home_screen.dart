import 'package:flutter/material.dart';

import '../app_state.dart';
import '../core/discovery.dart';
import 'phone_folder_screen.dart';
import 'remote_screen.dart';
import 'settings_screen.dart';
import 'theme.dart';
import 'widgets.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  final _addr = TextEditingController();

  @override
  void dispose() {
    _addr.dispose();
    super.dispose();
  }

  void _open(AppState s, {Peer? peer, String? address}) {
    s.connect(peer: peer, address: address);
    Navigator.push(context, MaterialPageRoute(builder: (_) => const RemoteScreen()));
  }

  @override
  Widget build(BuildContext context) {
    final s = AppScope.of(context);
    return Scaffold(
      appBar: AppBar(
        title: const Text('Span'),
        actions: [
          IconButton(
            tooltip: 'Settings',
            icon: const Icon(Icons.tune),
            onPressed: () => Navigator.push(context, MaterialPageRoute(builder: (_) => const SettingsScreen())),
          ),
        ],
      ),
      body: SafeArea(
        child: ListView(padding: const EdgeInsets.fromLTRB(16, 4, 16, 24), children: [
          _thisPhone(context, s),
          if (s.receiverError != null) ...[
            const SizedBox(height: 10),
            Text(s.receiverError!, style: const TextStyle(color: SpanColors.danger)),
          ],
          if (s.incoming.isNotEmpty || s.transfers.isNotEmpty) ...[
            const SizedBox(height: 12),
            Bridge(
              state: s.incoming.isNotEmpty ? 'Your folder is open' : 'Transfers',
              connected: s.incoming.isNotEmpty,
              showLink: s.incoming.isNotEmpty, // a link line only when something is connected
              transfers: s.transfers.take(4).toList(),
              extra: [
                for (final a in s.incoming.values)
                  Padding(
                    padding: const EdgeInsets.only(top: 6),
                    child: Row(children: [
                      Expanded(
                        child: Text('${a.name} has your Span folder open',
                            style: const TextStyle(color: SpanColors.bridgeInk)),
                      ),
                      OutlinedButton(
                        style: OutlinedButton.styleFrom(
                            foregroundColor: SpanColors.bridgeInk, side: const BorderSide(color: SpanColors.bridgeLine)),
                        onPressed: () => s.revoke(a.sid),
                        child: const Text('Disconnect'),
                      ),
                    ]),
                  ),
                if (s.transfers.any((t) => t.status != 'running'))
                  Align(
                    alignment: Alignment.centerRight,
                    child: TextButton(
                      onPressed: s.clearFinished,
                      child: const Text('Clear finished', style: TextStyle(color: SpanColors.bridgeMuted)),
                    ),
                  ),
              ],
            ),
          ],
          const SizedBox(height: 12),
          _computers(context, s),
          if (s.status.isNotEmpty) ...[
            const SizedBox(height: 12),
            Text(s.status, style: const TextStyle(color: SpanColors.muted)),
          ],
        ]),
      ),
    );
  }

  Widget _thisPhone(BuildContext context, AppState s) => Pane(
        child: Row(children: [
          const Dot(SpanColors.local, size: 11),
          const SizedBox(width: 12),
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(s.name, style: display(21), maxLines: 1, overflow: TextOverflow.ellipsis),
              Text(
                s.ips.isEmpty ? 'This phone - not on Wi-Fi' : 'This phone, ${s.ips.first}',
                style: const TextStyle(color: SpanColors.muted),
              ),
            ]),
          ),
          OutlinedButton(
            onPressed: () => Navigator.push(context, MaterialPageRoute(builder: (_) => const PhoneFolderScreen())),
            child: const Text('Span folder'),
          ),
        ]),
      );

  Widget _computers(BuildContext context, AppState s) => Pane(
        padding: const EdgeInsets.fromLTRB(16, 14, 16, 14),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            const Dot(SpanColors.remote, size: 11),
            const SizedBox(width: 12),
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text('Other computers', style: display(21)),
                Text(
                  s.relayEnabled ? 'On this Wi-Fi and through your relay (${s.relayStatus})' : 'Tap one to open its shared folder',
                  style: const TextStyle(color: SpanColors.muted),
                ),
              ]),
            ),
          ]),
          const SizedBox(height: 8),
          if (s.peers.isEmpty)
            const Padding(
              padding: EdgeInsets.symmetric(vertical: 14),
              child: Text(
                'No computers found yet. Open Span on the computer and make sure this phone is on the same Wi-Fi, '
                'or type the computer\'s address below.',
                style: TextStyle(color: SpanColors.muted),
              ),
            ),
          for (final p in s.peers) ...[
            ListTile(
              contentPadding: EdgeInsets.zero,
              leading: Icon(p.via == 'relay' ? Icons.cloud_outlined : Icons.computer, color: SpanColors.remote),
              title: Text(p.name, style: display(17)),
              subtitle: Text(p.via == 'relay' ? 'Through your relay server' : p.ip),
              trailing: const Icon(Icons.chevron_right),
              onTap: () => _open(s, peer: p),
            ),
            const Divider(),
          ],
          const SizedBox(height: 10),
          Row(children: [
            Expanded(
              child: TextField(
                controller: _addr,
                keyboardType: TextInputType.url,
                decoration: const InputDecoration(hintText: 'Address, for example 192.168.1.20'),
                onSubmitted: (v) => v.trim().isEmpty ? null : _open(s, address: v),
              ),
            ),
            const SizedBox(width: 8),
            FilledButton(
              style: FilledButton.styleFrom(backgroundColor: SpanColors.remote),
              onPressed: () => _addr.text.trim().isEmpty ? null : _open(s, address: _addr.text),
              child: const Text('Open'),
            ),
          ]),
        ]),
      );
}
