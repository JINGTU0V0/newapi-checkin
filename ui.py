"""newapi-checkin 内嵌 Web 面板（--ui）。

只用标准库 http.server：
  GET  /              单页面板（状态 + 运行日志流 + 账本图表 + 配置编辑）
  GET  /api/status    站点列表 + 今日状态 + 今日收益
  GET  /api/ledger    账本（画曲线用）
  POST /api/run       触发签到（后台线程跑内核 run_batch）
  GET  /api/events    短轮询：返回 seq 之后的站点完成事件
  GET/POST /api/file  读写 sites.yaml / creds.json 原文（带 token 才允许写；
                      本地无 sites.yaml 时 GET 返回内置模板只读文本）
  POST /api/site      表单增/改/删站点（写回 sites.yaml + 同步 creds.json）
  POST /api/detect    凭据实测探测签到模式（oneapi/claim/sign/proof/ocr/routerteam）

安全：默认只监听 127.0.0.1；绑到非本机地址必须配 --ui-token。
"""
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import checkin

BLOCK_RE = __import__("re").compile(r"^  - ")


def _yaml_scalar(v) -> str:
    import re as _re
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "" or _re.search(r"[:#\[\]{}\"']|^\s|\s$", s) or s.lower() in ("true", "false", "yes", "no"):
        return json.dumps(s, ensure_ascii=False)
    return s


def _render_site_block(site: dict) -> list:
    """站点 dict -> yaml 行（受限解析器支持的格式：标量 + 行内 dict/list）。"""
    lines = []
    for k, v in site.items():
        pre = "  - " if not lines else "    "
        if isinstance(v, dict):
            v = "{" + ", ".join(f"{kk}: {_yaml_scalar(vv)}" for kk, vv in v.items()) + "}"
        elif isinstance(v, list):
            v = "[" + ", ".join(_yaml_scalar(x) for x in v) + "]"
        else:
            v = _yaml_scalar(v)
        lines.append(f"{pre}{k}: {v}")
    return lines


def _find_blocks(lines: list) -> list:
    """sites: 正文里每个 '  - ' 块的 (start, end) 行号区间。"""
    idxs = [i for i, l in enumerate(lines) if BLOCK_RE.match(l)]
    blocks = []
    for n, i in enumerate(idxs):
        end = idxs[n + 1] if n + 1 < len(idxs) else len(lines)
        while end > i + 1 and not lines[end - 1].strip():
            end -= 1
        blocks.append((i, end))
    return blocks


def _block_name(lines: list, start: int, end: int) -> str:
    m = __import__("re").search(r"name:\s*(.+)", "\n".join(lines[start:end]))
    return checkin._scalar(m.group(1).strip()) if m else ""


def _split_sites_text(text: str):
    """-> (head_lines 含 'sites:' 行, body_lines)。没有 sites: 段就补一个。"""
    lines = text.splitlines()
    for i, l in enumerate(lines):
        if __import__("re").match(r"sites:\s*(\[\])?\s*$", l):
            if l.rstrip() != "sites:":       # 'sites: []' 归一化为 'sites:'
                lines = lines[:i] + ["sites:"] + lines[i + 1:]
            return lines[:i + 1], lines[i + 1:]
    return lines + ["sites:"], []


