#!/usr/bin/env python3
import asyncio, aiohttp, json, time, sys, argparse, termios, tty, select
from datetime import datetime
"""
altare_site.sh – dash.altare.app

Auth via cookie (after Discord login in browser).

Get cookie:
1. Login at https://dash.altare.app
2. Open DevTools (F12) → Network
3. Find request to api-india.altare.app
4. Copy "Cookie" header value
5. Use with --cookie "..." or set COOKIE variable
"""

COOKIE = ""

HEARTBEAT_INTERVAL = 29

API_BASE  = "https://api-india.altare.app"
DASH_BASE = "https://dash.altare.app"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

WEBHOOK_URL     = ""
WEBHOOK_ENABLED = False


def ts():
    return datetime.now().strftime("%H:%M:%S")

def log(msg, tag=".."):
    print(f"\n[{ts()}] [{tag}] {msg}", flush=True)

def fmt_up(s):
    s = int(s)
    return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"


class Bot:
    def __init__(self, cookie,
                 send_to=None, send_min=None,
                 webhook_url=None, webhook_on=False):
        self.cookie      = cookie
        self.session     = None
        self.running     = False
        self.hb_count    = 0
        self.start_time  = None
        self.wallet      = 0.0
        self.session_id  = None
        self.sending     = False
        self.send_to     = send_to
        self.send_min    = send_min
        self.last_auto   = 0
        self.webhook_url = webhook_url or WEBHOOK_URL
        self.webhook_on  = webhook_on or WEBHOOK_ENABLED
        self.last_webhook= 0

    def hdrs(self):
        return {
            "User-Agent":         UA,
            "Accept":             "application/json, */*",
            "Content-Type":       "application/json",
            "Origin":             DASH_BASE,
            "Referer":            DASH_BASE + "/",
            "Cookie":             self.cookie,
            "sec-ch-ua":          '"Not:A-Brand";v="99", "Google Chrome";v="145"',
            "sec-ch-ua-mobile":   "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest":     "empty",
            "sec-fetch-mode":     "cors",
            "sec-fetch-site":     "same-site",
            "priority":           "u=1, i",
        }

    async def req(self, method, url, **kw):
        try:
            async with self.session.request(
                method, url,
                headers=self.hdrs(),
                timeout=aiohttp.ClientTimeout(total=15),
                **kw
            ) as r:
                try:    body = await r.json(content_type=None)
                except: body = await r.text()
                return r.status, body
        except asyncio.TimeoutError: return 0, "timeout"
        except Exception as e:       return 0, str(e)

    async def webhook(self, msg):
        if not self.webhook_on or not self.webhook_url:
            return
        try:
            async with self.session.post(
                self.webhook_url,
                json={"content": msg},
                timeout=aiohttp.ClientTimeout(total=8)
            ) as r: pass
        except: pass

    async def afk_start(self):
        s, b = await self.req(
            "POST", f"{API_BASE}/api/afk/sessions/start",
            json={"hashrate": 10}
        )
        if s in (200, 201):
            sid = None
            if isinstance(b, dict):
                sid = (b.get("id") or b.get("sessionId")
                       or b.get("session_id")
                       or (b.get("data") or {}).get("id"))
            if sid:
                self.session_id = str(sid)
                self.start_time = self.start_time or time.time()
                log(f"AFK start OK | session: {self.session_id}", "OK")
                return True
            log("Start OK but no session_id, trying /current...", "..")
            return await self.afk_get_current()
        if s == 409:
            log("AFK already active, fetching current session...", "OK")
            return await self.afk_get_current()
        log(f"AFK start error [{s}]: {str(b)[:200]}", "!!")
        return False

    async def afk_get_current(self):
        s, b = await self.req("GET", f"{API_BASE}/api/afk/sessions/current")
        if s == 200 and isinstance(b, dict):
            raw = b.get("data", b)
            if isinstance(raw, dict) and "session" in raw:
                raw = raw["session"]
            sid = (raw.get("id") or raw.get("sessionId") or raw.get("session_id"))
            if sid:
                self.session_id = str(sid)
                self.start_time = self.start_time or time.time()
                log(f"Current session: {self.session_id}", "OK")
                return True
        log(f"Could not retrieve current session [{s}]: {str(b)[:150]}", "!!")
        return False

    async def afk_heartbeat(self):
        if not self.session_id:
            return False, 0
        s, b = await self.req(
            "POST",
            f"{API_BASE}/api/afk/sessions/{self.session_id}/heartbeat"
        )
        return s in (200, 201, 204), s

    async def afk_stop(self):
        if not self.session_id:
            return
        s, b = await self.req(
            "POST",
            f"{API_BASE}/api/afk/sessions/{self.session_id}/stop",
            json={}
        )
        log(f"AFK stop [{s}]", "..")
        await self.afk_claim()

    async def afk_claim(self):
        if not self.session_id:
            log("No session_id to claim", "!!")
            return False

        endpoints = [
            f"/api/afk/sessions/{self.session_id}/claim",
            f"/api/afk/sessions/claim",
            f"/api/afk/rewards/claim",
            f"/api/afk/claim",
        ]
        for path in endpoints:
            s, b = await self.req("POST", f"{API_BASE}{path}", json={})
            if s in (200, 201):
                reward = 0
                if isinstance(b, dict):
                    raw = b.get("data", b) if isinstance(b.get("data"), dict) else b
                    for k in ("reward", "amount", "coins", "earned", "balance"):
                        if k in raw and raw[k] is not None:
                            reward = float(raw[k])
                            break
                await self.fetch_wallet()
                log(f"Claim OK! +{reward} | Wallet: {self.wallet:.2f}", "OK")
                if self.webhook_on and self.webhook_url:
                    await self.webhook(
                        f"[AFK] Claim successful! +{reward} "
                        f"| Wallet: {self.wallet:.2f}")
                return True
            elif s == 404:
                continue
            elif s == 409:
                log(f"Claim [{s}]: {str(b)[:120]} (may have already been claimed)", "..")
                return False
            else:
                log(f"Claim [{s}] at {path}: {str(b)[:120]}", "!!")

        log("No suitable claim endpoint found (tried all paths)", "!!")
        return False

    async def fetch_wallet(self):
        for path in ["/api/wallet", "/api/user/wallet", "/api/users/me"]:
            s, b = await self.req("GET", f"{API_BASE}{path}")
            if s == 200 and isinstance(b, dict):
                raw = b.get("data", b) if isinstance(b.get("data"), dict) else b
                for k in ("balance", "balanceCents", "coins", "amount", "credits"):
                    if k in raw and raw[k] is not None:
                        v = float(raw[k])
                        self.wallet = round(v / 100, 2) if k == "balanceCents" else v
                        return self.wallet
        return self.wallet

    async def fetch_afk_history(self):
        s, b = await self.req("GET", f"{API_BASE}/api/afk/history")
        if s == 200:
            return b
        return {}

    async def transfer(self, recipient, amount, note="bot send"):
        if self.sending: return False, "Already processing"
        self.sending = True
        try:
            s, b = await self.req(
                "POST", f"{API_BASE}/api/wallet/transfer",
                json={"to": recipient, "amount": amount, "note": note}
            )
            if s in (200, 201):
                return True, b
            return False, f"[{s}] {str(b)[:200]}"
        except Exception as e:
            return False, str(e)
        finally:
            self.sending = False

    async def check_auto_send(self):
        if not self.send_to or not self.send_min: return
        if time.time() - self.last_auto < 60: return
        self.last_auto = time.time()
        await self.fetch_wallet()
        if self.wallet >= self.send_min:
            amount = self.wallet
            log(f"Auto-send {amount} -> {self.send_to}", ">>")
            ok, r = await self.transfer(self.send_to, amount)
            if ok:
                log("Auto-send OK", "OK")
                await self.fetch_wallet()
                await self.webhook(
                    f"[AFK] Auto-send {amount} -> {self.send_to} "
                    f"| Wallet: {self.wallet:.2f}")
            else:
                log(f"Auto-send error: {r}", "!!")

    async def manual_send(self):
        try:
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except: pass
        print("\n" + "="*48)
        print("  SEND COINS")
        print("="*48)
        print(f"  Current wallet: {self.wallet:.2f}\n")
        try:
            to = input("  Recipient (@user or username): ").strip()
            if not to: print("  Cancelled."); return
            try:
                amt = float(input("  Amount: ").strip())
                if amt <= 0:
                    print("  Invalid amount. Cancelled."); return
            except ValueError:
                print("  Invalid amount. Cancelled."); return
            note = input("  Note (press Enter to skip): ").strip()
            print(f"\n  -> {amt} to {to}")
            c = input("  Press 's' to confirm: ").strip().lower()
            if c == 's':
                ok, r = await self.transfer(to, amt, note or "manual send")
                if ok:
                    print("  [OK] Transfer successful!")
                    await self.fetch_wallet()
                    print(f"  New wallet: {self.wallet:.2f}")
                    await self.webhook(
                        f"[AFK] Manual send {amt} -> {to} "
                        f"| Wallet: {self.wallet:.2f}")
                else:
                    print(f"  [!!] Error: {r}")
            else:
                print("  Cancelled.")
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled.")
        print("="*48 + "\n")

    async def keyboard_loop(self):
        try:
            fd  = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            try:
                while self.running:
                    if select.select([sys.stdin], [], [], 0.1)[0]:
                        ch = sys.stdin.read(1).lower()
                        if ch == 's':
                            termios.tcsetattr(fd, termios.TCSADRAIN, old)
                            await self.manual_send()
                            tty.setcbreak(fd)
                        elif ch == 'c':
                            termios.tcsetattr(fd, termios.TCSADRAIN, old)
                            log("Claiming coins...", ">>")
                            await self.afk_claim()
                            tty.setcbreak(fd)
                        elif ch == 'r':
                            termios.tcsetattr(fd, termios.TCSADRAIN, old)
                            log("Refreshing session...", "..")
                            await self.afk_get_current()
                            tty.setcbreak(fd)
                        elif ch == 'q':
                            termios.tcflush(fd, termios.TCIFLUSH)
                            print()
                            log("Pressed q, stopping...", "..")
                            self.running = False
                            break
                    await asyncio.sleep(0.1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass

    async def run(self):
        if not self.cookie:
            log("No cookie provided! Add --cookie '...' when running.", "!!")
            log("How to get cookie: F12 -> Network -> any request to api-india.altare.app -> Copy Cookie header", "..")
            return

        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=True),
            cookie_jar=jar
        ) as sess:
            self.session = sess

            log("Checking AFK session...", ">>")
            if not await self.afk_start():
                log("Could not start AFK. Check your cookie.", "!!")
                return

            await self.fetch_wallet()
            log(f"Wallet: {self.wallet:.2f}", "OK")

            hist = await self.fetch_afk_history()
            if isinstance(hist, (list, dict)):
                log(f"History: {str(hist)[:120]}", "OK")

            self.running  = True
            self.hb_count = 0

            if self.webhook_on and self.webhook_url:
                await self.webhook(
                    f"[AFK] Bot started | Session: {self.session_id} "
                    f"| Wallet: {self.wallet:.2f}")

            print()
            log("[s] send coins  [c] claim  [r] refresh session  [q] stop+claim+quit", "OK")
            if self.send_to:
                log(f"Auto-send: >= {self.send_min} -> {self.send_to}", "OK")
            print()

            kb = asyncio.create_task(self.keyboard_loop())
            try:
                while self.running:
                    ok, hbs = await self.afk_heartbeat()
                    self.hb_count += 1

                    if self.hb_count % 5 == 0:
                        await self.fetch_wallet()

                    if self.webhook_on and time.time() - self.last_webhook > 600:
                        self.last_webhook = time.time()
                        up = fmt_up(time.time() - self.start_time)
                        await self.webhook(
                            f"[AFK] Uptime: {up} | HB: {self.hb_count} "
                            f"| Wallet: {self.wallet:.2f}")

                    await self.check_auto_send()

                    up   = fmt_up(time.time() - self.start_time)
                    icon = "[OK]" if ok else "[!!]"
                    auto = (f" | Auto>={self.send_min}->{self.send_to}"
                            if self.send_to else "")
                    wh   = " | WH:ON" if self.webhook_on else ""
                    print(
                        f"\r{icon} {up} | HB:{self.hb_count} [{hbs}] "
                        f"| Wallet:{self.wallet:.2f}{auto}{wh}   ",
                        end="", flush=True
                    )

                    if not ok:
                        log(f"HB error [{hbs}], refreshing session...", "!!")
                        await asyncio.sleep(5)
                        retry = 0
                        while self.running:
                            if await self.afk_start():
                                log("AFK restart successful!", "OK")
                                break
                            retry += 1
                            log(f"Retry {retry}/10, waiting 60s...", "!!")
                            if retry >= 10:
                                log("Exceeded 10 retries, stopping bot.", "!!")
                                self.running = False
                                break
                            await asyncio.sleep(60)
                        continue

                    await asyncio.sleep(HEARTBEAT_INTERVAL)

            except KeyboardInterrupt:
                print()
                log("Ctrl+C, stopping...", "..")

            finally:
                self.running = False
                if not kb.done():
                    kb.cancel()
                    try: await kb
                    except asyncio.CancelledError: pass

                await self.afk_stop()
                await self.fetch_wallet()

                if self.webhook_on and self.webhook_url:
                    up = fmt_up(time.time() - self.start_time)
                    await self.webhook(
                        f"[AFK] Bot stopped | Uptime: {up} "
                        f"| HB: {self.hb_count} | Wallet: {self.wallet:.2f}")

                print()
                print("="*50)
                log(f"Uptime:    {fmt_up(time.time()-self.start_time)}", "OK")
                log(f"Heartbeat: {self.hb_count}", "OK")
                log(f"Wallet:    {self.wallet:.2f}", "OK")
                print("="*50)


