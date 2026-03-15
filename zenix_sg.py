#!/usr/bin/env python3
import asyncio, aiohttp, json, time, sys, argparse, re, termios, tty, select
from datetime import datetime

EMAIL    = "your@email.com"
PASSWORD = "yourpassword"

HEARTBEAT_INTERVAL = 60         
EARN_INTERVAL      = 43200       # 12h  (daily earn)
RENEW_INTERVAL     = 86400       # 24h  (renew)

BASE_URL = "https://dash.zenix.sg"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/145.0.0.0 Safari/537.36")

ACTION_LOGIN         = "60acb33fd6579148670d99f82a6dd17dcaecbd1b7d"
ACTION_AFK_START     = "40de3d9cc38d86ab1187275ed57e6dfaaa39763315"
ACTION_AFK_HEARTBEAT = "40fa965c6bc0301f0a3219d29f6bd84bcfa9128681"
ACTION_EARN          = "40c9fe642b4a8c5f9020a7fa7858618363f4ce5924"
ACTION_RENEW         = "004cf06bc9bc329dcf2afcd895b6614c8421637efa"

WEBHOOK_URL     = ""
WEBHOOK_ENABLED = False

UUID_RE = re.compile(
    r'["\[,]([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'
)

def ts():
    return datetime.now().strftime("%H:%M:%S")

def log(msg, tag=".."):
    print(f"\n[{ts()}] [{tag}] {msg}", flush=True)

def fmt_up(s):
    s = int(s)
    return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"

def fmt_next(seconds):
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h:02d}h{m:02d}m"


