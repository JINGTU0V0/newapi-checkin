#!/usr/bin/env python3
"""
公益站发现工具 —— 自动收集可签名的 new-api / one-api 公益站候选

数据源（GitHub 上每日/定期自动更新的站点导航仓库）:
  A. panxunying/ai-coding-welfare  data/sites.json + data/live.json（含签到额度、探活结果）
  B. bubblevv/ai-api-gongyi-nav    data/sites.json（tags 带「签到」的条目）
  C. 1sh1ro/ai-api-zhongzhuan      README.md 表格里的 URL（兜底源）

工作流:
  1. 拉取各源 -> 提取候选站点 URL
  2. 与 checkin.py 的 SITES 及已忽略清单去重
  3. 探活: GET /api/status 判定是否 new-api/one-api 系、是否开启签到
  4. --deep 模式追加：登录/注册/Turnstile 检测，判定能否自动签到
  5. 输出发现报告 sites_found.json + 控制台摘要 + Telegram 通知（有新站才发）

用法:
  python3 checkin_scan.py            # 完整扫描并通知
  python3 checkin_scan.py --no-notify
  python3 checkin_scan.py --json     # 只输出 JSON
  python3 checkin_scan.py --deep     # 深度检测：探活成功后自动检测登录/注册/Turnstile
  python3 checkin_scan.py --probe-auth <url>  # 单站深度检测（不用跑全量扫描）

新站接入: 把名字+URL 加进 checkin_creds.json 的 passwords 和 checkin.py 的 SITES 即可。
检测输出含 auto_possible / blocker / recommended 字段，TG 通知会标注哪些站能自动签。
"""

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
TIMEOUT = 20

# GitHub 直连不稳时走 mihomo 127.0.0.1:7890；SCAN_PROXY=off 禁用
PROXY = os.environ.get("SCAN_PROXY", "")
if not PROXY:
    import urllib.request as _ur
    _sys = _ur.getproxies()
    PROXY = _sys.get("https") or ""
    if not PROXY and os.environ.get("GITHUB_VIA_PROXY", "1") == "1":
        for p in ("127.0.0.1:7890",):
            try:
                requests.get("https://api.github.com/zen", timeout=6, proxies={"https": f"http://{p}"})
                PROXY = f"http://{p}"
                break
            except Exception:
                pass
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None
PROBE_PROXY = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _get(url: str, **kw) -> requests.Response:
    kw.setdefault("headers", {"User-Agent": UA})
    kw.setdefault("timeout", TIMEOUT)
    kw.setdefault("verify", False)
    if PROXIES and "github" in url:
        kw["proxies"] = PROXIES
    return requests.get(url, **kw)


BASE = "https://raw.githubusercontent.com/{repo}/main/{path}"
SOURCES = {
    "panxunying": [
        BASE.format(repo="panxunying/ai-coding-welfare", path="data/sites.json"),
        BASE.format(repo="panxunying/ai-coding-welfare", path="data/live.json"),
    ],
    "bubblevv": [
        BASE.format(repo="bubblevv/ai-api-gongyi-nav", path="data/sites.json"),
    ],
    "1sh1ro": [
        BASE.format(repo="1sh1ro/ai-api-zhongzhuan", path="README.md"),
    ],
}


def fetch(url: str) -> str | None:
    try:
        r = _get(url)
        if r.status_code == 200:
            return r.text
    except Exception as e:
        print(f"  ⚠️ fetch {url} ERR: {str(e)[:80]}")
    return None


# --------------------------------------------------------------------------- #
# 解析各数据源
# --------------------------------------------------------------------------- #

def _host(url: str) -> str:
    return urlsplit(url).hostname or url


def norm_url(u: str) -> str:
    if not u:
        return ""
    u = u.strip().strip("/")
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u


