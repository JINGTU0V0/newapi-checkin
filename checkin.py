#!/usr/bin/env python3
"""
newapi-checkin — 多站点 New API / One API 公益中转站每日自动签到

配置驱动，无第三方浏览器依赖：
  sites.yaml  声明站点与反自动化模式（sign/proof/ocr/claim/routerteam）
  creds.json  账号凭证，密码字段支持 ${ENV_VAR} 占位

内核单文件，除 requests 外仅标准库 + 可选 PyYAML/ddddocr。
"""

import argparse
import base64
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# PyInstaller exe：配置与状态放 exe 同目录（__file__ 在临时解包目录里，不能用）
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent

# Windows 控制台切 UTF-8 代码页，避免中文/emoji 在 GBK 终端乱码
if os.name == "nt":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# 每站完成时的回调列表（--ui 用它把日志推给浏览器）
LOG_HOOK: list = []

SITES_FILE = Path(os.environ.get("CHECKIN_SITES", APP_DIR / "sites.yaml"))
CREDS_FILE = Path(os.environ.get("CHECKIN_CREDS", APP_DIR / "creds.json"))
STATE_FILE = Path(os.environ.get("CHECKIN_STATE", APP_DIR / ".checkin_state.json"))
LEDGER_FILE = Path(os.environ.get("CHECKIN_LEDGER", APP_DIR / ".checkin_ledger.json"))

# ChatFire 式前端 bundle 里硬编码的签到签名密钥，可被站点条目 sign_key 覆盖
DEFAULT_SIGN_KEY = b"your-secret-key-here"

# --------------------------------------------------------------------------- #
# 配置加载
# --------------------------------------------------------------------------- #

