## uiaaa v0.5 beta
import asyncio
import time
import sys
import os
import random
import string
import json
import aiohttp
import argparse
from datetime import datetime
try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False
    print("Error: Install playwright first")
    print("  pip install playwright --break-system-packages")
    print("  playwright install chromium")

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

DEBUG = "--debug" in sys.argv

class Watchdog:
    def __init__(self, silent=False):
        self.silent = silent
        self.bot_tasks = {}
        self.last_heartbeat = {}
        self.running = True
        self.restart_count = {}
        
    def log(self, msg):
        if not self.silent:
            timestamp = datetime.now().strftime("%H:%M:%S")
            print(f"[{timestamp}] {msg}")
    
    async def monitor_task(self, task_id, task):
        self.last_heartbeat[task_id] = time.time()
        
        while self.running:
            if task.done():
                return task_id, task.exception()
            
            await asyncio.sleep(5)
    
    async def check_health(self):
        while self.running:
            current_time = time.time()
            
            for task_id, last_time in list(self.last_heartbeat.items()):
                if current_time - last_time > 300:
                    self.log(f"Task {task_id} timeout detected")
                    if task_id in self.bot_tasks:
                        task = self.bot_tasks[task_id]
                        if not task.done():
                            task.cancel()
                        
                        return task_id, "timeout"
            
            await asyncio.sleep(10)
        
        return None, None
    
    async def run_with_restart(self, account_cookies, account_id, webhook_url, check_interval, silent, auto_send_threshold=None, auto_send_recipient=None):
        self.restart_count[account_id] = 0
        
        while self.running:
            self.log(f"Starting account {account_id}")
            
            try:
                bot = NA1AFKBot(account_cookies, silent=silent, auto_send_threshold=auto_send_threshold, auto_send_recipient=auto_send_recipient)
                bot.webhook_url = webhook_url
                bot.watchdog = self
                bot.watchdog_id = account_id
                
                await bot.setup(headless=True)
                await bot.run(check_interval=check_interval)
                
            except KeyboardInterrupt:
                self.log(f"Account {account_id} stopped")
                raise
            except asyncio.CancelledError:
                self.log(f"Account {account_id} cancelled")
                break
            except Exception as e:
                self.restart_count[account_id] += 1
                self.log(f"Account {account_id} crashed: {e}")
                
                if self.restart_count[account_id] > 10:
                    self.log(f"Account {account_id} failed too many times")
                    break
                
                wait_time = min(60, 5 * self.restart_count[account_id])
                self.log(f"Restarting account {account_id} in {wait_time}s")
                await asyncio.sleep(wait_time)
    
    def update_heartbeat(self, task_id):
        self.last_heartbeat[task_id] = time.time()

