"""离线控制流测试：用假 session 跑 fetch_tokens.MiCloud._2fa 的三条分支。

不联网、不碰账号。目的：在真机上再花一次登录机会之前，先证明
「按 identity/list 下发的渠道选 verifyPhone / verifyEmail」这条路走得通。

跑法： C:/Users/zhiya/.workbuddy/binaries/python/envs/default/Scripts/python.exe -X utf8 .workbuddy/2fa_offline_test.py
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

# 前三条用例固定走文件模式（否则在终端里跑测试会 block 在 input()）；第四条单独测手输模式
ft.INPUT_MODE = "file"

context_url = "https://account.xiaomi.com/fe/service/identity/authStart?sid=xiaomiio&context=CTX&callback=x"


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


def base_handlers(flag_options, verify_body, sess):
    """identity/list 之后：按渠道给 verify 响应，再补完 result/check → STS 的尾巴。"""
    def identity_list(url, kw):
        return Resp(200, "&&&START&&&" + json.dumps(flag_options))

    def auth_start(url, kw):
        return Resp(200, "")

    def verify(url, kw):
        return Resp(200, "", js=verify_body)

    def result_check(url, kw):
        return Resp(302, "", headers={"Location": "https://account.xiaomi.com/identity/result/check/end?sid=xiaomiio"})

    def result_end(url, kw):
        return Resp(200, "", headers={"extension-pragma": json.dumps({"ssecurity": "S123"}),
                                      "Location": "https://sts.api.io.mi.com/sts?foo"})

    def sts(url, kw):
        sess.cookies.set("serviceToken", "STOK")
        return Resp(200, "")

    def send_email(url, kw):
        return Resp(200, "", js={"code": 0, "desc": "成功"})

    return [
        ("GET", "identity/authStart", auth_start),
        ("GET", "/identity/list", identity_list),
        ("POST", "identity/auth/sendEmailTicket", send_email),
        ("POST", "identity/auth/verifyPhone", verify),
        ("POST", "identity/auth/verifyEmail", verify),
        ("GET", "check/end", result_end),
        ("GET", "identity/result/check", result_check),
        ("GET", "sts.api.io.mi.com/sts", sts),
    ]


def run_case(name, flag_options, verify_body, expect_exit=False, expect_verify=None,
             expect_send=False, seed_code="654321"):
    sess = FakeSession([])
    sess.handlers = base_handlers(flag_options, verify_body, sess)
    # authStart 的副作用（设置 identity_session）改由闭包处理：直接在 session 上预置
    mc = ft.MiCloud()
    mc.s = sess
    sess.cookies.set("identity_session", "IDSESS")
    os.makedirs(ft.OUTDIR, exist_ok=True)
    with open(os.path.join(ft.OUTDIR, "2fa_code.txt"), "w", encoding="utf-8") as f:
        f.write(seed_code + "\n")

    exit_msg = None
    try:
        mc._2fa(context_url)
        ok = True
    except SystemExit as e:
        ok, exit_msg = False, str(e)

    called = [(m, u) for m, u, _ in sess.calls]
    status = "PASS"
    problems = []
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
        if expect_send and not any("sendEmailTicket" in u for _, u in called):
            status, problems = "FAIL", problems + ["邮箱分支应调用 sendEmailTicket"]
        if not expect_send and any("sendEmailTicket" in u for _, u in called):
            status, problems = "FAIL", problems + ["短信分支不该调用 sendEmailTicket"]
        if not mc.serviceToken and not expect_exit:
            status, problems = "FAIL", problems + ["serviceToken 没拿到（尾巴逻辑断了）"]

    # 校验 verify 请求里带了 _flag 与 identity_session
    for m, u, kw in sess.calls:
        if "verifyPhone" in u or "verifyEmail" in u:
            data = kw.get("data") or {}
            if str(data.get("_flag")) not in ("4", "8"):
                status, problems = "FAIL", problems + [f"_flag 缺失：{data!r}"]
            if not (kw.get("cookies") or {}).get("identity_session"):
                status, problems = "FAIL", problems + ["verify 请求没带 identity_session cookie"]
            if data.get("ticket") != seed_code:
                status, problems = "FAIL", problems + [f"ticket 不是文件里那个：{data!r}"]

    print(f"[{status}] {name}")
    for p in problems:
        print("        -", p)
    return status == "PASS"


def run_tty_case(name, typed="654321"):
    """手输模式：stdin 是终端 → 用 input() 取值，不依赖任何文件。"""
    import builtins
    sess = FakeSession([])
    sess.handlers = base_handlers(
        {"flag": 4, "options": [4]},
        {"code": 0, "location": "https://account.xiaomi.com/identity/result/check?sid=xiaomiio&context=CTX"},
        sess)
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
        if "verifyPhone" in u and (kw.get("data") or {}).get("ticket") != typed:
            problems.append(f"用的不是手输的值：{(kw.get('data') or {})!r}")
    if os.path.exists(code_path):
        problems.append("手输模式不该去读/写 2fa_code.txt")

    print(f"[{'PASS' if not problems else 'FAIL'}] {name}")
    for p in problems:
        print("        -", p)
    return not problems


results = [
    run_case("手机号账号（options:[4]）→ 走 verifyPhone 成功",
             {"flag": 4, "options": [4]},
             {"code": 0, "location": "https://account.xiaomi.com/identity/result/check?sid=xiaomiio&context=CTX"},
             expect_verify="verifyPhone", expect_send=False),
    run_case("邮箱账号（options 含 8）→ 走 verifyEmail 成功",
             {"flag": 8, "options": [8]},
             {"code": 0, "location": "https://account.xiaomi.com/identity/result/check?sid=xiaomiio&context=CTX"},
             expect_verify="verifyEmail", expect_send=True),
    run_case("码被拒（code:2）→ 明确报错而不是「密码错误」",
             {"flag": 4, "options": [4]},
             {"code": 2, "flag": 4, "options": [4], "version": "v2"},
             expect_exit=True),
    run_tty_case("人手输模式（stdin 是终端）→ 直接敲码，不碰文件"),
]

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
