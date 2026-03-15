#!/usr/bin/env python3
import asyncio, aiohttp, json, time, sys, argparse, termios, tty, select
from datetime import datetime

EMAIL    = "your@email.com"
PASSWORD = "yourpassword"

HEARTBEAT_INTERVAL = 29

API_BASE = "https://api.altare.sh"
WEB_BASE = "https://altare.sh"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")

WEBHOOK_URL     = ""
WEBHOOK_ENABLED = False

def ts():
    return datetime.now().strftime("%H:%M:%S")

def log(msg, tag=".."):
    print(f"\n[{ts()}] [{tag}] {msg}", flush=True)

def fmt_up(s):
    s = int(s)
    return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"

def cents_to_coins(cents):
    return round(cents / 100, 2)

class Bot:
    def __init__(self, email, password,
                 send_to=None, send_min=None,
                 webhook_url=None, webhook_on=False):
        self.email      = email
        self.password   = password
        self.token      = None
        self.tenant_id  = None
        self.session    = None
        self.running    = False
        self.hb_count   = 0
        self.start_time = None
        self.wallet     = 0.0
        self.sending    = False
        self.handle     = ""
        self.send_to    = send_to
        self.send_min   = send_min
        self.last_auto  = 0
        self.webhook_url  = webhook_url or WEBHOOK_URL
        self.webhook_on   = webhook_on or WEBHOOK_ENABLED
        self.last_webhook = 0
        self.last_daily_check = 0

    def hdrs(self):
        h = {
            "User-Agent":   UA,
            "Accept":       "application/json",
            "Content-Type": "application/json",
            "Origin":       WEB_BASE,
            "Referer":      WEB_BASE + "/",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

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
            ) as r:
                pass
        except:
            pass

    async def login(self):
        log(f"Login: {self.email}", ">>")
        s, b = await self.req("POST", f"{API_BASE}/api/auth/login",
                              json={"identifier": self.email,
                                    "password":   self.password})
        if s == 200 and isinstance(b, dict):
            tok = (b.get("token") or b.get("access_token")
                   or (b.get("data") or {}).get("token")
                   or (b.get("data") or {}).get("access_token"))
            if tok:
                self.token = tok
                log(f"Login OK  token: {tok[:20]}...", "OK")
                return True
            log(f"No token found. Body: {json.dumps(b)[:200]}", "!!")
        else:
            log(f"Login failed [{s}]: {str(b)[:200]}", "!!")
        return False

    async def get_tenant(self):
        for url in [f"{WEB_BASE}/api/tenants", f"{API_BASE}/api/tenants"]:
            s, b = await self.req("GET", url)
            if s != 200:
                continue
            tenants = b if isinstance(b, list) else []
            if isinstance(b, dict):
                for k in ("data", "tenants", "items", "results"):
                    if k in b and isinstance(b[k], list):
                        tenants = b[k]; break
                if not tenants:
                    tenants = [b]
            if tenants:
                t = tenants[0]
                for f in ("id", "uuid", "tenant_id", "tenantId", "_id"):
                    v = t.get(f)
                    if v and str(v) not in ("None", ""):
                        self.tenant_id = str(v); break
                if self.tenant_id:
                    log(f"Tenant: {t.get('name','?')}  {self.tenant_id}", "OK")
                    return True
        log("Could not retrieve tenant", "!!")
        return False

    async def fetch_wallet(self):
        s, b = await self.req("GET",
            f"{API_BASE}/api/tenants/{self.tenant_id}/wallet")
        if s == 200 and isinstance(b, dict):
            raw = b.get("data", b) if isinstance(b.get("data"), dict) else b
            if "balanceCents" in raw:
                self.wallet = cents_to_coins(raw["balanceCents"])
            for k in ("balance", "coins", "amount", "total", "credits"):
                if k in raw and raw[k] is not None:
                    self.wallet = float(raw[k]); break
            pe = raw.get("paymentsEnabled")
            self.handle = raw.get("handle", "")
            if pe is False and not getattr(self, "_pe_warned", False):
                self._pe_warned = True
                log("!! paymentsEnabled=False - transfers disabled! Enable it in Settings.", "!!")
            return self.wallet
        return self.wallet

    async def fetch_rewards_info(self):
        for base in [WEB_BASE, API_BASE]:
            s, b = await self.req("GET",
                f"{base}/api/tenants/{self.tenant_id}/rewards")
            if s == 200 and isinstance(b, dict):
                return b
        return {}

    async def do_daily_claim(self):
        rewards = await self.fetch_rewards_info()
        daily   = rewards.get("daily") or rewards.get("data", {}).get("daily") or {}
        if not daily:
            return
        can_claim = daily.get("canClaim", False)
        streak    = daily.get("currentStreak", 0)
        next_str  = daily.get("nextStreak", 0)
        total_c   = daily.get("totalRewardCents", 0)
        if not can_claim:
            log(f"Daily: not claimable yet | Streak={streak}", "..")
            return
        log(f"Daily claim! Streak {streak}->{next_str} "
            f"| +{cents_to_coins(total_c):.2f} coins...", ">>")
        for base in [WEB_BASE, API_BASE]:
            s, b = await self.req("POST",
                f"{base}/api/tenants/{self.tenant_id}/rewards/daily/claim",
                json={})
            if s in (200, 201, 204):
                await self.fetch_wallet()
                msg = (f"[AFK] Daily claim OK! Streak: {next_str} "
                       f"| +{cents_to_coins(total_c):.2f} coins "
                       f"| Wallet: {self.wallet:.2f} coins")
                log(msg, "OK")
                await self.webhook(msg)
                return
        log("Daily claim failed.", "!!")

    async def daily_loop(self):
        await self.do_daily_claim()
        while self.running:
            await asyncio.sleep(86400)
            if self.running:
                await self.do_daily_claim()

    async def afk_start(self):
        s, b = await self.req("POST",
            f"{WEB_BASE}/api/tenants/{self.tenant_id}/rewards/afk/start",
            json={})
        if s in (200, 201, 204):
            self.start_time = self.start_time or time.time()
            log("AFK start OK", "OK"); return True
        if s == 409:
            self.start_time = self.start_time or time.time()
            log("AFK already active, continuing", "OK"); return True
        log(f"AFK start error [{s}]: {str(b)[:150]}", "!!")
        return False

    async def afk_heartbeat(self):
        s, b = await self.req("POST",
            f"{WEB_BASE}/api/tenants/{self.tenant_id}/rewards/afk/heartbeat",
            json={})
        return s in (200, 201, 204), s

    async def afk_stop(self):
        s, b = await self.req("POST",
            f"{WEB_BASE}/api/tenants/{self.tenant_id}/rewards/afk/stop",
            json={})
        log(f"AFK stop [{s}]", "..")

    async def transfer(self, recipient, amount, note="bot send"):
        if self.sending: return False, "Already processing"
        self.sending = True
        handle    = recipient if recipient.startswith("@") else f"@{recipient}"
        amt_cents = int(amount * 100)
        ep = f"{API_BASE}/api/tenants/{self.tenant_id}/wallet/transfer"
        payload = {"to": handle, "amountCents": amt_cents}
        try:
            s, b = await self.req("POST", ep, json=payload)
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
            log(f"Auto-send {amount} coins -> {self.send_to}", ">>")
            ok, r = await self.transfer(self.send_to, amount, "auto-send")
            if ok:
                log("Auto-send OK", "OK")
                await self.fetch_wallet()
                await self.webhook(
                    f"[AFK] Auto-send {amount} coins -> {self.send_to} "
                    f"| Wallet: {self.wallet:.2f} coins")
            else:
                log(f"Auto-send error: {r}", "!!")

    async def manual_send(self):
        try:
            fd  = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except: pass
        print("\n" + "="*48)
        print("  SEND COINS")
        print("="*48)
        print(f"  Current wallet: {self.wallet:.2f} coins\n")
        try:
            to = input("  Recipient (@user or email): ").strip()
            if not to: print("  Cancelled."); return
            try:
                amt = float(input("  Amount: ").strip())
                if amt <= 0 or amt > self.wallet:
                    print(f"  Invalid amount (max {self.wallet:.2f}). Cancelled.")
                    return
            except ValueError:
                print("  Invalid amount. Cancelled."); return
            note = input("  Note (press Enter to skip): ").strip()
            print(f"\n  -> {amt} coins to {to}")
            c = input("  Press 's' to confirm: ").strip().lower()
            if c == 's':
                ok, r = await self.transfer(to, amt, note or "manual send")
                if ok:
                    print("  [OK] Transfer successful!")
                    await self.fetch_wallet()
                    print(f"  New wallet: {self.wallet:.2f} coins")
                    await self.webhook(
                        f"[AFK] Manual send {amt} coins -> {to} "
                        f"| Wallet: {self.wallet:.2f} coins")
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
                        elif ch == 'd':
                            termios.tcsetattr(fd, termios.TCSADRAIN, old)
                            await self.do_daily_claim()
                            tty.setcbreak(fd)
                        elif ch == 'q':
                            termios.tcflush(fd, termios.TCIFLUSH)
                            print()
                            log("Pressed q, stopping...", "..")
                            self.running = False; break
                    await asyncio.sleep(0.1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass

    async def run(self):
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=True)
        ) as sess:
            self.session = sess
            if not await self.login():      return
            if not await self.get_tenant(): return
            await self.fetch_wallet()
            log(f"Wallet: {self.wallet:.2f} coins", "OK")
            rewards = await self.fetch_rewards_info()
            if rewards:
                afk   = rewards.get("afk") or {}
                daily = rewards.get("daily") or {}
                rate  = afk.get("ratePerMinuteCents", 0)
                log(f"AFK rate: {cents_to_coins(rate):.2f} coins/min "
                    f"({afk.get('activeCount', 0)} users AFK)", "OK")
                if daily:
                    can   = daily.get("canClaim", False)
                    streak= daily.get("currentStreak", 0)
                    tot   = daily.get("totalRewardCents", 0)
                    log(f"Daily: canClaim={can} | Streak={streak} "
                        f"| Reward={cents_to_coins(tot):.2f} coins", "OK")
            if not await self.afk_start(): return
            self.running  = True
            self.hb_count = 0
            if self.webhook_on and self.webhook_url:
                await self.webhook(f"[AFK] Bot started | Wallet: {self.wallet:.2f} coins")
            print()
            log("[s] send coins  [d] daily claim  [q] quit", "OK")
            if self.send_to:
                log(f"Auto-send: >= {self.send_min} coins -> {self.send_to}", "OK")
            if self.webhook_on:
                log("Webhook: ON", "OK")
            print()
            kb    = asyncio.create_task(self.keyboard_loop())
            daily = asyncio.create_task(self.daily_loop())
            try:
                while self.running:
                    ok, hbs = await self.afk_heartbeat()
                    self.hb_count += 1

                    if self.hb_count % 3 == 0:
                        await self.fetch_wallet()

                    if self.webhook_on and time.time() - self.last_webhook > 600:
                        self.last_webhook = time.time()
                        up = fmt_up(time.time() - self.start_time)
                        await self.webhook(
                            f"[AFK] Uptime: {up} | HB: {self.hb_count} "
                            f"| Wallet: {self.wallet:.2f} coins")

                    await self.check_auto_send()

                    up   = fmt_up(time.time() - self.start_time)
                    icon = "[OK]" if ok else "[!!]"
                    auto = (f" | Auto>={self.send_min}->{self.send_to}"
                            if self.send_to else "")
                    wh   = " | WH:ON" if self.webhook_on else ""
                    print(
                        f"\r{icon} {up} | HB:{self.hb_count} [{hbs}] "
                        f"| Wallet:{self.wallet:.2f}{auto}{wh}    ",
                        end="", flush=True
                    )

                    if not ok:
                        log(f"HB error [{hbs}], restarting in 5s...", "!!")
                        await asyncio.sleep(5)
                        await self.afk_stop()

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
                for task in [kb, daily]:
                    if not task.done():
                        task.cancel()
                        try: await task
                        except asyncio.CancelledError: pass

                await self.afk_stop()
                await self.fetch_wallet()

                if self.webhook_on and self.webhook_url:
                    up = fmt_up(time.time() - self.start_time)
                    await self.webhook(
                        f"[AFK] Bot stopped | Uptime: {up} "
                        f"| HB: {self.hb_count} | Wallet: {self.wallet:.2f} coins")

                print()
                print("="*50)
                log(f"Uptime:    {fmt_up(time.time()-self.start_time)}", "OK")
                log(f"Heartbeat: {self.hb_count}", "OK")
                log(f"Wallet:    {self.wallet:.2f} coins", "OK")
                print("="*50)

