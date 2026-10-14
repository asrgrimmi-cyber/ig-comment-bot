"""
ig-comment-bot: open-source "comment KEYWORD -> auto-DM a link" for Instagram.

Uses only the official Instagram API with Instagram Login (graph.instagram.com):
  * webhook field "comments"            -> someone comments on your post/reel
  * POST /{ig-user-id}/messages          -> private reply (DM) to that comment (recipient.comment_id)
  * POST /{comment-id}/replies           -> optional public reply under the comment
Python standard library only. MIT licence.

Run:   python igbot.py                 (reads .env next to this file)
       python igbot.py --selftest      (signed fake webhook against a mock Graph API, no Meta needed)
Endpoints (also reachable under /ig/... so it can sit behind a path-mounted proxy):
  GET  /webhook   Meta verification handshake (hub.challenge)
  POST /webhook   comment events (X-Hub-Signature-256 checked with the app secret)
  GET  /health    {"ok":true,...}
  GET  /stats?key=ADMIN_KEY   recent events
"""
import hashlib, hmac, json, os, random, re, sqlite3, sys, threading, time, urllib.error, urllib.parse, urllib.request
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_env(path=HERE / ".env"):
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
E = os.environ.get
CFG = {
    "port": int(E("PORT", "8091")),
    "app_secret": E("IG_APP_SECRET", ""),
    "verify_token": E("IG_VERIFY_TOKEN", ""),
    "token": E("IG_ACCESS_TOKEN", ""),
    "ig_user_id": E("IG_USER_ID", ""),
    "graph": E("GRAPH_BASE", "https://graph.instagram.com/v23.0").rstrip("/"),
    "keywords": [k.strip().lower() for k in E("KEYWORDS", "board").split(",") if k.strip()],
    "media_ids": {m.strip() for m in E("MEDIA_IDS", "").split(",") if m.strip()},   # empty = every post
    "link": E("LINK", "https://asrgrimmi-cyber.github.io/next-stop/?src=ig"),
    "dm_text": E("DM_TEXT", "Hey {name}! 👋 Here's Next Stop, the to-do list that works like a departures board:\n{link}\n\nCheck in, book your first flight (only the name is needed), and tap “Report a problem” if anything breaks 🙏"),
    "public_replies": [r for r in E("PUBLIC_REPLIES", "Sent you the link 📩 check your DMs (or Message requests)!|Link's in your DMs ✈️|Check your DMs 🙌 boarding pass sent!").split("|") if r],
    "public_reply_on": E("PUBLIC_REPLY", "1") == "1",
    "sheet_log": E("SHEET_LOG_URL", ""),
    "sheet_key": E("SHEET_LOG_KEY", "nextstop-testers"),
    "admin_key": E("ADMIN_KEY", ""),
    "dry_run": E("DRY_RUN", "0") == "1",
    "db": Path(E("DB_PATH", str(HERE / "igbot.db"))),
}
SOURCE_URL = "https://github.com/asrgrimmi-cyber/ig-comment-bot"


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ---------------- storage ----------------
def db():
    c = sqlite3.connect(CFG["db"], timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db():
    with closing(db()) as c, c:
        c.execute("""CREATE TABLE IF NOT EXISTS events(
            comment_id TEXT PRIMARY KEY, media_id TEXT, user_id TEXT, username TEXT, text TEXT,
            received REAL, dm_status TEXT, dm_error TEXT, reply_status TEXT, reply_error TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS ev_user_media ON events(user_id, media_id)")
        c.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")


def kv_get(k, default=""):
    with closing(db()) as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def kv_set(k, v):
    with closing(db()) as c, c:
        c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def access_token():
    return kv_get("access_token") or CFG["token"]


# ---------------- Graph API ----------------
def graph(method, path, params=None, body=None, timeout=20):
    url = CFG["graph"] + path
    q = dict(params or {})
    q["access_token"] = access_token()
    url += ("&" if "?" in url else "?") + urllib.parse.urlencode(q)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(raw).get("error", {})
            msg = f"{e.code} {err.get('type','')} {err.get('code','')}: {err.get('message', raw)}"
        except ValueError:
            msg = f"{e.code}: {raw[:300]}"
        raise RuntimeError(msg) from None


def send_private_reply(comment_id, text):
    if CFG["dry_run"]:
        return {"dry_run": True}
    return graph("POST", f"/{CFG['ig_user_id'] or 'me'}/messages",
                 body={"recipient": {"comment_id": comment_id}, "message": {"text": text}})


def send_public_reply(comment_id, text):
    if CFG["dry_run"]:
        return {"dry_run": True}
    return graph("POST", f"/{comment_id}/replies", body={"message": text})


def refresh_token_if_due():
    """Long-lived Instagram tokens last 60 days; refresh every 7 days."""
    if CFG["dry_run"] or not access_token():
        return
    last = float(kv_get("token_refreshed", "0") or 0)
    if time.time() - last < 7 * 86400:
        return
    try:
        base = CFG["graph"].rsplit("/v", 1)[0]
        url = base + "/refresh_access_token?" + urllib.parse.urlencode({"grant_type": "ig_refresh_token", "access_token": access_token()})
        with urllib.request.urlopen(url, timeout=20) as r:
            d = json.loads(r.read())
        if d.get("access_token"):
            kv_set("access_token", d["access_token"]); kv_set("token_refreshed", str(time.time()))
            log("token refreshed, expires in", d.get("expires_in"), "s")
    except Exception as e:  # noqa: BLE001
        log("token refresh failed:", e)


def sheet_log(etype, username, detail):
    if not CFG["sheet_log"]:
        return
    ev = {"type": etype, "detail": detail, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
          "tid": "ig:" + username, "name": "@" + username, "email": "", "ua": "instagram", "v": "igbot"}
    try:
        req = urllib.request.Request(CFG["sheet_log"], data=json.dumps({"k": CFG["sheet_key"], "events": [ev]}).encode(),
                                     headers={"Content-Type": "text/plain"}, method="POST")
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:  # noqa: BLE001
        log("sheet log failed:", e)


# ---------------- core logic ----------------
def matches(text):
    words = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    return any(k in words for k in CFG["keywords"])


def handle_comment(v):
    cid = v.get("id")
    frm = v.get("from") or {}
    uid, uname = str(frm.get("id", "")), frm.get("username", "") or "there"
    media = str((v.get("media") or {}).get("id", ""))
    text = v.get("text", "")
    if not cid or not matches(text):
        return "ignored: no keyword"
    if CFG["ig_user_id"] and uid == CFG["ig_user_id"]:
        return "ignored: own comment"
    if CFG["media_ids"] and media not in CFG["media_ids"]:
        return "ignored: other post"
    with closing(db()) as c, c:
        if c.execute("SELECT 1 FROM events WHERE comment_id=?", (cid,)).fetchone():
            return "ignored: duplicate delivery"
        repeat = c.execute("SELECT 1 FROM events WHERE user_id=? AND media_id=? AND dm_status='sent'", (uid, media)).fetchone()
        c.execute("INSERT INTO events(comment_id,media_id,user_id,username,text,received) VALUES(?,?,?,?,?,?)",
                  (cid, media, uid, uname, text[:500], time.time()))
    if repeat:
        with closing(db()) as c, c:
            c.execute("UPDATE events SET dm_status='skipped: already sent for this post' WHERE comment_id=?", (cid,))
        return "skipped: user already got the link for this post"
    # 1) DM (private reply) — the important part
    dm_text = CFG["dm_text"].replace("\\n", "\n").format(name=uname, link=CFG["link"])
    try:
        send_private_reply(cid, dm_text); dm, dm_err = "sent", ""
    except Exception as e:  # noqa: BLE001
        dm, dm_err = "failed", str(e)[:500]
    # 2) public reply (optional)
    rp, rp_err = "off", ""
    if CFG["public_reply_on"]:
        try:
            send_public_reply(cid, random.choice(CFG["public_replies"])); rp = "sent"
        except Exception as e:  # noqa: BLE001
            rp, rp_err = "failed", str(e)[:500]
    with closing(db()) as c, c:
        c.execute("UPDATE events SET dm_status=?, dm_error=?, reply_status=?, reply_error=? WHERE comment_id=?",
                  (dm, dm_err, rp, rp_err, cid))
    log(f"@{uname} '{text[:40]}' on {media}: DM {dm} {dm_err} | reply {rp} {rp_err}")
    threading.Thread(target=sheet_log, args=("ig_comment_dm_" + dm, uname, f"media {media}: {text[:120]}"), daemon=True).start()
    return f"dm {dm}, reply {rp}"


def process_payload(payload):
    results = []
    if payload.get("object") not in ("instagram", "page", None):
        return results
    for entry in payload.get("entry", []):
        for ch in entry.get("changes", []):
            if ch.get("field") in ("comments", "live_comments"):
                try:
                    results.append(handle_comment(ch.get("value") or {}))
                except Exception as e:  # noqa: BLE001
                    log("handler error:", repr(e)); results.append("error")
    return results


def valid_signature(raw, header):
    secret = CFG["app_secret"]
    if not secret:
        return CFG["dry_run"]          # never accept unsigned events in live mode
    if not header or not header.startswith("sha256="):
        return False
    exp = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(exp, header[7:])


# ---------------- HTTP ----------------
class H(BaseHTTPRequestHandler):
    server_version = "igbot/1.0"

    def log_message(self, fmt, *a):
        pass

    def reply(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def route(self):
        p = urllib.parse.urlsplit(self.path)
        path = p.path[3:] if p.path.startswith("/ig/") else p.path
        return path.rstrip("/") or "/", urllib.parse.parse_qs(p.query)

    def do_GET(self):
        path, q = self.route()
        if path == "/webhook":
            mode, tok, ch = (q.get(k, [""])[0] for k in ("hub.mode", "hub.verify_token", "hub.challenge"))
            if mode == "subscribe" and CFG["verify_token"] and hmac.compare_digest(tok, CFG["verify_token"]):
                log("webhook verified by Meta"); return self.reply(200, ch, "text/plain")
            return self.reply(403, {"error": "verification failed"})
        if path in ("/health", "/"):
            return self.reply(200, {"ok": True, "service": "ig-comment-bot", "keywords": CFG["keywords"],
                                    "dry_run": CFG["dry_run"], "configured": bool(access_token() and CFG["app_secret"]),
                                    "source": SOURCE_URL})
        if path == "/stats":
            if not CFG["admin_key"] or not hmac.compare_digest(q.get("key", [""])[0], CFG["admin_key"]):
                return self.reply(403, {"error": "bad key"})
            with closing(db()) as c:
                rows = c.execute("SELECT datetime(received,'unixepoch','localtime'),username,media_id,text,dm_status,dm_error,reply_status "
                                 "FROM events ORDER BY received DESC LIMIT 100").fetchall()
                tot = dict(c.execute("SELECT dm_status,count(*) FROM events GROUP BY dm_status").fetchall())
            return self.reply(200, {"totals": tot, "recent": [dict(zip(["time", "user", "media", "text", "dm", "dm_error", "reply"], r)) for r in rows]})
        if path == "/privacy":
            return self.reply(200, "<h1>Privacy</h1><p>This bot only reads comments on our own Instagram posts that contain a keyword, "
                                   "and replies with a link. It stores the commenter's username, comment text and delivery status "
                                   "to avoid duplicate messages. Nothing is sold or shared. To be removed, DM us 'delete my data'.</p>", "text/html")
        return self.reply(404, {"error": "not found"})

    def do_POST(self):
        path, _ = self.route()
        if path != "/webhook":
            return self.reply(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            return self.reply(413, {"error": "too big"})
        raw = self.rfile.read(n)
        if not valid_signature(raw, self.headers.get("X-Hub-Signature-256", "")):
            log("rejected: bad signature"); return self.reply(401, {"error": "bad signature"})
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            return self.reply(400, {"error": "bad json"})
        # answer Meta fast (it retries slow/failed deliveries), work in the background
        threading.Thread(target=process_payload, args=(payload,), daemon=True).start()
        return self.reply(200, {"ok": True})


def main():
    init_db()
    missing = [k for k in ("app_secret", "verify_token", "token", "ig_user_id") if not CFG[k]]
    log(f"ig-comment-bot on :{CFG['port']} keywords={CFG['keywords']} dry_run={CFG['dry_run']}"
        + (f"  NOT CONFIGURED: {', '.join(missing)}" if missing else ""))

    def refresher():
        while True:
            refresh_token_if_due(); time.sleep(6 * 3600)
    threading.Thread(target=refresher, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", CFG["port"]), H).serve_forever()


# ---------------- self test ----------------
def selftest():
    """End-to-end: signed webhook -> bot -> mock Graph API. No Meta account needed."""
    import tempfile
    calls = []

    class Mock(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            calls.append((urllib.parse.urlsplit(self.path).path, body, urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)))
            fail = "fail" in json.dumps(body)
            self.send_response(400 if fail else 200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps({"error": {"message": "mock failure", "code": 10}} if fail else {"id": "m1"}).encode())

    mock = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    tmp = Path(tempfile.mkdtemp())
    CFG.update(app_secret="s3cret", verify_token="vt", token="TOK", ig_user_id="1789", dry_run=False, public_reply_on=True,
               graph=f"http://127.0.0.1:{mock.server_port}/v23.0", db=tmp / "t.db", keywords=["board"], media_ids=set(),
               sheet_log="", admin_key="adm")
    init_db()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    ok_all = True

    def check(name, cond, extra=""):
        nonlocal ok_all
        ok_all &= bool(cond); print(("PASS " if cond else "FAIL ") + name + (f" | {extra}" if extra else ""))

    def post(payload, secret="s3cret", prefix=""):
        raw = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        req = urllib.request.Request(base + prefix + "/webhook", data=raw, method="POST", headers={"X-Hub-Signature-256": sig})
        try:
            return urllib.request.urlopen(req).status
        except urllib.error.HTTPError as e:
            return e.code

    def ev(cid, text, uid="555", user="ana", media="M1"):
        return {"object": "instagram", "entry": [{"id": "1789", "time": 1, "changes": [{"field": "comments",
                "value": {"id": cid, "text": text, "from": {"id": uid, "username": user}, "media": {"id": media, "media_product_type": "REELS"}}}]}]}

    def get(url):
        try:
            r = urllib.request.urlopen(base + url); return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, ""

    st, body = get("/webhook?hub.mode=subscribe&hub.verify_token=vt&hub.challenge=12345")
    check("verification handshake echoes challenge", st == 200 and body == "12345")
    check("wrong verify token rejected", get("/webhook?hub.mode=subscribe&hub.verify_token=no&hub.challenge=1")[0] == 403)
    check("works under /ig/ prefix too", get("/ig/webhook?hub.mode=subscribe&hub.verify_token=vt&hub.challenge=9")[1] == "9")
    check("bad signature rejected", post(ev("c0", "BOARD"), secret="wrong") == 401)

    check("signed event accepted", post(ev("c1", "BOARD please!")) == 200); time.sleep(.6)
    dm = [c for c in calls if c[0].endswith("/1789/messages")]
    rp = [c for c in calls if c[0].endswith("/c1/replies")]
    check("DM sent as private reply to the comment", dm and dm[0][1]["recipient"] == {"comment_id": "c1"} and "next-stop" in dm[0][1]["message"]["text"])
    check("DM is personalised", dm and "ana" in dm[0][1]["message"]["text"])
    check("access token sent", dm and dm[0][2].get("access_token") == ["TOK"])
    check("public reply posted", len(rp) == 1)
    n = len(calls)
    post(ev("c1", "BOARD please!")); time.sleep(.4)
    check("Meta re-delivery of same comment ignored", len(calls) == n)
    post(ev("c2", "board 🙏")); time.sleep(.4)
    check("same user, same post: no second DM", len(calls) == n)
    post(ev("c3", "Board", uid="777", user="ben", media="M2")); time.sleep(.5)
    check("new user gets DM (case-insensitive)", len(calls) == n + 2)
    n = len(calls)
    post(ev("c4", "cardboard and keyboard", uid="888")); post(ev("c5", "nice reel", uid="889")); time.sleep(.4)
    check("no false match (cardboard/keyboard/other text)", len(calls) == n)
    post(ev("c6", "BOARD", uid="1789")); time.sleep(.4)
    check("own comments ignored", len(calls) == n)
    post(ev("c7", "BOARD fail", uid="999", user="fail")); time.sleep(.5)
    st, body = get("/stats?key=adm")
    stats = json.loads(body) if st == 200 else {}
    check("failed DM recorded with error", any(r["dm"] == "failed" and "mock failure" in (r["dm_error"] or "") for r in stats.get("recent", [])))
    check("stats need admin key", get("/stats?key=x")[0] == 403)
    CFG["media_ids"] = {"ONLY"}
    n = len(calls); post(ev("c8", "BOARD", uid="1000", media="OTHER")); time.sleep(.4)
    check("MEDIA_IDS filter respected", len(calls) == n)
    print(f"\nself-test {'PASSED' if ok_all else 'FAILED'}  totals={stats.get('totals')}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(selftest() if "--selftest" in sys.argv else main())