def parse_panxunying() -> list[dict]:
    out: list[dict] = []
    meta: dict[str, dict] = {}
    txt = fetch(SOURCES["panxunying"][0])
    if txt:
        try:
            data = json.loads(txt)
            for s in data.get("sites", []):
                url = norm_url(s.get("homeUrl") or s.get("signupUrl") or "")
                if not url:
                    continue
                info = meta.setdefault(_host(url), {
                    "name": s.get("name", ""),
                    "sources": set(),
                    "checkin_credit": None,
                })
                info["sources"].add("panxunying")
                c = (s.get("credits") or {}).get("dailyCheckin")
                if c:
                    info["checkin_credit"] = c
                info["tags"] = s.get("tags") or []
        except Exception as e:
            print(f"  ⚠️ panxunying sites.json 解析失败: {e}")
    txt = fetch(SOURCES["panxunying"][1])
    if txt:
        try:
            live = json.loads(txt)
            online = {_host(x.get("url", "")) for x in _iter_live(live) if x.get("online")}
            for h, info in meta.items():
                if h in online:
                    info["source_online"] = True
        except Exception:
            pass
    for h, info in meta.items():
        out.append({
            "url": "https://" + h,
            "name": info.get("name", ""),
            "sources": sorted(info["sources"]),
            "tags": info.get("tags", []),
            "checkin_credit": info.get("checkin_credit"),
            "listed_online": info.get("source_online"),
        })
    return out


def _iter_live(live: dict):
    for x in live.get("sites", []):
        if isinstance(x, dict):
            yield x


def parse_bubblevv() -> list[dict]:
    out: list[dict] = []
    txt = fetch(SOURCES["bubblevv"][0])
    if not txt:
        return out
    try:
        data = json.loads(txt)
        for s in data.get("sites", []):
            tags = s.get("tags") or []
            url = norm_url(s.get("url", ""))
            if not url:
                continue
            if "签到" not in tags and "签到" not in (s.get("note") or ""):
                continue
            out.append({
                "url": url,
                "name": s.get("name", ""),
                "sources": ["bubblevv"],
                "tags": tags,
                "note": (s.get("note") or "")[:120],
            })
    except Exception as e:
        print(f"  ⚠️ bubblevv sites.json 解析失败: {e}")
    return out


def parse_1sh1ro() -> list[dict]:
    out: list[dict] = []
    txt = fetch(SOURCES["1sh1ro"][0])
    if not txt:
        return out
    seen_hosts: set[str] = set()
    for m in re.finditer(r"https?://[^\s|)\[\]\"']+", txt):
        url = norm_url(m.group(0))
        if not url:
            continue
        h = _host(url)
        if h in seen_hosts or "github.com" in h or "raw.githubusercontent" in h:
            continue
        seen_hosts.add(h)
        out.append({"url": url, "name": "", "sources": ["1sh1ro"], "tags": []})
    return out


# --------------------------------------------------------------------------- #
# 探活
# --------------------------------------------------------------------------- #

def _probe_once(url: str, force_proxy: bool = False) -> dict:
    entry: dict = {}
    for path in ("/api/status",):
        try:
            kw = {"proxies": PROBE_PROXY} if force_proxy else {}
            r = _get(url + path, **kw)
        except Exception as e:
            entry["error"] = str(e)[:100]
            continue
        entry["http_status"] = r.status_code
        data: dict = {}
        try:
            j = r.json()
            data = j.get("data", j) or {}
        except Exception:
            data = {}
        if isinstance(data, dict) and data:
            entry["system_name"] = data.get("system_name", "")
            entry["server_board_url"] = data.get("server_board_url", "")
            for k in ("checkin_enabled", "CheckinEnabled"):
                if k in data:
                    entry["checkin_enabled"] = bool(data[k])
            if "self_use_mode_enabled" in data:
                entry["self_use_mode"] = bool(data["self_use_mode_enabled"])
            methods: list[str] = []
            if data.get("github_client_id"):
                methods.append("GitHub")
            if data.get("linuxdo_client_id"):
                methods.append("LinuxDO")
            if data.get("discord_client_id"):
                methods.append("Discord")
            entry["login_methods"] = methods
            entry["email_verification"] = bool(data.get("email_verification"))
            entry["api_ok"] = True
            entry["probe_path"] = path
            return entry
        entry["error"] = f"HTTP {r.status_code}, 无 /api/status JSON（非 new-api/one-api?）"
    return entry


def probe(entry: dict) -> dict:
    url = entry["url"].rstrip("/")
    entry["host"] = _host(url)
    result = _probe_once(url)
    if not result.get("api_ok"):
        retry = _probe_once(url, force_proxy=True)
        if retry.get("api_ok"):
            retry["via_proxy"] = True
            result = retry
    entry.update({k: v for k, v in result.items() if k not in entry or not entry.get(k)})
    return entry


# --------------------------------------------------------------------------- #
# 深度检测：登录/注册/Turnstile / GitHub OAuth
# --------------------------------------------------------------------------- #

