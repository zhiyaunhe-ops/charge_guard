"""离线控制流测试：用假 session 跑 fetch_tokens.MiCloud._2fa 的各条分支。

不联网、不碰账号。目的：在真机上再花一次登录机会之前，先证明
「按 identity/list 下发的方式选渠道 + 触发 + 显式下发验证码 + 提交」这条路走得通。

跑法： C:/Users/zhiya/.workbuddy/binaries/python/envs/default/Scripts/python.exe -X utf8 patches/test_fetch_tokens_2fa_offline.py
"""
import importlib.util
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "third_party", "xiaomi-ad1204-python", "fetch_tokens.py")

spec = importlib.util.spec_from_file_location("fetch_tokens", SCRIPT)
ft = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ft)

# 前四条用例固定走文件模式（否则在终端里跑测试会 block 在 input()）；最后一条单独测手输模式
ft.INPUT_MODE = "file"

context_url = "https://account.xiaomi.com/fe/service/identity/authStart?sid=xiaomiio&context=CTX&callback=x"
OK_VERIFY = {"code": 0, "location": "https://account.xiaomi.com/identity/result/check?sid=xiaomiio&context=CTX"}


class Resp:
    def __init__(self, status=200, text="", js=None, headers=None, url="https://account.xiaomi.com/"):
        self.status_code, self.text, self._js = status, text, js
        self.headers, self.url = headers or {}, url

    def json(self):
        if self._js is None:
            raise ValueError("not json")
        return self._js


