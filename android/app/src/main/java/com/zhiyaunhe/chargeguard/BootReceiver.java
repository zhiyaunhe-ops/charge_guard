package com.zhiyaunhe.chargeguard;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;

/** 开机自启：只要用户上次是「启动」状态就拉起前台服务。 */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        if (!Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction())) return;
        SharedPreferences prefs = context.getSharedPreferences("cfg", Context.MODE_PRIVATE);
        if (!prefs.getBoolean("auto_start", true)) return;
        Intent svc = new Intent(context, GuardService.class);
        context.startForegroundService(svc);
    }
}