async def main():
    p = argparse.ArgumentParser(description="altare.sh AFK Bot")
    p.add_argument("email",        nargs="?", default=EMAIL)
    p.add_argument("password",     nargs="?", default=PASSWORD)
    p.add_argument("--u",          dest="send_to",  default=None,
                   help="Auto-send recipient when balance threshold is reached")
    p.add_argument("--c",          dest="send_min", type=float, default=None,
                   help="Auto-send threshold (coin amount)")
    p.add_argument("--webhook",    dest="webhook",  default=None,
                   help="Discord / webhook URL")
    p.add_argument("--no-webhook", dest="no_wh",   action="store_true",
                   help="Disable webhook even if WEBHOOK_URL is set in config")
    a = p.parse_args()
    if a.email == "your@email.com":
        p.print_help()
        print("\nExample:\n"
              "  python3 altare_afk.py email pass\n"
              "  python3 altare_afk.py email pass --u @someone --c 500\n"
              "  python3 altare_afk.py email pass --webhook https://discord.com/api/webhooks/...")
        return
    if bool(a.send_to) != bool(a.send_min):
        print("[!!] --u and --c must be used together"); return
    webhook_url = a.webhook or WEBHOOK_URL
    webhook_on  = bool(webhook_url) and not a.no_wh
    await Bot(a.email, a.password,
              a.send_to, a.send_min,
              webhook_url, webhook_on).run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[..] stop.")
