#!/usr/bin/env python3
import asyncio, aiohttp, argparse, sys, time, select, re, urllib.parse
from datetime import datetime

EMAIL    = ""
PASSWORD = ""

TICK_INTERVAL = 60
BASE          = "https://altare.gg"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)
def ts():
    return datetime.now().strftime("%H:%M:%S")
def log(msg, tag=".."):
    print(f"\n[{ts()}] [{tag}] {msg}", flush=True)
def fmt_up(s):
    s = int(s)
    return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"
class AltareBot:
    def __init__(self, email, password):
        self.email    = email.strip()
        self.password = password.strip()
        self.session  = None
        self.csrf     = ""
        self.running  = False
        self.tick_count   = 0
        self.total_earned = 0
        self.credits      = 0
        self.multiplier   = 1.0
        self.active_users = 1
        self.start_time   = None
    def _base_hdrs(self):
        return {
            "User-Agent":         UA,
            "Accept-Language":    "en-US,en;q=0.9",
            "sec-ch-ua":          '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
            "sec-ch-ua-mobile":   "?0",
            "sec-ch-ua-platform": '"Windows"',
        }
    def _api_hdrs(self):
        return {
            **self._base_hdrs(),
            "Accept":           "application/json",
            "Content-Type":     "application/json",
            "Referer":          f"{BASE}/idle-earning",
            "X-CSRF-TOKEN":     self.csrf,
            "X-Requested-With": "XMLHttpRequest",
            "sec-fetch-dest":   "empty",
            "sec-fetch-mode":   "cors",
            "sec-fetch-site":   "same-origin",
            "priority":         "u=1, i",
        }
    async def _fetch_csrf(self, url) -> str:
        try:
            async with self.session.get(
                url,
                headers={**self._base_hdrs(), "Accept": "text/html"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                html = await r.text()
        except Exception as e:
            log(f"Failed to load {url}: {e}", "!!")
            return ""
        m = re.search(r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)', html)
        if m:
            return m.group(1)
        m = re.search(r'"csrf"\s*:\s*"([A-Za-z0-9+/=_\-]{20,})"', html)
        if m:
            return m.group(1)
        m = re.search(r'["\']_token["\']\s*[=:]\s*["\']([A-Za-z0-9+/=_\-]{20,})["\']', html)
        if m:
            return m.group(1)
        for c in self.session.cookie_jar:
            if c.key == "XSRF-TOKEN":
                return urllib.parse.unquote(c.value)
        return ""
    async def login(self) -> bool:
        log("Loading login page...", "..")
        csrf = await self._fetch_csrf(f"{BASE}/login")
        if not csrf:
            log("Failed to get CSRF token from /login!", "!!")
            return False
        log(f"CSRF (pre-login): {csrf[:24]}...", "..")
        payload = {
            "email":    self.email,
            "password": self.password,
            "remember": "on",
            "_token":   csrf,
        }
        try:
            async with self.session.post(
                f"{BASE}/login",
                data=payload,
                headers={
                    **self._base_hdrs(),
                    "Accept":         "text/html,application/xhtml+xml,*/*",
                    "Content-Type":   "application/x-www-form-urlencoded",
                    "Referer":        f"{BASE}/login",
                    "sec-fetch-dest": "document",
                    "sec-fetch-mode": "navigate",
                    "sec-fetch-site": "same-origin",
                },
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                final_url = str(r.url)
                status    = r.status
                body      = await r.text()
        except Exception as e:
            log(f"Login request failed: {e}", "!!")
            return False
        if "/login" in final_url:
            body_lower = body.lower()
            if any(kw in body_lower for kw in [
                "credentials", "invalid", "incorrect",
                "wrong", "unauthorized", "these credentials"
            ]):
                log("Wrong email or password!", "!!")
            elif status == 419:
                log("CSRF expired during login, retrying...", "!!")
                return await self.login()
            elif status == 429:
                log("Too many attempts, waiting 30s...", "!!")
                await asyncio.sleep(30)
                return await self.login()
            else:
                log(f"Login failed (status {status}), body: {body[:200]}", "!!")
            return False
        log(f"Logged in! Redirected to {final_url}", "OK")
        new_csrf = await self._fetch_csrf(f"{BASE}/idle-earning")
        if new_csrf:
            self.csrf = new_csrf
            log(f"CSRF (post-login): {self.csrf[:24]}...", "OK")
            return True
        for c in self.session.cookie_jar:
            if c.key == "XSRF-TOKEN":
                self.csrf = urllib.parse.unquote(c.value)
                log(f"CSRF from cookie: {self.csrf[:24]}...", "OK")
                return True
        log("Logged in but failed to get CSRF token!", "!!")
        return False
    async def _refresh_csrf(self) -> bool:
        log("Refreshing CSRF...", "..")
        new = await self._fetch_csrf(f"{BASE}/idle-earning")
        if new:
            self.csrf = new
            log(f"New CSRF: {self.csrf[:24]}...", "OK")
            return True
        return False
    async def idle_tick(self):
        try:
            async with self.session.post(
                f"{BASE}/api/idle-tick",
                headers=self._api_hdrs(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                try:    body = await r.json(content_type=None)
                except: body = await r.text()
                return r.status, body
        except asyncio.TimeoutError:
            return 0, "timeout"
        except Exception as e:
            return 0, str(e)
    def party_label(self, u):
        if u >= 17: return "Legendary"
        if u >= 9:  return "Mega Party"
        if u >= 5:  return "Super Party"
        if u >= 2:  return "Party"
        return "Solo"
    def print_status(self, ok, code):
        icon  = "[OK]" if ok else "[!!]"
        up    = fmt_up(time.time() - self.start_time)
        label = self.party_label(self.active_users)
        print(
            f"\r{icon} {up} | Tick:{self.tick_count} [{code}] "
            f"| +{self.total_earned} earned | Credits:{self.credits} "
            f"| {self.multiplier:.2f}x {label} ({self.active_users} online)   ",
            end="", flush=True,
        )
    async def run(self):
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=True),
            cookie_jar=jar,
        ) as sess:
            self.session = sess

            if not await self.login():
                log("Login failed, stopping.", "!!")
                return
            self.running    = True
            self.start_time = time.time()
            log("Idle earning started! Press [q] to stop", "OK")
            print()
            kb = asyncio.create_task(self._kb_loop())
            consecutive_fail = 0
            try:
                while self.running:
                    status, body = await self.idle_tick()
                    self.tick_count += 1
                    ok = status in (200, 201)
                    if ok and isinstance(body, dict):
                        if not body.get("cooldown"):
                            self.total_earned += body.get("earned", 0) or 0
                        self.credits      = body.get("credits",     self.credits)
                        self.multiplier   = body.get("multiplier",  self.multiplier)
                        self.active_users = body.get("activeUsers", self.active_users)
                        consecutive_fail  = 0
                    elif status == 419:
                        consecutive_fail += 1
                        log(f"CSRF expired (419), attempt {consecutive_fail}", "!!")
                        if await self._refresh_csrf():
                            await asyncio.sleep(2)
                            continue
                        if consecutive_fail >= 3:
                            log("Re-logging in...", "..")
                            if not await self.login():
                                self.running = False; break
                            consecutive_fail = 0
                    elif status == 401:
                        log("Session expired (401), re-logging in...", "!!")
                        if not await self.login():
                            self.running = False; break
                        consecutive_fail = 0
                    elif status == 429:
                        log("Rate limited (429), waiting 60s...", "!!")
                        await asyncio.sleep(60)
                        continue
                    elif not ok:
                        log(f"Tick error [{status}]: {str(body)[:150]}", "!!")
                    self.print_status(ok, status)
                    for _ in range(TICK_INTERVAL * 10):
                        if not self.running: break
                        await asyncio.sleep(0.1)
            except KeyboardInterrupt:
                print()
                log("Ctrl+C, stopping...", "..")
            finally:
                self.running = False
                if not kb.done():
                    kb.cancel()
                    try: await kb
                    except asyncio.CancelledError: pass
                print()
                print("=" * 55)
                log(f"Uptime:   {fmt_up(time.time() - self.start_time)}", "OK")
                log(f"Ticks:    {self.tick_count}", "OK")
                log(f"Earned:   +{self.total_earned} credits", "OK")
                log(f"Credits:  {self.credits}", "OK")
                print("=" * 55)
    async def _kb_loop(self):
        try:
            import termios, tty
            fd  = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            try:
                while self.running:
                    if select.select([sys.stdin], [], [], 0.1)[0]:
                        ch = sys.stdin.read(1).lower()
                        if ch == 'q':
                            termios.tcsetattr(fd, termios.TCSADRAIN, old)
                            print()
                            log("Stopped by user.", "..")
                            self.running = False
                            break
                    await asyncio.sleep(0.1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            while self.running:
                await asyncio.sleep(1)
async def main():
    p = argparse.ArgumentParser(description="altare.gg Idle Earning Bot")
    p.add_argument("--email",    default=EMAIL,    help="altare.gg login email")
    p.add_argument("--password", default=PASSWORD, help="Password")
    a = p.parse_args()
    if not a.email or not a.password:
        p.print_help()
        print("\n[!!] --email and --password are required\n")
        return
    await AltareBot(a.email, a.password).run()
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[..] stop.")
