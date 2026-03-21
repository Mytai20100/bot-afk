#Version idk =) this host die on 23/7/2025 
import requests, re, json, websocket, ssl, time, traceback, base64
from datetime import datetime
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DISCORD_TOKEN = ""   # your token Discord 

def format_next_reward(ms):
    return str(int(ms / 1000))
def get_user_coins(session, cookie):
    try:
        resp = session.get("https://my.sryzen.com/api/coins", cookies=cookie, verify=False)
        if resp.status_code == 200:
            return resp.json().get("coins", 0)
    except:
        pass
    return 0
def print_session_log(start_time, coins_pm, next_reward_ms, coins):
    elapsed_sec = int(time.time() - start_time)
    h, m, s = elapsed_sec // 3600, (elapsed_sec % 3600) // 60, elapsed_sec % 60
    session_str = f"{h:02d}:{m:02d}:{s:02d}"
    next_reward_sec = format_next_reward(next_reward_ms)
    print(f"\r| session: {session_str} | coinsPerMinute={coins_pm} nextRewardIn={next_reward_sec}s | coin={coins} |", end='')
while True:
    try:
        start_time = time.time()
        print("\n=== Starting new session (AFK SOLO) ===")
        session = requests.Session()
        login_resp = session.get("http://103.70.164.4:3000/auth/discord/login",
                                 allow_redirects=False, verify=False)
        cookie = login_resp.cookies.get_dict()
        cookie_str = "; ".join([f"{k}={v}" for k, v in cookie.items()])
        urls = re.findall(r'https?://\S+', login_resp.text)
        headers = {"authorization": DISCORD_TOKEN}
        payload = {"authorize": True, "integration_type": 0}
        auth_resp = session.post(urls[0], headers=headers, json=payload, verify=False)
        location = auth_resp.json().get("location", "")
        redirected = location.replace("https://my.sryzen.com/", "http://103.70.164.4:3000/")
        session.get(redirected, cookies=cookie, verify=False)
        afk_resp = session.post("http://103.70.164.4:3000/api/afk", json={}, cookies=cookie, verify=False)
        print(f"[AFK SOLO] {afk_resp.status_code} {afk_resp.text[:200]}")
        coins = get_user_coins(session, cookie)
        def on_message(ws, msg):
            try:
                data = json.loads(msg)
                if data.get("type") == "afk_state":
                    coins_pm = data.get("coinsPerMinute", 0)
                    next_reward = data.get("nextRewardIn", 0)
                    coins_now = get_user_coins(session, cookie)
                    print_session_log(start_time, coins_pm, next_reward, coins_now)
            except:
                pass
        ws = websocket.WebSocketApp(
            "wss://my.sryzen.com/ws",
            on_message=on_message,
            cookie=cookie_str,
            on_error=lambda ws, err: print(f"\n[WS ERROR] {err}"),
            on_close=lambda ws, code, msg: print(f"\n[WS CLOSED] {code} {msg}")
        )
        ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE, "check_hostname": False})
    except Exception:
        traceback.print_exc()
        time.sleep(5)
