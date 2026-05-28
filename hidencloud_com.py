# it isn't an bot afk 
# IT help cover chat with ai agent cloudy (claude sonnect 4.6) in freepanel of hidencloud
# I think it so 
import requests
import json
import sys
import re
import os
import shutil
import time
import uuid
import yaml
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from threading import Thread, Lock

CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))

BASE_URL = "https://freepanel.hidencloud.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.5",
}

COOKIE_REFRESH_SCRIPT = r"""
(async () => {
    const cookies = await chrome.cookies.getAll({domain: 'freepanel.hidencloud.com'});
    const allCookies = await chrome.cookies.getAll({domain: '.hidencloud.com'});
    const merged = {};
    for (const c of [...cookies, ...allCookies]) merged[c.name] = c.value;
    const html = document.querySelector('meta[name="csrf-token"]')?.content;
    const config = {cookies: merged, csrf: html};
    console.log('=== COPY BELOW ===');
    copy(JSON.stringify(config, null, 2));
    console.log('=== COPIED TO CLIPBOARD ===');
    return config;
})();
""".strip()

COOKIE_REFRESH_FALLBACK = r"""
// Paste this in browser console, then paste result back here
var c={};document.cookie.split('; ').forEach(x=>{var p=x.indexOf('=');c[x.slice(0,p)]=x.slice(p+1)});
var csrf=document.querySelector('meta[name=\"csrf-token\"]')?.content;
JSON.stringify({cookies:c,csrf:csrf})
""".strip()


CONFIG_FILE = 'config.yml'

def load_config():
    path = os.path.join(CONFIG_DIR, CONFIG_FILE)
    if os.path.exists(path):
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}


def save_config(cfg):
    path = os.path.join(CONFIG_DIR, CONFIG_FILE)
    with open(path, 'w') as f:
        yaml.dump(cfg, f, default_flow_style=False)


