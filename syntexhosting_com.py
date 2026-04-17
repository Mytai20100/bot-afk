#!/usr/bin/env python3
import asyncio, aiohttp, json, time, sys, argparse, termios, tty, select
from datetime import datetime

# CONFIG 
EMAIL    = "your@email.com"
PASSWORD = "yourpassword"

WORK_INTERVAL      = 60        # seconds between each /billingafk/work POST
STATUS_INTERVAL    = 20        # seconds between status checks
MINUTES_PER_WORK   = 1         # minutes_afk value sent each work POST

API_BASE = "https://syntexhosting.com"
WEB_BASE = "https://syntexhosting.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36"
)

DAILY_CLAIM_ENABLED = False
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
    def __init__(self, email, password,
                 send_to=None, send_min=None,
                 webhook_url=None, webhook_on=False,
                 daily_claim=True):
        self.email      = email
        self.password   = password
        self.session    = None
        self.running    = False
        self.work_count = 0
        self.start_time = None
        self.wallet     = 0.0
        self.currency   = "Credits"
        self.sending    = False
        self.send_to    = send_to
        self.send_min   = send_min
        self.last_auto  = 0
        self.webhook_url  = webhook_url or WEBHOOK_URL
        self.webhook_on   = webhook_on  or WEBHOOK_ENABLED
        self.daily_claim  = daily_claim
        self.last_webhook = 0
        self.user_uuid    = ""
    # headers 
    def hdrs(self, referer=None):
        return {
            "User-Agent":         UA,
            "Accept":             "application/json, text/plain, */*",
            "Accept-Language":    "vi-VN,vi;q=0.9,fr-FR;q=0.8,fr;q=0.7,en-US;q=0.6,en;q=0.5",
            "Content-Type":       "application/json",
            "sec-ch-ua":          '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            "sec-ch-ua-mobile":   "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest":     "empty",
            "sec-fetch-mode":     "cors",
            "sec-fetch-site":     "same-origin",
            "Origin":             WEB_BASE,
            "Referer":            referer or (WEB_BASE + "/dashboard/earn/afk"),
            "priority":           "u=1, i",
        }
    # generic request 
    async def req(self, method, url, referer=None, **kw):
        try:
            async with self.session.request(
                method, url,
                headers=self.hdrs(referer),
                timeout=aiohttp.ClientTimeout(total=15),
                **kw
            ) as r:
                try:    body = await r.json(content_type=None)
                except: body = await r.text()
                return r.status, body
        except asyncio.TimeoutError: return 0, "timeout"
        except Exception as e:       return 0, str(e)
    # webhook 
    async def webhook(self, msg):
        if not self.webhook_on or not self.webhook_url:
            return
        try:
            async with self.session.post(
                self.webhook_url,
                json={"content": msg},
                timeout=aiohttp.ClientTimeout(total=8)
            ):
                pass
        except:
            pass
    # login 
    async def login(self):
        log(f"Logging in as {self.email} ...", ">>")
        s, b = await self.req(
            "PUT",
            f"{API_BASE}/api/user/auth/login",
            referer=WEB_BASE + "/auth/login",
            json={
                "username_or_email": self.email,
                "password":          self.password,
                "turnstile_token":   ""
            }
        )
        if s == 200 and isinstance(b, dict) and b.get("success", True) is not False:
            log("Login OK  (session cookie)", "OK")
            await self._fetch_session()
            return True

        log(f"Login failed [{s}]: {str(b)[:200]}", "!!")
        return False
    async def _fetch_session(self):
        s, b = await self.req(
            "GET", f"{API_BASE}/api/user/session",
            referer=WEB_BASE + "/auth/login"
        )
        if s == 200 and isinstance(b, dict):
            data = b.get("data") or b
            self.user_uuid = (
                data.get("uuid") or data.get("userUuid")
                or data.get("user", {}).get("uuid", "")
            )
            name = data.get("username") or data.get("email") or ""
            if self.user_uuid:
                log(f"Session OK  user={name}  uuid={self.user_uuid}", "OK")
    # fetch wallet → /api/user/billingcore/credits 
    async def fetch_wallet(self):
        s, b = await self.req(
            "GET",
            f"{API_BASE}/api/user/billingcore/credits",
            referer=f"{WEB_BASE}/dashboard/billing"
        )
        if s == 200 and isinstance(b, dict):
            data = b.get("data") or {}
            credits = data.get("credits")
            if credits is not None:
                self.wallet = float(credits)
                cur = data.get("currency") or {}
                self.currency = cur.get("code") or "Credits"
                return self.wallet
            log(f"[wallet] no 'credits' key — raw keys: {list(data.keys())}", "!!")
        else:
            log(f"[wallet] billingcore/credits [{s}]: {str(b)[:120]}", "!!")
        # fallback: session
        s, b = await self.req("GET", f"{API_BASE}/api/user/session",
                              referer=f"{WEB_BASE}/dashboard")
        if s == 200 and isinstance(b, dict):
            data = b.get("data") or b
            for k in ("credits", "balance", "coins", "balanceCents"):
                v = data.get(k)
                if v is not None:
                    self.wallet = float(v) / (100 if "cents" in k.lower() else 1)
                    return self.wallet

        return self.wallet
    # billingafk/status 
    async def afk_status(self):
        afk_ref = (
            f"{WEB_BASE}/components/billingafk//billingafk/dist/afk.html"
            + (f"?userUuid={self.user_uuid}" if self.user_uuid else "")
        )
        s, b = await self.req("GET", f"{API_BASE}/api/user/billingafk/status",
                              referer=afk_ref)
        return s, b
    # billingafk/work 
    async def afk_work(self):
        afk_ref = (
            f"{WEB_BASE}/components/billingafk//billingafk/dist/afk.html"
            + (f"?userUuid={self.user_uuid}" if self.user_uuid else "")
        )
        s, b = await self.req(
            "POST",
            f"{API_BASE}/api/user/billingafk/work",
            referer=afk_ref,
            json={"minutes_afk": MINUTES_PER_WORK}
        )
        return s, b
    # daily claim 
    async def do_daily_claim(self):
        if not self.daily_claim:
            log("Daily claim: DISABLED", "..")
            return
        s, b = await self.req("GET", f"{API_BASE}/api/user/daily/status")
        if s != 200 or not isinstance(b, dict):
            log(f"Daily status error [{s}]", "!!")
            return
        data = b.get("data") or b
        if not data.get("canClaim", False):
            log(f"Daily: not claimable yet | streak={data.get('streak', '?')}", "..")
            return
        s2, b2 = await self.req("POST", f"{API_BASE}/api/user/daily/claim", json={})
        if s2 in (200, 201, 204):
            await self.fetch_wallet()
            msg = f"[AFK] Daily claim OK! | Wallet: {self.wallet:.2f} {self.currency}"
            log(msg, "OK")
            await self.webhook(msg)
        else:
            log(f"Daily claim failed [{s2}]: {str(b2)[:150]}", "!!")

    async def daily_loop(self):
        await self.do_daily_claim()
        while self.running:
            await asyncio.sleep(86400)
            if self.running:
                await self.do_daily_claim()
    # auto-send 
    async def check_auto_send(self):
        if not self.send_to or not self.send_min:
            return
        if time.time() - self.last_auto < 60:
            return
        self.last_auto = time.time()
        await self.fetch_wallet()
        if self.wallet >= self.send_min:
            amount = self.wallet
            log(f"Auto-send {amount} {self.currency} -> {self.send_to}", ">>")
            ok, r = await self.transfer(self.send_to, amount)
            if ok:
                log("Auto-send OK", "OK")
                await self.fetch_wallet()
                await self.webhook(
                    f"[AFK] Auto-send {amount} {self.currency} -> {self.send_to} "
                    f"| Wallet: {self.wallet:.2f} {self.currency}")
            else:
                log(f"Auto-send error: {r}", "!!")
    async def transfer(self, recipient, amount, note="bot send"):
        if self.sending:
            return False, "Already processing"
        self.sending = True
        handle    = recipient if recipient.startswith("@") else f"@{recipient}"
        amt_cents = int(amount * 100)
        try:
            s, b = await self.req(
                "POST",
                f"{API_BASE}/api/user/wallet/transfer",
                json={"to": handle, "amountCents": amt_cents, "note": note}
            )
            if s in (200, 201):
                return True, b
            return False, f"[{s}] {str(b)[:200]}"
        except Exception as e:
            return False, str(e)
        finally:
            self.sending = False
    # manual send 
    async def manual_send(self):
        try:
            fd  = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except:
            pass
        print("\n" + "="*48)
        print("  SEND CREDITS")
        print("="*48)
        print(f"  Wallet: {self.wallet:.2f} {self.currency}\n")
        try:
            to = input("  Recipient (@user or email): ").strip()
            if not to:
                print("  Cancelled."); return
            try:
                amt = float(input("  Amount: ").strip())
                if amt <= 0 or amt > self.wallet:
                    print(f"  Invalid amount (max {self.wallet:.2f}). Cancelled.")
                    return
            except ValueError:
                print("  Invalid amount. Cancelled."); return
            note = input("  Note (Enter to skip): ").strip()
            c    = input(f"\n  Send {amt} {self.currency} to {to}? [s to confirm]: ").strip().lower()
            if c == 's':
                ok, r = await self.transfer(to, amt, note or "manual send")
                if ok:
                    print("  [OK] Sent!")
                    await self.fetch_wallet()
                    print(f"  New balance: {self.wallet:.2f} {self.currency}")
                    await self.webhook(
                        f"[AFK] Manual send {amt} {self.currency} -> {to} "
                        f"| Wallet: {self.wallet:.2f} {self.currency}")
                else:
                    print(f"  [!!] Error: {r}")
            else:
                print("  Cancelled.")
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled.")
        print("="*48 + "\n")
    # keyboard loop 
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
                        elif ch == 'd':
                            termios.tcsetattr(fd, termios.TCSADRAIN, old)
                            await self.do_daily_claim()
                            tty.setcbreak(fd)
                        elif ch == 'q':
                            print()
                            log("Pressed q, stopping...", "..")
                            self.running = False
                            break
                    await asyncio.sleep(0.1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass
    # main run loop 
    async def run(self):
        connector  = aiohttp.TCPConnector(ssl=True)
        cookie_jar = aiohttp.CookieJar()
        async with aiohttp.ClientSession(
            connector=connector,
            cookie_jar=cookie_jar
        ) as sess:
            self.session = sess

            if not await self.login():
                return

            await self.fetch_wallet()
            log(f"Wallet: {self.wallet:.2f} {self.currency}", "OK")

            st_code, st_body = await self.afk_status()
            if st_code == 200 and isinstance(st_body, dict):
                data = st_body.get("data") or st_body
                log(f"AFK status: active={data.get('active','?')} "
                    f"| earned={data.get('minutes_earned', data.get('coins_earned','?'))}",
                    "OK")
                if self.user_uuid == "" and data.get("userUuid"):
                    self.user_uuid = data["userUuid"]
            else:
                log(f"AFK status [{st_code}]: {str(st_body)[:150]}", "!!")
            self.running    = True
            self.work_count = 0
            self.start_time = time.time()

            if self.webhook_on and self.webhook_url:
                await self.webhook(
                    f"[AFK] Bot started | Wallet: {self.wallet:.2f} {self.currency}")
            print()
            log("[s] send credits  [d] daily claim  [q] quit", "OK")
            log(f"Daily claim: {'ON' if self.daily_claim else 'OFF'}", "OK")
            if self.send_to:
                log(f"Auto-send: >= {self.send_min} {self.currency} -> {self.send_to}", "OK")
            print()
            kb_task    = asyncio.create_task(self.keyboard_loop())
            daily_task = asyncio.create_task(self.daily_loop())
            last_status = time.time()
            last_work   = 0
            try:
                while self.running:
                    now = time.time()
                    # POST /billingafk/work every WORK_INTERVAL 
                    if now - last_work >= WORK_INTERVAL:
                        w_code, w_body = await self.afk_work()
                        self.work_count += 1
                        last_work = now
                        if w_code not in (200, 201, 204):
                            log(f"Work POST error [{w_code}]: {str(w_body)[:150]}", "!!")
                    # GET /billingafk/status every STATUS_INTERVAL 
                    if now - last_status >= STATUS_INTERVAL:
                        await self.afk_status()
                        last_status = now
                    if self.work_count > 0 and self.work_count % 3 == 0:
                        await self.fetch_wallet()
                    if self.webhook_on and now - self.last_webhook > 600:
                        self.last_webhook = now
                        up = fmt_up(now - self.start_time)
                        await self.webhook(
                            f"[AFK] Uptime: {up} | Work: {self.work_count} "
                            f"| Wallet: {self.wallet:.2f} {self.currency}")
                    await self.check_auto_send()
                    up   = fmt_up(time.time() - self.start_time)
                    auto = f" | Auto>={self.send_min}->{self.send_to}" if self.send_to else ""
                    wh   = " | WH:ON" if self.webhook_on else ""
                    print(
                        f"\r[OK] {up} | Work:{self.work_count} "
                        f"| Wallet:{self.wallet:.2f} {self.currency}{auto}{wh}    ",
                        end="", flush=True
                    )
                    await asyncio.sleep(5)
            except KeyboardInterrupt:
                print()
                log("Ctrl+C — shutting down...", "..")
            finally:
                self.running = False
                for task in [kb_task, daily_task]:
                    if not task.done():
                        task.cancel()
                        try:   await task
                        except asyncio.CancelledError: pass
                await self.fetch_wallet()
                if self.webhook_on and self.webhook_url:
                    up = fmt_up(time.time() - self.start_time)
                    await self.webhook(
                        f"[AFK] Bot stopped | Uptime: {up} "
                        f"| Work: {self.work_count} | Wallet: {self.wallet:.2f} {self.currency}")
                print()
                print("="*50)
                log(f"Uptime:     {fmt_up(time.time()-self.start_time)}", "OK")
                log(f"Work calls: {self.work_count}", "OK")
                log(f"Wallet:     {self.wallet:.2f} {self.currency}", "OK")
                print("="*50)
# CLI 
async def main():
    p = argparse.ArgumentParser(description="SyntexHosting AFK Bot (new API)")
    p.add_argument("email",        nargs="?", default=EMAIL)
    p.add_argument("password",     nargs="?", default=PASSWORD)
    p.add_argument("--u",          dest="send_to",  default=None,
                   help="Wallet handle to auto-send credits to")
    p.add_argument("--c",          dest="send_min", type=float, default=None,
                   help="Auto-send threshold (credits)")
    p.add_argument("--webhook",    dest="webhook",  default=None,
                   help="Discord / webhook URL")
    p.add_argument("--no-daily",   dest="no_daily", action="store_true",
                   help="Disable automatic daily claim")
    p.add_argument("--no-webhook", dest="no_wh",    action="store_true",
                   help="Disable webhook")
    a = p.parse_args()
    if a.email == "your@email.com":
        p.print_help()
        print("\nExamples:\n"
              "  python3 afk_bot_syntex.py email@example.com mypassword\n"
              "  python3 afk_bot_syntex.py email pass --u @someone --c 10\n"
              "  python3 afk_bot_syntex.py email pass "
              "--webhook https://discord.com/api/webhooks/...")
        return
    if bool(a.send_to) != bool(a.send_min):
        print("[!!] --u and --c must be used together")
        return
    webhook_url = a.webhook or WEBHOOK_URL
    webhook_on  = bool(webhook_url) and not a.no_wh
    daily_claim = DAILY_CLAIM_ENABLED and not a.no_daily
    await Bot(
        a.email, a.password,
        a.send_to, a.send_min,
        webhook_url, webhook_on,
        daily_claim
    ).run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[..] stop.")