class ZenixBot:
    def __init__(self, email, password, user_id=None,
                 webhook_url=None, webhook_on=False):
        self.email        = email
        self.password     = password
        self.session      = None
        self.running      = False
        self.hb_count     = 0
        self.start_time   = None
        self.user_id      = user_id
        self.webhook_url  = webhook_url or WEBHOOK_URL
        self.webhook_on   = webhook_on  or WEBHOOK_ENABLED
        self.last_webhook = 0
        self.last_earn    = 0
        self.last_renew   = 0

    def _base_hdrs(self):
        return {
            "User-Agent":         UA,
            "Accept-Language":    "vi-VN,vi;q=0.9,en-US;q=0.6,en;q=0.5",
            "sec-ch-ua":          '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
            "sec-ch-ua-mobile":   "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-site":     "same-origin",
            "Origin":             BASE_URL,
            "Referer":            BASE_URL + "/dashboard/afk",
        }

    def _action_hdrs(self, action_hash, page_path="/dashboard/afk"):
        import urllib.parse
        segment = page_path.strip("/").split("/")[-1]
        state = json.dumps([
            "", {"children": ["dashboard", {"children": [
                segment, {"children": ["__PAGE__", {}, None, None]},
                None, None]}, None, None]},
            None, None, True
        ], separators=(',', ':'))
        h = self._base_hdrs()
        h.update({
            "Accept":                  "text/x-component",
            "Content-Type":            "text/plain;charset=UTF-8",
            "next-action":             action_hash,
            "next-router-state-tree":  urllib.parse.quote(state),
            "Referer":                 BASE_URL + page_path,
            "Priority":                "u=1, i",
            "sec-fetch-dest":          "empty",
            "sec-fetch-mode":          "cors",
        })
        return h

    async def req(self, method, url, **kw):
        try:
            async with self.session.request(
                method, url,
                headers=kw.pop("headers", self._base_hdrs()),
                timeout=aiohttp.ClientTimeout(total=20),
                **kw
            ) as r:
                try:    body = await r.json(content_type=None)
                except: body = await r.text()
                return r.status, body
        except asyncio.TimeoutError: return 0, "timeout"
        except Exception as e:       return 0, str(e)

    async def webhook(self, msg):
        if not (self.webhook_on and self.webhook_url):
            return
        try:
            async with self.session.post(
                self.webhook_url, json={"content": msg},
                timeout=aiohttp.ClientTimeout(total=8)
            ) as _: pass
        except: pass

    async def login(self):
        import urllib.parse
        log(f"Login: {self.email}", ">>")
        state = json.dumps([
            "", {"children": ["(auth)", {"children": [
                "login", {"children": ["__PAGE__", {}, None, None]},
                None, None]}, None, None]},
            None, None, True
        ], separators=(',', ':'))
        hdrs = self._base_hdrs()
        hdrs.update({
            "Accept":                  "text/x-component",
            "Content-Type":            "text/plain;charset=UTF-8",
            "next-action":             ACTION_LOGIN,
            "next-router-state-tree":  urllib.parse.quote(state),
            "Priority":                "u=1, i",
            "Referer":                 BASE_URL + "/login",
            "sec-fetch-dest":          "empty",
            "sec-fetch-mode":          "cors",
        })
        s, b = await self.req("POST", f"{BASE_URL}/login",
                              headers=hdrs,
                              data=json.dumps([self.email, self.password]))
        if s in (200, 201, 204):
            log("Login OK", "OK")
            if isinstance(b, str):
                self._extract_uuid(b, "login RSC")
            await self._fetch_profile()
            return True
        log(f"Login failed [{s}]: {str(b)[:200]}", "!!")
        return False

    def _extract_uuid(self, text, source=""):
        if self.user_id:
            return
        for m in UUID_RE.findall(text):
            if m and "0000-0000" not in m:
                self.user_id = m
                log(f"User ID (from {source}): {self.user_id}", "OK")
                return

    async def _fetch_profile(self):
        if self.user_id:
            log(f"User ID: {self.user_id}", "OK")
            return
        for ep in ["/api/auth/session", "/api/me", "/api/user/me", "/api/user"]:
            hdrs = self._base_hdrs()
            hdrs["Accept"] = "application/json"
            s, b = await self.req("GET", f"{BASE_URL}{ep}", headers=hdrs)
            if s == 200 and isinstance(b, dict):
                uid = (b.get("user", {}).get("id")
                       or b.get("id") or b.get("uuid") or b.get("userId"))
                if uid:
                    self.user_id = str(uid)
                    log(f"User ID (from {ep}): {self.user_id}", "OK")
                    return
            elif s == 200 and isinstance(b, str):
                self._extract_uuid(b, ep)
                if self.user_id: return

        # 2. Parse dashboard HTML / RSC payload
        for path in ["/dashboard/afk", "/dashboard", "/dashboard/earn"]:
            hdrs = self._base_hdrs()
            hdrs["Accept"] = "text/html,*/*"
            s, b = await self.req("GET", f"{BASE_URL}{path}", headers=hdrs)
            if s == 200 and isinstance(b, str):
                self._extract_uuid(b, f"page {path}")
                if self.user_id: return

        log("Could not retrieve user ID automatically. Use --id <uuid> if needed.", "!!")

    def _body(self):
        return json.dumps([self.user_id]) if self.user_id else "[]"

    async def afk_start(self):
        s, b = await self.req(
            "POST", f"{BASE_URL}/dashboard/afk",
            headers=self._action_hdrs(ACTION_AFK_START),
            data=self._body()
        )
        if s in (200, 201, 204):
            self.start_time = self.start_time or time.time()
            log("AFK start OK", "OK"); return True
        if s == 409:
            self.start_time = self.start_time or time.time()
            log("AFK already active", "OK"); return True
        log(f"AFK start error [{s}]: {str(b)[:150]}", "!!")
        return False

    async def afk_heartbeat(self):
        s, b = await self.req(
            "POST", f"{BASE_URL}/dashboard/afk",
            headers=self._action_hdrs(ACTION_AFK_HEARTBEAT),
            data=self._body()
        )
        return s in (200, 201, 204), s

    async def do_earn(self):
        s, b = await self.req(
            "POST", f"{BASE_URL}/dashboard/earn",
            headers=self._action_hdrs(ACTION_EARN, "/dashboard/earn"),
            data=self._body()
        )
        ok = s in (200, 201, 204)
        log(f"Earn {'OK' if ok else 'FAIL'} [{s}]", "OK" if ok else "!!")
        if ok:
            self.last_earn = time.time()
            await self.webhook("[Zenix] Daily Earn OK!")
        return ok

    async def earn_loop(self):
        await self.do_earn()
        while self.running:
            wait = max(0, EARN_INTERVAL - (time.time() - self.last_earn))
            log(f"Next earn in {fmt_next(wait)}", "..")
            await asyncio.sleep(wait)
            if self.running:
                await self.do_earn()

    async def do_renew(self):
        import urllib.parse
        state = json.dumps([
            "", {"children": ["dashboard", {"children": [
                "renew", {"children": ["__PAGE__", {}, None, None]},
                None, None]}, None, None]},
            None, None, True
        ], separators=(',', ':'))
        hdrs = self._base_hdrs()
        hdrs.update({
            "Accept":                  "text/x-component",
            "Content-Type":            "text/plain;charset=UTF-8",
            "next-action":             ACTION_RENEW,
            "next-router-state-tree":  urllib.parse.quote(state),
            "Referer":                 BASE_URL + "/dashboard/renew",
            "Priority":                "u=1, i",
            "sec-fetch-dest":          "empty",
            "sec-fetch-mode":          "cors",
        })
        s, b = await self.req("POST", f"{BASE_URL}/dashboard/renew",
                              headers=hdrs, data="[]")
        ok = s in (200, 201, 204)
        log(f"Renew {'OK' if ok else 'FAIL'} [{s}]", "OK" if ok else "!!")
        if ok:
            self.last_renew = time.time()
            await self.webhook("[Zenix] Renew OK!")
        return ok

    async def renew_loop(self):
        await self.do_renew()
        while self.running:
            wait = max(0, RENEW_INTERVAL - (time.time() - self.last_renew))
            log(f"Next renew in {fmt_next(wait)}", "..")
            await asyncio.sleep(wait)
            if self.running:
                await self.do_renew()

    async def keyboard_loop(self):
        try:
            if not sys.stdin.isatty():
                log("Server mode – keyboard disabled.", "..")
                while self.running: await asyncio.sleep(1)
                return
            fd  = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            try:
                while self.running:
                    if select.select([sys.stdin], [], [], 0.1)[0]:
                        ch = sys.stdin.read(1).lower()
                        if ch == 'r':
                            termios.tcsetattr(fd, termios.TCSADRAIN, old)
                            await self.do_renew(); tty.setcbreak(fd)
                        elif ch == 'e':
                            termios.tcsetattr(fd, termios.TCSADRAIN, old)
                            await self.do_earn(); tty.setcbreak(fd)
                        elif ch == 'q':
                            termios.tcflush(fd, termios.TCIFLUSH)
                            print(); log("Pressed q, stopping...", "..")
                            self.running = False; break
                    await asyncio.sleep(0.1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception as e:
            log(f"Keyboard error: {e}", "..")
            while self.running: await asyncio.sleep(1)

    async def run(self):
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=True),
            cookie_jar=aiohttp.CookieJar()
        ) as sess:
            self.session = sess

            if not await self.login():
                return

            if not self.user_id:
                log("WARNING: No user ID – coins may not be counted!", "!!")

            if not await self.afk_start():
                return

            self.running  = True
            self.hb_count = 0

            if self.webhook_on and self.webhook_url:
                await self.webhook("[Zenix AFK] Bot started!")

            print()
            log(f"User ID : {self.user_id or '(not found – use --id)'}", "OK")
            log(f"Earn    : every {EARN_INTERVAL//3600}h  |  Renew: every {RENEW_INTERVAL//3600}h", "OK")
            log("[r] renew  [e] earn  [q] quit", "OK")
            print()

            tasks = [
                asyncio.create_task(self.keyboard_loop()),
                asyncio.create_task(self.earn_loop()),
                asyncio.create_task(self.renew_loop()),
            ]

            try:
                while self.running:
                    ok, hbs = await self.afk_heartbeat()
                    self.hb_count += 1

                    if self.webhook_on and time.time() - self.last_webhook > 600:
                        self.last_webhook = time.time()
                        up = fmt_up(time.time() - self.start_time)
                        en = fmt_next(max(0, EARN_INTERVAL  - (time.time() - self.last_earn)))
                        rn = fmt_next(max(0, RENEW_INTERVAL - (time.time() - self.last_renew)))
                        await self.webhook(
                            f"[Zenix AFK] Uptime: {up} | HB: {self.hb_count} "
                            f"| Next earn: {en} | Next renew: {rn}")

                    up   = fmt_up(time.time() - self.start_time)
                    icon = "[OK]" if ok else "[!!]"
                    en   = fmt_next(max(0, EARN_INTERVAL  - (time.time() - self.last_earn)))
                    rn   = fmt_next(max(0, RENEW_INTERVAL - (time.time() - self.last_renew)))
                    print(f"\r{icon} {up} | HB:{self.hb_count}[{hbs}] | Earn:{en} | Renew:{rn}   ",
                          end="", flush=True)

                    if not ok:
                        log(f"HB error [{hbs}], restarting in 5s...", "!!")
                        await asyncio.sleep(5)
                        retry = 0
                        while self.running:
                            if await self.afk_start():
                                log("AFK restart OK!", "OK"); break
                            retry += 1
                            log(f"Retry {retry}/10, waiting 60s...", "!!")
                            if retry >= 10:
                                log("Exceeded 10 retries, giving up.", "!!")
                                self.running = False; break
                            await asyncio.sleep(60)
                        continue

                    await asyncio.sleep(HEARTBEAT_INTERVAL)

            except KeyboardInterrupt:
                print(); log("Ctrl+C, stopping...", "..")

            finally:
                self.running = False
                for t in tasks:
                    if not t.done():
                        t.cancel()
                        try: await t
                        except asyncio.CancelledError: pass

                if self.webhook_on and self.webhook_url:
                    up = fmt_up(time.time() - self.start_time)
                    await self.webhook(
                        f"[Zenix AFK] Bot stopped | Uptime: {up} | HB: {self.hb_count}")

                print()
                print("=" * 50)
                log(f"Uptime:    {fmt_up(time.time() - self.start_time)}", "OK")
                log(f"Heartbeat: {self.hb_count}", "OK")
                print("=" * 50)


async def main():
    p = argparse.ArgumentParser(description="dash.zenix.sg AFK Bot")
    p.add_argument("email",        nargs="?", default=EMAIL)
    p.add_argument("password",     nargs="?", default=PASSWORD)
    p.add_argument("--id",         dest="user_id", default=None,
                   help="User UUID (if bot cannot collect it automatically)")
    p.add_argument("--webhook",    dest="webhook",  default=None)
    p.add_argument("--no-webhook", dest="no_wh",   action="store_true")
    a = p.parse_args()

    if a.email == "your@email.com":
        p.print_help()
        print("\nExample:"
              "\n  python3 zenix_sg.py 'email@x.com' 'pass'"
              "\n  python3 zenix_sg.py 'email@x.com' 'pass' --id ed7a809f-..."
              "\n  python3 zenix_sg.py 'email@x.com' 'pass' --webhook https://discord.com/...")
        return

    webhook_url = a.webhook or WEBHOOK_URL
    webhook_on  = bool(webhook_url) and not a.no_wh

    await ZenixBot(a.email, a.password, a.user_id, webhook_url, webhook_on).run()
  
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[..] stop.")