def _sites_mutate(action: str, payload: dict) -> str:
    """对 sites.yaml 原文做增/改/删站点块，返回新文本（改 creds 由调用方负责）。"""
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("站点名称不能为空")
    src = checkin.SITES_FILE.read_text(encoding="utf-8") if checkin.SITES_FILE.exists() \
        else (checkin.builtin_template_text() or "sites:\n")
    head, body = _split_sites_text(src)
    blocks = _find_blocks(body)
    find = (payload.get("old_name") or name).strip() if action == "update" else name
    hit = next(((i, e) for i, e in blocks if _block_name(body, i, e) == find), None)
    if action == "update" and not hit:
        raise ValueError(f"站点 {find} 不存在")

    if action == "delete":
        if not hit:
            raise ValueError(f"站点 {name} 不存在")
        i, e = hit
        body = body[:i] + body[e:]
    elif action in ("add", "update") and not hit:
        # 新站追加到尾部：若末尾是模板注释示例块，先裁掉（注释不能跟进真站块）
        def _is_cmt(ln):
            st = ln.strip()
            return not st or st.startswith("#")
        cut = len(body)
        while cut > 0 and _is_cmt(body[cut - 1]):
            cut -= 1
        trailing_comment = cut < len(body) and any(
            l.strip().startswith("#") for l in body[cut:])
        if trailing_comment:
            body = body[:cut]
    if action != "delete":
        fields = {}
        if hit:  # 改：保留原块里表单没有的高级字段
            i, e = hit
            try:
                parsed = checkin._parse_sites(_TmpYaml("sites:\n" + "\n".join(body[i:e])))
                fields = (parsed.get("sites") or [{}])[0]
            except Exception:
                fields = {"name": name}
        for k in ("base_url", "mode", "credential"):
            v = (payload.get(k) or "").strip()
            if k == "mode":
                v = v or fields.get("mode") or "oneapi"
                if v == "oneapi" and k not in payload:
                    continue
            if v:
                fields[k] = v
        if payload.get("username") is not None:
            fields["username"] = payload["username"].strip()
        fields["name"] = name
        ordered = {"name": fields.pop("name")}
        ordered.update(fields)
        new_block = _render_site_block(ordered)
        if hit:
            i, e = hit
            body = body[:i] + new_block + body[e:]
        else:
            if body and body[-1].strip():
                body.append("")
            body += new_block
    out = "\n".join(head + body) + "\n"
    checkin._parse_sites(_TmpYaml(out))  # 存前校验
    return out


def _creds_mutate(action: str, payload: dict) -> None:
    """同步 creds.json 的 credentials.<站名>；密码留空/'***' 表示不动。"""
    name = payload["name"].strip()
    cre = checkin._load_json(checkin.CREDS_FILE) if checkin.CREDS_FILE.exists() else {}
    table = cre.setdefault("credentials", {})
    if action == "delete":
        table.pop(name, None)
    else:
        entry = table.setdefault(name, {})
        old = (payload.get("old_name") or "").strip()
        if old and old != name and old in table:  # 重命名：账号跟着搬
            moved = table.pop(old)
            if not entry.get("username"):
                entry.update({k: v for k, v in moved.items() if k not in entry or not entry[k]})
        if payload.get("username"):
            entry["username"] = payload["username"].strip()
        pw = payload.get("password") or ""
        if pw and pw != "***":
            entry["password"] = pw
        entry.setdefault("username", "")
        entry.setdefault("password", "")
    checkin._save_json(checkin.CREDS_FILE, cre)

RUN = {
    "lock": threading.Lock(),
    "running": False,
    "events": [],      # [{seq, name, status, award, log}]
    "seq": 0,
    "started": None,
    "summary": None,
}


def _on_site_done(name, status, log, award):
    with RUN["lock"]:
        RUN["seq"] += 1
        RUN["events"].append({"seq": RUN["seq"], "name": name,
                              "status": status, "award": award, "log": log})
        RUN["events"] = RUN["events"][-100:]


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1", "")


def _has_cred(s) -> bool:
    import re as _re
    pw = s.get("password") or ""
    return bool(pw) and pw != "***" and not _re.fullmatch(r"\$\{\w+\}", pw)


def _mask_creds(text: str) -> str:
    """GET creds 时把明文密码换成 ***；${ENV} 占位不是秘密，原样保留。"""
    try:
        obj = json.loads(text)
    except Exception:
        return text

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "password" and isinstance(v, str) and v and not v.startswith("${"):
                    o[k] = "***"
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(obj)
    return json.dumps(obj, ensure_ascii=False, indent=1)


def _unmask_creds(new_text: str, old_text: str) -> str:
    """保存时把面板里没动过的 *** 占位还原成磁盘上的原密码。"""
    try:
        new, old = json.loads(new_text), json.loads(old_text)
    except Exception:
        return new_text

    def restore(n, o):
        if isinstance(n, dict) and isinstance(o, dict):
            for k, v in n.items():
                if k == "password" and v == "***" and isinstance(o.get(k), str):
                    n[k] = o[k]
                else:
                    restore(v, o.get(k))
        elif isinstance(n, list) and isinstance(o, list):
            for i, x in enumerate(n):
                restore(x, o[i] if i < len(o) else None)
    restore(new, old)
    return json.dumps(new, ensure_ascii=False, indent=1)


