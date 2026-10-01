package com.zhiyaunhe.chargeguard;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;

/** 复活闹钟：GuardService 每次启动都会预约 15 分钟后的一次性闹钟（收到后再由服务续约），
 *  进程无论怎么死（SIGKILL 自救 / 系统回收 / 上滑清理），只要不是 force-stop，
 *  闹钟都会照常触发并重新拉起前台服务 —— 这是 2026-10-01 实测 HyperOS 上唯一
 *  能自动补位的通道（START_STICKY 被拦、BootReceiver 只管开机）。
 *
 *  用户按过「停止」后 should_run=false，这里直接空转，不会违背用户的停止意图。 */
public class GuardAlarmReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        SharedPreferences prefs = context.getSharedPreferences("cfg", Context.MODE_PRIVATE);
        if (!prefs.getBoolean("should_run", true)) return;
        context.startForegroundService(new Intent(context, GuardService.class));
    }
}
