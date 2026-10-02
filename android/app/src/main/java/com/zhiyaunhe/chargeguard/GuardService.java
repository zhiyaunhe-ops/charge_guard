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

import org.json.JSONObject;

import java.nio.charset.StandardCharsets;

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
    private Thread javaWatchdog;
    private volatile boolean running = false;
    private volatile long startedAt = 0;   // 服务启动时刻：看门狗只查启动之后的 mtime

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
        // v2.5：只在充电时持锁。恒持 PARTIAL_WAKE_LOCK 让 CPU 永远进不了 suspend ——
        // batterystats 实测 chargeguard:loop 连续持有 10h31m，占待机耗电大头。
        // 插电：持锁保证 60s 节拍（充电时耗电无所谓，且要精确切 65%）；
        // 不插电：放锁深睡，5 分钟一次的复活闹钟（ELAPSED_WAKEUP）负责唤醒采样，
        // 待机掉电 ~1%/10min，5 分钟粒度足够。读不到电量按充电处理：宁可多耗电不能漏管。
        if (isPlugged()) {
            wakeLock.acquire();
        } else {
            wakeLock.release();
        }
        startedAt = System.currentTimeMillis();
        scheduleRevivalAlarm();
        startPython();
        startJavaWatchdog();
        return START_STICKY;
    }

    private boolean isPlugged() {
        try {
            android.content.Intent i = registerReceiver(null,
                    new android.content.IntentFilter(android.content.Intent.ACTION_BATTERY_CHANGED));
            return i != null && i.getIntExtra("plugged", 0) != 0;
        } catch (Exception e) {
            return true;
        }
    }

    /** Java 侧卡死看门狗（v2.3）：python 侧看门狗（android_main._watchdog）会被 GIL
     *  一起冻住 —— 2026-10-01 20:48 那次 stall 里三个 python 线程全无声，stall_count
     *  都没来得及自增。这条线程不碰 python，只盯 status.json 的 mtime（守护每轮刷新）；
     *  超时 ⇒ 把全部线程的 Java 栈（能看到卡在哪个 Java 方法，如 registerReceiver）
     *  落盘 files/stall_dump_java.txt，再 killProcess（SIGKILL，立死无弹窗）。 */
    private synchronized void startJavaWatchdog() {
        if (javaWatchdog != null && javaWatchdog.isAlive()) return;
        final long intervalSec = readIntervalSec();
        // v2.5：不插电时 CPU 深睡、靠 5 分钟闹钟唤醒采样，status mtime 合法间隔拉长到
        // ~300s+；阈值必须盖过它，否则每次唤醒都会误杀。600s 起步。
        final long thresholdMs = Math.max(600_000L, intervalSec * 8000 + 60_000);
        javaWatchdog = new Thread(() -> {
            while (running) {
                try {
                    Thread.sleep(30_000);
                } catch (InterruptedException e) {
                    return;
                }
                if (!running) return;
                java.io.File status = new java.io.File(getFilesDir(), "status.json");
                long mtime = status.exists() ? status.lastModified() : 0L;
                long ref = Math.max(mtime, startedAt);
                if (ref > 0 && System.currentTimeMillis() - ref > thresholdMs) {
                    dumpThreadsAndKill(thresholdMs, mtime);
                    return;
                }
            }
        }, "java-watchdog");
        javaWatchdog.setDaemon(true);
        javaWatchdog.start();
    }

    private long readIntervalSec() {
        try {
            JSONObject cfg = new JSONObject(new String(
                    java.nio.file.Files.readAllBytes(
                            new java.io.File(getFilesDir(), "config.json").toPath()),
                    StandardCharsets.UTF_8));
            return cfg.getJSONObject("loop").getLong("interval_sec");
        } catch (Exception e) {
            return 60L;
        }
    }

    private void dumpThreadsAndKill(long thresholdMs, long mtime) {
        StringBuilder sb = new StringBuilder();
        sb.append(String.format("===== java watchdog %tF %<tT: status mtime stale > %d ms (mtime=%d) =====%n",
                System.currentTimeMillis(), thresholdMs, mtime));
        for (java.util.Map.Entry<Thread, java.lang.StackTraceElement[]> e
                : Thread.getAllStackTraces().entrySet()) {
            Thread t = e.getKey();
            sb.append("\n\"").append(t.getName()).append("\" state=").append(t.getState()).append('\n');
            for (StackTraceElement el : e.getValue()) sb.append("    at ").append(el).append('\n');
        }
        try {
            java.io.File out = new java.io.File(getFilesDir(), "stall_dump_java.txt");
            java.io.FileWriter w = new java.io.FileWriter(out, true);
            w.write(sb.toString());
            w.close();
        } catch (Exception ignored) { }
        Log.e(TAG, "status stale > " + thresholdMs + "ms; dumping threads and killing process");
        android.os.Process.killProcess(android.os.Process.myPid());
    }

    /** 复活闹钟链：每次服务启动都预约 15 分钟后的一次性闹钟（到点由 GuardAlarmReceiver
     *  拉起服务 → onStartCommand 再续约）。进程死透后闹钟仍会触发（除非被 force-stop），
     *  这是 START_STICKY 在 HyperOS 上被拦之后验证可行的自动补位通道
     *  （2026-10-01：SIGKILL 自救 / SwipeUpClean 之后系统都没重启过服务）。 */
    private void scheduleRevivalAlarm() {
        AlarmManager am = (AlarmManager) getSystemService(ALARM_SERVICE);
        PendingIntent pi = PendingIntent.getBroadcast(this, 2,
                new Intent(this, GuardAlarmReceiver.class),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        am.setAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP,
                SystemClock.elapsedRealtime() + 5 * 60 * 1000L, pi);
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