def _expand(value):
    """递归替换字符串里的 ${ENV_VAR} 占位。环境变量缺失时**保留占位原文**
    （load_config 不炸），由真正用到它的地方（登录、通知）再校验报错。"""
    if isinstance(value, str):
        return re.sub(r"\$\{(\w+)\}",
                      lambda m: os.environ.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _require_env(resolved: str, original_tpl: str = "") -> str:
    """取一个应当已被展开的值；仍是 ${VAR} 形态就说明环境变量没配。"""
    m = re.fullmatch(r"\$\{(\w+)\}", resolved or "")
    if m:
        raise SystemExit(f"❌ 环境变量未设置: {m.group(1)}（配置里写了 {m.group(0)}）")
    return resolved


def _parse_sites(path: Path) -> dict:
    """解析 sites.yaml。优先 PyYAML；没装则用内置的受限解析器
    （支持本仓库格式：两层嵌套 + 行内 {a: b, c: d} + 注释 + 引号标量）。"""
    text = path.read_text(encoding="utf-8")
    try:
        import yaml
        return yaml.safe_load(text) or {}
    except ModuleNotFoundError:
        pass

    root: dict = {}
    # 栈条目: (indent, ctx, parent, key, want_list)
    stack: list = [(0, root, None, None, False, "key")]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if line.startswith("- "):
            # 列表项：弹掉上一个条目自身的子上下文（缩进 >= 本项）
            while len(stack) > 1 and (indent < stack[-1][0]
                                      or (indent == stack[-1][0] and stack[-1][5] == "item")):
                stack.pop()
        else:
            while len(stack) > 1 and indent < stack[-1][0]:
                stack.pop()
        ctx = stack[-1][1]
        if line.startswith("- "):
            item = line[2:].strip()
            if isinstance(ctx, dict):  # 首个列表项：把该 key 的容器换成 list
                parent, key = stack[-1][2], stack[-1][3]
                ctx = parent[key] = []
                stack[-1] = (stack[-1][0], ctx, parent, key, True, "key")
            if item.startswith("{") and item.endswith("}"):
                ctx.append(_inline_map(item))
            else:
                child: dict = {}
                ctx.append(child)
                m = re.match(r"(\w+):\s*(.*)", item)
                if m and m.group(2):
                    child[m.group(1)] = _scalar(m.group(2))
                # item child：dash 缩进 + 2 以内的 key 都归属它
                stack.append((indent + 2, child, None, None, False, "item"))
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if not val:
            child = {}
            ctx[key] = child
            stack.append((indent + 2, child, ctx, key, True, "key"))
        elif val.startswith("["):
            ctx[key] = _inline_list(val)
        elif val.startswith("{"):
            ctx[key] = _inline_map(val)
        else:
            ctx[key] = _scalar(val)
    # 清理从未被列表项填充的空 dict 占位（如 sites: 下全是 {- ...} 行内项）
    return root


def _inline_map(s: str) -> dict:
    body = s.strip()[1:-1]
    out = {}
    for part in re.split(r",(?![^\[]*\])", body):
        k, _, v = part.partition(":")
        if k.strip():
            out[k.strip()] = _scalar(v.strip())
    return out


def _inline_list(s: str) -> list:
    body = s.strip()[1:-1]
    return [_scalar(x.strip()) for x in body.split(",") if x.strip()]


def _scalar(s: str):
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    low = s.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return s


# 受限解析器的 `- name:` 列表项落在 dict 里，需要把带 "__idx__" 痕迹的结构还原。
# 更简单可靠：解析时维护 ctx——见 _parse_sites 里 child dict 直接 append 分支。

class _PathText:
    """把字符串伪装成可读文件，复用 _parse_sites。"""
    def __init__(self, text: str):
        self._t = text

    def read_text(self, encoding="utf-8"):
        return self._t


def builtin_template_text() -> str:
    """内置签到模板原文（exe 解包目录或源码目录的 sites.example.yaml）。找不到返回 ''。"""
    for cand in (Path(getattr(sys, "_MEIPASS", "")) / "sites.example.yaml",
                 APP_DIR / "sites.example.yaml",
                 APP_DIR / "sites.yaml"):
        try:
            if cand.exists():
                return cand.read_text(encoding="utf-8")
        except OSError:
            continue
    return ""


def load_config() -> dict:
    cfg = {}
    if SITES_FILE.exists():
        cfg = _expand(_parse_sites(SITES_FILE))
    else:  # 没有本地配置：直接用内置模板（只读，不落盘）
        tpl = builtin_template_text()
        if tpl:
            try:
                cfg = _expand(_parse_sites(_PathText(tpl)))
            except Exception:
                cfg = {}
    creds = {}
    if CREDS_FILE.exists():
        creds = _expand(json.loads(CREDS_FILE.read_text(encoding="utf-8")))
    cfg["credentials"] = {**(cfg.get("credentials") or {}), **(creds.get("credentials") or creds)}
    cfg.setdefault("settings", {})
    if not isinstance(cfg.get("sites"), list):  # 空 'sites:' 段解析成 None，归一化成 []
        cfg["sites"] = []
    return cfg


def resolve_site(site: dict, cfg: dict) -> dict:
    """把 credentials 表按站名（或 credential 字段指定键）合并进站点条目。"""
    cred_key = site.get("credential") or site["name"]
    creds = cfg.get("credentials") or {}
    entry = creds.get(cred_key)
    if isinstance(entry, dict):
        merged = {**site, **entry}
    elif entry is not None:  # 允许 "站点: [user, pass]" 简写
        merged = {**site, "username": entry[0], "password": entry[1]}
    else:
        merged = dict(site)
    merged.setdefault("username", cfg.get("defaults", {}).get("username", ""))
    # mode 展开成内核开关：mode: claim/sign/proof/ocr 等价于写 claim: true（显式布尔优先）
    mode = merged.get("mode")
    if mode in ("claim", "sign", "proof", "ocr") and not isinstance(merged.get(mode), bool):
        merged[mode] = True
    return merged


# --------------------------------------------------------------------------- #
# 输出 / 状态
# --------------------------------------------------------------------------- #

def banner(site: dict, suffix: str = "") -> None:
    print("=" * 40)
    print(f"[{site['name']}] {site.get('base_url', '')}{suffix}")
    print("=" * 40)


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_json(path: Path, data: dict) -> None:
    # 原子写：先写临时文件再 rename，避免进程中途被杀留下半截 JSON
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_state() -> tuple[dict, dict]:
    """返回 (当天 {站名: 状态}, 通知标记)；跨天自动作废。"""
    st = _load_json(STATE_FILE)
    if st.get("date") != datetime.now().strftime("%Y-%m-%d"):
        return {}, {}
    marks = {k: v for k, v in st.items() if k not in ("date", "sites")}
    return st.get("sites", {}), marks


def save_state(sites: dict, marks: dict) -> None:
    _save_json(STATE_FILE, {"date": datetime.now().strftime("%Y-%m-%d"),
                            "sites": sites, **marks})


def _fmt_quota(v, unit_rate=None) -> str:
    if unit_rate:
        return f"{v / unit_rate:.2f}$"
    return f"{v} 额度"


def update_ledger(name: str, status: str, award: dict, unit_rate=None) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    led = _load_json(LEDGER_FILE)
    entry = led.setdefault(today, {}).setdefault(name, {})
    entry["status"] = status
    if award.get("award"):
        entry["quota_awarded"] = award["award"]
    if award.get("balance"):
        entry["balance_quota"] = award["balance"]
    for old in [k for k in led if k < today]:
        del led[old]
    _save_json(LEDGER_FILE, led)


def send_notify(cfg: dict, text: str) -> bool:
    """按 settings.notify 推送。支持 telegram / webhook(通用 POST JSON) / shell(自定义命令，
    消息经 stdin 传入)。全部未配置返回 False。"""
    nc = cfg.get("settings", {}).get("notify") or {}
    sent = False
    tg = nc.get("telegram") or {}
    if tg.get("token") and tg.get("chat_id"):
        if re.search(r"\$\{\w+\}", str(tg["token"]) + str(tg["chat_id"])):
            print("⚠️ Telegram 配置里 ${ENV} 占位未展开（环境变量没配），跳过该通知渠道")
        else:
            try:
                proxies = {"http": tg.get("proxy"), "https": tg.get("proxy")} if tg.get("proxy") else None
                r = requests.post(f"https://api.telegram.org/bot{tg['token']}/sendMessage",
                                  json={"chat_id": tg["chat_id"], "text": text},
                                  proxies=proxies, timeout=10)
                sent |= bool(r.json().get("ok"))
                if not r.json().get("ok"):
                    print(f"⚠️ Telegram 被拒: {r.text[:120]}")
            except Exception as e:
                print(f"⚠️ Telegram 失败: {e}")
    wh = nc.get("webhook")
    if wh:
        try:
            requests.post(wh, json={"text": text, "content": text}, timeout=10).raise_for_status()
            sent = True
        except Exception as e:
            print(f"⚠️ Webhook 失败: {e}")
    cmd = nc.get("shell")
    if cmd:
        try:
            subprocess.run(cmd, shell=True, input=text.encode("utf-8"), timeout=30)
            sent = True
        except Exception as e:
            print(f"⚠️ 通知命令失败: {e}")
    return sent


def build_notify_message(results: dict, awards: dict, tally, fails, marks: dict, unit_rate=None) -> str:
    """失败当天每站只报一次；当天全部完成后日报只推一次。"""
    today = datetime.now().strftime("%Y-%m-%d")
    new_fails = [n for n in fails if marks.get(f"fail_notified:{n}") != today]
    all_done = not fails
    summary_sent = marks.get("summary_date") == today
    if not new_fails and (not all_done or summary_sent):
        return ""

    lines = []
    if new_fails:
        for n in new_fails:
            marks[f"fail_notified:{n}"] = today
        lines.append(f"⚠️ 签到失败 {len(new_fails)} 站: {', '.join(new_fails)}\n（当天各站只报一次；后续自动重试）")
    if all_done and not summary_sent:
        marks["summary_date"] = today
        day = {n: a.get("award") for n, a in awards.items()
               if isinstance(a.get("award"), (int, float)) and a["award"]}
        total = sum(day.values())
        head = f"✅ 签到完成 {tally[0] + tally[1]} 站 · {today} {datetime.now():%H:%M}"
        if total:
            head += f" · 今日合计 {_fmt_quota(total, unit_rate)}"
        lines.append(head)
        for n, v in sorted(day.items(), key=lambda x: -x[1]):
            lines.append(f"· {n} +{_fmt_quota(v, unit_rate)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# HTTP 核心（One API / New API）
# --------------------------------------------------------------------------- #

def new_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = UA
    return s


def logout(session: requests.Session, base_url: str) -> None:
    try:
        if session.post(f"{base_url}/api/user/auth/logout", timeout=5).ok:
            return
    except Exception:
        pass
    try:
        session.get(f"{base_url}/api/user/logout", timeout=5)
    except Exception:
        pass


def login(session: requests.Session, site: dict, verify_ssl=True):
    try:
        session.post(f"{site['base_url']}/api/user/auth/logout", timeout=5)
    except Exception:
        pass
    resp = session.post(
        f"{site['base_url']}/api/user/login",
        json={"username": site["username"], "password": site["password"]},
        timeout=15, verify=verify_ssl)
    try:
        data = resp.json()
    except Exception:
        return False, f"登录响应非 JSON (status={resp.status_code})"
    if not data.get("success"):
        return False, data.get("message", "未知错误")
    inner = data.get("data", {})
    return True, str(inner["id"]) if "id" in inner else inner.get("access_token")


def login_with_ssl_fallback(session, site):
    try:
        return login(session, site)
    except Exception as e:
        if "SSL" not in type(e).__name__ and "SSL" not in str(e):
            raise
        print("  ⚠️ SSL 错误，降级重试...")
        return login(session, site, verify_ssl=False)


def precheck(session, base_url, headers, award) -> str | None:
    """GET /api/user/checkin 预检今日状态。已签 -> 'done'；未签 -> None 并顺带记余额。"""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        d = session.get(f"{base_url}/api/user/checkin", headers=headers, timeout=10).json()
    except Exception:
        return None
    data = d.get("data") if isinstance(d, dict) else None
    if not (isinstance(d, dict) and d.get("success") and isinstance(data, dict)):
        return None
    stats = data.get("stats") or {}
    checked = stats.get("checked_in_today")
    if checked is None:
        recs = stats.get("records") or []
        checked = bool(recs and recs[0].get("checkin_date") == today)
    if checked:
        print(f"  ⏭️ 预检：今日已签到（连签 {stats.get('checkin_count', '?')} 天）")
        award["award"] = next((r.get("quota_awarded") for r in (stats.get("records") or [])
                               if r.get("checkin_date") == today), None)
        return "done"
    print("  🔍 预检：今日未签，执行签到")
    try:
        u = session.get(f"{base_url}/api/user/self", headers=headers, timeout=10).json()
        q = (u.get("data") or {}).get("quota")
        if isinstance(q, (int, float)):
            award["balance"] = q
    except Exception:
        pass
    return None


def solve_verification_proof(session, base_url, purpose="checkin") -> str:
    """Altcha 式 PoW：暴力找 sha256(salt+n)==challenge 的 n，换一次性 proof。"""
    d = session.post(f"{base_url}/api/verification/challenges",
                     json={"purpose": purpose}, timeout=15).json()["data"]
    c = d["challenge"]
    n = next((i for i in range(c["maxnumber"] + 1)
              if hashlib.sha256(f"{c['salt']}{i}".encode()).hexdigest() == c["challenge"]), None)
    if n is None:
        return ""
    payload = base64.b64encode(json.dumps({
        "algorithm": c["algorithm"], "challenge": c["challenge"], "number": n,
        "salt": c["salt"], "signature": c["signature"]}).encode()).decode()
    v = session.post(f"{base_url}/api/verification/challenges/verify",
                     json={"purpose": purpose, "challenge_id": d["challenge_id"],
                           "payload": payload}, timeout=15).json()
    return v.get("data", {}).get("proof", "") if v.get("success") else ""


def checkin_with_ocr(session, base_url, headers, attempts=25) -> dict:
    """图形验证码站：ddddocr 识别后提交；识别失败即换新图重试。"""
    import ddddocr
    from PIL import Image
    ocr = ddddocr.DdddOcr(show_ad=False)
    last = {"success": False, "message": "验证码识别失败"}
    for _ in range(attempts):
        d = session.post(f"{base_url}/api/checkin_captcha/", headers=headers, timeout=15).json().get("data")
        if not isinstance(d, dict):
            continue
        im = Image.open(io.BytesIO(base64.b64decode(d["picPath"].split(",", 1)[1])))
        flat = Image.new("RGB", im.size, "white")
        flat.paste(im, mask=im.split()[3] if im.mode == "RGBA" else None)
        buf = io.BytesIO()
        flat.save(buf, "PNG")
        last = session.post(f"{base_url}/api/user/checkin", headers=headers, timeout=15,
                            params={"captchaId": d["captchaId"],
                                    "captcha": ocr.classification(buf.getvalue())}).json()
        if "captcha" not in str(last.get("message", "")).lower():
            return last
    return last


def checkin(session, site, auth_info, headers) -> dict:
    base = site["base_url"]
    if site.get("claim"):
        return session.post(f"{base}/api/portal/daily-public-quota/claim",
                            headers=headers, timeout=15).json()
    if site.get("ocr"):
        return checkin_with_ocr(session, base, headers)
    if site.get("proof"):
        proof = solve_verification_proof(session, base)
        if not proof:
            return {"success": False, "message": "PoW 验证失败"}
        headers = {**headers, "X-Verification-Proof": proof}
    params = {}
    if site.get("sign"):
        ts = int(time.time())
        key = str(site.get("sign_key", DEFAULT_SIGN_KEY.decode())).encode()
        params = {"timestamp": ts,
                  "signature": hmac.new(key, f"{ts}:{auth_info}".encode(), hashlib.sha256).hexdigest(),
                  "timezone": site.get("timezone", "Asia/Shanghai")}
    if site.get("code") and site.get("checkin_code"):
        return session.post(f"{base}/api/user/checkin", headers=headers, params=params,
                            json={"code": site["checkin_code"]}, timeout=15).json()
    return session.post(f"{base}/api/user/checkin", headers=headers,
                        params=params, timeout=15).json()


def detect_mode(base_url: str, username: str, password: str) -> tuple[str, list]:
    """凭据实测探测站点签到类型，返回 (mode, 依据步骤)。
    探测顺序：New/One API 账密登录 -> 直接 POST 签到（成功即 oneapi；失败按报错
    关键词映射 claim/sign/proof/ocr）-> RouterTeam JWT 登录。全程幂等：若 POST
    顺手把今日签到完成了，也如实返回 oneapi。"""
    steps = []
    site = {"name": "?", "base_url": base_url.rstrip("/"), "username": username,
            "password": password}
    s = new_session()
    try:
        ok, auth = login_with_ssl_fallback(s, site)
    except Exception as e:
        ok, auth = False, f"连接失败 {type(e).__name__}"
    if ok:
        headers = {}
        if str(auth).isdigit():
            headers["New-Api-User"] = str(auth)
        else:
            s.headers["Authorization"] = f"Bearer {auth}"
        steps.append("✔ /api/user/login 账密登录成功（New/One API 系）")
        msg = ""
        try:
            d = s.post(f"{site['base_url']}/api/user/checkin", headers=headers,
                       timeout=15).json()
            if isinstance(d, dict) and d.get("success"):
                steps.append("✔ POST /api/user/checkin 直接成功 → 标准签到")
                logout(s, site["base_url"])
                return "oneapi", steps
            msg = str(d.get("message", "")) if isinstance(d, dict) else str(d)
            steps.append(f"· POST 签到未直接通过: {msg[:80]}")
            if "已" in msg or "already" in msg.lower():  # 今日已签：接口本身是标准式
                steps.append("✔ 提示今日已签到 → 标准签到（oneapi）")
                logout(s, site["base_url"])
                return "oneapi", steps
        except Exception as e:
            steps.append(f"· POST 签到异常: {type(e).__name__}")
        low = msg.lower()
        if any(k in msg for k in ("领取", "额度", "公益")) or "claim" in low:
            steps.append("✔ 报错指向额度领取接口 → claim")
            mode = "claim"
        elif any(k in msg for k in ("签名", "timestamp")) or "signature" in low:
            steps.append("✔ 报错要求 timestamp+签名 → sign")
            mode = "sign"
        elif "验证码" in msg or "captcha" in low:
            steps.append("✔ 报错要求图形验证码 → ocr")
            mode = "ocr"
        elif any(k in msg for k in ("验证", "proof", "human")) or "verification" in low:
            steps.append("✔ 报错要求人机验证(PoW) → proof")
            mode = "proof"
        else:
            try:  # 最后试一次 claim 端点，通就算 claim
                c = s.post(f"{site['base_url']}/api/portal/daily-public-quota/claim",
                           headers=headers, timeout=15).json()
                if isinstance(c, dict) and c.get("success"):
                    steps.append("✔ 额度领取端点可用 → claim")
                    mode = "claim"
                else:
                    steps.append("· 无法确定，按标准签到处理（可手动改模式）")
                    mode = "oneapi"
            except Exception:
                steps.append("· 无法确定，按标准签到处理（可手动改模式）")
                mode = "oneapi"
        logout(s, site["base_url"])
        return mode, steps
    steps.append(f"· New/One API 登录失败: {str(auth)[:80]}")
    # RouterTeam 式 JWT 站
    try:
        r = s.post(f"{site['base_url']}/api/auth/login",
                   json={"username": username, "password": password}, timeout=15)
        if r.ok and r.json().get("accessToken"):
            steps.append("✔ /api/auth/login 返回 accessToken → routerteam")
            return "routerteam", steps
        steps.append(f"· JWT 登录也不通（status={r.status_code}）")
    except Exception as e:
        steps.append(f"· JWT 登录异常: {type(e).__name__}")
    steps.append("· 探测未通过：若账号密码还没填对，保存后签到时会再报错；默认按标准签到")
    return "oneapi", steps


def report(result: dict, award: dict | None = None) -> str:
    if "error" in result:
        print(f"  ❌ 调用失败: {result['error']}")
        return "fail"
    if "raw" in result:
        print(f"  ⚠️ 返回异常: {str(result['raw'])[:200]}")
        return "fail"
    if result.get("success"):
        data = result.get("data")
        if award is not None:
            srcs = [result, data if isinstance(data, dict) else {}]
            for src in srcs:
                for key in ("quota_awarded", "quota", "daily_compute_points"):
                    if isinstance(src.get(key), (int, float)) and src[key]:
                        award["award"] = src[key]
                        break
                else:
                    continue
                break
        if "quota" in result:
            print(f"  ✅ {result.get('message', '签到成功')}! +{result['quota']} 额度")
        elif not isinstance(data, dict):
            print(f"  ✅ {data}")
        elif "daily_compute_points" in data:
            done = "今日已领取 " if data.get("claimed") else "领取成功! +"
            print(f"  ✅ {done}{data.get('daily_compute_points', 0)} 算力点")
            return "done" if data.get("claimed") else "ok"
        elif "quota_awarded" in data:
            print(f"  ✅ 签到成功! +{data['quota_awarded']} 额度")
        else:
            print(f"  ✅ 成功! {data}")
        return "ok"
    msg = result.get("message", str(result))
    done = "已" in msg or "already" in msg.lower()
    print(f"  {'⏭️ ' if done else '⚠️ '} {msg}")
    return "done" if done else "fail"


# --------------------------------------------------------------------------- #
# 站点处理
# --------------------------------------------------------------------------- #

def handle_routerteam(site: dict, award: dict) -> str:
    base = site["base_url"]
    s = new_session()
    r = s.post(f"{base}/api/auth/login",
               json={"username": site["username"], "password": site["password"]}, timeout=15)
    token = r.json().get("accessToken") if r.ok else None
    if not token:
        print(f"  登录失败: {r.status_code} {r.text[:150]}")
        return "fail"
    s.headers["Authorization"] = f"Bearer {token}"
    print("  登录成功，正在签到...")
    statuses = []
    for label, ep in (("签到", "/api/user/reward-center/sign-in"),
                      ("抽奖报名", "/api/user/daily-lottery/current/join")):
        try:
            resp = s.post(f"{base}{ep}", timeout=15)
            d = resp.json()
        except Exception as e:
            print(f"  ❌ {label}失败: {type(e).__name__}: {e}")
            statuses.append("fail")
            continue
        if resp.ok:
            amount = d.get("rewardAmount")
            if label == "签到" and amount:
                award["award"] = amount
            print(f"  ✅ {label}: {d.get('message', 'ok')}" + (f" +${amount:.2f}" if amount else ""))
            statuses.append("ok")
        elif "already" in str(d.get("code", "")):
            print(f"  ⏭️  {label}: {d.get('message')}")
            statuses.append("done")
        else:
            print(f"  ⚠️  {label}: {d.get('message', resp.text[:150])}")
            statuses.append("fail")
    return "fail" if "fail" in statuses else ("done" if all(x == "done" for x in statuses) else "ok")


def handle_http(site: dict, award: dict) -> str:
    session = new_session()
    try:
        ok, auth_info = login_with_ssl_fallback(session, site)
    except Exception as e:
        print(f"  ❌ 连接失败: {type(e).__name__}: {e}")
        return "fail"
    if not ok:
        print(f"  登录失败: {auth_info}")
        return "fail"
    print("  登录成功，正在签到...")
    headers = {}
    if auth_info and auth_info.isdigit():
        headers["New-Api-User"] = auth_info
    else:
        session.headers["Authorization"] = f"Bearer {auth_info}"
    status = None
    try:
        if not site.get("claim") and not site.get("ocr"):
            status = precheck(session, site["base_url"], headers, award)
        if status is None:
            status = report(checkin(session, site, auth_info, headers), award)
    except Exception as e:
        print(f"  ❌ 签到请求失败: {type(e).__name__}: {e}")
        status = "fail"
    finally:
        logout(session, site["base_url"])
    return status or "fail"


class ThreadOut(io.TextIOBase):
    """按线程分流 print，并发下日志不交错。"""

    def __init__(self, real):
        self._real = real
        self._local = threading.local()

    def capture(self):
        self._local.buf = io.StringIO()

    def release(self) -> str:
        buf = getattr(self._local, "buf", None)
        self._local.buf = None
        return buf.getvalue() if buf else ""

    def write(self, s):
        return (getattr(self._local, "buf", None) or self._real).write(s)

    def flush(self):
        self._real.flush()


def run_site(site: dict) -> tuple[str, str, dict]:
    award: dict = {}
    sys.stdout.capture()
    try:
        banner(site)
        try:
            mode = site.get("mode")
            if mode == "routerteam":
                status = handle_routerteam(site, award)
            else:
                status = handle_http(site, award)
        except Exception as e:
            print(f"  ❌ 异常: {type(e).__name__}: {e}")
            status = "fail"
        print()
    finally:
        out = sys.stdout.release()
    if LOG_HOOK:
        for fn in list(LOG_HOOK):
            try:
                fn(name=site["name"], status=status, log=out,
                   award=award.get("award") or 0)
            except Exception:
                pass
    return status, out, award


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# 交互菜单 / 常驻定时（exe 双击、桌面用户友好）
# --------------------------------------------------------------------------- #

def _next_fire(times: list) -> datetime:
    """给定 ['06:00','10:30']，返回今天/明天里下一个触发点。"""
    now = datetime.now()
    cands = []
    for t in times:
        h, m = map(int, t.split(":"))
        for day in (0, 1):
            d = (now + timedelta(days=day)).replace(hour=h, minute=m,
                                                     second=0, microsecond=0)
            if d > now:
                cands.append(d)
    return min(cands)


def daemon_loop(times: list) -> None:
    print(f"⏰ 常驻模式：每天 {' / '.join(times)} 自动签到（Ctrl+C 退出）")
    while True:
        nxt = _next_fire(times)
        print(f"   下次运行 {nxt:%Y-%m-%d %H:%M}")
        while True:
            secs = (nxt - datetime.now()).total_seconds()
            if secs <= 0:
                break
            time.sleep(min(secs, 30))
        for argv in (["--today", "--notify"], ["--today", "--notify"]):
            # 第一轮全量补跑 + 15 分钟后第二轮捡漏（对应服务器双 timer 思路）
            sys.argv = ["checkin"] + argv
            try:
                run()
            except Exception as e:
                print(f"[daemon] 本轮异常: {e}")
            if argv == ["--today", "--notify"]:
                end = time.time() + 15 * 60
                while time.time() < end:
                    time.sleep(min(30, end - time.time()))


def _ensure_writable_config() -> bool:
    """需要编辑/落盘配置时：本地没有 sites.yaml 就先把内置模板写出来。"""
    if SITES_FILE.exists():
        return True
    tpl = builtin_template_text()
    if not tpl:
        print(f"❌ 找不到 {SITES_FILE}，也找不到内置模板 sites.example.yaml")
        return False
    SITES_FILE.write_text(tpl, encoding="utf-8")
    print(f"📝 已把内置模板写到 {SITES_FILE}，改站点/账号请编辑它（或用 Web 面板表单）。")
    if not CREDS_FILE.exists():
        CREDS_FILE.write_text(json.dumps({"credentials": {
            c["name"]: {"username": "", "password": ""}
            for c in _site_pairs(_PathText(tpl)) if c}},
            ensure_ascii=False, indent=1), encoding="utf-8")
    return True


def _site_pairs(path: Path) -> list:
    try:
        cfg = _parse_sites(path)
        return [{"name": s["name"], "password": ""} for s in cfg.get("sites", [])]
    except Exception:
        return []


def menu() -> None:  # 无参数双击运行时的交互界面
    while True:
        print("\n========== newapi-checkin ==========")
        print("  1. 立即签到（全量）")
        print("  2. 补跑今日失败站点")
        print("  3. 查看站点列表")
        print("  4. 编辑 sites.yaml / creds.json")
        print("  5. 常驻定时（每天 06:00 / 10:30）")
        print("  6. 打开 Web 面板（本地图形界面）")
        print("  0. 退出")
        try:
            c = input("请选择: ").strip()
        except EOFError:
            return
        try:
            if c == "1":
                sys.argv = ["checkin", "--notify"]
                run()
            elif c == "2":
                sys.argv = ["checkin", "--today", "--notify"]
                run()
            elif c == "3":
                sys.argv = ["checkin", "--list"]
                run()
            elif c == "4":
                if _ensure_writable_config():
                    for f in (SITES_FILE, CREDS_FILE):
                        if f.exists():
                            os.startfile(str(f))  # noqa: S606  Windows 专用，exe 场景
            elif c == "5":
                daemon_loop(["06:00", "10:30"])
            elif c == "6":
                import ui
                ui.serve("127.0.0.1", 8686)  # 自动开浏览器，Ctrl+C 停
            elif c == "0":
                return
        except SystemExit:  # argparse 不会在菜单里退出
            pass
        except Exception as e:
            print(f"❌ {e}")


def run_batch(targets: list, cfg: dict, state: dict, marks: dict,
              notify: bool, jobs: int = 8) -> dict:
    """并发跑 targets，写账本/状态，返回 {results, awards, tally, fails}。"""
    unit_rate = cfg.get("settings", {}).get("quota_per_dollar")
    results, awards = dict(state), {}
    t0 = time.time()
    print(f"▶ 待跑 {len(targets)} 站\n")
    real_stdout, sys.stdout = sys.stdout, ThreadOut(sys.stdout)
    try:
        with ThreadPoolExecutor(max_workers=min(jobs, len(targets))) as ex:
            futs = {ex.submit(run_site, s): s for s in targets}
            for f in as_completed(futs):
                status, out, award = f.result()
                print(out, end="")
                results[futs[f]["name"]] = status
                awards[futs[f]["name"]] = award
    finally:
        sys.stdout = real_stdout

    for name, status in results.items():
        update_ledger(name, status, awards.get(name) or {}, unit_rate)

    tally = [list(results.values()).count(k) for k in ("ok", "done", "fail")]
    fails = [n for n, v in results.items() if v == "fail"]
    print(f"{'=' * 40}\n⏱ {time.time() - t0:.1f}s  ✅{tally[0]} ⏭️{tally[1]} ⚠️{tally[2]}")
    if fails:
        print(f"失败: {', '.join(fails)}\n稍后重试: python checkin.py --today")

    if notify:
        msg = build_notify_message(results, awards, tally, fails, marks, unit_rate)
        if msg:
            print("📣 " + msg)
            send_notify(cfg, msg)
    save_state(results, marks)
    return {"results": results, "awards": awards, "tally": tally, "fails": fails}


def run() -> None:
    ap = argparse.ArgumentParser(description="New API / One API 多站点自动签到")
    ap.add_argument("--today", action="store_true", help="增量：跳过当天已完成站，只跑失败/未跑")
    ap.add_argument("--only", help="只跑名字含关键字的站，逗号分隔")
    ap.add_argument("--list", action="store_true", help="列出配置的站点后退出")
    ap.add_argument("-j", "--jobs", type=int, default=8, help="并发数（默认 8）")
    ap.add_argument("--notify", action="store_true", help="按 settings.notify 推送结果")
    ap.add_argument("--daemon", action="store_true", help="常驻进程，每天定时自动跑")
    ap.add_argument("--daemon-at", default="06:00,10:30", help="常驻触发时间，逗号分隔 HH:MM")
    ap.add_argument("--ui", action="store_true", help="启动本地 Web 面板")
    ap.add_argument("--ui-host", default="127.0.0.1", help="面板监听地址（默认仅本机）")
    ap.add_argument("--ui-port", type=int, default=8686, help="面板端口（默认 8686）")
    ap.add_argument("--ui-token", default="", help="面板访问 token（绑非本机地址时必填）")
    args = ap.parse_args()

    if args.ui:
        import ui
        ui.serve(args.ui_host, args.ui_port, args.ui_token)
        return

    if args.daemon:
        daemon_loop([t.strip() for t in args.daemon_at.split(",") if t.strip()])
        return

    cfg = load_config()
    sites = [resolve_site(s, cfg) for s in cfg["sites"] if s.get("enabled", True)]
    if args.list:
        if not sites:
            print("  （还没有站点：用 Web 面板「＋ 添加网站」，或编辑 sites.yaml）")
        for s in sites:
            pw = s.get("password") or ""
            flag = "✓" if pw and pw != "***" and not re.fullmatch(r"\$\{\w+\}", pw) else "✗无凭证"
            print(f"  [{flag}] {s['name']:<14} {s.get('base_url','')}  mode={s.get('mode','oneapi')}")
        return

    state, marks = load_state() if args.today else ({}, {})
    keys = [k.strip().lower() for k in args.only.split(",")] if args.only else []
    targets = pick_targets(sites, state, keys)
    skipped = len(sites) - len(targets)
    if skipped:
        print(f"⏭️  今日已完成/已过滤 {skipped} 站")
    if not targets:
        print("✅ 无待跑站点")
        return
    run_batch(targets, cfg, state, marks, args.notify, args.jobs)


def pick_targets(sites: list, state: dict, keys: list) -> list:
    """过滤出本轮要跑的站：有凭证、匹配 --only 关键字、今日未完成。"""
    targets = []
    for s in sites:
        pw = s.get("password") or ""
        if not pw or pw == "***" or re.fullmatch(r"\$\{\w+\}", pw):
            continue
        if keys and not any(k in s["name"].lower() for k in keys):
            continue
        if state.get(s["name"]) in ("ok", "done"):
            continue
        targets.append(s)
    return targets


if __name__ == "__main__":
    if len(sys.argv) == 1:
        menu()           # 双击 exe / 无参数运行：交互菜单
    else:
        run()