class CloudyBot:
    def __init__(self, server_input, config=None):
        self.session = requests.Session()
        self.server_uuid = None
        self.server_short = None
        self.conversation_id = None
        self.csrf_token = None
        self.server_aliases = {}
        self.settings = {
            "allow_tools": True,
            "allow_web_search": False,
            "allow_thinking": True,
        }
        if config:
            self.server_aliases = config.get('servers') or {}
            self._apply_config(config)
        self._apply_server(server_input)

    def _apply_server(self, server_input):
        if '-' in server_input and server_input.count('-') == 4:
            self.server_uuid = server_input
            self.server_short = server_input.split('-')[0]
        elif server_input in self.server_aliases:
            self.server_uuid = self.server_aliases[server_input]
            self.server_short = self.server_uuid.split('-')[0]
        elif server_input in self.server_aliases.values():
            self.server_uuid = server_input
            self.server_short = server_input.split('-')[0]
        else:
            self.server_short = server_input
            self.server_uuid = server_input

    def _apply_config(self, cfg):
        ck = cfg.get('cookies', {})
        for k, v in ck.items():
            self.session.cookies.set(k, v, domain="freepanel.hidencloud.com", path="/")
            self.session.cookies.set(k, v, domain=".hidencloud.com", path="/")
        self.csrf_token = cfg.get('csrf') or self.csrf_token
        s = cfg.get('settings')
        if s:
            self.settings.update(s)

    def refresh_csrf(self):
        url = f"{BASE_URL}/server/{self.server_short}"
        resp = self.session.get(url, headers=HEADERS)
        m = re.search(r'<meta name="csrf-token" content="([^"]+)"', resp.text)
        self.csrf_token = m.group(1) if m else None
        return self.csrf_token is not None

    def is_connected(self):
        return bool(self.csrf_token) and self.refresh_csrf()

    def connect(self):
        if self.refresh_csrf():
            return True
        return False

    def _api_headers(self, accept="application/json"):
        return {
            "X-CSRF-TOKEN": self.csrf_token,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": accept,
            **HEADERS,
            "Referer": f"{BASE_URL}/server/{self.server_short}",
        }

    def send_message_stream(self, message):
        url = f"{BASE_URL}/api/client/servers/{self.server_uuid}/cloudy/message/stream"
        headers = {
            **self._api_headers("text/event-stream"),
            "Content-Type": "application/json",
        }
        payload = {
            "message": message,
            "conversation_id": self.conversation_id or 0,
            "allow_tools": self.settings["allow_tools"],
            "allow_thinking": self.settings["allow_thinking"],
            "allow_web_search": self.settings["allow_web_search"],
            "image_tokens": [],
        }
        resp = self.session.post(url, json=payload, headers=headers, stream=True)
        for line in resp.iter_lines():
            if not line:
                continue
            if line.startswith(b'data: '):
                yield json.loads(line[6:])

    def send_message(self, message):
        last_cid = self.conversation_id
        full_text = ""
        for data in self.send_message_stream(message):
            t = data.get('type')
            if t == 'conversation_id':
                last_cid = data['id']
            elif t == 'token':
                full_text += data.get('text', '')
            elif t == 'done':
                self.conversation_id = last_cid
                return full_text, data
        self.conversation_id = last_cid
        return full_text, {}

    def fetch_conversations(self):
        url = f"{BASE_URL}/api/client/servers/{self.server_uuid}/cloudy/conversations"
        return self.session.get(url, headers=self._api_headers()).json()

    def fetch_messages(self, conv_id):
        url = f"{BASE_URL}/api/client/servers/{self.server_uuid}/cloudy/conversations/{conv_id}/messages"
        return self.session.get(url, headers=self._api_headers()).json()

    def show_mcp_menu(self):
        keys = ["allow_web_search", "allow_tools", "allow_thinking"]
        labels = {
            "allow_web_search": "Web Search",
            "allow_tools": "Allow server interaction",
            "allow_thinking": "Deep thinking mode",
        }
        descs = {
            "allow_web_search": "Let AI search the web",
            "allow_tools": "Cloudy can manage your server (files, commands, power)",
            "allow_thinking": "Show AI's internal reasoning",
        }
        while True:
            print("\n\x1b[36m=== MCP Settings ===\x1b[0m")
            for i, k in enumerate(keys):
                ck = "\x1b[32mON\x1b[0m" if self.settings[k] else "\x1b[31mOFF\x1b[0m"
                print(f"  {i+1}. [{ck}] {labels[k]}")
                print(f"     \x1b[90m{descs[k]}\x1b[0m")
            print("  b. Back")
            ch = input("\nToggle # (1-3) or b: ").strip().lower()
            if ch == 'b':
                break
            if ch in ('1', '2', '3'):
                self.settings[keys[int(ch) - 1]] = not self.settings[keys[int(ch) - 1]]

    def show_sessions(self):
        data = self.fetch_conversations()
        convs = data.get('data', [])
        if not convs:
            print("No conversations found.")
            return
        print("\n\x1b[36m=== Conversations ===\x1b[0m")
        for i, c in enumerate(convs):
            cid = c.get('id', '?')
            title = (c.get('title') or '(no title)')[:60]
            dt = c.get('updated_at', '')[:10]
            print(f"  {i+1}. [ID:{cid}] {title} \x1b[90m({dt})\x1b[0m")
        print("  b. Back")
        ch = input("\nSelect # (or b): ").strip().lower()
        if ch == 'b':
            return
        try:
            idx = int(ch) - 1
            if 0 <= idx < len(convs):
                self.view_conversation(convs[idx]['id'])
        except (ValueError, IndexError):
            print("Invalid selection")

    def view_conversation(self, conv_id):
        data = self.fetch_messages(conv_id)
        msgs = data.get('messages', [])
        if not msgs:
            print("No messages.")
            return
        lines = []
        for m in msgs:
            role = m.get('role', '?').upper()
            content = m.get('content', '')
            lines.append(f"\x1b[36m--- {role} ---\x1b[0m")
            lines.append(content)
        h = shutil.get_terminal_size().lines - 3
        total = len(lines)
        idx = 0
        while idx < total:
            os.system('clear' if os.name == 'posix' else 'cls')
            for l in lines[idx:idx + h]:
                print(l)
            end = min(idx + h, total)
            print(f"\x1b[90m--- {idx + 1}-{end}/{total} | Enter=next, k=prev, q=quit ---\x1b[0m")
            try:
                cmd = input().strip().lower()
                if cmd == 'q':
                    break
                if cmd in ('', 'j', 'down'):
                    idx += h
                elif cmd in ('k', 'up'):
                    idx = max(0, idx - h)
            except (EOFError, KeyboardInterrupt):
                break
        set_cur = input(f"\nSet as current session? (y/N): ").strip().lower()
        if set_cur == 'y':
            self.conversation_id = conv_id
            print(f"Active session: {conv_id}")

    def new_session(self):
        if self.conversation_id:
            print(f"Cleared session (was: {self.conversation_id})")
        self.conversation_id = None

    def cli_run(self):
        print("\nCommands:")
        print("  \x1b[32m/mcp\x1b[0m      - Toggle MCP settings")
        print("  \x1b[32m/session\x1b[0m  - View conversations & history")
        print("  \x1b[32m/new\x1b[0m      - New conversation")
        print("  \x1b[32m/refresh\x1b[0m  - Show cookie refresh instructions")
        print("  \x1b[32m/exit\x1b[0m     - Quit")
        print()
        while True:
            try:
                raw = input("\x1b[32m>\x1b[0m ")
            except (EOFError, KeyboardInterrupt):
                print("\nBye!")
                break
            cmd = raw.strip()
            if cmd.lower() in ('/exit', '/quit'):
                break
            if cmd == '/mcp':
                self.show_mcp_menu()
                continue
            if cmd == '/session':
                self.show_sessions()
                continue
            if cmd == '/new':
                self.new_session()
                continue
            if cmd == '/refresh':
                print_cookie_help()
                continue
            if not cmd:
                continue
            self.conversation_id = None
            for data in self.send_message_stream(cmd):
                t = data.get('type')
                if t in ('conversation_id',):
                    self.conversation_id = data['id']
                elif t == 'status':
                    print(f"\n\x1b[33m[{data.get('message', '')}]\x1b[0m", flush=True)
                elif t == 'thinking_token':
                    pass
                elif t == 'token':
                    print(data.get('text', ''), end='', flush=True)
                elif t == 'done':
                    print()
                    usage = data.get('remaining', {})
                    if usage:
                        print(
                            f"\x1b[90m[Tokens: {usage.get('daily_used', '?')}/{usage.get('daily_limit', '?')}]\x1b[0m")


