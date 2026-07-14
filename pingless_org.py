import requests, json, websocket, ssl, time, traceback, threading, sys
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PINGLESS_SID = ""   # pingless.sid cookie from browser (F12 -> Application -> Cookies -> dash.pingless.org)

BASE_URL = "https://dash.pingless.org"

def print_session_log(start_time, coins_pm, next_reward_ms, total_earned):
    elapsed_sec = int(time.time() - start_time)
    h, m, s = elapsed_sec // 3600, (elapsed_sec % 3600) // 60, elapsed_sec % 60
    session_str = f"{h:02d}:{m:02d}:{s:02d}"
    next_reward_sec = str(int(next_reward_ms / 1000))
    print(f"\r| session: {session_str} | coinsPerMinute={coins_pm} nextRewardIn={next_reward_sec}s | earned={total_earned:.2f} |", end='')

_ws_ref = None
_quit_flag = False

def run_afk():
    global _ws_ref, _quit_flag
    _quit_flag = False
    start_time = time.time()
    total_earned = 0.0
    last_state = None
    print("Starting AFK Session (Pingless)")

    def on_message(ws, msg):
        nonlocal total_earned, last_state
        try:
            data = json.loads(msg)
            if data.get("type") == "afk_state":
                coins_pm = data.get("coinsPerMinute", 0)
                next_reward = data.get("nextRewardIn", 0)
                if last_state and next_reward > last_state.get("nextRewardIn", 0) + 5000:
                    total_earned += coins_pm
                last_state = data
                print_session_log(start_time, coins_pm, next_reward, total_earned)
        except:
            pass

    def on_error(ws, err):
        if not _quit_flag:
            print(f"\n[WS ERROR] {err}")

    def on_close(ws, code, msg):
        if not _quit_flag:
            print(f"\n[WS CLOSED] {code} {msg}")

    ws = websocket.WebSocketApp(
        f"{BASE_URL.replace('https', 'wss')}/api/afk/ws",
        on_message=on_message,
        cookie=f"pingless.sid={PINGLESS_SID}",
        on_error=on_error,
        on_close=on_close
    )
    _ws_ref = ws
    def listen_quit():
        global _quit_flag
        while not _quit_flag:
            c = sys.stdin.read(1)
            if c == 'q':
                _quit_flag = True
                ws.close()
                break
    t = threading.Thread(target=listen_quit, daemon=True)
    t.start()
    ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE, "check_hostname": False})

def check_and_claim_daily():
    try:
        session = requests.Session()
        jar = requests.cookies.RequestsCookieJar()
        jar.set("pingless.sid", PINGLESS_SID, domain="dash.pingless.org", path="/")
        r = session.get(f"{BASE_URL}/api/dailystatus", cookies=jar, verify=False)
        data = r.json()
        if data.get("text") == "1":
            print(f"[DAILY] Reward available ({data.get('rewardAmount', 150)} credits). Claiming...")
            claim = session.post(f"{BASE_URL}/api/daily-coins", cookies=jar, verify=False)
            print(f"[DAILY] Claim response: {claim.status_code} {claim.text[:100]}")
        elif data.get("text") == "0":
            next_at = data.get("nextClaimAt", "unknown")
            print(f"[DAILY] Already claimed. Next claim at: {next_at}")
        else:
            print(f"[DAILY] Status: {data.get('text', 'unknown')}")
    except Exception as e:
        print(f"[DAILY] Error: {e}")

while not _quit_flag:
    try:
        if not PINGLESS_SID:
            print("\n[ERROR] Set PINGLESS_SID first. Get it from browser: F12 -> Application -> Cookies -> dash.pingless.org -> pingless.sid")
            time.sleep(30)
            continue
        check_and_claim_daily()
        if _quit_flag:
            break
        run_afk()
    except Exception:
        if not _quit_flag:
            traceback.print_exc()
            time.sleep(5)

print("\n[QUIT] Exited.")