async def main():
    p = argparse.ArgumentParser(
        description="altare.sh AFK Bot v2 (dash.altare.app)",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Example:\n"
            "  python3 altare_site_afk.py --cookie 'cf_clearance=xxx; session=yyy'\n"
            "  python3 altare_site_afk.py --cookie '...' --u @someone --c 500\n"
            "  python3 altare_site_afk.py --cookie '...' --webhook https://discord.com/api/webhooks/...\n\n"
            "How to get cookie:\n"
            "  1. Open https://dash.altare.app, login with Discord\n"
            "  2. F12 -> Network -> click any request to api-india.altare.app\n"
            "  3. Scroll down to Headers -> Request Headers -> Copy the full 'Cookie' value\n"
            "  4. Paste into --cookie '...' (inside quotes)\n"
        )
    )
    p.add_argument("--cookie",     default=COOKIE,
                   help="Browser cookie (required if not set in script)")
    p.add_argument("--u",          dest="send_to",  default=None,
                   help="Auto-send: recipient username")
    p.add_argument("--c",          dest="send_min", type=float, default=None,
                   help="Auto-send: balance threshold to trigger send")
    p.add_argument("--webhook",    default=None,
                   help="Discord webhook URL")
    p.add_argument("--no-webhook", dest="no_wh", action="store_true",
                   help="Disable webhook")
    a = p.parse_args()

    if not a.cookie:
        p.print_help()
        print("\n[!!] Missing --cookie. See instructions above.\n")
        return

    if bool(a.send_to) != bool(a.send_min):
        print("[!!] --u and --c must be used together")
        return

    webhook_url = a.webhook or WEBHOOK_URL
    webhook_on  = bool(webhook_url) and not a.no_wh

    await Bot(
        a.cookie,
        a.send_to, a.send_min,
        webhook_url, webhook_on
    ).run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[..] stop.")
