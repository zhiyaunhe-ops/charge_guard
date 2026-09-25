# 对第三方实现的本地补丁

这些补丁**不属于我们的代码**，是为了排错/可用性对 `third_party/` 里克隆下来的实现做的最小改动。
`third_party/` 本身在 `.gitignore` 里（各自有上游仓库），所以补丁以 `.patch` 形式单独留档，
保证「改了什么、基于哪个上游版本」可追溯。

## 0001 — fetch-tokens-2fa-diagnostics

- 目标：`third_party/xiaomi-ad1204-python/fetch_tokens.py`
- 上游版本：`ohaiibuzzle/xiaomi-ad1204-python` @ `9227df07b79af7d2ddb6730ffdabedc53bdae77f`（2026-09-15）
- 起因：登录时过完图形验证码、也填了邮箱 2FA 码，却被报
  `[-] invalid login/password (step 2)` —— 而**这个文案是误导的**：
  `_2fa_email()` 在校验失败时只 `return False`，`login()` 把所有失败统一报成这一句，
  于是「2FA 没过」看起来像「密码错了」。
- 更要紧的是：`sendEmailTicket` 与 `verifyEmail` 两个 POST 的响应被直接丢弃，
  服务端说了什么（错误码、是否真的发了邮件、ick cookie 有没有）全都看不到，
  失败就变成只能盲目重试 —— 而盲目重试正是会触发风控的那种操作。
- 改动：只加诊断输出，**不改任何控制流**。打印
  `sendEmailTicket` 的 HTTP 状态与 body、`ick` cookie 是否存在、
  `verifyEmail` 的 HTTP 状态与 body、以及 Location 头。
- 应用方式：
  ```bash
  cd third_party/xiaomi-ad1204-python
  git apply ../../patches/0001-fetch-tokens-2fa-diagnostics.patch
  ```
- 回滚：`git -C third_party/xiaomi-ad1204-python checkout -- fetch_tokens.py`

⚠️ 上游更新后应用本补丁可能冲突 —— 冲突就以「重新加诊断输出」为准，别硬套。