def deep_check(url: str) -> dict:
    """深度检测：登录/注册/Turnstile、GitHub PAT 可能性、模式推断"""
    url = url.rstrip("/")
    res: dict = {
        "url": url,
        "auto_possible": False,
        "blocker": None,
        "recommended": None,
        "evidence": [],
        "checkin_enabled": None,
        "system_name": "",
        "login_methods": [],
    }
    PROBE = PROBE_PROXY

    # 1) /api/status 登录方式
    status = _probe_once(url)
    lm = status.get("login_methods", [])
    ce = status.get("checkin_enabled")
    res["checkin_enabled"] = ce
    res["system_name"] = status.get("system_name", "")
    res["login_methods"] = lm
    if lm:
        res["evidence"].append("status.login_methods=" + ",".join(lm))

    # 2) 尝试密码登录（探测 fake token 是否被 Turnstile/验证码拦）
    for path in ("/api/user/login", "/api/user/login ", "/api/login"):
        payload = {"username": "probe", "password": "ProbePass123!"}
        try:
            r = requests.post(
                url + path,
                json=payload,
                timeout=20,
                verify=False,
                headers={"User-Agent": UA, "Content-Type": "application/json"},
                proxies=PROBE,
            )
            text = r.text[:200]
            if ("turnstile" in text.lower() and "false" not in text.replace(" ", "")) or "验证码" in text or "captcha" in text.lower():
                res["blocker"] = "turnstile"
                res["evidence"].append(f"{path} => captcha required")
                break
            if r.status_code == 200:
                j = r.json()
                msg = str(j.get("message", ""))
                d = j.get("data") or {}
                if isinstance(d, dict) and "turnstile_required" in d:
                    # 新版 new-api 明确告知是否需要 Turnstile
                    if d["turnstile_required"]:
                        res["blocker"] = "turnstile"
                        res["evidence"].append(f"{path} => turnstile_required=true")
                        break
                    res["evidence"].append(f"{path} => turnstile_required=false")
                    res["auto_possible"] = True
                    res["recommended"] = "password"
                    break
                if "密码" in msg or "password" in msg.lower() or "unauthorized" in msg.lower() or "封禁" in msg or "banned" in msg.lower():
                    res["evidence"].append(
                        f"{path} => password login accepted (no captcha, real creds will work)"
                    )
                    res["auto_possible"] = True
                    res["recommended"] = "password"
                    break
        except Exception as e:
            res["evidence"].append(f"{path} ERR: {str(e)[:80]}")
            continue

    # 3) 若 Turnstile 拦住，测注册端点是否同样被拦（确认是否全站封锁）
    if res["blocker"] == "turnstile":
        for path in ("/api/user/register", "/api/register"):
            try:
                r = requests.post(
                    url + path,
                    json={"username": "probe", "password": "ProbePass123!"},
                    timeout=20,
                    verify=False,
                    headers={"User-Agent": UA, "Content-Type": "application/json"},
                    proxies=PROBE,
                )
                text = r.text[:200]
                if "Turnstile" in text or "验证码" in text or "captcha" in text.lower():
                    res["evidence"].append("register also needs turnstile")
                    break
            except Exception:
                pass

    # 4) GitHub OAuth 端点探测（非登录，只是看有没有配置）
    if "GitHub" in lm:
        res["evidence"].append("GitHub OAuth configured in /api/status")
        if not res["auto_possible"]:
            res["recommended"] = res.get("recommended") or "oauth_browser"
            res["auto_possible"] = False

    # 5) 特殊端点
    for path in ("/api/user/info", "/api/checkin/token", "/api/token/"):
        try:
            r = requests.get(url + path, timeout=10, verify=False,
                             headers={"User-Agent": UA}, proxies=PROBE)
            if r.status_code == 200:
                res["evidence"].append(f"{path} => 200")
        except Exception:
            pass

    # 6) 综合判定
    if not res["blocker"]:
        if res.get("recommended") in ("password", None):
            res["auto_possible"] = True
            res["recommended"] = "password"
            res["blocker"] = None
    return res


# --------------------------------------------------------------------------- #
# 通知
# --------------------------------------------------------------------------- #

