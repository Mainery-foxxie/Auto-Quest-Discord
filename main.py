#!/usr/bin/env python3
"""
Discord Quest Auto-Completer
Velocity X Ver (3.0) - Undetected + Custom Table
"""

import requests
import time
import json
import random
import sys
import os
import re
import base64
import traceback
from datetime import datetime, timezone
from typing import Optional

# ── Configuration from file ─────────────────────────────────────────────────────
CONFIG_FILE = "config.json"

def load_config():
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"❌ {CONFIG_FILE} not found. Please create it from config.json.example")
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"❌ {CONFIG_FILE} contains invalid JSON")
        sys.exit(1)

config = load_config()

TOKEN = config.get("TOKEN_DISCORD", "")
if not TOKEN:
    print("❌ TOKEN_DISCORD not set in config.json")
    sys.exit(1)

# Base intervals – will be randomized
BASE_POLL_INTERVAL = config.get("POLL_INTERVAL", 60)
BASE_HEARTBEAT_INTERVAL = config.get("HEARTBEAT_INTERVAL", 20)
AUTO_ACCEPT = config.get("AUTO_ACCEPT", True)
LOG_PROGRESS = config.get("LOG_PROGRESS", True)
DEBUG = config.get("DEBUG", False)  # Set to False to reduce logs

# Randomization ranges
POLL_JITTER = 30   # +/- 30 seconds
HEARTBEAT_JITTER = 15  # +/- 15 seconds

SUPPORTED_TASKS = [
    "WATCH_VIDEO",
    "PLAY_ON_DESKTOP",
    "STREAM_ON_DESKTOP",
    "PLAY_ACTIVITY",
    "WATCH_VIDEO_ON_MOBILE",
]

# ── Logging (minimal for undetected mode) ─────────────────────────────────────
class Colors:
    RESET  = "\033[0m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    CYAN   = "\033[96m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"

def log(msg: str, level: str = "info"):
    if not DEBUG and level == "debug":
        return
    ts = datetime.now().strftime("%H:%M:%S")
    prefix = {
        "info":     f"{Colors.CYAN}[INFO]{Colors.RESET}",
        "ok":       f"{Colors.GREEN}[  OK]{Colors.RESET}",
        "warn":     f"{Colors.YELLOW}[WARN]{Colors.RESET}",
        "error":    f"{Colors.RED}[ ERR]{Colors.RESET}",
        "progress": f"{Colors.DIM}[PROG]{Colors.RESET}",
    }.get(level, f"[{level.upper()}]")
    if LOG_PROGRESS or level != "progress":
        print(f"{Colors.DIM}{ts}{Colors.RESET} {prefix} {msg}")

# ── Rotating User-Agent and Super-Properties ───────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) discord/1.0.9175 Chrome/128.0.6613.186 Electron/32.2.7 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) discord/1.0.9176 Chrome/129.0.6668.100 Electron/32.2.8 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) discord/1.0.9174 Chrome/127.0.6533.120 Electron/31.8.0 Safari/537.36",
]

def get_random_user_agent():
    return random.choice(USER_AGENTS)

def fetch_latest_build_number() -> int:
    FALLBACK = 504649
    try:
        log("Fetching latest build number...", "info")
        ua = get_random_user_agent()
        r = requests.get("https://discord.com/app", headers={"User-Agent": ua}, timeout=15)
        if r.status_code != 200:
            return FALLBACK
        scripts = re.findall(r'/assets/([a-f0-9]+)\.js', r.text)
        if not scripts:
            scripts_alt = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
            scripts = [s.split('/')[-1].replace('.js', '') for s in scripts_alt]
        if not scripts:
            return FALLBACK
        for asset_hash in scripts[-5:]:
            try:
                ar = requests.get(f"https://discord.com/assets/{asset_hash}.js", headers={"User-Agent": ua}, timeout=15)
                m = re.search(r'buildNumber["\s:]+["\s]*(\d{5,7})', ar.text)
                if m:
                    return int(m.group(1))
            except Exception:
                continue
        return FALLBACK
    except Exception:
        return FALLBACK

