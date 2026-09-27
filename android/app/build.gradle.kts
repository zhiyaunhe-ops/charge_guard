plugins {
    id("com.android.application")
    id("com.chaquo.python")
}

android {
    namespace = "com.zhiyaunhe.chargeguard"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.zhiyaunhe.chargeguard"
        minSdk = 24
        targetSdk = 33          // 刻意不追新：33 以下前台服务不必声明 type，通知权限也少一道弹窗
        versionCode = 1
        versionName = "1.0"
        ndk {
            // arm64 给 K70 Pro；x86_64 给 MuMu（先在模拟器里验一轮再上真机）
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

// Python 源码与数据文件放 app/src/main/python/（Chaquopy 默认目录）。
// charge_guard_phone.py 与 phone/ 同名文件必须保持一致 —— CI 里有 diff 检查，改动请走
// scripts/sync_android_python.sh，不要直接改这边。
chaquopy {
    defaultConfig {
        version = "3.13"
    }
}
