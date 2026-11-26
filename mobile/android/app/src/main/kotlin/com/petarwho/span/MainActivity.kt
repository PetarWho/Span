package com.petarwho.span

import android.content.Context
import android.net.wifi.WifiManager
import android.os.Build
import android.provider.Settings
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {
    private var multicastLock: WifiManager.MulticastLock? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "span/native").setMethodCallHandler { call, result ->
            when (call.method) {
                // Many phones drop broadcast packets to save power. While Span is open
                // this lock lets the discovery broadcasts from computers through.
                "acquireMulticastLock" -> {
                    if (multicastLock == null) {
                        val wifi = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
                        multicastLock = wifi.createMulticastLock("span-discovery").apply {
                            setReferenceCounted(false)
                            acquire()
                        }
                    }
                    result.success(true)
                }
                "releaseMulticastLock" -> {
                    multicastLock?.release()
                    multicastLock = null
                    result.success(true)
                }
                // The name the user gave the phone ("Petar's Pixel"), else the model.
                "deviceName" -> {
                    val userName = try {
                        Settings.Global.getString(contentResolver, Settings.Global.DEVICE_NAME)
                    } catch (e: Exception) {
                        null
                    }
                    result.success(if (userName.isNullOrBlank()) Build.MODEL else userName)
                }
                else -> result.notImplemented()
            }
        }
    }

    override fun onDestroy() {
        multicastLock?.release()
        multicastLock = null
        super.onDestroy()
    }
}
