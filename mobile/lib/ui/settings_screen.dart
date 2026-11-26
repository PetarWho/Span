import 'package:flutter/material.dart';

import '../core/util.dart';
import 'theme.dart';
import 'widgets.dart';

class SettingsScreen extends StatefulWidget {
  const SettingsScreen({super.key});

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  late final TextEditingController _name, _addr, _pw;
  late bool _auto, _relayOn;
  bool _init = false;
  String? _error;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (_init) return;
    final s = AppScope.of(context);
    _name = TextEditingController(text: s.name);
    _addr = TextEditingController(text: s.relayAddress);
    _pw = TextEditingController(text: s.relayPassword);
    _auto = s.autoAccept;
    _relayOn = s.relayEnabled;
    _init = true;
  }

  @override
  void dispose() {
    _name.dispose();
    _addr.dispose();
    _pw.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    if (_relayOn) {
      try {
        parseTarget(_addr.text, relayPort);
      } catch (e) {
        setState(() => _error = 'Check the relay address: ${friendly(e)}');
        return;
      }
    }
    await AppScope.of(context).saveSettings(
        newName: _name.text, auto: _auto, relayOn: _relayOn, relayAddr: _addr.text, relayPw: _pw.text);
    if (mounted) Navigator.pop(context);
  }

  @override
  Widget build(BuildContext context) {
    final s = AppScope.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('Settings')),
      body: SafeArea(
        child: ListView(padding: const EdgeInsets.fromLTRB(16, 4, 16, 24), children: [
          Pane(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              const Text('Phone name', style: TextStyle(color: SpanColors.muted)),
              const SizedBox(height: 6),
              TextField(controller: _name),
              const SizedBox(height: 4),
              const Text('Computers see this name in their list.', style: TextStyle(color: SpanColors.muted, fontSize: 13)),
              const SizedBox(height: 12),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                value: _auto,
                onChanged: (v) => setState(() => _auto = v),
                title: const Text('Accept files sent to me without asking'),
                subtitle: const Text('This skips the verification code check, so leave it off on shared Wi-Fi.'),
              ),
              const Divider(),
              const SizedBox(height: 10),
              const Text('Span folder', style: TextStyle(color: SpanColors.muted)),
              Text(s.shareDir.replaceFirst('/storage/emulated/0/', '')),
            ]),
          ),
          const SizedBox(height: 12),
          Pane(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text('Relay server', style: display(19)),
              const SizedBox(height: 4),
              const Text(
                'Optional. A relay lets this phone reach your computers when it\'s not on the same Wi-Fi. '
                'Transfers stay end-to-end encrypted, and computers on the same Wi-Fi are still reached directly.',
                style: TextStyle(color: SpanColors.muted),
              ),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                value: _relayOn,
                onChanged: (v) => setState(() => _relayOn = v),
                title: const Text('Use a relay server'),
              ),
              TextField(
                controller: _addr,
                enabled: _relayOn,
                keyboardType: TextInputType.url,
                decoration: const InputDecoration(hintText: 'myserver.com:50507'),
              ),
              const SizedBox(height: 8),
              TextField(
                controller: _pw,
                enabled: _relayOn,
                obscureText: true,
                decoration: const InputDecoration(hintText: 'Relay password'),
              ),
              if (s.relayStatus.isNotEmpty) ...[
                const SizedBox(height: 8),
                Text('Relay ${s.relayStatus}', style: const TextStyle(color: SpanColors.muted)),
              ],
            ]),
          ),
          if (_error != null) ...[
            const SizedBox(height: 10),
            Text(_error!, style: const TextStyle(color: SpanColors.danger)),
          ],
          const SizedBox(height: 16),
          Align(alignment: Alignment.centerRight, child: FilledButton(onPressed: _save, child: const Text('Save'))),
        ]),
      ),
    );
  }
}