class Cookies:
    def __init__(self):
        self.d = {}

    def get(self, k, default=None, domain=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v


class FakeSession:
    def __init__(self, handlers):
        self.handlers, self.cookies, self.calls = handlers, Cookies(), []

    def _do(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        for m, sub, fn in self.handlers:
            if m == method and sub in url:
                return fn(url, kwargs)
        raise AssertionError(f"unhandled {method} {url}")

    def get(self, url, **kw):
        return self._do("GET", url, kw)

    def post(self, url, **kw):
        return self._do("POST", url, kw)


def base_handlers(flag_options, verify_body, sess, sts_sets_token=True, resume_works=False):
    """按 Yonsm/MiService 抓包的顺序造响应：
    authStart → identity/list → 触发 GET → send*Ticket → verify → result/check → STS。"""

    def identity_list(url, kw):
        return Resp(200, "&&&START&&&" + json.dumps(flag_options))

    def trigger(url, kw):
        return Resp(200, "", js={"code": 0})

    def send_ticket(url, kw):
        return Resp(200, "", js={"code": 0, "desc": "成功"})

    def verify(url, kw):
        return Resp(200, "", js=verify_body)

    def result_end(url, kw):
        return Resp(200, "", headers={"extension-pragma": json.dumps({"ssecurity": "S123"}),
                                      "Location": "https://sts.api.io.mi.com/sts?foo"})

    def result_check(url, kw):
        return Resp(302, "", headers={"Location": "https://account.xiaomi.com/identity/result/check/end?sid=xiaomiio"})

    def sts(url, kw):
        if sts_sets_token:
            sess.cookies.set("serviceToken", "STOK")
        return Resp(200, "")

    def resume(url, kw):
        js = {"code": 0, "ssecurity": "S123"}
        if resume_works:
            sess.cookies.set("serviceToken", "RESUMED")
            js["location"] = "https://sts.api.io.mi.com/sts?resumed"
        return Resp(200, "&&&START&&&" + json.dumps(js))

    return [
        ("GET", "identity/authStart", lambda u, k: Resp(200, "")),
        ("GET", "/identity/list", identity_list),
        ("GET", "identity/auth/verifyPhone", trigger),
        ("GET", "identity/auth/verifyEmail", trigger),
        ("POST", "identity/auth/sendPhoneTicket", send_ticket),
        ("POST", "identity/auth/sendEmailTicket", send_ticket),
        ("POST", "identity/auth/verifyPhone", verify),
        ("POST", "identity/auth/verifyEmail", verify),
        ("GET", "check/end", result_end),
        ("GET", "identity/result/check", result_check),
        ("GET", "sts.api.io.mi.com/sts", sts),
        ("GET", "pass/serviceLogin", resume),
    ]


def run_case(name, flag_options, verify_body, expect_exit=False, expect_verify=None,
             expect_send=None, seed_code="654321", sts_sets_token=True, resume_works=False,
             expect_token=None, use_file=True):
    sess = FakeSession([])
    sess.handlers = base_handlers(flag_options, verify_body, sess,
                                  sts_sets_token=sts_sets_token, resume_works=resume_works)
    mc = ft.MiCloud()
    mc.s = sess
    sess.cookies.set("identity_session", "IDSESS")
    code_path = os.path.join(ft.OUTDIR, "2fa_code.txt")
    os.makedirs(ft.OUTDIR, exist_ok=True)
    if os.path.exists(code_path):
        os.remove(code_path)
    if use_file:
        with open(code_path, "w", encoding="utf-8") as f:
            f.write(seed_code + "\n")

    exit_msg = None
    try:
        mc._2fa(context_url)
        ok = True
    except SystemExit as e:
        ok, exit_msg = False, str(e)

    called = [(m, u) for m, u, _ in sess.calls]
    status, problems = "PASS", []

    if expect_exit:
        if ok or "2FA 被服务端拒绝" not in (exit_msg or ""):
            status, problems = "FAIL", [f"期望带明确文案的 SystemExit，实际 ok={ok} exit={exit_msg!r}"]
    else:
        if not ok:
            status, problems = "FAIL", [f"不该退出：{exit_msg}"]
        if expect_verify and not any(expect_verify in u for _, u in called):
            status, problems = "FAIL", problems + [f"{expect_verify} 没有被调用；calls={called}"]
        wrong = "verifyEmail" if expect_verify == "verifyPhone" else "verifyPhone"
        if any(wrong in u for _, u in called):
            status, problems = "FAIL", problems + [f"不该调用 {wrong}；calls={called}"]
        if expect_send:
            if not any(expect_send in u for _, u in called):
                status, problems = "FAIL", problems + [f"{expect_send} 没有被调用（码根本没发出去）"]
            other = "sendEmailTicket" if expect_send == "sendPhoneTicket" else "sendPhoneTicket"
            if any(other in u for _, u in called):
                status, problems = "FAIL", problems + [f"不该调用 {other}；calls={called}"]
            # 触发必须在「下发」之前 —— 抓包里的顺序是 ①触发 ②下发，反过来服务端不认
            trg = next((i for i, (m, u) in enumerate(called) if m == "GET" and expect_verify in u), None)
            snd = next((i for i, (m, u) in enumerate(called) if expect_send in u), None)
            if trg is None or snd is None or trg > snd:
                status, problems = "FAIL", problems + [f"顺序不对：触发={trg} 下发={snd}；calls={called}"]
        if not mc.serviceToken and not expect_exit:
            status, problems = "FAIL", problems + ["serviceToken 没拿到（尾巴逻辑断了）"]
        if expect_token and mc.serviceToken != expect_token:
            status, problems = "FAIL", problems + [f"serviceToken 应为 {expect_token}，实际 {mc.serviceToken!r}"]

    for m, u, kw in sess.calls:
        if "verifyPhone" in u or "verifyEmail" in u:
            if m != "POST":
                continue
            data = kw.get("data") or {}
            if str(data.get("_flag")) not in ("4", "8"):
                status, problems = "FAIL", problems + [f"_flag 缺失：{data!r}"]
            if not (kw.get("cookies") or {}).get("identity_session"):
                status, problems = "FAIL", problems + ["verify 请求没带 identity_session cookie"]
            if data.get("ticket") != seed_code:
                status, problems = "FAIL", problems + [f"ticket 不是给的那个：{data!r}"]

    print(f"[{status}] {name}")
    for p in problems:
        print("        -", p)
    return status == "PASS"


def run_tty_case(name, typed="654321"):
    """手输模式：stdin 是终端 → 用 input() 取值，不依赖任何文件。"""
    import builtins
    sess = FakeSession([])
    sess.handlers = base_handlers({"flag": 4, "options": [4]}, OK_VERIFY, sess)
    mc = ft.MiCloud()
    mc.s = sess
    sess.cookies.set("identity_session", "IDSESS")
    code_path = os.path.join(ft.OUTDIR, "2fa_code.txt")
    if os.path.exists(code_path):          # 先删掉，证明成功不是文件兜底给的
        os.remove(code_path)

    class FakeTTY:
        def isatty(self):
            return True

    prompts, old = [], (sys.stdin, builtins.input, ft.INPUT_MODE)
    sys.stdin = FakeTTY()
    builtins.input = lambda prompt="": (prompts.append(prompt), typed)[1]
    ft.INPUT_MODE = "auto"
    try:
        ok = bool(mc._2fa(context_url))
    finally:
        sys.stdin, builtins.input, ft.INPUT_MODE = old

    called = [(m, u) for m, u, _ in sess.calls]
    problems = []
    if not ok:
        problems.append("手输模式没能完成 _2fa")
    if not any("verifyPhone" in u for _, u in called):
        problems.append(f"没走 verifyPhone；calls={called}")
    if not prompts or "SMS" not in prompts[0]:
        problems.append(f"提示语没有告诉用户看短信：{prompts!r}")
    for m, u, kw in sess.calls:
        if m == "POST" and "verifyPhone" in u and (kw.get("data") or {}).get("ticket") != typed:
            problems.append(f"用的不是手输的值：{(kw.get('data') or {})!r}")
    if os.path.exists(code_path):
        problems.append("手输模式不该去读/写 2fa_code.txt")

    print(f"[{'PASS' if not problems else 'FAIL'}] {name}")
    for p in problems:
        print("        -", p)
    return not problems


results = [
    run_case("手机号账号（flag=4）→ 触发+sendPhoneTicket，verifyPhone 成功",
             {"flag": 4, "options": [4]}, OK_VERIFY,
             expect_verify="verifyPhone", expect_send="sendPhoneTicket"),
    run_case("邮箱账号（flag=8）→ 触发+sendEmailTicket，verifyEmail 成功",
             {"flag": 8, "options": [8]}, OK_VERIFY,
             expect_verify="verifyEmail", expect_send="sendEmailTicket"),
    run_case("码被拒（code:2）→ 明确报错而不是「密码错误」",
             {"flag": 4, "options": [4]},
             {"code": 2, "flag": 4, "options": [4], "version": "v2"},
             expect_exit=True),
    run_case("验证通过但 STS 没给 token → resume serviceLogin 兜底捡回来",
             {"flag": 4, "options": [4]}, OK_VERIFY,
             expect_verify="verifyPhone", expect_send="sendPhoneTicket",
             sts_sets_token=False, resume_works=True, expect_token="RESUMED"),
    run_tty_case("人手输模式（stdin 是终端）→ 直接敲码，不碰文件"),
]

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