def print_cookie_help():
    print("""
\x1b[33m=== Cookie expired! Get fresh cookies: ===\x1b[0m

\x1b[36mOption 1: Console method\x1b[0m
1. Open browser DevTools (F12) on the HidenCloud tab
2. Copy and paste this into Console, then press Enter:

\x1b[32m""" + COOKIE_REFRESH_FALLBACK + """\x1b[0m

3. Copy the JSON output, then write to test/ik/config.yml

\x1b[36mOption 2: Use Chrome extension (if available)\x1b[0m
1. Open the HidenCloud tab in Chrome
2. Paste this in Console:

\x1b[32m""" + COOKIE_REFRESH_SCRIPT + """\x1b[0m

3. It will copy config to clipboard automatically
""")


# ---------- OpenAI-compatible API Server ----------

class OpenAICompatHandler(BaseHTTPRequestHandler):
    server_config = {}
    server_bot = None
    bot_lock = Lock()

    def log_message(self, format, *args):
        pass

    def _auth(self):
        key = self.headers.get("Authorization", "").removeprefix("Bearer ")
        valid = self.server_config.get("api_key", "")
        return key == valid

    def _send_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _model_name(self):
        return self.server_config.get("model", "cloudy")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/v1/models":
            self._send_json(200, {
                "object": "list",
                "data": [{"id": self._model_name(), "object": "model", "created": int(time.time()), "owned_by": "hidencloud"}]
            })
        elif parsed.path == "/health":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/v1/chat/completions":
            self._send_json(404, {"error": "Not found"})
            return
        if not self._auth():
            self._send_json(401, {"error": "Invalid API key"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            req = json.loads(body)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        msgs = req.get("messages", [])
        stream = req.get("stream", False)

        if not msgs:
            self._send_json(400, {"error": "No messages"})
            return

        last_user = ""
        for m in msgs:
            if m.get("role") == "user":
                last_user = m.get("content", "")

        if not last_user:
            self._send_json(400, {"error": "No user message"})
            return

        with self.bot_lock:
            bot = self.server_bot
            if not bot or not bot.is_connected():
                self._send_json(503, {"error": "Session expired. Refresh cookies and restart server."})
                return
            model = req.get("model", self._model_name())
            cid = req.get("conversation_id", bot.conversation_id or 0)
            bot.conversation_id = cid

            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()

                resp_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
                created = int(time.time())

                chunk = {
                    "id": resp_id, "object": "chat.completion.chunk", "created": created,
                    "model": model, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]
                }
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.flush()

                full = ""
                for data in bot.send_message_stream(last_user):
                    t = data.get('type')
                    if t == 'conversation_id':
                        bot.conversation_id = data['id']
                    elif t == 'token':
                        text = data.get('text', '')
                        full += text
                        chunk = {
                            "id": resp_id, "object": "chat.completion.chunk", "created": created,
                            "model": model,
                            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]
                        }
                        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                        self.wfile.flush()
                    elif t == 'done':
                        chunk = {
                            "id": resp_id, "object": "chat.completion.chunk", "created": created,
                            "model": model,
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                        }
                        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                        break
            else:
                full = ""
                done_data = None
                for data in bot.send_message_stream(last_user):
                    t = data.get('type')
                    if t == 'conversation_id':
                        bot.conversation_id = data['id']
                    elif t == 'token':
                        full += data.get('text', '')
                    elif t == 'done':
                        done_data = data

                resp_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
                created = int(time.time())
                usage = done_data.get('remaining', {}) if done_data else {}
                self._send_json(200, {
                    "id": resp_id, "object": "chat.completion", "created": created,
                    "model": model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": full}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": usage.get('daily_used', 0),
                        "completion_tokens": len(full.split()),
                        "total_tokens": usage.get('daily_used', 0) + len(full.split()),
                    }
                })