class NA1AFKBot:
    def __init__(self, cookies_dict, base_url="https://panel.na1.host", silent=False, auto_send_threshold=None, auto_send_recipient=None):
        self.cookies = cookies_dict
        self.base_url = base_url
        self.browser = None
        self.context = None
        self.page = None
        self.running = False
        self.start_time = None
        self.username = None
        self.natag = None
        self.webhook_url = None
        self.check_count = 0
        self.modal_open = False
        self.silent = silent
        self.last_interaction = time.time()
        self.wallet_balance = 0
        self.watchdog = None
        self.watchdog_id = None
        self.auto_send_threshold = auto_send_threshold
        self.auto_send_recipient = auto_send_recipient
        self.keyboard_listener_active = False
        self.last_auto_send = 0

    def log(self, msg, force=False):
        if DEBUG or force or not self.silent:
            print(msg)

    async def setup(self, headless=True):
        self.log("Initializing browser...", force=True)
        playwright = await async_playwright().start()
        
        self.browser = await playwright.chromium.launch(
            headless=headless,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disable-software-rasterizer',
                '--disable-extensions',
                '--disable-background-networking',
                '--disable-background-timer-throttling',
                '--disable-backgrounding-occluded-windows',
                '--disable-breakpad',
                '--disable-component-extensions-with-background-pages',
                '--disable-features=TranslateUI,BlinkGenPropertyTrees,AudioServiceOutOfProcess',
                '--disable-ipc-flooding-protection',
                '--disable-renderer-backgrounding',
                '--disable-sync',
                '--force-color-profile=srgb',
                '--metrics-recording-only',
                '--mute-audio',
                '--no-first-run',
                '--disable-default-apps',
                '--disable-hang-monitor',
                '--disable-prompt-on-repost',
                '--disable-domain-reliability',
                '--disk-cache-size=1',
                '--memory-pressure-off',
                '--disable-notifications',
                '--disable-popup-blocking',
                '--disable-infobars',
                '--disable-session-crashed-bubble',
                '--disable-backing-store-limit',
                '--disable-client-side-phishing-detection',
                '--disable-component-update',
                '--disable-dinosaur-easter-egg',
                '--no-default-browser-check',
            ]
        )
        
        self.context = await self.browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            java_script_enabled=True,
            accept_downloads=False,
            has_touch=False,
            is_mobile=False,
            locale='en-US',
            timezone_id='America/New_York',
            color_scheme='dark',
        )
        
        async def route_handler(route):
            url = route.request.url
            resource_type = route.request.resource_type
            
            if resource_type in ["image", "font", "media"]:
                await route.abort()
                return
            
            block_patterns = [
                "analytics",
                "tracking",
                "telemetry",
                "sentry",
                "gtag",
                "google-analytics",
                "facebook",
                "doubleclick",
                "ads",
                "pixel",
                "tracker",
                "beacon",
                "cdn-cgi/rum",
            ]
            
            if any(pattern in url for pattern in block_patterns):
                await route.abort()
                return
            
            await route.continue_()
        
        await self.context.route("**/*", route_handler)
        
        await self.context.add_init_script(r"""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            
            window.chrome = { runtime: {} };
            
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );
            
            const style = document.createElement('style');
            style.textContent = `
                *, *::before, *::after {
                    animation-duration: 0.01s !important;
                    transition-duration: 0.01s !important;
                }
                
                .max-w-\[1400px\] .mt-8,
                .max-w-\[1400px\] .grid,
                [aria-roledescription="sortable"],
                ._spinner_1u50m_1,
                ._dot_1u50m_6,
                .mb-4.grid,
                .border-green-500,
                .bg-green-500\/25,
                .border-l-8.border-green-500,
                footer,
                ._footerContent_pb86g_1,
                #DndDescribedBy-0,
                #DndLiveRegion-0,
                [id^="DndDescribedBy"],
                [id^="DndLiveRegion"],
                .crisp-client {
                    display: none !important;
                }
                
                body, html {
                    overflow: hidden !important;
                }
            `;
            document.head.appendChild(style);
        """)
        
        playwright_cookies = []
        for name, value in self.cookies.items():
            playwright_cookies.append({
                'name': name,
                'value': value,
                'domain': 'panel.na1.host',
                'path': '/',
                'secure': True,
                'sameSite': 'Lax'
            })
        await self.context.add_cookies(playwright_cookies)
        
        self.page = await self.context.new_page()
        self.log("Browser ready", force=True)

    async def fetch_wallet_balance(self):
        try:
            cookies_header = '; '.join([f"{k}={v}" for k, v in self.cookies.items()])
            headers = {
                'Cookie': cookies_header,
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'application/json',
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.base_url}/api/client/account/wallet",
                    headers=headers,
                    timeout=10
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self.wallet_balance = data.get('coins', 0)
                        return self.wallet_balance
            return None
        except Exception as e:
            return None

    async def send_coins_via_ui(self, recipient, amount, description=""):
        try:
            await asyncio.sleep(0.05)
            
            # Click Send Coins tab
            send_tab = await self.page.wait_for_selector('button:has-text("Send Coins")', timeout=2000)
            if send_tab:
                await send_tab.click()
                await asyncio.sleep(0.15)
            
            # Fill recipient
            recipient_input = await self.page.wait_for_selector('input[name="recipient"]', timeout=2000)
            await recipient_input.click()
            await recipient_input.fill(recipient)
            await asyncio.sleep(0.08)
            
            # Fill amount
            amount_input = await self.page.wait_for_selector('input[type="number"]', timeout=2000)
            await amount_input.click()
            await amount_input.fill(str(amount))
            await asyncio.sleep(0.08)
            
            # Fill description if provided
            if description:
                try:
                    desc_input = await self.page.wait_for_selector('input[name="description"]', timeout=800)
                    await desc_input.fill(description)
                    await asyncio.sleep(0.05)
                except:
                    pass
            
            # Wait for send button to be enabled
            send_button = await self.page.wait_for_selector('button[type="submit"]:has-text("Send")', timeout=2000)
            
            # Quick wait for button enable
            is_disabled = True
            for _ in range(15):
                is_disabled = await send_button.evaluate('button => button.disabled')
                if not is_disabled:
                    break
                await asyncio.sleep(0.2)
            
            if is_disabled:
                return False, "Button disabled"
            
            await send_button.click()
            await asyncio.sleep(0.5)
            
            # Go back to AFK tab quickly
            try:
                afk_tab = await self.page.wait_for_selector('button:has-text("AFK")', timeout=1500)
                if afk_tab:
                    await afk_tab.click()
                    await asyncio.sleep(0.2)
            except:
                pass
            
            return True, "Sent"
            
        except Exception as e:
            return False, f"UI error: {str(e)}"

    async def send_discord_webhook_send(self, recipient, amount):
        if not self.webhook_url:
            return
        
        wallet = await self.fetch_wallet_balance()
        wallet_str = f"{wallet:,.0f}" if wallet is not None else "?"
        
        sender = self.natag if self.natag else self.username
        content = f"{sender} -> {recipient} | {amount} coins | Wallet: {wallet_str} coins"
        
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(self.webhook_url, json={"content": content}, timeout=10)
        except:
            pass

    async def manual_send_coins(self):
        try:
            # Move to next line for input
            print("\nSend Coins")
            print("-" * 40)
            
            recipient = input("Recipient (@nametag or email): ").strip()
            if not recipient:
                # Clear 3 lines and return to monitoring
                print("\033[F\033[K\033[F\033[K\033[F\033[K\033[F", end='', flush=True)
                return
            
            amount_str = input("Amount: ").strip()
            try:
                amount = int(amount_str)
                if amount <= 0:
                    print("\033[F\033[K\033[F\033[K\033[F\033[K\033[F\033[K\033[F", end='', flush=True)
                    return
            except ValueError:
                print("\033[F\033[K\033[F\033[K\033[F\033[K\033[F\033[K\033[F", end='', flush=True)
                return
            
            description = input("Description (optional): ").strip()
            
            balance = await self.fetch_wallet_balance()
            if balance and amount > balance:
                # Clear 5 lines and show error
                print("\033[F\033[K\033[F\033[K\033[F\033[K\033[F\033[K\033[F\033[K\033[F", end='', flush=True)
                print(f"Insufficient balance: {balance} coins" + " " * 60, end='', flush=True)
                await asyncio.sleep(1.5)
                print("\r" + " " * 120 + "\r", end='', flush=True)
                return
            
            # Clear all 5 input lines, move back to monitoring line
            print("\033[F\033[K\033[F\033[K\033[F\033[K\033[F\033[K\033[F\033[K\033[F", end='', flush=True)
            
            # Show sending on monitoring line
            print(f"Sending {amount} coins..." + " " * 80, end='', flush=True)
            
            # Send via UI
            success, result = await self.send_coins_via_ui(recipient, amount, description)
            
            # Show result on same line
            print("\r" + " " * 120 + "\r", end='', flush=True)
            if success:
                print(f"Success! Sent {amount} coins" + " " * 60, end='', flush=True)
                await self.send_discord_webhook_send(recipient, amount)
                await self.fetch_wallet_balance()
            else:
                print(f"Failed: {result}" + " " * 60, end='', flush=True)
            
            # Wait then clear, let monitoring continue
            await asyncio.sleep(1.5)
            print("\r" + " " * 120 + "\r", end='', flush=True)
            
        except Exception as e:
            print("\r" + " " * 120 + "\r", end='', flush=True)
    async def check_auto_send(self):
        if not self.auto_send_threshold or not self.auto_send_recipient:
            return
        current_time = time.time()
        if current_time - self.last_auto_send < 300:
            return
        # Fetch wallet balance FIRST before checking modal
        balance = await self.fetch_wallet_balance()
        if balance is None:
            return
        
        # Check if balance meets threshold
        if balance >= self.auto_send_threshold:
            amount_to_send = balance
            
            if not self.silent:
                print(f"\rSending {amount_to_send} coins..." + " " * 80, end='', flush=True)
            
            # Send coins (will handle modal navigation internally)
            success, result = await self.send_coins_via_ui(
                self.auto_send_recipient, 
                amount_to_send, 
                "auto-send"
            )
            
            if success:
                if not self.silent:
                    print(f"\rSuccess! Sent {amount_to_send} coins" + " " * 60, end='', flush=True)
                
                self.last_auto_send = current_time
                await self.send_discord_webhook_send(self.auto_send_recipient, amount_to_send)
                # Update wallet balance after sending
                await self.fetch_wallet_balance()
                await asyncio.sleep(1.5)
                print("\r" + " " * 120 + "\r", end='', flush=True)
            else:
                if not self.silent:
                    print("\r" + " " * 120 + "\r", end='', flush=True)

    async def extract_user_info(self):
        try:
            user_data = await self.page.evaluate("""() => {
                if (window.PterodactylUser) {
                    return window.PterodactylUser;
                }
                return null;
            }""")
            
            if not user_data:
                self.log("Could not find PterodactylUser", force=True)
                return False
            
            if DEBUG:
                self.log(f"User data: {json.dumps(user_data, indent=2)}", force=True)
            
            self.username = user_data.get('username', 'user')
            self.natag = user_data.get('natag')
            
            if not self.silent:
                print(f"User: {self.username}")
                if self.natag:
                    print(f"NAtag: @{self.natag}")
            
            return True
        except Exception as e:
            self.log(f"Extract user info error: {e}", force=True)
            if DEBUG:
                import traceback
                traceback.print_exc()
            return False

    async def check_wallet_setup_modal(self):
        try:
            wallet_modal = await self.page.query_selector('h2:has-text("Setup Your Wallet")')
            return wallet_modal is not None
        except:
            return False

    async def setup_wallet(self):
        try:
            if not await self.check_wallet_setup_modal():
                if DEBUG:
                    self.log("No wallet setup modal", force=True)
                return True
            
            if not self.silent:
                print("Wallet setup required")
            
            random_digits = ''.join(random.choices(string.digits, k=4))
            new_natag = f"{self.username}{random_digits}"
            
            if DEBUG:
                print(f"Creating NAtag: @{new_natag}")
            
            input_field = await self.page.query_selector('input#natag')
            if not input_field:
                if DEBUG:
                    self.log("NAtag input not found", force=True)
                return False
            
            await input_field.click()
            await asyncio.sleep(random.uniform(0.1, 0.3))
            await input_field.fill(new_natag)
            await asyncio.sleep(random.uniform(0.5, 1.0))
            
            setup_btn = await self.page.query_selector('button[type="submit"]:has-text("Setup Wallet")')
            if setup_btn:
                await setup_btn.click()
                await asyncio.sleep(3)
                
                if not await self.check_wallet_setup_modal():
                    if not self.silent:
                        print(f"Wallet setup: @{new_natag}")
                    self.natag = new_natag
                    return True
                else:
                    if DEBUG:
                        print("Wallet setup failed")
            
            return False
        except Exception as e:
            self.log(f"Wallet setup error: {e}", force=True)
            if DEBUG:
                import traceback
                traceback.print_exc()
            return False

    async def is_joined(self):
        try:
            leave_btn = await self.page.query_selector('button:has-text("Leave AFK")')
            return leave_btn is not None
        except:
            return False

    async def has_join_button(self):
        try:
            join_btn = await self.page.query_selector('button:has-text("Join AFK Page")')
            return join_btn is not None
        except:
            return False

    async def get_dom_stats(self):
        try:
            stats = {}
            users_el = await self.page.query_selector('div.flex.items-center.justify-between:has-text("Active Users:") span.text-xl')
            if users_el:
                stats['active_users'] = await users_el.text_content()
            
            mult_el = await self.page.query_selector('div.flex.items-center.justify-between:has-text("Multiplier:") span.text-xl')
            if mult_el:
                stats['multiplier'] = await mult_el.text_content()
            
            cpm_el = await self.page.query_selector('div.flex.items-center.justify-between:has-text("Coins per Minute:") span.text-xl')
            if cpm_el:
                stats['cpm'] = await cpm_el.text_content()
            
            time_el = await self.page.query_selector('div.flex.items-center.justify-between:has-text("Time Active:") span.text-gray-200')
            if time_el:
                stats['time_active'] = await time_el.text_content()
            
            coins_el = await self.page.query_selector('div.flex.items-center.justify-between:has-text("Coins Earned:") span.text-xl')
            if coins_el:
                stats['coins_earned'] = await coins_el.text_content()
            
            next_el = await self.page.query_selector('div.flex.items-center.justify-between:has-text("Next Coin In:") span.font-mono')
            if next_el:
                stats['next_coin'] = await next_el.text_content()
            
            return stats
        except Exception as e:
            if DEBUG:
                self.log(f"Get stats error: {e}", force=True)
            return {}

    async def simulate_human_activity(self):
        try:
            current_time = time.time()
            if current_time - self.last_interaction < 120:
                return
            
            self.last_interaction = current_time
            
            viewport_width = 1920
            viewport_height = 1080
            
            x = random.randint(100, viewport_width - 100)
            y = random.randint(100, viewport_height - 100)
            
            await self.page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.1, 0.3))
            
            if random.random() < 0.3:
                scroll_delta = random.randint(-100, 100)
                await self.page.mouse.wheel(0, scroll_delta)
            
            await asyncio.sleep(random.uniform(0.2, 0.5))
            
        except Exception as e:
            pass

    async def close_all_modals(self):
        try:
            for _ in range(3):
                await self.page.keyboard.press('Escape')
                await asyncio.sleep(0.2)
            
            dismiss_btns = await self.page.query_selector_all('button:has-text("Dismiss")')
            for btn in dismiss_btns:
                try:
                    await btn.click(timeout=1000)
                    await asyncio.sleep(0.2)
                except:
                    pass
            
            maybe_later_btns = await self.page.query_selector_all('button:has-text("Maybe later")')
            for btn in maybe_later_btns:
                try:
                    await btn.click(timeout=1000)
                    await asyncio.sleep(0.2)
                except:
                    pass
            
            self.modal_open = False
        except Exception as e:
            pass

    async def open_afk_modal(self):
        try:
            if self.modal_open:
                return True
            
            await self.close_all_modals()
            
            if DEBUG:
                self.log("Looking for coins button...", force=True)
            
            coins_btn = None
            coins_selectors = [
                'button:has-text("coins")',
                'button.bg-cyan-600\\/10',
                'button:has-text("Coins")',
                'button[class*="cyan"]'
            ]
            
            for selector in coins_selectors:
                coins_btn = await self.page.query_selector(selector)
                if coins_btn:
                    if DEBUG:
                        self.log(f"Found coins button: {selector}", force=True)
                    break
            
            if not coins_btn:
                if DEBUG:
                    self.log("Coins button not found", force=True)
                return False
            
            await asyncio.sleep(random.uniform(0.3, 0.7))
            
            if DEBUG:
                self.log("Clicking coins button...", force=True)
            
            try:
                await coins_btn.click(force=True, timeout=5000)
            except:
                await coins_btn.click(timeout=5000)
            
            await asyncio.sleep(random.uniform(1.5, 2.5))
            
            if await self.check_wallet_setup_modal():
                if not await self.setup_wallet():
                    return False
                await asyncio.sleep(2)
                await coins_btn.click(force=True)
                await asyncio.sleep(2)
            
            if DEBUG:
                self.log("Looking for AFK tab...", force=True)
            
            afk_selectors = [
                'button:has-text("AFK")',
                'button[role="tab"]:has-text("AFK")',
                '[role="tab"]:has-text("AFK")',
                'div[role="tab"]:has-text("AFK")',
            ]
            
            afk_tab = None
            for selector in afk_selectors:
                try:
                    afk_tab = await self.page.wait_for_selector(selector, timeout=3000)
                    if afk_tab:
                        if DEBUG:
                            self.log(f"Found AFK tab: {selector}", force=True)
                        break
                except:
                    continue
            
            if not afk_tab:
                if DEBUG:
                    self.log("AFK tab not found", force=True)
                return False
            
            await asyncio.sleep(random.uniform(0.2, 0.5))
            
            if DEBUG:
                self.log("Clicking AFK tab...", force=True)
            
            await afk_tab.click(force=True)
            await asyncio.sleep(random.uniform(1.0, 2.0))
            
            self.modal_open = True
            return True
        except Exception as e:
            if DEBUG:
                self.log(f"Open modal error: {e}", force=True)
                import traceback
                traceback.print_exc()
            return False

    async def join_afk(self):
        try:
            if not await self.open_afk_modal():
                return False
            
            if await self.is_joined():
                if DEBUG:
                    self.log("Already joined", force=True)
                return True
            
            if not await self.has_join_button():
                if DEBUG:
                    self.log("No join button found", force=True)
                return False
            
            if DEBUG:
                self.log("Clicking Join button...", force=True)
            
            join_btn = await self.page.wait_for_selector('button:has-text("Join AFK Page")', timeout=5000)
            if join_btn:
                await asyncio.sleep(random.uniform(0.5, 1.0))
                await join_btn.click()
                await asyncio.sleep(random.uniform(3.0, 5.0))
                
                is_joined = await self.is_joined()
                if DEBUG:
                    self.log(f"Join confirmed: {is_joined}", force=True)
                return is_joined
            
            return False
        except Exception as e:
            if DEBUG:
                self.log(f"Join error: {e}", force=True)
                import traceback
                traceback.print_exc()
            return False

    async def send_discord_webhook(self, uptime_str, coins):
        if not self.webhook_url:
            return
        
        wallet = await self.fetch_wallet_balance()
        if wallet is not None:
            wallet_str = f"{wallet:,.1f}"
        else:
            wallet_str = "N/A"
        
        content = f"{self.username} | {uptime_str} | Coins: {coins} | Wallet: {wallet_str} coins"
        payload = {"content": content}
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.webhook_url, json=payload, timeout=10) as resp:
                    if resp.status not in (200, 204):
                        pass
        except Exception as e:
            pass

    def format_uptime(self, seconds):
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    async def keyboard_listener(self):
        try:
            import termios
            import tty
            import select
            
            stdin_fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(stdin_fd)
            
            try:
                tty.setcbreak(stdin_fd)
                
                while self.running:
                    if select.select([sys.stdin], [], [], 0.1)[0]:
                        char = sys.stdin.read(1).lower()
                        
                        if char == 's':
                            print("\r" + " " * 120, end='', flush=True)
                            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_settings)
                            await self.manual_send_coins()
                            tty.setcbreak(stdin_fd)
                        
                        elif char == 'k':
                            print("\r" + " " * 120, end='', flush=True)
                            print("\rStopping...")
                            self.running = False
                            break
                    
                    await asyncio.sleep(0.1)
            finally:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_settings)
        except Exception as e:
            pass

    async def monitor_loop(self, check_interval=30):
        self.start_time = time.time()
        self.running = True
        error_count = 0
        max_errors = 5
        last_coins = "0"
        last_webhook_time = 0
        
        if not self.silent:
            print("Press 's' to send coins | Press 'k' to stop\n")
        
        if DEBUG:
            self.log("Waiting before first check...", force=True)
        
        await asyncio.sleep(random.uniform(8, 12))
        
        keyboard_task = None
        if not self.silent:
            keyboard_task = asyncio.create_task(self.keyboard_listener())
        
        try:
            while self.running:
                self.check_count += 1
                
                if self.watchdog and self.watchdog_id:
                    self.watchdog.update_heartbeat(self.watchdog_id)
                
                # GC every 10 checks
                if self.check_count % 10 == 0:
                    try:
                        await self.page.evaluate('if (window.gc) window.gc();')
                        if DEBUG:
                            self.log(f"GC hint sent (check #{self.check_count})", force=True)
                    except:
                        pass
                
                if self.check_count % 5 == 0:
                    await self.simulate_human_activity()
                
                # Check auto send
                await self.check_auto_send()
                
                if not self.modal_open:
                    if DEBUG:
                        print("\nReopening modal...")
                    self.modal_open = False
                    await self.open_afk_modal()
                    await asyncio.sleep(random.uniform(3, 5))
                    continue
                
                is_joined = await self.is_joined()
                if not is_joined:
                    if DEBUG:
                        print("\nRejoining AFK...")
                    self.modal_open = False
                    await self.join_afk()
                    self.start_time = time.time()
                    await asyncio.sleep(random.uniform(8, 12))
                    continue
                
                stats = await self.get_dom_stats()
                if not stats:
                    error_count += 1
                    if DEBUG:
                        self.log(f"DOM error ({error_count}/{max_errors})", force=True)
                    if error_count >= max_errors:
                        if DEBUG:
                            print("\nRestarting session...")
                        self.modal_open = False
                        await self.join_afk()
                        error_count = 0
                        self.start_time = time.time()
                        await asyncio.sleep(random.uniform(8, 12))
                    await asyncio.sleep(check_interval)
                    continue
                
                error_count = 0
                active_users = stats.get('active_users', '?')
                multiplier = stats.get('multiplier', '?')
                cpm = stats.get('cpm', '?')
                time_active = stats.get('time_active', '?')
                coins_earned = stats.get('coins_earned', '?')
                next_coin = stats.get('next_coin', '?')
                
                uptime = time.time() - self.start_time
                uptime_str = self.format_uptime(uptime)
                
                if not self.silent:
                    line = f"[{uptime_str}] users:{active_users} {multiplier} {cpm}/m | active:{time_active} earn:{coins_earned} next:{next_coin}"
                    print(f"\r{' ' * 100}\r{line}", end='', flush=True)
                
                # Only send webhook when coins change OR every 10 minutes
                current_time = time.time()
                if coins_earned != last_coins or (current_time - last_webhook_time) >= 600:
                    await self.send_discord_webhook(uptime_str, coins_earned)
                    last_coins = coins_earned
                    last_webhook_time = current_time
                
                jitter = random.uniform(-2, 2)
                await asyncio.sleep(check_interval + jitter)
                
        except KeyboardInterrupt:
            if not self.silent:
                print("\n")
            self.running = False
            raise
        finally:
            if keyboard_task and not keyboard_task.done():
                keyboard_task.cancel()
                try:
                    await keyboard_task
                except asyncio.CancelledError:
                    pass

    async def run(self, check_interval=30):
        try:
            await self.page.goto(self.base_url, wait_until='networkidle', timeout=60000)
            await asyncio.sleep(random.uniform(3, 5))
            
            await self.close_all_modals()
            await asyncio.sleep(random.uniform(1, 2))
            
            await self.extract_user_info()
            
            if await self.check_wallet_setup_modal():
                if not await self.setup_wallet():
                    if not self.silent:
                        print("Failed to setup wallet")
                    return
            
            if not self.silent:
                print("Joining AFK...")
            
            if not await self.join_afk():
                if not self.silent:
                    print("Failed to join")
                return
            
            await asyncio.sleep(random.uniform(5, 8))
            
            is_joined = await self.is_joined()
            if is_joined:
                if not self.silent:
                    print("Joined AFK session")
                    stats = await self.get_dom_stats()
                    if stats:
                        print(f"Stats: {stats.get('active_users', '?')} users, {stats.get('multiplier', '?')}, {stats.get('cpm', '?')}/m")
                    print()
            else:
                if not self.silent:
                    print("Join verification failed")
                return
            
            await self.monitor_loop(check_interval)
            
        except KeyboardInterrupt:
            raise
        finally:
            if self.browser:
                await self.browser.close()

