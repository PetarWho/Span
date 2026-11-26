import 'package:flutter/material.dart';

import '../app_state.dart';
import '../core/transfer.dart';
import '../core/util.dart';
import 'theme.dart';

/// Gives every screen access to the one AppState.
class AppScope extends InheritedNotifier<AppState> {
  const AppScope({super.key, required AppState state, required super.child}) : super(notifier: state);

  static AppState of(BuildContext context) => context.dependOnInheritedWidgetOfExactType<AppScope>()!.notifier!;
}

class Dot extends StatelessWidget {
  final Color color;
  final double size;
  const Dot(this.color, {super.key, this.size = 10});

  @override
  Widget build(BuildContext context) =>
      Container(width: size, height: size, decoration: BoxDecoration(color: color, shape: BoxShape.circle));
}

/// White rounded panel, the same as the desktop panes.
class Pane extends StatelessWidget {
  final Widget child;
  final EdgeInsetsGeometry padding;
  const Pane({super.key, required this.child, this.padding = const EdgeInsets.all(16)});

  @override
  Widget build(BuildContext context) => Container(
        decoration: BoxDecoration(
          color: SpanColors.surface,
          border: Border.all(color: SpanColors.line),
          borderRadius: BorderRadius.circular(12),
        ),
        padding: padding,
        child: child,
      );
}

/// The horizontal link from this phone (left) to the other computer (right).
class LinkLine extends StatelessWidget {
  final bool connected;
  const LinkLine({super.key, required this.connected});

  @override
  Widget build(BuildContext context) =>
      SizedBox(height: 28, child: CustomPaint(painter: _LinkPainter(connected), size: Size.infinite));
}

class _LinkPainter extends CustomPainter {
  final bool connected;
  _LinkPainter(this.connected);

  @override
  void paint(Canvas canvas, Size size) {
    const r = 5.0;
    final y = size.height / 2;
    final paint = Paint()
      ..color = connected ? SpanColors.bridgeInk : SpanColors.bridgeMuted
      ..strokeWidth = 1.5;
    if (connected) {
      canvas.drawLine(Offset(r * 2, y), Offset(size.width - r * 2, y), paint);
    } else {
      for (var x = r * 2; x < size.width - r * 2; x += 8) {
        canvas.drawLine(Offset(x, y), Offset(x + 4, y), paint);
      }
    }
    canvas.drawCircle(Offset(r, y), r, Paint()..color = SpanColors.toRemote);
    canvas.drawCircle(
        Offset(size.width - r, y), r, Paint()..color = connected ? SpanColors.toLocal : SpanColors.bridgeLine);
    if (connected) {
      final cx = size.width / 2;
      canvas.drawRect(Rect.fromCenter(center: Offset(cx, y), width: 26, height: 26), Paint()..color = SpanColors.bridge);
      canvas.drawRRect(
          RRect.fromRectAndRadius(Rect.fromLTWH(cx - 6, y - 2, 12, 9), const Radius.circular(2)),
          Paint()..color = SpanColors.bridgeInk);
      canvas.drawArc(Rect.fromLTWH(cx - 3.5, y - 8.5, 7, 7), 3.14159, 3.14159, false,
          Paint()
            ..color = SpanColors.bridgeInk
            ..style = PaintingStyle.stroke
            ..strokeWidth = 1.6);
      canvas.drawLine(Offset(cx - 3.5, y - 5), Offset(cx - 3.5, y - 2),
          Paint()..color = SpanColors.bridgeInk..strokeWidth = 1.6);
      canvas.drawLine(Offset(cx + 3.5, y - 5), Offset(cx + 3.5, y - 2),
          Paint()..color = SpanColors.bridgeInk..strokeWidth = 1.6);
    }
  }

  @override
  bool shouldRepaint(_LinkPainter old) => old.connected != connected;
}

/// One transfer row on the graphite bridge.
class TransferRow extends StatelessWidget {
  final Transfer t;
  const TransferRow(this.t, {super.key});