def serve_mode(bot, host="0.0.0.0", port=4999):
    cfg = load_config()
    api_key = cfg.get("api_key", "cloudy-default-key")
    model = cfg.get("model", "claude-sonnect-4-6")

    OpenAICompatHandler.server_config = {"api_key": api_key, "model": model}
    OpenAICompatHandler.server_bot = bot

    server = HTTPServer((host, port), OpenAICompatHandler)
    print(f"\x1b[36mOpenAI-compatible API running on http://{host}:{port}\x1b[0m")
    print(f"\x1b[36mEndpoints:\x1b[0m")
    print(f"  POST http://{host}:{port}/v1/chat/completions")
    print(f"  GET  http://{host}:{port}/v1/models")
    print(f"\x1b[36mModel:\x1b[0m {model}")
    print(f"\x1b[36mAPI Key:\x1b[0m {api_key}")
    print(f"\x1b[36mUsage:\x1b[0m curl http://{host}:{port}/v1/chat/completions \\")
    print(f"    -H \"Authorization: Bearer {api_key}\" \\")
    print(f"    -H \"Content-Type: application/json\" \\")
    print(f"    -d '{{\"model\":\"{model}\",\"messages\":[{{\"role\":\"user\",\"content\":\"hello\"}}],\"stream\":true}}'")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


def print_usage():
    cfg = load_config()
    servers = cfg.get('servers', {})
    model = cfg.get('model', 'claude-sonnect-4-6')
    print("Usage:")
    if servers:
        print("  python3 bot.py <name>       # Chat with a named server")
        for name in servers:
            uuid = servers[name]
            short = uuid.split('-')[0]
            print(f"    \x1b[36m{name}\x1b[0m  ->  {short}  ({uuid[:20]}...)")
    print("  python3 bot.py <short_code>  # Or use short code directly")
    print("  python3 bot.py <name> serve [port]  # API server mode")
    print("  python3 bot.py --help-cookies        # Cookie help")
    print()
    print("Configured servers (edit test/ik/config.yml → servers:):")
    for name in servers:
        print(f"  \x1b[32m{name}\x1b[0m")
    print()
    print("API server model:", model)
    print()
    print("Examples from config.yml:")
    for name in servers:
        print(f"  python3 bot.py {name}")
        print(f"  python3 bot.py {name} serve 4999")


def main():
    if len(sys.argv) < 2:
        print_usage()
        sys.exit(1)

    if sys.argv[1] == "--help-cookies":
        print_cookie_help()
        return

    cfg = load_config()
    if not cfg.get('cookies'):
        print("\x1b[31mNo cookies found!\x1b[0m")
        print_cookie_help()
        sys.exit(1)

    bot = CloudyBot(sys.argv[1], cfg)
    if not bot.connect():
        print("\x1b[31mConnection failed. Cookies may be expired.\x1b[0m")
        print_cookie_help()
        sys.exit(1)

    if len(sys.argv) >= 3 and sys.argv[2] == "serve":
        port = int(sys.argv[3]) if len(sys.argv) >= 4 else 4999
        serve_mode(bot, port=port)
    else:
        print(f"Connected: {bot.server_short}")
        bot.cli_run()


if __name__ == "__main__":
    main()
