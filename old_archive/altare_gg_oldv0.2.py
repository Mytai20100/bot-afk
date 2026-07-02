#!/usr/bin/env python3
import asyncio, aiohttp, argparse, sys, time, select, re, urllib.parse, random, string
from datetime import datetime

EMAIL    = ""
PASSWORD = ""
DAILY_AFK = False

TICK_INTERVAL = 29
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
    def __init__(self, email, password, bug_mode=False, team_name="FarmTeam", user_handle=None, random_suffix=False, daily_afk=False):
        self.email    = email.strip()
        self.password = password.strip()
        self.bug_mode = bug_mode
        self.team_name = team_name
        self.user_handle = user_handle
        self.random_suffix = random_suffix
        self.daily_afk = daily_afk
        self.session  = None
        self.csrf     = ""
        self.running  = False
        self.tick_count   = 0
        self.total_earned = 0
        self.credits      = 0
        self.multiplier   = 1.0
        self.active_users = 1
        self.start_time   = None
        self.original_tenant_id = None
        self.farm_count = 0
        self.afk_started = False
        self.current_team_name = "Unknown"
        self.last_balance_update = 0
    def _base_hdrs(self):
        return {
            "User-Agent":         UA,
            "Accept-Language":    "en-US,en;q=0.9",
            "sec-ch-ua":          '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
            "sec-ch-ua-mobile":   "?0",
            "sec-ch-ua-platform": '"Windows"',
        }
    def _api_hdrs(self, referer=None):
        return {
            **self._base_hdrs(),
            "Accept":           "*/*",
            "Content-Type":     "application/json",
            "Referer":          referer or f"{BASE}/billing/rewards/afk",
            "authorization":    f"Bearer {self.csrf}",
            "sec-fetch-dest":   "empty",
            "sec-fetch-mode":   "cors",
            "sec-fetch-site":   "same-origin",
            "priority":         "u=1, i",
        }
    async def login(self) -> bool:
        log("Logging in...", "..")
        payload = {
            "identifier": self.email,
            "password":   self.password,
        }
        try:
            async with self.session.post(
                f"{BASE}/api/auth/login",
                json=payload,
                headers={
                    **self._base_hdrs(),
                    "Accept":         "application/json",
                    "Content-Type":   "application/json",
                    "Referer":        f"{BASE}/login",
                    "sec-fetch-dest": "empty",
                    "sec-fetch-mode": "cors",
                    "sec-fetch-site": "same-origin",
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                status = r.status
                try:
                    body = await r.json()
                except:
                    body = await r.text()
        except Exception as e:
            log(f"Login request failed: {e}", "!!")
            return False
        if status == 200:
            if isinstance(body, dict):
                token = body.get("token") or body.get("access_token") or body.get("bearer")
                if token:
                    self.csrf = token
                    log(f"Logged in! Token: {token[:30]}...", "OK")
                    return True
            log("Login successful but no token in response", "!!")
            log(f"Response: {str(body)[:200]}", "!!")
            return False
        elif status == 401:
            log("Wrong email or password!", "!!")
            return False
        elif status == 429:
            log("Too many attempts, waiting 30s...", "!!")
            await asyncio.sleep(30)
            return await self.login()
        else:
            log(f"Login failed (status {status}): {str(body)[:200]}", "!!")
            return False
    async def _get_tenant_id(self) -> str:
        try:
            async with self.session.get(
                f"{BASE}/api/tenants",
                headers=self._api_hdrs(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status != 200:
                    return ""
                data = await r.json()
                if isinstance(data, dict) and "items" in data:
                    items = data["items"]
                    if items and len(items) > 0:
                        self.current_team_name = items[0].get("name", "Unknown")
                        return items[0].get("id", "")
                elif isinstance(data, list) and len(data) > 0:
                    self.current_team_name = data[0].get("name", "Unknown")
                    return data[0].get("id", "")
                elif isinstance(data, dict):
                    self.current_team_name = data.get("name", "Unknown")
                    return data.get("id", "")
        except Exception:
            pass
        return ""
    async def claim_daily_reward(self):
        log("Claiming daily reward...", "..")
        tenant_id = await self._get_tenant_id()
        if not tenant_id:
            log("Failed to get tenant_id, skipping daily reward", "!!")
            return False
        try:
            async with self.session.post(
                f"{BASE}/api/tenants/{tenant_id}/rewards/claim",
                headers=self._api_hdrs(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    if data.get("ok"):
                        reward = data.get("totalRewardCents", 0)
                        streak = data.get("newStreak", 0)
                        balance = data.get("balanceCents", 0)
                        log(f"Daily reward claimed! +{reward} cents | Streak: {streak} | Balance: {balance}", "OK")
                        return True
                elif r.status == 400:
                    body = await r.text()
                    if "already claimed" in body.lower() or "cooldown" in body.lower():
                        log("Daily reward already claimed today", "..")
                    else:
                        log(f"Daily reward error: {body[:100]}", "!!")
                else:
                    log(f"Daily reward failed: {r.status}", "!!")
        except Exception as e:
            log(f"Error claiming daily: {e}", "!!")
        return False
    async def create_team(self, name: str) -> str:
        rand_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
        team_name = f"{name}_{rand_suffix}"
        log(f"Creating team: {team_name}", "..")
        try:
            async with self.session.post(
                f"{BASE}/api/tenants",
                json={"name": team_name},
                headers=self._api_hdrs(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status in (200, 201):
                    data = await r.json()
                    tenant_id = data.get("id", "")
                    if tenant_id:
                        log(f"Team created: {team_name} (ID: {tenant_id[:20]}...)", "OK")
                        return tenant_id
                else:
                    body = await r.text()
                    log(f"Failed to create team [{r.status}]: {body[:100]}", "!!")
        except Exception as e:
            log(f"Error creating team: {e}", "!!")
        return ""
    async def switch_team(self, tenant_id: str) -> bool:
        log(f"Switching to team: {tenant_id[:20]}...", "..")
        try:
            async with self.session.post(
                f"{BASE}/api/user/switch-tenant",
                json={"tenantId": tenant_id},
                headers=self._api_hdrs(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status == 200:
                    log("Team switched", "OK")
                    return True
                else:
                    log(f"Switch failed: {r.status}", "!!")
        except Exception as e:
            log(f"Error switching team: {e}", "!!")
        return False
    async def enable_payments(self, tenant_id: str) -> bool:
        log("Enabling payments for team...", "..")
        try:
            async with self.session.put(
                f"{BASE}/api/tenants/{tenant_id}",
                json={"paymentsEnabled": True},
                headers=self._api_hdrs(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status in (200, 204):
                    log("Payments enabled", "OK")
                    return True
                else:
                    body = await r.text()
                    log(f"Enable payments failed [{r.status}]: {body[:100]}", "!!")
        except Exception as e:
            log(f"Error enabling payments: {e}", "!!")
        return False
    async def delete_team(self, tenant_id: str) -> bool:
        log(f"Deleting team: {tenant_id[:20]}...", "..")
        if hasattr(self, 'original_tenant_id') and self.original_tenant_id and tenant_id != self.original_tenant_id:
            try:
                teams = await self.list_teams()
                team_to_delete = None
                for t in teams:
                    if t.get("id") == tenant_id:
                        team_to_delete = t
                        break
                if team_to_delete:
                    balance = team_to_delete.get("creditsCents", 0)
                    if balance > 0:
                        log(f"  Transferring {balance} cents to main account first...", "..")
                        await self.transfer_credits(tenant_id, self.original_tenant_id, balance)
                        await asyncio.sleep(2)
            except Exception as e:
                log(f"  Error transferring before delete: {e}", "!!")
        try:
            async with self.session.delete(
                f"{BASE}/api/tenants/{tenant_id}",
                headers=self._api_hdrs(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status in (200, 204):
                    log("Team deleted", "OK")
                    return True
                else:
                    body = await r.text()
                    log(f"Delete failed [{r.status}]: {body[:100]}", "!!")
        except Exception as e:
            log(f"Error deleting team: {e}", "!!")
        return False
    async def list_teams(self) -> list:
        try:
            async with self.session.get(
                f"{BASE}/api/tenants",
                headers=self._api_hdrs(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    if isinstance(data, dict) and "items" in data:
                        return data["items"]
                    elif isinstance(data, list):
                        return data
        except Exception as e:
            log(f"Error listing teams: {e}", "!!")
        return []
    async def claim_daily_for_team(self, tenant_id: str, team_name: str = "Team") -> bool:
        log(f"Claiming daily for {team_name}...", "..")
        try:
            async with self.session.post(
                f"{BASE}/api/tenants/{tenant_id}/rewards/claim",
                headers=self._api_hdrs(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    if data.get("ok"):
                        reward = data.get("totalRewardCents", 0)
                        streak = data.get("newStreak", 0)
                        balance = data.get("balanceCents", 0)
                        log(f"Daily claimed for {team_name}! +{reward} cents | Streak: {streak} | Balance: {balance}", "OK")
                        return True
                elif r.status == 400:
                    body = await r.text()
                    if "already claimed" in body.lower() or "cooldown" in body.lower():
                        log(f"{team_name}: Already claimed today", "..")
                    else:
                        log(f"{team_name}: Daily error: {body[:100]}", "!!")
                else:
                    log(f"{team_name}: Daily failed: {r.status}", "!!")
        except Exception as e:
            log(f"{team_name}: Error claiming daily: {e}", "!!")
        return False
    async def set_team_handle(self, tenant_id: str, handle: str) -> bool:
        log(f"Setting handle '{handle}' for team...", "..")
        try:
            async with self.session.put(
                f"{BASE}/api/tenants/{tenant_id}",
                json={"handle": handle},
                headers=self._api_hdrs(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status in (200, 204):
                    log("Handle set", "OK")
                    return True
                else:
                    body = await r.text()
                    log(f"Set handle failed [{r.status}]: {body[:100]}", "!!")
        except Exception as e:
            log(f"Error setting handle: {e}", "!!")
        return False
    async def get_wallet_info(self, tenant_id: str) -> dict:
        """Get wallet information including balance and handle"""
        try:
            async with self.session.get(
                f"{BASE}/api/tenants/{tenant_id}/wallet",
                headers=self._api_hdrs(referer=f"{BASE}/billing/credits/transactions"),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status == 200:
                    return await r.json()
        except Exception as e:
            log(f"Error getting wallet info: {e}", "!!")
        return {}
    
    async def update_wallet_settings(self, tenant_id: str, handle: str, payments_enabled: bool = True) -> bool:
        log(f"Setting wallet handle '{handle}'...", "..")
        try:
            async with self.session.patch(
                f"{BASE}/api/tenants/{tenant_id}/wallet/settings",
                json={"paymentsEnabled": payments_enabled, "handle": handle},
                headers=self._api_hdrs(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status in (200, 204):
                    log("Wallet handle set", "OK")
                    return True
                else:
                    body = await r.text()
                    log(f"Set wallet handle failed [{r.status}]: {body[:100]}", "!!")
        except Exception as e:
            log(f"Error setting wallet handle: {e}", "!!")
        return False
    
    async def transfer_credits_by_handle(self, from_tenant: str, to_handle: str, amount_cents: int) -> bool:
        log(f"Transferring {amount_cents} cents to @{to_handle}...", "..")
        try:
            async with self.session.post(
                f"{BASE}/api/tenants/{from_tenant}/wallet/transfer",
                json={"to": to_handle, "amountCents": amount_cents},
                headers=self._api_hdrs(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status in (200, 201):
                    log(f"Transferred {amount_cents} cents", "OK")
                    return True
                else:
                    body = await r.text()
                    log(f"Transfer failed [{r.status}]: {body[:100]}", "!!")
        except Exception as e:
            log(f"Error transferring: {e}", "!!")
        return False
    
    async def transfer_credits(self, from_tenant: str, to_tenant: str, amount_cents: int) -> bool:
        log(f"Transferring {amount_cents} cents...", "..")
        try:
            async with self.session.post(
                f"{BASE}/api/tenants/{from_tenant}/credits/transfer",
                json={"toTenantId": to_tenant, "amountCents": amount_cents},
                headers=self._api_hdrs(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status in (200, 201):
                    log(f"Transferred {amount_cents} cents", "OK")
                    return True
                else:
                    body = await r.text()
                    log(f"Transfer failed: {body[:100]}", "!!")
        except Exception as e:
            log(f"Error transferring: {e}", "!!")
        return False
    async def farm_daily_loop(self, max_farms=100):
        log(f"Starting farm loop (max {max_farms} cycles)...", "OK")
        log("", "..")
        teams = await self.list_teams()
        if len(teams) == 0:
            log("No teams found!", "!!")
            return
        main_team = None
        for team in teams:
            if team.get("name") == "Default":
                main_team = team
                break
        if not main_team:
            main_team = teams[0]
        self.original_tenant_id = main_team.get("id")
        original_name = main_team.get("name")
        log(f"Main account: {original_name} ({self.original_tenant_id[:20]}...)", "..")
        if self.user_handle:
            if self.random_suffix:
                main_handle = f"{self.user_handle}_{random.randint(1000, 9999)}"
            else:
                main_handle = self.user_handle
        else:
            # Default: main_xxxx
            main_handle = f"main_{random.randint(1000, 9999)}"
        log(f"Setting main wallet handle: @{main_handle}", "..")
        await self.update_wallet_settings(self.original_tenant_id, main_handle)
        await asyncio.sleep(1)
        total_earned = 0
        successful_cycles = 0
        for cycle in range(max_farms):
            log(f"\n{'='*60}", ">>")
            log(f"CYCLE #{cycle + 1}/{max_farms}", ">>")
            log(f"{'='*60}\n", ">>")
            log("Cleaning up empty teams...", "..")
            teams = await self.list_teams()
            for team in teams:
                team_id = team.get("id")
                team_name = team.get("name", "")
                wallet_info = await self.get_wallet_info(team_id)
                balance = wallet_info.get("balanceCents", 0)
                if team_id != self.original_tenant_id and balance == 0:
                    log(f"  Deleting: {team_name} (balance: 0)", "..")
                    if await self.delete_team(team_id):
                        log(f"  ✓ Deleted", "OK")
                        await asyncio.sleep(1)
            await asyncio.sleep(2)
            teams = await self.list_teams()
            if len(teams) >= 2:
                log(f"Already have {len(teams)} teams (max 2).", "!!")
                log("Cannot create more teams. Stopping...", "!!")
                break
            new_team_id = await self.create_team(self.team_name)
            if not new_team_id:
                log("Failed to create team, stopping...", "!!")
                break
            await asyncio.sleep(2)
            teams = await self.list_teams()
            created_team_name = None
            for t in teams:
                if t.get("id") == new_team_id:
                    created_team_name = t.get("name", "")
                    break
            if created_team_name:
                temp_handle = created_team_name.lower().replace(" ", "_")
            else:
                temp_handle = f"temp_{random.randint(1000, 9999)}"
            await self.update_wallet_settings(new_team_id, temp_handle)
            await asyncio.sleep(1)
            claimed = await self.claim_daily_for_team(new_team_id, f"Farm-{cycle+1}")
            if claimed:
                reward_amount = 7500  # 75 coin
                total_earned += reward_amount
                successful_cycles += 1
                log(f"\n✓ Cycle {cycle+1} complete. Earned: +75 cents", "OK")
                await asyncio.sleep(2)
                log(f"Transferring credits to main account...", "..")
                if await self.transfer_credits_by_handle(new_team_id, main_handle, reward_amount):
                    log(f"✓ Transfer successful!", "OK")
                else:
                    log(f"✗ Transfer failed - credits remain in temp team", "!!")
                await asyncio.sleep(2)
            else:
                log("\n✗ Claim failed - daily cooldown active!", "!!")
                log("Cannot farm more today. Stopping...", "!!")
                log("Deleting unused team...", "..")
                await asyncio.sleep(2)
                await self.delete_team(new_team_id)
                break
            if cycle + 1 < max_farms:
                log(f"Waiting 3s before next cycle...", "..")
                await asyncio.sleep(3)
        log(f"\n{'='*60}", "OK")
        log(f"Farm loop finished!", "OK")
        log(f"Successful cycles: {successful_cycles}", "OK")
        log(f"Total earned: {total_earned} cents = {total_earned/100:.2f} credits", "OK")
        log(f"All credits transferred to main account: {original_name}", "OK")
        log(f"{'='*60}", "OK")
    
    async def start_afk(self) -> bool:
        """Start AFK session"""
        tenant_id = await self._get_tenant_id()
        if not tenant_id:
            log("Failed to get tenant_id for AFK start", "!!")
            return False
        
        log("Starting AFK session...", "..")
        try:
            async with self.session.post(
                f"{BASE}/api/tenants/{tenant_id}/rewards/afk/start",
                json={},
                headers=self._api_hdrs(referer=f"{BASE}/billing/rewards/afk"),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status in (200, 201):
                    log("AFK session started", "OK")
                    return True
                else:
                    body = await r.text()
                    log(f"AFK start failed [{r.status}]: {body[:100]}", "!!")
        except Exception as e:
            log(f"Error starting AFK: {e}", "!!")
        return False
    
    
    async def update_balance(self):
        """Update balance from wallet API"""
        tenant_id = await self._get_tenant_id()
        if not tenant_id:
            return
        
        wallet_info = await self.get_wallet_info(tenant_id)
        if wallet_info:
            balance_cents = wallet_info.get("balanceCents", 0)
            self.credits = balance_cents / 100.0
    
    async def afk_heartbeat(self) -> tuple:
        """Send AFK heartbeat"""
        tenant_id = await self._get_tenant_id()
        if not tenant_id:
            return (0, "No tenant_id")
        
        try:
            async with self.session.post(
                f"{BASE}/api/tenants/{tenant_id}/rewards/afk/heartbeat",
                headers=self._api_hdrs(referer=f"{BASE}/billing/rewards/afk"),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                status = r.status
                try:
                    body = await r.json()
                except:
                    body = await r.text()
                return (status, body)
        except Exception as e:
            return (0, str(e))
    
    async def idle_tick(self):
        tenant_id = await self._get_tenant_id()
        if not tenant_id:
            return (0, "No tenant_id")
        try:
            async with self.session.post(
                f"{BASE}/api/tenants/{tenant_id}/idle/tick",
                headers=self._api_hdrs(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                status = r.status
                try:
                    body = await r.json()
                except:
                    body = await r.text()
                return (status, body)
        except Exception as e:
            return (0, str(e))
    def print_status(self, ok, status):
        uptime = fmt_up(time.time() - self.start_time)
        status_icon = "✓" if ok else "✗"
        print(
            f"\r[{ts()}] {status_icon} Team: {self.current_team_name} | "
            f"Uptime: {uptime} | "
            f"Credits: {self.credits:.2f} | "
            f"Earned: +{self.total_earned:.2f} | "
            f"Multiplier: {self.multiplier:.1f}x | "
            f"Active: {self.active_users} | "
            f"Ticks: {self.tick_count}",
            end="", flush=True
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
            if self.bug_mode:
                await self.farm_daily_loop(getattr(self, 'max_farms', 100))
                return
            if self.daily_afk:
                await self.claim_daily_reward()
            afk_started = await self.start_afk()
            if not afk_started:
                log("AFK start failed, trying to switch teams to fix...", "..")
                current_tenant_id = await self._get_tenant_id()
                teams = await self.list_teams()
                for team in teams:
                    team_id = team.get("id")
                    if team_id != current_tenant_id:
                        log(f"Switching to team: {team.get('name', 'Unknown')}", "..")
                        if await self.switch_team(team_id):
                            await asyncio.sleep(2)
                            log(f"Switching back to original team...", "..")
                            if await self.switch_team(current_tenant_id):
                                await asyncio.sleep(2)
                                if await self.start_afk():
                                    log("AFK session started after team switch!", "OK")
                                    afk_started = True
                                    break
                
                if not afk_started:
                    log("Failed to start AFK session, stopping.", "!!")
                    return
            
            self.afk_started = True
            self.running    = True
            self.start_time = time.time()
            await self.update_balance()
            initial_balance = self.credits
            self.last_balance_update = time.time()
            log(f"AFK earning started on team: {self.current_team_name}! Press [q] to stop", "OK")
            print()
            kb = asyncio.create_task(self._kb_loop())
            consecutive_fail = 0
            afk_fail_count = 0
            try:
                while self.running:
                    status, body = await self.afk_heartbeat()
                    self.tick_count += 1
                    ok = status in (200, 201)
                    if time.time() - self.last_balance_update >= 60:
                        await self.update_balance()
                        self.total_earned = self.credits - initial_balance
                        self.last_balance_update = time.time()
                    
                    if ok and isinstance(body, dict):
                        self.multiplier   = body.get("multiplier",  self.multiplier)
                        self.active_users = body.get("activeUsers", self.active_users)
                        consecutive_fail  = 0
                        afk_fail_count = 0
                    elif status == 404 or status == 400:
                        afk_fail_count += 1
                        log(f"AFK error [{status}], attempt {afk_fail_count}", "!!")
                        
                        if afk_fail_count >= 3:
                            log("Too many AFK errors, trying to switch team...", "!!")
                            teams = await self.list_teams()
                            current_tenant_id = await self._get_tenant_id()
                            switched = False
                            for team in teams:
                                team_id = team.get("id")
                                if team_id != current_tenant_id:
                                    log(f"Switching to team: {team.get('name', 'Unknown')}", "..")
                                    if await self.switch_team(team_id):
                                        await asyncio.sleep(2)
                                        if await self.start_afk():
                                            switched = True
                                            afk_fail_count = 0
                                            log(f"Switched to team: {self.current_team_name}", "OK")
                                            break
                            
                            if not switched:
                                log("No available team to switch to, stopping...", "!!")
                                self.running = False
                                break
                    elif status == 419:
                        consecutive_fail += 1
                        log(f"Token expired (419), attempt {consecutive_fail}", "!!")
                        if consecutive_fail >= 3:
                            log("Re-logging in...", "..")
                            if not await self.login():
                                self.running = False; break
                            await self.start_afk()
                            consecutive_fail = 0
                    elif status == 401:
                        log("Session expired (401), re-logging in...", "!!")
                        if not await self.login():
                            self.running = False; break
                        await self.start_afk()
                        consecutive_fail = 0
                    elif status == 429:
                        log("Rate limited (429), waiting 60s...", "!!")
                        await asyncio.sleep(60)
                        continue
                    elif not ok:
                        log(f"Heartbeat error [{status}]: {str(body)[:150]}", "!!")
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
    p = argparse.ArgumentParser(description="altare.gg Bot")
    p.add_argument("--email",    default=EMAIL,    help="altare.gg login email")
    p.add_argument("--password", default=PASSWORD, help="Password")
    p.add_argument("-b", "--bug", action="store_true", help="WARNING: HIGH RISK OF BAN! Enable bug/farm mode (create teams to farm daily rewards)")
    p.add_argument("-n", "--name", default="FarmTeam", help="Base name for teams in bug mode (default: FarmTeam)")
    p.add_argument("--max-farms", type=int, default=100, help="Max number of farms in bug mode (default: 100)")
    p.add_argument("-u", "--user", dest="user_handle", help="Custom wallet handle for main account (e.g., myname)")
    p.add_argument("-r", "--random", action="store_true", help="Add random suffix to user handle (e.g., myname_1234)")
    p.add_argument("--daily-afk", action="store_true", default=DAILY_AFK, help="Auto claim daily reward when starting AFK mode")
    a = p.parse_args()
    if not a.email or not a.password:
        p.print_help()
        print("\n[!!] --email and --password are required\n")
        return
    bot = AltareBot(a.email, a.password, bug_mode=a.bug, team_name=a.name, 
                    user_handle=a.user_handle, random_suffix=a.random, daily_afk=a.daily_afk)
    if a.bug:
        bot.max_farms = a.max_farms
    await bot.run()
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[..] stop.")