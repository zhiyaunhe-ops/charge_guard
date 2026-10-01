package com.zhiyaunhe.chargeguard;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.util.Log;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import org.json.JSONObject;

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;

/** 唯一的界面：填插座 IP/token 与阈值 → 启动/停止前台服务；看状态；做读/写测试。
 *  界面刻意做成单页程序化布局（无 XML layout、无第三方库），可构建性优先。 */
public class MainActivity extends Activity {
    private static final String TAG = "chargeguard";
    private static final int REQ_NOTIF = 1;

    private EditText etIp, etToken, etStop, etResume, etInterval;
    private CheckBox cbAutoStart;
    private TextView tvStatus;
    private final Handler refresh = new Handler(Looper.getMainLooper());
    private final Runnable refreshTick = new Runnable() {
        @Override public void run() {
            tvStatus.setText(readStatusText());
            refresh.postDelayed(this, 2000);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(buildUi());
        loadPrefs();
        // 只在还没授权时请求：2026-10-01 实测无脑请求让每次打开都弹一次权限窗。
        // 用户点过「拒绝」后（USER_FIXED）系统也不再弹，前台通知丢失可接受——
        // 守护不依赖通知，只是常驻提示。
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                        != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIF);
        }
    }

    @Override protected void onResume() { super.onResume(); refresh.post(refreshTick); }
    @Override protected void onPause() { super.onPause(); refresh.removeCallbacks(refreshTick); }

    // ------------------------------------------------------------ UI
    private View buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(16), dp(16), dp(16), dp(16));

        root.addView(label("插座 IP"));
        etIp = new EditText(this);
        etIp.setInputType(InputType.TYPE_CLASS_PHONE);
        etIp.setHint("192.168.0.99");
        root.addView(etIp);

        root.addView(label("插座 token（32 位 hex，米家派生的局域网令牌）"));
        etToken = new EditText(this);
        etToken.setInputType(InputType.TYPE_CLASS_TEXT);
        etToken.setTypeface(Typeface.MONOSPACE);
        root.addView(etToken);

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        etStop = smallField("65"); etResume = smallField("50"); etInterval = smallField("60");
        row.addView(titled("停于 %", etStop), w0());
        row.addView(titled("起于 %", etResume), w0());
        row.addView(titled("间隔 s", etInterval), w0());
        root.addView(row);

        cbAutoStart = new CheckBox(this);
        cbAutoStart.setText("开机自启（建议开）");
        cbAutoStart.setChecked(true);
        root.addView(cbAutoStart);

        LinearLayout btns = new LinearLayout(this);
        btns.setOrientation(LinearLayout.HORIZONTAL);
        btns.addView(button("保存并启动", v -> saveAndStart()), w0());
        btns.addView(button("停止", v -> stopGuard()), w0());
        root.addView(btns);

        LinearLayout btns2 = new LinearLayout(this);
        btns2.setOrientation(LinearLayout.HORIZONTAL);
        btns2.addView(button("读插座测试", v -> runPy("probe_plug", "读插座…")), w0());
        btns2.addView(button("写入测试(通→断)", v -> runPy("write_test", "写入测试…")), w0());
        root.addView(btns2);

        root.addView(label("状态"));
        tvStatus = new TextView(this);
        tvStatus.setTypeface(Typeface.MONOSPACE);
        tvStatus.setTextSize(12);
        root.addView(tvStatus);

        ScrollView sv = new ScrollView(this);
        sv.addView(root);
        return sv;
    }

    private TextView label(String s) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setPadding(0, dp(10), 0, dp(2));
        return t;
    }

    private EditText smallField(String hint) {
        EditText e = new EditText(this);
        e.setInputType(InputType.TYPE_CLASS_NUMBER);
        e.setHint(hint);
        e.setGravity(Gravity.TOP);
        return e;
    }

    private LinearLayout titled(String title, EditText field) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(dp(0), dp(6), dp(8), 0);
        TextView t = new TextView(this);
        t.setText(title);
        box.addView(t);
        box.addView(field);
        return box;
    }

    private Button button(String text, View.OnClickListener onClick) {
        Button b = new Button(this);
        b.setText(text);
        b.setOnClickListener(onClick);
        return b;
    }

    private LinearLayout.LayoutParams w0() {
        return new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
    }

    private int dp(int v) { return (int) (v * getResources().getDisplayMetrics().density); }

    // ------------------------------------------------------------ 动作
    private void loadPrefs() {
        SharedPreferences p = prefs();
        etIp.setText(p.getString("ip", "192.168.0.99"));
        etToken.setText(p.getString("token", ""));
        etStop.setText(String.valueOf(p.getInt("stop_at", 65)));
        etResume.setText(String.valueOf(p.getInt("resume_at", 50)));
        etInterval.setText(String.valueOf(p.getInt("interval_sec", 60)));
        cbAutoStart.setChecked(p.getBoolean("auto_start", true));
    }

    private SharedPreferences prefs() { return getSharedPreferences("cfg", Context.MODE_PRIVATE); }

    private void saveAndStart() {
        String ip = etIp.getText().toString().trim();
        String token = etToken.getText().toString().trim().toLowerCase();
        int stop = parseInt(etStop, 65), resume = parseInt(etResume, 50), interval = parseInt(etInterval, 60);
        if (ip.isEmpty()) { toast("插座 IP 不能为空"); return; }
        if (!token.matches("[0-9a-f]{32}")) { toast("token 必须是 32 位 hex"); return; }
        if (resume >= stop) { toast("起于 必须小于 停于"); return; }

        prefs().edit()
                .putString("ip", ip).putString("token", token)
                .putInt("stop_at", stop).putInt("resume_at", resume)
                .putInt("interval_sec", interval)
                .putBoolean("auto_start", cbAutoStart.isChecked())
                .apply();

        try {
            JSONObject cfg = new JSONObject();
            cfg.put("plug", new JSONObject()
                    .put("ip", ip).put("token", token).put("label", "充电器插座"));
            cfg.put("policy", new JSONObject()
                    .put("stop_at", stop).put("resume_at", resume));
            cfg.put("loop", new JSONObject().put("interval_sec", interval));
            Files.write(Paths.get(getFilesDir().getAbsolutePath(), "config.json"),
                    cfg.toString().getBytes(StandardCharsets.UTF_8));
        } catch (Exception e) {
            toast("写 config.json 失败: " + e);
            return;
        }

        requestAllFilesAccess();   // best effort：不授权也能跑，只是 CSV/状态落不到 /sdcard
        Intent svc = new Intent(this, GuardService.class);
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(svc); else startService(svc);
        toast("守护已启动");
    }

    private void stopGuard() {
        stopService(new Intent(this, GuardService.class));
        toast("守护已停止");
    }

    private void requestAllFilesAccess() {
        if (Build.VERSION.SDK_INT >= 30 && !Environment.isExternalStorageManager()) {
            try {
                startActivity(new Intent("android.settings.MANAGE_APP_ALL_FILES_ACCESS_PERMISSION",
                        Uri.parse("package:" + getPackageName())));
            } catch (Exception e) {
                Log.w(TAG, "no all-files settings page", e);
            }
        }
    }

    private void runPy(String fn, String working) {
        tvStatus.setText(working);
        new Thread(() -> {
            String out;
            try {
                if (!Python.isStarted()) Python.start(new AndroidPlatform(getApplicationContext()));
                PyObject mod = Python.getInstance().getModule("android_main");
                out = mod.callAttr(fn, getFilesDir().getAbsolutePath()).toString();
            } catch (Throwable t) {
                out = "ERR " + t;
                Log.e(TAG, "runPy " + fn, t);
            }
            final String s = out;
            runOnUiThread(() -> tvStatus.setText(s));
        }, "guard-pytest").start();
    }

    // ------------------------------------------------------------ 状态
    private String readStatusText() {
        for (String path : new String[]{
                new File(getFilesDir(), "status.json").getAbsolutePath(),
                "/sdcard/charge_guard/apk_status.json"}) {
            try {
                if (new File(path).exists()) {
                    JSONObject st = new JSONObject(new String(Files.readAllBytes(Paths.get(path)), StandardCharsets.UTF_8));
                    return "采样: " + st.opt("ts") + "\n电量: " + st.opt("level") + "%  "
                            + st.opt("temp") + "℃\n插座: " + st.opt("plug_on")
                            + "\n连续失败: " + st.opt("err_streak")
                            + "\n判定: " + st.opt("pending") + "\nCSV: " + st.opt("csv_path");
                }
            } catch (Exception ignored) { }
        }
        return "（还没有状态 —— 启动守护后每 " + etInterval.getText() + "s 刷新一次）";
    }

    private int parseInt(EditText e, int def) {
        try { return Integer.parseInt(e.getText().toString().trim()); }
        catch (Exception ex) { return def; }
    }

    private void toast(String s) { Toast.makeText(this, s, Toast.LENGTH_SHORT).show(); }
}
