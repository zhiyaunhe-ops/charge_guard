package com.zhiyaunhe.chargeguard;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.os.IBinder;
import android.os.PowerManager;
import android.util.Log;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

/** 前台服务：常驻跑 Python 守护循环（charge_guard_phone 的判定 + miIO 控插座）。
 *
 *  为什么用前台服务：Termux 方案死于「应用被划掉 / MIUI 强制停止」——前台服务是
 *  Android 官方的常驻形态，配合 MIUI 的「自启动 + 省电策略无限制」就是能做到的上限。
 *  START_STICKY：被系统回收后尽量拉回。
 */
public class GuardService extends Service {
    private static final String TAG = "chargeguard";
    private static final String CHANNEL_ID = "guard";
    private static final int NOTIF_ID = 1;

    private PowerManager.WakeLock wakeLock;
    private Thread worker;

    @Override
    public void onCreate() {
        super.onCreate();
        NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        nm.createNotificationChannel(new NotificationChannel(CHANNEL_ID, "充电守护",
                NotificationManager.IMPORTANCE_LOW));
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        startForeground(NOTIF_ID, buildNotification("65/50 滞回管理插座"));
        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        if (wakeLock == null) {
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "chargeguard:loop");
            wakeLock.setReferenceCounted(false);
        }
        wakeLock.acquire();
        startPython();
        return START_STICKY;
    }

    private synchronized void startPython() {
        if (worker != null && worker.isAlive()) return;   // 单实例：服务里只允许一个循环
        if (!Python.isStarted()) {
            Python.start(new AndroidPlatform(getApplicationContext()));
        }
        final Context app = getApplicationContext();
        final String filesDir = getFilesDir().getAbsolutePath();
        worker = new Thread(() -> {
            try {
                PyObject mod = Python.getInstance().getModule("android_main");
                mod.callAttr("start_guard", filesDir, app);
            } catch (Throwable t) {
                Log.e(TAG, "python guard loop died", t);
            }
        }, "guard-python");
        worker.start();
    }

    private Notification buildNotification(String text) {
        return new Notification.Builder(this, CHANNEL_ID)
                .setSmallIcon(android.R.drawable.ic_lock_idle_charging)
                .setContentTitle("充电守护运行中")
                .setContentText(text)
                .setOngoing(true)
                .build();
    }

    @Override
    public void onDestroy() {
        try {
            if (Python.isStarted()) {
                Python.getInstance().getModule("android_main").callAttr("stop_guard");
            }
        } catch (Throwable ignored) { }
        if (wakeLock != null) wakeLock.release();
        stopForeground(STOP_FOREGROUND_REMOVE);
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
