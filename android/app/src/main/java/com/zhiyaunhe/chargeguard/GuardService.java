package com.zhiyaunhe.chargeguard;

import android.app.AlarmManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.os.SystemClock;
import android.util.Log;

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
    private Thread supervisor;
    private volatile boolean running = false;

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

    /** 监督线程：start_guard 无论正常返回还是抛异常，30s 后重新拉起。
     *
     *  2026-10-01 实测：Python 循环会在服务存活时无声死掉（status 12:50:26 后不再更新，
     *  前台服务和进程都活着），单层线程死了只有一条日志、没有任何补救。这层保证守护
     *  死后至多 30s+一个周期 内回来；start_guard 自身会把 STOP 标志复位，无需额外状态。 */
    private synchronized void startPython() {
        if (supervisor != null && supervisor.isAlive()) return;   // 单实例：服务里只允许一个监督者
        if (!Python.isStarted()) {
            Python.start(new AndroidPlatform(getApplicationContext()));
        }
        running = true;
        final Context app = getApplicationContext();
        final String filesDir = getFilesDir().getAbsolutePath();
        supervisor = new Thread(() -> {
            while (running) {
                try {
                    Python.getInstance().getModule("android_main")
                            .callAttr("start_guard", filesDir, app);
                    if (running) Log.w(TAG, "guard loop returned unexpectedly; restart in 30s");
                } catch (Throwable t) {
                    Log.e(TAG, "guard loop died; restart in 30s", t);
                }
                for (int i = 0; running && i < 30; i++) {   // 1s 步进：onDestroy 最多等 1s
                    try {
                        Thread.sleep(1000);
                    } catch (InterruptedException e) {
                        return;
                    }
                }
            }
        }, "guard-supervisor");
        supervisor.start();
    }

    private Notification buildNotification(String text) {
        return new Notification.Builder(this, CHANNEL_ID)
                .setSmallIcon(android.R.drawable.ic_lock_idle_charging)
                .setContentTitle("充电守护运行中")
                .setContentText(text)
                .setOngoing(true)
                .build();
    }

    /** 进程被杀前的最后自救窗口：上滑清理（HyperOS SwipeUpClean，2026-10-01 实测
     *  am_kill due to SwipeUpClean，START_STICKY 也被拦）走到这里时进程还在，
     *  用闹钟预约 3s 后重新拉起。能否成功取决于 ROM；最可靠的仍是用户侧三项设置：
     *  MIUI 自启动 + 省电策略无限制 + 最近任务锁定。 */
    @Override
    public void onTaskRemoved(Intent rootIntent) {
        if (getSharedPreferences("cfg", MODE_PRIVATE).getBoolean("auto_start", true)) {
            AlarmManager am = (AlarmManager) getSystemService(ALARM_SERVICE);
            Intent svc = new Intent(this, GuardService.class);
            PendingIntent pi = Build.VERSION.SDK_INT >= 26
                    ? PendingIntent.getForegroundService(this, 1, svc,
                            PendingIntent.FLAG_ONE_SHOT | PendingIntent.FLAG_IMMUTABLE)
                    : PendingIntent.getService(this, 1, svc,
                            PendingIntent.FLAG_ONE_SHOT | PendingIntent.FLAG_IMMUTABLE);
            am.setAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP,
                    SystemClock.elapsedRealtime() + 3000, pi);
        }
        super.onTaskRemoved(rootIntent);
    }

    @Override
    public void onDestroy() {
        running = false;
        try {
            if (Python.isStarted()) {
                Python.getInstance().getModule("android_main").callAttr("stop_guard");
            }
        } catch (Throwable ignored) { }
        if (supervisor != null) supervisor.interrupt();
        if (wakeLock != null) wakeLock.release();
        stopForeground(STOP_FOREGROUND_REMOVE);
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