def notify(text: str) -> bool:
    try:
        creds = json.loads(CREDS_FILE.read_text(encoding="utf-8"))
        tg = creds.get("telegram", {})
        token, chat = tg.get("token"), tg.get("chat_id")
        if not (token and chat):
            return False
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
                timeout=15,
            )
        except Exception:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
                timeout=20,
                proxies=PROBE_PROXY,
            )
        if not r.ok:
            print(f"⚠️ Telegram 返回 {r.status_code}: {r.text[:120]}")
            return False
        return True
    except Exception as e:
        print(f"⚠️ Telegram 通知失败: {str(e)[:150]}")
        return False


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def collect() -> list[dict]:
    cands: dict[str, dict] = {}
    for parser in (parse_panxunying, parse_bubblevv, parse_1sh1ro):
        try:
            items = parser()
        except Exception as e:
            print(f"  ⚠️ {parser.__name__}: {e}")
            continue
        print(f"  {parser.__name__}: {len(items)} 条")
        for it in items:
            h = _host(it["url"])
            if h in cands:
                c = cands[h]
                c["sources"] = sorted(set(c["sources"]) | set(it["sources"]))
                c["name"] = c["name"] or it.get("name", "")
                for k in ("checkin_credit", "note", "tags"):
                    if it.get(k) and not c.get(k):
                        c[k] = it[k]
            else:
                cands[h] = {**it, "url": "https://" + h}
    return list(cands.values())


def _auth_label(r: dict) -> str:
    if r.get("blocker") == "turnstile":
        return "🚫 Turnstile 验证码"
    if r.get("auto_possible"):
        return "✅ 可自动签到（密码）"
    if "GitHub" in r.get("login_methods", []):
        return "🔗 GitHub OAuth（需浏览器）"
    if "LinuxDO" in r.get("login_methods", []):
        return "🔗 LinuxDO OAuth（需浏览器）"
    return "❓ 未知"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    ap.add_argument("--probe-workers", type=int, default=8)
    ap.add_argument("--deep", action="store_true",
                    help="深度检测：对所有探活成功的站检测登录/注册/Turnstile")
    ap.add_argument("--probe-auth", metavar="URL",
                    help="单站深度检测（不跑全量扫描）")
    args = ap.parse_args()

    # 单站深度检测
    if args.probe_auth:
        url = norm_url(args.probe_auth)
        if not url:
            print("无效 URL")
            return 1
        print(f"[deep] 单站检测: {url}")
        r = deep_check(url)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0

    st = load_state()
    seen = st.setdefault("seen", {})
    ignored = {x.lower() for x in st.get("ignored", [])}
    announced = {x.lower() for x in st.get("announced", [])}
    configured = known_urls()

    print(f"[{datetime.now():%F %T}] 开始发现扫描…")
    cands = collect()
    print(f"  合并去重后候选: {len(cands)}")

    todo, results = [], []
    now = time.time()
    for c in cands:
        h = _host(c["url"])
        if h in configured or h in ignored:
            continue
        cached = seen.get(h)
        if cached and cached.get("api_ok") and now - cached.get("probed_at", 0) < 7 * 86400:
            c.update({k: cached[k] for k in (
                "checkin_enabled", "system_name", "api_ok", "login_methods", "email_verification"
            ) if k in cached})
            if cached.get("deep") and now - cached.get("deep_at", 0) < 7 * 86400:
                c["deep"] = cached["deep"]
            results.append(c)
            continue
        todo.append(c)

    if todo:
        print(f"  探活 {len(todo)} 个…")
        with ThreadPoolExecutor(max_workers=args.probe_workers) as ex:
            for r in ex.map(probe, todo):
                results.append(r)

    for c in results:
        h = _host(c["url"])
        cached_seen = seen.get(h, {})
        if c.get("deep"):
            cached_seen = {**cached_seen, "deep": c["deep"], "deep_at": now}
        seen[h] = {
            "name": c.get("name", ""),
            "url": c["url"],
            "probed_at": now,
            "api_ok": c.get("api_ok", False),
            "checkin_enabled": c.get("checkin_enabled"),
            "system_name": c.get("system_name", ""),
            "first_seen": cached_seen.get("first_seen", now),
            **{k: cached_seen[k] for k in ("deep", "deep_at") if k in cached_seen},
        }
    st["last_scan"] = datetime.now().strftime("%F %T")
    save_state(st)

    live = [c for c in results if c.get("api_ok")]
    new_sites = [c for c in live if _host(c["url"]) not in announced]
    signable = [c for c in live if c.get("checkin_enabled") is not False]

    # --deep 追加深度检测
    deep_results: dict[str, dict] = {}
    if args.deep and signable:
        targets = [c for c in signable if not c.get("deep")]
        if targets:
            print(f"  [deep] 深度检测 {len(targets)} 个…")
            with ThreadPoolExecutor(max_workers=min(4, args.probe_workers)) as ex:
                for r in ex.map(lambda c: deep_check(c["url"]), targets):
                    deep_results[_host(r["url"])] = r
            for c in targets:
                c["deep"] = deep_results.get(_host(c["url"]), {})

    report = {
        "scan_at": st["last_scan"],
        "candidates": len(cands),
        "configured_skip": len([c for c in cands if _host(c["url"]) in configured]),
        "probed_ok": len(live),
        "new_since_last": len(new_sites),
        "signable": [
            {
                "name": c.get("name") or c.get("system_name"),
                "url": c["url"],
                "checkin_enabled": c.get("checkin_enabled"),
                "system_name": c.get("system_name", ""),
                "checkin_credit": c.get("checkin_credit"),
                "sources": c.get("sources"),
                "tags": c.get("tags"),
                "deep": c.get("deep"),
                "first_seen": datetime.fromtimestamp(seen[_host(c["url"])]["first_seen"]).strftime("%F"),
            }
            for c in sorted(signable, key=lambda x: (x.get("checkin_enabled") is not True, x.get("name", "")))
        ],
        "dead": [
            {"url": c["url"], "error": c.get("error", str(c.get("http_status", "")))}
            for c in results if not c.get("api_ok")
        ],
    }
    REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"\n可签到候选: {len(report['signable'])}（探活失败/非签到系: {len(report['dead'])}）")
    for s in report["signable"]:
        flag = ("✅签到开" if s["checkin_enabled"]
                else ("❓未明" if s["checkin_enabled"] is None else "🚫签到关"))
        credit = f" 日签到≈{s['checkin_credit']}" if s.get("checkin_credit") else ""
        deep = s.get("deep") or {}
        auth = f" [{_auth_label(deep)}]" if deep else ""
        print(f"  [{flag}] {s['name'] or s['url']}  {s['url']}  源:{','.join(s['sources'] or [])}{credit}{auth}")
        if deep and deep.get("evidence"):
            for ev in deep["evidence"][:3]:
                print(f"      · {ev}")

    if not args.no_notify and new_sites:
        lines = [f"🔍 签到站发现 · 新站 {len(new_sites)} 个"]
        for s in report["signable"][:15]:
            if seen[_host(s["url"])].get("first_seen", 0) >= now - 86400:
                deep = s.get("deep") or {}
                label = _auth_label(deep) if deep else ""
                lines.append(f"• {s['name'] or s['url']} {s['url']} {label}")
        if len(lines) > 1:
            lines.append("详情: ~/sites_found.json（登记账号后再加进 checkin.py）")
            if notify("\n".join(lines)):
                done = st.setdefault("announced", [])
                for c in new_sites:
                    h = _host(c["url"])
                    if h not in done:
                        done.append(h)

    save_state(st)
    print(f"\n报告已写: {REPORT_FILE}")
    return 0


