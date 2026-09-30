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
            // 只留 arm64（K70 Pro）。x86_64 是给 MuMu 试跑的，代价是整个 Python 运行时 ×2
            //（实测 +10.3MB），手机是唯一目标后就不值得带。要在模拟器试跑时临时加回。
            abiFilters += listOf("arm64-v8a")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    // 固定 debug 签名：CI 每次运行随机生成的 debug key 会让 install -r 报
    // SIGNATURES DO NOT MATCH（2026-10-01 实测），只能卸载重装、配置全丢。
    // keystore 是 PKCS12、密码就在下面 —— 私有仓库 + 只签 debug 包，接受。
    signingConfigs {
        getByName("debug") {
            storeFile = file("../debug.keystore")
            storePassword = "chargeguard"
            keyAlias = "chargeguard"
            keyPassword = "chargeguard"
            storeType = "PKCS12"
        }
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