  @override
  Widget build(BuildContext context) {
    final color = t.toRemote ? SpanColors.toRemote : SpanColors.toLocal;
    final failed = t.status == 'failed';
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Icon(t.toRemote ? Icons.arrow_forward : Icons.arrow_back,
            size: 20, color: color, semanticLabel: t.toRemote ? 'to the other computer' : 'to this phone'),
        const SizedBox(width: 10),
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(t.title,
                maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(color: SpanColors.bridgeInk)),
            const SizedBox(height: 5),
            ClipRRect(
              borderRadius: BorderRadius.circular(1),
              child: LinearProgressIndicator(
                value: t.status == 'done' ? 1 : (t.total > 0 ? t.done / t.total : null),
                minHeight: 3,
                backgroundColor: SpanColors.bridgeLine,
                color: failed ? const Color(0xFFE07A73) : color,
              ),
            ),
            const SizedBox(height: 4),
            Text(t.message, style: const TextStyle(color: SpanColors.bridgeMuted, fontSize: 12.5)),
          ]),
        ),
        if (t.status == 'running' && t.cancel != null)
          IconButton(
            visualDensity: VisualDensity.compact,
            tooltip: 'Cancel this transfer',
            icon: const Icon(Icons.close, size: 18, color: SpanColors.bridgeMuted),
            onPressed: t.cancel!.cancel,
          ),
      ]),
    );
  }
}

/// The graphite band: link state, verification code and transfers in flight.
class Bridge extends StatelessWidget {
  final String state;
  final String? code;
  final bool connected;
  final List<Transfer> transfers;
  final List<Widget> extra;
  final bool showLink;
  const Bridge({
    this.showLink = true,
    super.key,
    required this.state,
    this.code,
    this.connected = false,
    this.transfers = const [],
    this.extra = const [],
  });

  @override
  Widget build(BuildContext context) => Container(
        decoration: BoxDecoration(color: SpanColors.bridge, borderRadius: BorderRadius.circular(12)),
        padding: const EdgeInsets.fromLTRB(16, 14, 16, 10),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            Expanded(child: Text(state, style: display(17, color: SpanColors.bridgeInk))),
            if (code != null)
              Text(code!, style: display(22, weight: FontWeight.w400, color: SpanColors.bridgeInk)),
          ]),
          if (showLink) ...[const SizedBox(height: 8), LinkLine(connected: connected)],
          for (final t in transfers) TransferRow(t),
          ...extra,
        ]),
      );
}

/// Asks before giving access, with the verification code front and centre.
Future<bool> askDialog(
  BuildContext context, {
  required String title,
  required String body,
  required String code,
  required String yes,
  required String no,
  String? details,
}) async {
  final r = await showDialog<bool>(
    context: context,
    barrierDismissible: false,
    builder: (ctx) => AlertDialog(
      backgroundColor: SpanColors.surface,
      title: Text(title, style: display(22)),
      content: SingleChildScrollView(
        child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(body),
          if (details != null) ...[
            const SizedBox(height: 8),
            Text(details, style: const TextStyle(color: SpanColors.muted, fontSize: 13)),
          ],
          const SizedBox(height: 16),
          Container(
            width: double.infinity,
            padding: const EdgeInsets.all(14),
            decoration: BoxDecoration(color: SpanColors.paper, borderRadius: BorderRadius.circular(10)),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              const Text('Verification code', style: TextStyle(color: SpanColors.muted)),
              Text(code, style: display(36, weight: FontWeight.w400)),
              const Text('Allow only if it matches the code on the other screen.',
                  style: TextStyle(color: SpanColors.muted, fontSize: 13)),
            ]),
          ),
        ]),
      ),
      actions: [
        TextButton(autofocus: true, onPressed: () => Navigator.pop(ctx, false), child: Text(no)),
        FilledButton(
          style: FilledButton.styleFrom(backgroundColor: SpanColors.remote),
          onPressed: () => Navigator.pop(ctx, true),
          child: Text(yes),
        ),
      ],
    ),
  );
  return r ?? false;
}

String filesSummary(List<IncomingFile> files) {
  final names = files.take(6).map((f) => f.rel).join('\n');
  return files.length > 6 ? '$names\n...and ${files.length - 6} more' : names;
}

String sizeText(int bytes) => human(bytes);