# --------------------------------------------------------------------------- #
# 状态
# --------------------------------------------------------------------------- #

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_FILE = SCRIPT_DIR / ".checkin_scan.json"
REPORT_FILE = SCRIPT_DIR / "sites_found.json"
CHECKIN_PY = SCRIPT_DIR / "checkin.py"
CREDS_FILE = SCRIPT_DIR / "checkin_creds.json"


def load_state() -> dict:
    try:
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"seen": {}, "ignored": [], "announced": []}
    # 旧格式 seen 是 host 列表 -> 迁移为 dict（保留 first_seen 语义为"曾经见过"）
    old = st.get("seen", {})
    if isinstance(old, list):
        now = time.time()
        st["seen"] = {h: {"url": "https://" + h, "first_seen": now, "api_ok": True,
                          "probed_at": 0, "legacy": True}
                      for h in old if isinstance(h, str)}
    elif not isinstance(old, dict):
        st["seen"] = {}
    st.setdefault("ignored", [])
    st.setdefault("announced", [])
    return st


def save_state(st: dict) -> None:
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def known_urls() -> set:
    hosts: set[str] = set()
    try:
        text = CHECKIN_PY.read_text(encoding="utf-8")
        for m in re.finditer(r'"base_url":\s*"(https?://[^"]+)"', text):
            hosts.add(_host(m.group(1)))
    except Exception:
        pass
    try:
        creds = json.loads(CREDS_FILE.read_text(encoding="utf-8"))
        for u in creds.get("urls", {}).values():
            hosts.add(_host(u))
    except Exception:
        pass
    return hosts


if __name__ == "__main__":
    sys.exit(main())
