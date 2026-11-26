// Span's design tokens, shared with the desktop window: cobalt always means
// "this device", jade "the other computer", graphite is the bridge between.
import 'package:flutter/material.dart';

class SpanColors {
  static const paper = Color(0xFFF2F4F6);
  static const surface = Color(0xFFFFFFFF);
  static const ink = Color(0xFF1A1E23);
  static const muted = Color(0xFF5F6873);
  static const line = Color(0xFFD9DEE3);
  static const local = Color(0xFF2B59C3);
  static const localTint = Color(0xFFE2E9F8);
  static const remote = Color(0xFF0E8A6E);
  static const remoteTint = Color(0xFFDCF0E9);
  static const bridge = Color(0xFF262B33);
  static const bridgeInk = Color(0xFFE8ECF0);
  static const bridgeMuted = Color(0xFF9AA3AD);
  static const bridgeLine = Color(0xFF3B424C);
  static const toRemote = Color(0xFF6E95F0); // arrows / bars on the bridge
  static const toLocal = Color(0xFF3FC4A3);
  static const danger = Color(0xFFB3261E);
}

/// Condensed DIN-like display face: Android ships Roboto Condensed as
/// "sans-serif-condensed", matching Bahnschrift / DIN Alternate on desktop.
const displayFamily = 'sans-serif-condensed';

TextStyle display(double size, {FontWeight weight = FontWeight.w600, Color? color}) => TextStyle(
      fontFamily: displayFamily,
      fontSize: size,
      fontWeight: weight,
      color: color ?? SpanColors.ink,
      fontFeatures: const [FontFeature.tabularFigures()],
    );

ThemeData spanTheme() {
  final scheme = ColorScheme.fromSeed(
    seedColor: SpanColors.local,
    primary: SpanColors.local,
    secondary: SpanColors.remote,
    surface: SpanColors.surface,
    error: SpanColors.danger,
  );
  return ThemeData(
    useMaterial3: true,
    colorScheme: scheme,
    scaffoldBackgroundColor: SpanColors.paper,
    appBarTheme: AppBarTheme(
      backgroundColor: SpanColors.paper,
      foregroundColor: SpanColors.ink,
      elevation: 0,
      scrolledUnderElevation: 0,
      titleTextStyle: display(22),
    ),
    dividerTheme: const DividerThemeData(color: SpanColors.line, space: 1, thickness: 1),
    listTileTheme: const ListTileThemeData(iconColor: SpanColors.muted),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: SpanColors.paper,
      isDense: true,
      border: OutlineInputBorder(borderRadius: BorderRadius.circular(8), borderSide: const BorderSide(color: SpanColors.line)),
      enabledBorder:
          OutlineInputBorder(borderRadius: BorderRadius.circular(8), borderSide: const BorderSide(color: SpanColors.line)),
      focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(8), borderSide: const BorderSide(color: SpanColors.local, width: 1.5)),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8))),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        foregroundColor: SpanColors.ink,
        side: const BorderSide(color: SpanColors.line),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
      ),
    ),
    snackBarTheme: const SnackBarThemeData(
      backgroundColor: SpanColors.bridge,
      contentTextStyle: TextStyle(color: SpanColors.bridgeInk),
      behavior: SnackBarBehavior.floating,
    ),
  );
}