def setup_wizard():
    print("=" * 60)
    print("NA1 AFK BOT - SETUP WIZARD")
    print("=" * 60)
    print()
    
    num_accounts_str = input("Number of accounts to configure: ").strip()
    try:
        num_accounts = int(num_accounts_str)
        if num_accounts < 1:
            print("Error: Must configure at least 1 account")
            return False
    except ValueError:
        print("Error: Invalid number")
        return False
    
    accounts = []
    
    for i in range(num_accounts):
        print()
        print(f"Account {i+1}/{num_accounts}")
        print("-" * 60)
        print("Instructions:")
        print("1. Log in to https://panel.na1.host in your browser")
        print("2. Open Developer Tools (F12)")
        print("3. Go to 'Application' or 'Storage' tab")
        print("4. Find 'Cookies' -> 'https://panel.na1.host'")
        print("5. Copy the values for:")
        print("   - pterodactyl_session")
        print("   - remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d")
        print()
        
        pterodactyl_session = input("pterodactyl_session: ").strip()
        if not pterodactyl_session:
            print("Error: pterodactyl_session is required")
            return False
        
        remember_web = input("remember_web_...: ").strip()
        if not remember_web:
            print("Error: remember_web cookie is required")
            return False
        
        accounts.append({
            'pterodactyl_session': pterodactyl_session,
            'remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d': remember_web
        })
    
    print()
    print("-" * 60)
    discord_webhook = input("Discord webhook URL (optional): ").strip()
    
    print()
    auto_send_yn = input("Enable auto-send? (y/n): ").lower()
    auto_send_threshold = None
    auto_send_recipient = None
    
    if auto_send_yn == 'y':
        try:
            threshold_str = input("Send when balance reaches (coins): ").strip()
            auto_send_threshold = int(threshold_str)
            auto_send_recipient = input("Send to (@nametag or email): ").strip()
            
            if not auto_send_recipient:
                print("No recipient specified, auto-send disabled")
                auto_send_threshold = None
        except ValueError:
            print("Invalid threshold, auto-send disabled")
    
    config_data = {'accounts': accounts}
    
    if discord_webhook:
        config_data['discord'] = {'webhook': discord_webhook}
    
    if auto_send_threshold and auto_send_recipient:
        config_data['auto_send'] = {
            'threshold': auto_send_threshold,
            'recipient': auto_send_recipient
        }
    
    try:
        try:
            import yaml as yaml_module
        except ImportError:
            print("\nInstalling PyYAML...")
            os.system("pip install pyyaml --break-system-packages")
            try:
                import yaml as yaml_module
            except ImportError:
                print("Error: Failed to install PyYAML")
                return False
        
        with open('config.yml', 'w') as f:
            yaml_module.dump(config_data, f, default_flow_style=False)
        
        print()
        print("Configuration saved to config.yml")
        print(f"Configured {num_accounts} account(s)")
        print()
        return True
    except Exception as e:
        print(f"\nError saving config: {e}")
        if DEBUG:
            import traceback
            traceback.print_exc()
        return False