def make_super_properties(build_number: int) -> str:
    # Randomize some fields to avoid fingerprinting
    client_version = f"1.0.{random.randint(9170, 9180)}"
    obj = {
        "os": "Windows",
        "browser": "Discord Client",
        "release_channel": "stable",
        "client_version": client_version,
        "os_version": f"10.0.{random.randint(22000, 26100)}",
        "os_arch": "x64",
        "app_arch": "x64",
        "system_locale": random.choice(["en-US", "en-GB", "fr-FR", "de-DE"]),
        "browser_user_agent": get_random_user_agent(),
        "browser_version": f"{random.randint(31, 33)}.{random.randint(0, 9)}.{random.randint(0, 9)}",
        "client_build_number": build_number,
        "native_build_number": random.randint(59000, 60000),
        "client_event_source": None,
    }
    return base64.b64encode(json.dumps(obj).encode()).decode()

# ── HTTP helpers with random delays ───────────────────────────────────────────
class DiscordAPI:
    def __init__(self, token: str, build_number: int):
        self.token = token
        self.session = requests.Session()
        self.build_number = build_number
        self.update_headers()

    def update_headers(self):
        ua = get_random_user_agent()
        sp = make_super_properties(self.build_number)
        self.session.headers.update({
            "Authorization": self.token,
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Language": random.choice(["en-US,en;q=0.9", "en-GB,en;q=0.8", "fr-FR,en;q=0.7"]),
            "User-Agent": ua,
            "X-Super-Properties": sp,
            "X-Discord-Locale": random.choice(["en-US", "en-GB", "fr-FR"]),
            "X-Discord-Timezone": random.choice(["America/New_York", "Europe/London", "Asia/Tokyo"]),
            "Origin": "https://discord.com",
            "Referer": "https://discord.com/channels/@me",
        })

    def _random_delay(self):
        # Random delay before request (100-800ms) to appear human
        time.sleep(random.uniform(0.1, 0.8))

    def get(self, path: str, **kwargs) -> requests.Response:
        self._random_delay()
        # Occasionally rotate headers
        if random.random() < 0.05:  # 5% chance per request
            self.update_headers()
        url = f"https://discord.com/api/v9{path}"
        log(f"GET {path}", "debug")
        r = self.session.get(url, **kwargs)
        return r

    def post(self, path: str, payload: Optional[dict] = None, **kwargs) -> requests.Response:
        self._random_delay()
        if random.random() < 0.05:
            self.update_headers()
        url = f"https://discord.com/api/v9{path}"
        log(f"POST {path}", "debug")
        r = self.session.post(url, json=payload, **kwargs)
        return r

    def validate_token(self) -> bool:
        try:
            r = self.get("/users/@me")
            if r.status_code == 200:
                user = r.json()
                name = user.get("username", "?")
                self.user_id = user['id']
                self.username = name
                log(f"Logged in as: {Colors.BOLD}{name}{Colors.RESET} (ID: {user['id']})", "ok")
                return True
            else:
                log(f"Invalid token", "error")
                return False
        except Exception as e:
            log(f"Cannot connect: {e}", "error")
            return False

# ── Quest helpers (same as before) ────────────────────────────────────────────
def _get(d: Optional[dict], *keys):
    if d is None:
        return None
    for k in keys:
        if k in d:
            return d[k]
    return None

def get_task_config(quest: dict) -> Optional[dict]:
    cfg = quest.get("config", {})
    return _get(cfg, "taskConfig", "task_config", "taskConfigV2", "task_config_v2")

def get_quest_name(quest: dict) -> str:
    cfg = quest.get("config", {})
    msgs = cfg.get("messages", {})
    name = _get(msgs, "questName", "quest_name")
    if name:
        return name.strip()
    game = _get(msgs, "gameTitle", "game_title")
    if game:
        return game.strip()
    app_name = cfg.get("application", {}).get("name")
    if app_name:
        return app_name
    return f"Quest#{quest.get('id', '?')}"

def get_quest_reward(quest: dict) -> str:
    cfg = quest.get("config", {})
    msgs = cfg.get("messages", {})
    reward = _get(msgs, "rewardName", "reward_name")
    if reward:
        return reward.strip()
    rewards = cfg.get("rewards", [])
    if rewards and isinstance(rewards, list):
        first = rewards[0]
        if isinstance(first, dict):
            return first.get("name", "Unknown Reward")
    return "Unknown Reward"