def _status_payload() -> dict:
    cfg = checkin.load_config()
    sites = [checkin.resolve_site(s, cfg) for s in cfg["sites"] if s.get("enabled", True)]
    state, _ = checkin.load_state()
    today = checkin.datetime.now().strftime("%Y-%m-%d")
    led = checkin._load_json(checkin.LEDGER_FILE).get(today, {})
    rate = cfg.get("settings", {}).get("quota_per_dollar") or 500000
    out = []
    for s in sites:
        day = led.get(s["name"], {})
        out.append({
            "name": s["name"],
            "base_url": s.get("base_url", ""),
            "mode": s.get("mode", "oneapi"),
            "username": s.get("username", ""),
            "credential": s.get("credential", ""),
            "cred": _has_cred(s),
            "status": state.get(s["name"], ""),
            "awarded_usd": round((day.get("quota_awarded") or 0) / rate, 4),
            "balance_usd": round((day.get("balance_quota") or 0) / rate, 2),
        })
    return {"date": today, "running": RUN["running"], "sites": out,
            "today_usd": round(sum(x["awarded_usd"] for x in out), 2)}


def _start_run(mode: str, notify: bool, only: list | None = None) -> str:
    if RUN["running"]:
        return "已有签到任务在跑"
    cfg = checkin.load_config()
    sites = [checkin.resolve_site(s, cfg) for s in cfg["sites"] if s.get("enabled", True)]
    state, marks = checkin.load_state() if mode == "today" else ({}, {})
    if only:  # 面板按当前显示名单精确指定（retry = 失败站列表）
        want = {n.lower() for n in only}
        sites = [s for s in sites if s["name"].lower() in want]
    targets = checkin.pick_targets(sites, state, [])
    if not targets:
        return "没有待跑站点（今日已全部完成或凭证缺失）"

    def worker():
        try:
            res = checkin.run_batch(targets, cfg, state, marks, notify)
            RUN["summary"] = {"tally": res["tally"], "fails": res["fails"]}
        except Exception as e:
            RUN["summary"] = {"error": f"{type(e).__name__}: {e}"}
        finally:
            RUN["running"] = False

    RUN.update(running=True, started=time.time(), summary=None)
    with RUN["lock"]:
        RUN["events"].clear()
        RUN["seq"] = 0
    threading.Thread(target=worker, daemon=True).start()
    return ""


