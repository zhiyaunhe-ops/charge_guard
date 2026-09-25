# 对第三方实现的本地补丁

这些补丁**不属于我们的代码**，是为了排错/可用性对 `third_party/` 里克隆下来的实现做的最小改动。
`third_party/` 本身在 `.gitignore` 里（各自有上游仓库），所以补丁以 `.patch` 形式单独留档，
保证「改了什么、基于哪个上游版本」可追溯。

## 0001 — fetch-tokens-local-fixes

- 目标：`third_party/xiaomi-ad1204-python/fetch_tokens.py`
- 上游版本：`ohaiibuzzle/xiaomi-ad1204-python` @ `9227df07b79af7d2ddb6730ffdabedc53bdae77f`（2026-09-15）
- 应用方式：
  ```bash
  cd third_party/xiaomi-ad1204-python
  git apply ../../patches/0001-fetch-tokens-local-fixes.patch
  ```
- 回滚：`git -C third_party/xiaomi-ad1204-python checkout -- fetch_tokens.py`
- 已验证：
  - 对上游原始文件 `git apply --check` 通过，应用后与本地工作文件逐字节一致（roundtrip）；
  - `patches/test_fetch_tokens_2fa_offline.py` 用假 session 离线跑三条分支，
    **3/3 通过**（手机号分支走 `verifyPhone` 且不发邮件；邮箱分支走 `verifyEmail`；
    码被拒时报明确文案而不是「密码错误」）：
    ```bash
    C:/Users/zhiya/.workbuddy/binaries/python/envs/default/Scripts/python.exe -X utf8 patches/test_fetch_tokens_2fa_offline.py
    ```

本补丁包含四处改动，按发现顺序：

### 1. 非交互输入（`wait_for_input_file`）

`captcha code` 与 `2FA code` 原本走 `input()`，非交互驱动方（双击的 .bat / 自动化）给不出 stdin。
改为轮询 `plugin_out/*.txt`：驱动方看到「在等什么」→ 把值写进文件 → 流程继续（取到即删，默认 600s 超时）。

### 2. 诊断输出（不再丢弃服务端响应）

`sendEmailTicket` / `verify*` 两个 POST 的响应原本被直接丢弃，失败时只能盲目重试
（而盲目重试正是会触发风控的操作）。现在打印 HTTP 状态、body、`Location`、
`ick` 与 `identity_session` cookie 是否存在。

### 3. 2FA 渠道跟随服务端（2026-09-25 实机定位）

- **现象**：邮箱里抄来的验证码是对的，`verifyEmail` 仍返回
  `{"code":2,"flag":4,"options":[4],"version":"v2","showFastUpdateEmailLink":false}`，
  最终被误导性地报成 `[-] invalid login/password (step 2)`。
- **原因**：`17704010514` 是手机号注册的 CN 账号，身份校验会话**只提供手机短信渠道**
  —— `identity/list` 返回 `options:[4]`，其中 `4 = verifyPhone（短信）`、`8 = verifyEmail（邮箱）`
  （映射与两个仍在维护的实现一致：`merdok/homebridge-miot`、`al-one/hass-xiaomi-miot`）。
  原实现硬编码邮箱通道，于是：`sendEmailTicket` 照样返回「成功」并把码发进邮箱，
  但服务端只认短信码 → `verifyEmail` 永远 `code:2`。**不是验证码抄错。**
  ⚠️ 待重跑确认：`identity/list` 的原文当时没有被打印（诊断补丁只加了 send/verify 两处），
  「只给短信」是从 verify 的失败响应体反推的。现在脚本会打印 `identity/list`，跑一次即可定论。
- **顺序纠正**：这一步**不等于账号开了二次验证**。账号设置里的「二次验证」是关着的也能触发
  —— 触发它的是小米的**风控**（新设备 / 新 UA / 新 IP / 频繁登录都会触发），
  官方口径是「每次登录都会检查网络环境，检测到异常或风险就必须验证」。
  所以「没开 2FA」与「被要求输验证码」两件事可以同时成立。
- **改动**：先 GET `identity/list`，按 `flag`/`options` 决定渠道 ——
  `8` → 邮箱（保留 `sendEmailTicket`，提示语不变）；
  `4` → 短信（验证码由 authStart 自动下发，没有对应的主动 send 调用，提示语改为「短信验证码」）；
  按渠道 POST `verifyEmail` / `verifyPhone`，并显式带上 `identity_session` cookie；
  返回 code 非 0 时直接以明确文案退出，不再谎报「密码错误」。

### 4. 客户端身份固定（`stable_client_identity`）

- **问题**：原实现**每次运行都随机生成 UA 与 `deviceId`**（`MiCloud.__init__` 里两段
  `random`），对账号系统来说这等价于「每次登录都换一台从没见过的设备」——
  正好落在风控最敏感的输入上，于是每轮都被要求身份验证。反复重试还会加重风控。
- **改动**：第一次生成后写进 `xiaomi.client.json`（已加进上游 `.gitignore`），之后一直复用；
  `login()` 里把 `deviceId` 打印出来，让「同一台设备」这件事可见。
  真实米家 App 的同一次安装里这两个值本来就是稳定的，这里只是照它的行为。
- **注意**：这是**降低触发概率**，不是保证——风控还看 IP 与登录频率。
  但一旦成功过一次，凭据会缓存进 `xiaomi.token`，之后重跑直接复用、不再登录（`load_token`），
  所以这套流程只需要闯过一次。

⚠️ 上游更新后应用本补丁可能冲突 —— 冲突就以「按上面三条重新加改动」为准，别硬套。