def get_expires_at(quest: dict) -> Optional[str]:
    cfg = quest.get("config", {})
    return _get(cfg, "expiresAt", "expires_at")

def get_user_status(quest: dict) -> dict:
    us = _get(quest, "userStatus", "user_status")
    return us if isinstance(us, dict) else {}

def is_completable(quest: dict) -> bool:
    expires = get_expires_at(quest)
    if expires:
        try:
            exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            if exp_dt <= datetime.now(timezone.utc):
                return False
        except Exception:
            pass
    tc = get_task_config(quest)
    if not tc or "tasks" not in tc:
        return False
    tasks = tc["tasks"]
    return any(tasks.get(t) is not None for t in SUPPORTED_TASKS)

def is_enrolled(quest: dict) -> bool:
    us = get_user_status(quest)
    return bool(_get(us, "enrolledAt", "enrolled_at"))

def is_completed(quest: dict) -> bool:
    us = get_user_status(quest)
    return bool(_get(us, "completedAt", "completed_at"))

def get_task_type(quest: dict) -> Optional[str]:
    tc = get_task_config(quest)
    if not tc or "tasks" not in tc:
        return None
    for t in SUPPORTED_TASKS:
        if tc["tasks"].get(t) is not None:
            return t
    return None

def get_seconds_needed(quest: dict) -> int:
    tc = get_task_config(quest)
    task_type = get_task_type(quest)
    if not tc or not task_type:
        return 0
    return tc["tasks"][task_type].get("target", 0)

def get_seconds_done(quest: dict) -> float:
    task_type = get_task_type(quest)
    if not task_type:
        return 0
    us = get_user_status(quest)
    progress = us.get("progress", {})
    if not progress:
        progress = {}
    return progress.get(task_type, {}).get("value", 0)

def get_enrolled_at(quest: dict) -> Optional[str]:
    us = get_user_status(quest)
    return _get(us, "enrolledAt", "enrolled_at")

def get_time_left_string(quest: dict) -> str:
    if is_completed(quest):
        return f"{Colors.GREEN}✅ DONE{Colors.RESET}"
    expires = get_expires_at(quest)
    if expires:
        try:
            exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if exp_dt <= now:
                return f"{Colors.RED}Expired{Colors.RESET}"
            delta = exp_dt - now
            total_seconds = int(delta.total_seconds())
            if total_seconds > 0:
                hours = total_seconds // 3600
                minutes = (total_seconds % 3600) // 60
                if hours > 0:
                    return f"{hours}h {minutes}m left"
                else:
                    return f"{minutes}m left"
        except Exception:
            pass
    if is_enrolled(quest) and not is_completed(quest):
        needed = get_seconds_needed(quest)
        done = get_seconds_done(quest)
        remaining = max(0, needed - done)
        if remaining <= 0:
            return f"{Colors.GREEN}✅ DONE{Colors.RESET}"
        minutes = remaining // 60
        seconds = remaining % 60
        if minutes > 0:
            return f"{minutes}m {seconds}s left"
        else:
            return f"{seconds}s left"
    return f"{Colors.YELLOW}○ Not started{Colors.RESET}"

def get_status_icon(quest: dict) -> str:
    if is_completed(quest):
        return f"{Colors.GREEN}✅ Completed{Colors.RESET}"
    elif is_enrolled(quest):
        return f"{Colors.YELLOW}▶ In Progress{Colors.RESET}"
    else:
        return f"{Colors.DIM}○ Not Started{Colors.RESET}"