class Handler(BaseHTTPRequestHandler):
    token = ""
    quiet = True

    def log_message(self, *a):  # 静音访问日志
        pass

    # ---------------- 基础设施 ----------------
    def _authed(self, q) -> bool:
        if not self.token:
            return True
        if self.headers.get("X-Checkin-Token") == self.token:
            return True
        if q.get("token", [""])[0] == self.token:
            return True
        return "checkin_token=" + self.token in self.headers.get("Cookie", "")

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        raw = body if isinstance(body, bytes) else json.dumps(
            body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    # ---------------- 路由 ----------------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._authed(q):
            return self._send(401, {"error": "需要 token（URL 加 ?token=*** 或请求头 X-Checkin-Token）"})
        if u.path in ("/", "/index.html"):
            extra = {}
            if self.token and q.get("token"):
                extra["Set-Cookie"] = f"checkin_token={self.token}; SameSite=Strict; Path=/"
            return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8", extra)
        if u.path == "/api/status":
            return self._send(200, _status_payload())
        if u.path == "/api/ledger":
            return self._send(200, checkin._load_json(checkin.LEDGER_FILE))
        if u.path == "/api/events":
            since = int(q.get("since", ["0"])[0] or 0)
            with RUN["lock"]:
                evs = [e for e in RUN["events"] if e["seq"] > since]
                return self._send(200, {"events": evs, "seq": RUN["seq"],
                                        "running": RUN["running"],
                                        "summary": RUN["summary"]})
        if u.path == "/api/file":
            p = {"sites": checkin.SITES_FILE, "creds": checkin.CREDS_FILE}.get(
                q.get("name", [""])[0])
            if not p:
                return self._send(400, {"error": "name 只能是 sites / creds"})
            exists = p.exists()
            if exists:
                text = p.read_text(encoding="utf-8")
            elif q["name"][0] == "sites":
                text = checkin.builtin_template_text()  # 内置模板只读展示，编辑保存时才落盘
            else:
                text = ""
            if q["name"][0] == "creds" and exists:
                text = _mask_creds(text)  # 明文密码不回显到浏览器
            return self._send(200, {"name": q["name"][0], "path": str(p), "exists": exists,
                                    "text": text})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._authed(q):
            return self._send(401, {"error": "需要 token"})
        body = self._body()
        if u.path == "/api/run":
            err = _start_run(body.get("mode", "today"), bool(body.get("notify")),
                             body.get("only") or None)
            return self._send(409 if err else 200, {"ok": not err, "error": err})
        if u.path == "/api/detect":
            base = (body.get("base_url") or "").strip().rstrip("/")
            if not base.startswith(("http://", "https://")):
                return self._send(400, {"error": "请先填写合法的站点地址（http/https）"})
            mode, steps = checkin.detect_mode(base, body.get("username") or "",
                                              body.get("password") or "")
            return self._send(200, {"mode": mode, "steps": steps})
        if u.path == "/api/site":
            action = body.get("action", "add")
            if action not in ("add", "update", "delete"):
                return self._send(400, {"error": "action 只能是 add / update / delete"})
            try:
                cfg = checkin.load_config()
                exist = {(s.get("name") or "").lower() for s in cfg["sites"]}
                nm = (body.get("name") or "").strip().lower()
                old = (body.get("old_name") or "").strip().lower()
                if action == "add" and nm in exist:
                    return self._send(400, {"error": f"站点 {body['name']} 已存在"})
                if action == "update" and old not in exist:
                    return self._send(400, {"error": f"站点 {body.get('old_name')} 不存在"})
                if action == "update" and nm != old and nm in exist:
                    return self._send(400, {"error": f"站点 {body['name']} 已存在，不能重名"})
                if action == "delete" and nm not in exist:
                    return self._send(400, {"error": f"站点 {body.get('name')} 不存在"})
                if action == "add" and not (body.get("base_url") or "").strip():
                    return self._send(400, {"error": "站点地址不能为空"})
                text = _sites_mutate(action, body)
                _creds_mutate(action, body)
                p = checkin.SITES_FILE
                tmp = p.with_suffix(p.suffix + ".tmp")
                tmp.write_text(text, encoding="utf-8")
                import os
                os.replace(tmp, p)
            except Exception as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"ok": True})
        if u.path == "/api/file":
            key = body.get("name")
            if key not in ("sites", "creds"):
                return self._send(400, {"error": "name 只能是 sites / creds"})
            p = {"sites": checkin.SITES_FILE, "creds": checkin.CREDS_FILE}[key]
            text = body.get("text", "")
            if key == "creds":
                try:
                    json.loads(text)
                except Exception as e:
                    return self._send(400, {"error": f"creds.json 不是合法 JSON: {e}"})
                if p.exists():  # 面板里没动过的 *** 占位还原成磁盘原密码
                    text = _unmask_creds(text, p.read_text(encoding="utf-8"))
            else:
                try:  # 试解析防止存坏配置
                    checkin._parse_sites(_TmpYaml(text))
                except Exception as e:
                    return self._send(400, {"error": f"sites.yaml 解析失败: {e}"})
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            try:
                p.chmod(0o600)
            except Exception:
                pass
            import os
            os.replace(tmp, p)
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})


class _TmpYaml:
    """给 _parse_sites 一个可 read_text 的临时对象做保存前校验。"""
    def __init__(self, text):
        self._t = text

    def read_text(self, encoding="utf-8"):
        return self._t


def _load_page() -> str:
    """单页前端：exe 从解包目录找，源码运行找 ui.py 旁的 web/。"""
    meipass = getattr(__import__("sys"), "_MEIPASS", None)
    roots = ([Path(meipass) / "web"] if meipass else []) + \
            [Path(__file__).resolve().parent / "web"]
    for r in roots:
        f = r / "index.html"
        if f.exists():
            return f.read_text(encoding="utf-8")
    return "<h3>缺少 web/index.html</h3>"


PAGE = _load_page()


def serve(host: str, port: int, token: str = "") -> None:
    if not _is_loopback(host) and not token:
        raise SystemExit("❌ 绑定到非本机地址必须同时设置 --ui-token，否则任何人都能改你的配置")
    Handler.token = token
    checkin.LOG_HOOK.append(_on_site_done)
    srv = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{'127.0.0.1' if _is_loopback(host) else host}:{port}"
    if token:
        url += "/?token=" + token
    print(f"🖥️ 面板已启动: {url}  (Ctrl+C 停止)")
    if _is_loopback(host):
        threading.Timer(0.5, lambda: __import__("webbrowser").open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