async def main():
    parser = argparse.ArgumentParser(description='NA1 AFK Bot')
    parser.add_argument('--setup', action='store_true', help='Run setup wizard')
    parser.add_argument('-c', '--check-interval', type=int, default=30, help='Check interval in seconds')
    parser.add_argument('-i', '--invisible', action='store_true', help='Silent mode, hide all output')
    parser.add_argument('-s', '--send-threshold', type=int, help='Auto-send when balance reaches this amount')
    parser.add_argument('-u', '--send-recipient', type=str, help='Recipient nametag for auto-send')
    
    args = parser.parse_args()
    
    if args.setup:
        setup_wizard()
        return
    
    silent = args.invisible
    
    if silent:
        os.system('clear' if os.name != 'nt' else 'cls')
    
    if not silent:
        if DEBUG:
            print("NA1 AFK BOT - DEBUG MODE")
        else:
            print("NA1 AFK BOT")
        print()
    
    if not HAS_PLAYWRIGHT:
        if not silent:
            print("Error: Playwright not installed")
            print("  pip install playwright --break-system-packages")
            print("  playwright install chromium")
        return
    
    if not os.path.exists('config.yml'):
        if not silent:
            print("config.yml not found!")
            print()
            setup_choice = input("Do you want to run the setup wizard? (y/n): ").lower()
            if setup_choice == 'y':
                if setup_wizard():
                    print("Please run the bot again to start.")
                return
            else:
                print("Cannot run without config.yml")
                return
        return
    
    try:
        if not HAS_YAML:
            if not silent:
                print("Error: PyYAML not installed")
                print("  pip install pyyaml --break-system-packages")
            return
        
        with open('config.yml', 'r') as f:
            config = yaml.safe_load(f)
        
        if config is None:
            if not silent:
                print("Error: config.yml is empty")
            return
        
        accounts = config.get('accounts', [])
        
        if not accounts:
            old_cookies = config.get('cookies')
            if old_cookies:
                accounts = [old_cookies]
            else:
                if not silent:
                    print("Error: No accounts in config.yml")
                return
        
        discord_cfg = config.get("discord", {})
        webhook_url = discord_cfg.get("webhook")
        
        auto_send_cfg = config.get("auto_send", {})
        config_threshold = auto_send_cfg.get("threshold")
        config_recipient = auto_send_cfg.get("recipient")
        
        auto_send_threshold = args.send_threshold or config_threshold
        auto_send_recipient = args.send_recipient or config_recipient
        
        if auto_send_threshold and not auto_send_recipient:
            if not silent:
                print("Error: Auto-send threshold set but no recipient specified")
                print("Use -u <recipient> or add to config.yml")
            return
        
        if not silent:
            print(f"Loaded {len(accounts)} account(s)")
            if auto_send_threshold and auto_send_recipient:
                print(f"Auto-send: {auto_send_threshold} coins -> {auto_send_recipient}")
            print()
        
        # Ask for check interval only if not provided via command line
        if not silent and args.check_interval == 30:
            interval_input = input("Check interval in seconds (default=30): ").strip()
            try:
                if interval_input:
                    args.check_interval = int(interval_input)
            except:
                pass
        
        if not silent:
            if DEBUG:
                print(f"\nConfig:")
                print(f"  Headless: True")
                print(f"  Interval: {args.check_interval}s")
                print(f"  Accounts: {len(accounts)}")
                print()
            else:
                print(f"Config: interval={args.check_interval}s, runtime=unlimited, mode=DOM")
                print()
        
        watchdog = Watchdog(silent=silent)
        
        tasks = []
        for i, account in enumerate(accounts):
            account_id = f"acc_{i+1}"
            
            task = asyncio.create_task(
                watchdog.run_with_restart(
                    account, 
                    account_id, 
                    webhook_url, 
                    args.check_interval, 
                    silent,
                    auto_send_threshold,
                    auto_send_recipient
                )
            )
            tasks.append(task)
            watchdog.bot_tasks[account_id] = task
            
            if i < len(accounts) - 1:
                await asyncio.sleep(random.uniform(2, 5))
        
        try:
            await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            if not silent:
                print("\nStopping...")
            watchdog.running = False
            for task in tasks:
                if not task.done():
                    task.cancel()
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            except:
                pass  
    except KeyboardInterrupt:
        if not silent:
            print("Stopped")
        sys.exit(0)
    except Exception as e:
        if not silent:
            print(f"Error: {e}")
            if DEBUG:
                import traceback
                traceback.print_exc()
        sys.exit(1)
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