# ── Undetected completion methods ─────────────────────────────────────────────
class QuestAutocompleter:
    def __init__(self, api: DiscordAPI):
        self.api = api
        self.completed_ids: set = set()
        self.user_id = getattr(api, 'user_id', 'Unknown')
        self.username = getattr(api, 'username', 'User')

    def fetch_quests(self) -> list:
        try:
            r = self.api.get("/quests/@me")
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict):
                    return data.get("quests", [])
                elif isinstance(data, list):
                    return data
                return []
            elif r.status_code == 429:
                retry_after = r.json().get("retry_after", 10)
                log(f"Rate limited – waiting {retry_after}s", "warn")
                time.sleep(retry_after + random.uniform(0, 2))
                return self.fetch_quests()
            else:
                return []
        except Exception as e:
            log(f"Error fetching quests: {e}", "error")
            return []

    def enroll_quest(self, quest: dict) -> bool:
        name = get_quest_name(quest)
        qid = quest["id"]
        # Random delay before enrolling (human reaction)
        time.sleep(random.uniform(1, 4))
        for attempt in range(1, 4):
            try:
                r = self.api.post(f"/quests/{qid}/enroll", {
                    "location": 11,
                    "is_targeted": False,
                    "metadata_raw": None,
                    "metadata_sealed": None,
                    "traffic_metadata_raw": quest.get("traffic_metadata_raw"),
                    "traffic_metadata_sealed": quest.get("traffic_metadata_sealed"),
                })
                if r.status_code == 429:
                    retry_after = r.json().get("retry_after", 5)
                    wait = retry_after + random.uniform(0.5, 2)
                    log(f"Rate limited enrolling {name} – waiting {wait:.1f}s", "warn")
                    time.sleep(wait)
                    continue
                if r.status_code in (200, 201, 204):
                    log(f"Enrolled: {Colors.BOLD}{name}{Colors.RESET}", "ok")
                    # Wait random time before starting completion
                    time.sleep(random.uniform(2, 8))
                    return True
                return False
            except Exception as e:
                log(f"Error enrolling: {e}", "error")
                return False
        return False

    def auto_accept(self, quests: list) -> list:
        if not AUTO_ACCEPT:
            return quests
        unaccepted = [q for q in quests if not is_enrolled(q) and not is_completed(q) and is_completable(q)]
        if not unaccepted:
            return quests
        log(f"Found {len(unaccepted)} unenrolled quests – auto‑accepting...", "info")
        for q in unaccepted:
            self.enroll_quest(q)
            time.sleep(random.uniform(5, 12))
        return self.fetch_quests()

    def complete_video(self, quest: dict):
        name = get_quest_name(quest)
        qid = quest["id"]
        seconds_needed = get_seconds_needed(quest)
        seconds_done = get_seconds_done(quest)
        enrolled_at_str = get_enrolled_at(quest)
        if enrolled_at_str:
            enrolled_ts = datetime.fromisoformat(enrolled_at_str.replace("Z", "+00:00")).timestamp()
        else:
            enrolled_ts = time.time()

        log(f"🎬 Video: {Colors.BOLD}{name}{Colors.RESET} ({seconds_done:.0f}/{seconds_needed}s)", "info")

        # Don't complete faster than real time
        start_time = time.time()
        last_sent = seconds_done
        while seconds_done < seconds_needed:
            elapsed = time.time() - start_time
            # Calculate realistic progress: cannot exceed real elapsed time + small buffer
            max_realistic = min(seconds_needed, elapsed + 5)
            if last_sent < max_realistic:
                # Send progress update
                new_timestamp = min(seconds_needed, last_sent + random.uniform(3, 9))
                try:
                    r = self.api.post(f"/quests/{qid}/video-progress", {
                        "timestamp": new_timestamp
                    })
                    if r.status_code == 200:
                        body = r.json()
                        if body.get("completed_at"):
                            log(f"✅ Completed: {name}", "ok")
                            return
                        last_sent = new_timestamp
                        seconds_done = new_timestamp
                        log(f"  [{name}] {seconds_done:.0f}/{seconds_needed}s", "progress")
                    elif r.status_code == 429:
                        retry_after = r.json().get("retry_after", 5)
                        time.sleep(retry_after + random.uniform(0, 1))
                except Exception:
                    pass
            # Wait random interval between 1-4 seconds
            time.sleep(random.uniform(1, 4))

        # Final sync
        try:
            self.api.post(f"/quests/{qid}/video-progress", {"timestamp": seconds_needed})
        except Exception:
            pass
        log(f"✅ Completed: {name}", "ok")

    def complete_heartbeat(self, quest: dict):
        name = get_quest_name(quest)
        qid = quest["id"]
        task_type = get_task_type(quest)
        seconds_needed = get_seconds_needed(quest)
        seconds_done = get_seconds_done(quest)
        remaining = max(0, seconds_needed - seconds_done)
        log(f"🎮 {task_type}: {name} (~{remaining // 60} min left)", "info")

        pid = random.randint(1000, 99999)
        start_time = time.time()
        last_heartbeat = 0

        while seconds_done < seconds_needed:
            now = time.time()
            # Heartbeat interval randomized between 15-35 seconds
            interval = random.uniform(15, 35)
            if now - last_heartbeat >= interval:
                try:
                    r = self.api.post(f"/quests/{qid}/heartbeat", {
                        "stream_key": f"call:{random.randint(0,5)}:{pid}",
                        "terminal": False,
                    })
                    if r.status_code == 200:
                        body = r.json()
                        progress_data = body.get("progress", {})
                        if progress_data and task_type in progress_data:
                            seconds_done = progress_data[task_type].get("value", seconds_done)
                        log(f"  [{name}] {seconds_done:.0f}/{seconds_needed}s", "progress")
                        if body.get("completed_at") or seconds_done >= seconds_needed:
                            break
                    elif r.status_code == 429:
                        retry_after = r.json().get("retry_after", 10)
                        time.sleep(retry_after + random.uniform(0, 2))
                        continue
                except Exception:
                    pass
                last_heartbeat = now
            time.sleep(random.uniform(2, 5))

        try:
            self.api.post(f"/quests/{qid}/heartbeat", {
                "stream_key": f"call:0:{pid}",
                "terminal": True,
            })
        except Exception:
            pass
        log(f"✅ Completed: {name}", "ok")

    def complete_activity(self, quest: dict):
        # Same as heartbeat but with fixed stream_key
        name = get_quest_name(quest)
        qid = quest["id"]
        seconds_needed = get_seconds_needed(quest)
        seconds_done = get_seconds_done(quest)
        remaining = max(0, seconds_needed - seconds_done)
        log(f"🕹️ Activity: {name} (~{remaining // 60} min left)", "info")

        stream_key = f"call:{random.randint(0,5)}:1"
        start_time = time.time()
        last_heartbeat = 0

        while seconds_done < seconds_needed:
            now = time.time()
            interval = random.uniform(15, 35)
            if now - last_heartbeat >= interval:
                try:
                    r = self.api.post(f"/quests/{qid}/heartbeat", {
                        "stream_key": stream_key,
                        "terminal": False,
                    })
                    if r.status_code == 200:
                        body = r.json()
                        progress_data = body.get("progress", {})
                        if progress_data and "PLAY_ACTIVITY" in progress_data:
                            seconds_done = progress_data["PLAY_ACTIVITY"].get("value", seconds_done)
                        log(f"  [{name}] {seconds_done:.0f}/{seconds_needed}s", "progress")
                        if body.get("completed_at") or seconds_done >= seconds_needed:
                            break
                    elif r.status_code == 429:
                        retry_after = r.json().get("retry_after", 10)
                        time.sleep(retry_after + random.uniform(0, 2))
                        continue
                except Exception:
                    pass
                last_heartbeat = now
            time.sleep(random.uniform(2, 5))

        try:
            self.api.post(f"/quests/{qid}/heartbeat", {
                "stream_key": stream_key,
                "terminal": True,
            })
        except Exception:
            pass
        log(f"✅ Completed: {name}", "ok")

    def process_quest(self, quest: dict):
        qid = quest.get("id")
        name = get_quest_name(quest)
        task_type = get_task_type(quest)
        if not task_type:
            return
        if qid in self.completed_ids:
            return
        log(f"━━━ Starting: {name} (task: {task_type}) ━━━", "info")
        # Random delay before starting
        time.sleep(random.uniform(1, 5))
        if task_type in ("WATCH_VIDEO", "WATCH_VIDEO_ON_MOBILE"):
            self.complete_video(quest)
        elif task_type in ("PLAY_ON_DESKTOP", "STREAM_ON_DESKTOP"):
            self.complete_heartbeat(quest)
        elif task_type == "PLAY_ACTIVITY":
            self.complete_activity(quest)
        self.completed_ids.add(qid)

    # ── Custom table exactly like your screenshot ─────────────────────────────
    def print_quest_table(self, quests: list):
        # Header with "SYSTEM RUNNING..."
        print(f"\n{Colors.BOLD}{Colors.CYAN}SYSTEM RUNNING...{Colors.RESET}\n")
        # User account line (mimics your screenshot)
        print(f"{Colors.BOLD}User Account:{Colors.RESET} {self.username}    {Colors.BOLD}User ID:{Colors.RESET} {self.user_id}\n")
        print(f"{Colors.BOLD}{Colors.CYAN}LIVE PROGRESS{Colors.RESET}\n")

        # Build rows
        rows = []
        for idx, q in enumerate(quests, start=1):
            name = get_quest_name(q)
            reward = get_quest_reward(q)
            time_left = get_time_left_string(q)
            status = get_status_icon(q)
            rows.append((idx, name, reward, time_left, status))

        if not rows:
            print(f"{Colors.YELLOW}No quests found.{Colors.RESET}")
            return

        # Column widths
        max_no = len(str(len(rows)))
        max_name = min(max(len(r[1]) for r in rows), 30)
        max_reward = min(max(len(r[2]) for r in rows), 25)
        max_time = min(max(len(r[3].replace(Colors.RESET, '')) for r in rows), 20)
        max_status = min(max(len(r[4].replace(Colors.RESET, '')) for r in rows), 15)

        # Header separator
        header = f"| {'No':<{max_no}} | {'Quest Name':<{max_name}} | {'Reward':<{max_reward}} | {'Time Left':<{max_time}} | {'Status':<{max_status}} |"
        separator = f"|-{'-'*max_no}-|-{'-'*max_name}-|-{'-'*max_reward}-|-{'-'*max_time}-|-{'-'*max_status}-|"
        print(header)
        print(separator)

        for row in rows:
            no, name, reward, time_left, status = row
            if len(name) > max_name:
                name = name[:max_name-3] + "..."
            if len(reward) > max_reward:
                reward = reward[:max_reward-3] + "..."
            print(f"| {no:<{max_no}} | {name:<{max_name}} | {reward:<{max_reward}} | {time_left:<{max_time}} | {status:<{max_status}} |")
        print()

    # ── Main loop with randomized poll interval ───────────────────────────────
    def run(self):
        log("=" * 60, "info")
        log(f"{Colors.BOLD}Discord Quest Auto-Completer v3.0 (Undetected){Colors.RESET}", "info")
        log(f"Auto-accept: {'ON' if AUTO_ACCEPT else 'OFF'}", "info")
        log("=" * 60, "info")

        cycle = 0
        while True:
            cycle += 1
            log(f"── Scan #{cycle} ──", "info")

            quests = self.fetch_quests()
            if not quests:
                log("No quests found", "info")
            else:
                self.print_quest_table(quests)
                quests = self.auto_accept(quests)
                actionable = [q for q in quests if is_enrolled(q) and not is_completed(q) and is_completable(q) and q.get("id") not in self.completed_ids]
                if actionable:
                    log(f"\n{len(actionable)} quest(s) ready to complete", "info")
                    for q in actionable:
                        self.process_quest(q)
                else:
                    log("No quests need completion", "info")

            # Randomize next poll interval
            next_wait = BASE_POLL_INTERVAL + random.randint(-POLL_JITTER, POLL_JITTER)
            next_wait = max(30, next_wait)  # Don't go below 30 seconds
            log(f"\nWaiting {next_wait}s... (Ctrl+C to stop)\n", "info")
            time.sleep(next_wait)

# ── Entry point ────────────────────────────────────────────────────────────────
def main():
    print(f"""
{Colors.BOLD}{Colors.CYAN}╔══════════════════════════════════════════════╗
║     Discord Quest Auto-Completer            ║
║        Velocity X Ver (3.0)                 ║
║     Undetected Mode · Custom Table          ║
╚══════════════════════════════════════════════╝{Colors.RESET}
""")
    build_number = fetch_latest_build_number()
    api = DiscordAPI(TOKEN, build_number)
    if not api.validate_token():
        sys.exit(1)
    completer = QuestAutocompleter(api)
    try:
        completer.run()
    except KeyboardInterrupt:
        print()
        log("Stopped.", "info")
        sys.exit(0)

if __name__ == "__main__":
    main()
