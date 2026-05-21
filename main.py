#!/usr/bin/env python3
"""
Discord Quest Auto-Completer  – improved edition
Reads configuration from config.json
"""

import requests
import time
import json
import random
import sys
import re
import base64
import traceback
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List

# ── Configuration ──────────────────────────────────────────────────────────────
CONFIG_FILE = "config.json"

# ── Constants ──────────────────────────────────────────────────────────────────
BUILD_NUMBER_FALLBACK = 504649
ENROLL_LOCATION       = 11          # Discord's internal location ID for quest enrollment
MAX_RATE_LIMIT_WAITS  = 5           # Max consecutive 429s before giving up on an operation
MAX_FETCH_RETRIES     = 3           # Max retries for fetching quests

VIDEO_TICK_INTERVAL   = 1.0         # seconds between video-progress ticks (matches extension)
VIDEO_SPEED           = 7.0         # video seconds advanced per tick (matches extension)
VIDEO_MAX_FUTURE      = 10.0        # max seconds ahead of real-time we can report

# ── Quest state (one per active quest, shared across threads) ──────────────────
@dataclass
class QuestState:
    """Mutable progress snapshot for a single in-flight quest."""
    quest:          dict
    task_type:      str
    seconds_needed: int
    seconds_done:   float
    enrolled_ts:    float           # UNIX timestamp when quest was enrolled
    name:           str
    completed:      bool  = False
    last_update:    float = field(default_factory=time.time)
    lock:           threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def advance(self, value: float):
        """Thread-safe progress update."""
        with self.lock:
            self.seconds_done = value
            if self.seconds_done >= self.seconds_needed:
                self.completed = True

    @property
    def remaining(self) -> float:
        return max(0.0, self.seconds_needed - self.seconds_done)

    @property
    def pct(self) -> float:
        if self.seconds_needed == 0:
            return 100.0
        return min(100.0, self.seconds_done / self.seconds_needed * 100)

def load_config():
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"❌ {CONFIG_FILE} not found.")
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"❌ {CONFIG_FILE} contains invalid JSON")
        sys.exit(1)

config = load_config()

TOKEN = config.get("TOKEN_DISCORD", "")
if not TOKEN:
    print("❌ TOKEN_DISCORD not set in config.json")
    sys.exit(1)

POLL_INTERVAL      = config.get("POLL_INTERVAL", 60)
HEARTBEAT_INTERVAL = config.get("HEARTBEAT_INTERVAL", 20)
AUTO_ACCEPT        = config.get("AUTO_ACCEPT", True)
LOG_PROGRESS       = config.get("LOG_PROGRESS", True)
DEBUG              = config.get("DEBUG", False)   # default OFF for stealth

# Known task types supported for completion
SUPPORTED_TASKS = [
    "WATCH_VIDEO",
    "PLAY_ON_DESKTOP",
    "STREAM_ON_DESKTOP",
    "PLAY_ACTIVITY",
    "WATCH_VIDEO_ON_MOBILE",
    # Additional types discovered via [?] quests – treated as heartbeat
    "PLAY_ON_MOBILE",
    "WATCH_VIDEO_ON_DESKTOP",
    "ACHIEVEMENT_IN_ACTIVITY",
]

# Task types that use heartbeat (play/stream) vs video-progress
HEARTBEAT_TASKS = {
    "PLAY_ON_DESKTOP",
    "STREAM_ON_DESKTOP",
    "PLAY_ACTIVITY",       # same heartbeat endpoint as PLAY_ON_DESKTOP
    "PLAY_ON_MOBILE",
}

ACHIEVEMENT_TASKS = {
    "ACHIEVEMENT_IN_ACTIVITY",
}
VIDEO_TASKS = {
    "WATCH_VIDEO",
    "WATCH_VIDEO_ON_MOBILE",
    "WATCH_VIDEO_ON_DESKTOP",
}

# ── Logging ────────────────────────────────────────────────────────────────────
class C:
    """ANSI color codes — automatically stripped when stdout is not a TTY."""
    _tty = sys.stdout.isatty()

    RESET  = "\033[0m"  if _tty else ""
    GREEN  = "\033[92m" if _tty else ""
    YELLOW = "\033[93m" if _tty else ""
    RED    = "\033[91m" if _tty else ""
    CYAN   = "\033[96m" if _tty else ""
    BOLD   = "\033[1m"  if _tty else ""
    DIM    = "\033[2m"  if _tty else ""

def log(msg: str, level: str = "info"):
    ts = datetime.now().strftime("%H:%M:%S")
    prefix = {
        "info":     f"{C.CYAN}[INFO]{C.RESET}",
        "ok":       f"{C.GREEN}[  OK]{C.RESET}",
        "warn":     f"{C.YELLOW}[WARN]{C.RESET}",
        "error":    f"{C.RED}[ ERR]{C.RESET}",
        "progress": f"{C.DIM}[PROG]{C.RESET}",
        "debug":    f"{C.DIM}[DBG ]{C.RESET}",
    }.get(level, f"[{level.upper()}]")
    if level == "debug" and not DEBUG:
        return
    if LOG_PROGRESS or level != "progress":
        print(f"{C.DIM}{ts}{C.RESET} {prefix} {msg}")

# ── Anti-detection helpers ─────────────────────────────────────────────────────
def jitter(base: float, pct: float = 0.20) -> float:
    """Return base ± pct% random variation."""
    spread = base * pct
    return base + random.uniform(-spread, spread)

def human_sleep(base: float, pct: float = 0.25):
    """Sleep for base seconds with random jitter."""
    t = max(0.5, jitter(base, pct))
    time.sleep(t)

def random_sleep(lo: float, hi: float):
    """Sleep for a uniformly random duration between lo and hi seconds.

    Use this instead of ``human_sleep(random.uniform(lo, hi))`` — the latter
    double-applies randomness (uniform pick *then* ±25% jitter).
    """
    time.sleep(random.uniform(lo, hi))

def _wait_for_rate_limit(response: requests.Response, context: str = "") -> float:
    """Extract retry_after from a 429 response, log it, and sleep.

    Returns the number of seconds waited.
    """
    try:
        retry_after = response.json().get("retry_after", 10)
    except Exception:
        retry_after = 10
    wait = retry_after + random.uniform(0.5, 2)
    label = f" ({context})" if context else ""
    log(f"  Rate limited{label} – waiting {wait:.1f}s", "warn")
    time.sleep(wait)
    return wait

# ── Build number ───────────────────────────────────────────────────────────────
def fetch_latest_build_number() -> int:
    try:
        log("Fetching Discord build number...", "info")
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        )
        r = requests.get("https://discord.com/app", headers={"User-Agent": ua}, timeout=15)
        if r.status_code != 200:
            log(f"Discord page returned {r.status_code}, using fallback", "warn")
            return BUILD_NUMBER_FALLBACK
        scripts = re.findall(r'/assets/([a-f0-9]+)\.js', r.text)
        if not scripts:
            alts = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
            scripts = [s.split('/')[-1].replace('.js', '') for s in alts]
        for asset_hash in scripts[-5:]:
            try:
                ar = requests.get(
                    f"https://discord.com/assets/{asset_hash}.js",
                    headers={"User-Agent": ua}, timeout=15
                )
                m = re.search(r'buildNumber["\s:]+["\s]*(\d{5,7})', ar.text)
                if m:
                    bn = int(m.group(1))
                    log(f"Build number: {C.BOLD}{bn}{C.RESET}", "ok")
                    return bn
            except Exception:
                continue
        log(f"Build number not found, using fallback {BUILD_NUMBER_FALLBACK}", "warn")
        return BUILD_NUMBER_FALLBACK
    except Exception as e:
        log(f"Error fetching build number: {e}, using fallback", "warn")
        return BUILD_NUMBER_FALLBACK

def make_super_properties(build_number: int) -> str:
    obj = {
        "os": "Windows",
        "browser": "Discord Client",
        "release_channel": "stable",
        "client_version": "1.0.9175",
        "os_version": "10.0.26100",
        "os_arch": "x64",
        "app_arch": "x64",
        "system_locale": "en-US",
        "browser_user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "discord/1.0.9175 Chrome/128.0.6613.186 "
            "Electron/32.2.7 Safari/537.36"
        ),
        "browser_version": "32.2.7",
        "client_build_number": build_number,
        "native_build_number": 59498,
        "client_event_source": None,
    }
    return base64.b64encode(json.dumps(obj, separators=(',', ':')).encode()).decode()

# ── Discord API ────────────────────────────────────────────────────────────────
class DiscordAPI:
    def __init__(self, token: str, build_number: int):
        self.token = token
        self.session = requests.Session()
        self._lock = threading.Lock()   # serialize requests — session is not thread-safe
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "discord/1.0.9175 Chrome/128.0.6613.186 "
            "Electron/32.2.7 Safari/537.36"
        )
        sp = make_super_properties(build_number)
        self.session.headers.update({
            "Authorization": token,
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "User-Agent": ua,
            "X-Super-Properties": sp,
            "X-Discord-Locale": "en-US",
            "X-Discord-Timezone": "America/New_York",
            "X-Debug-Options": "bugReporterEnabled",
            "Origin": "https://discord.com",
            "Referer": "https://discord.com/channels/@me",
            "DNT": "1",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        })

    def get(self, path: str, **kwargs) -> requests.Response:
        url = f"https://discord.com/api/v9{path}"
        log(f"GET {path}", "debug")
        with self._lock:
            time.sleep(random.uniform(0.1, 0.4))
            r = self.session.get(url, **kwargs)
        log(f"  -> {r.status_code}", "debug")
        return r

    def post(self, path: str, payload: Optional[dict] = None, **kwargs) -> requests.Response:
        url = f"https://discord.com/api/v9{path}"
        log(f"POST {path}", "debug")
        with self._lock:
            time.sleep(random.uniform(0.1, 0.4))
            r = self.session.post(url, json=payload, **kwargs)
        log(f"  -> {r.status_code}", "debug")
        return r

    def validate_token(self) -> bool:
        try:
            r = self.get("/users/@me")
            if r.status_code == 200:
                user = r.json()
                name = user.get("username", "?")
                log(f"Logged in as: {C.BOLD}{name}{C.RESET} (ID: {user['id']})", "ok")
                return True
            log(f"Invalid token (status {r.status_code})", "error")
            return False
        except Exception as e:
            log(f"Cannot connect to Discord: {e}", "error")
            return False

# ── Quest helpers ──────────────────────────────────────────────────────────────
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

def get_raw_task_keys(quest: dict) -> list:
    """Return all task keys present in the quest config (for debugging [?] quests)."""
    tc = get_task_config(quest)
    if not tc or "tasks" not in tc:
        return []
    return list(tc["tasks"].keys())

def get_activity_quest_info(quest: dict) -> dict:
    """
    Extract info for ACHIEVEMENT_IN_ACTIVITY quests.
    Returns dict with: app_id, event_name, target (how many times to fire).
    """
    tc = get_task_config(quest)
    if not tc:
        return {}
    task_data = tc.get("tasks", {}).get("ACHIEVEMENT_IN_ACTIVITY", {})

    # application_id lives inside an "applications" list
    app_id = None
    apps = task_data.get("applications") or []
    if apps and isinstance(apps, list):
        app_id = apps[0].get("id")
    # fallback: top-level application in config
    if not app_id:
        app_id = quest.get("config", {}).get("application", {}).get("id")

    return {
        "app_id":     app_id,
        "event_name": task_data.get("event_name", "progress"),
        "target":     task_data.get("target", 1),
    }

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
    progress = us.get("progress") or {}
    return progress.get(task_type, {}).get("value", 0)

def get_enrolled_at(quest: dict) -> Optional[str]:
    us = get_user_status(quest)
    return _get(us, "enrolledAt", "enrolled_at")

# ── Core logic ─────────────────────────────────────────────────────────────────
class QuestAutocompleter:
    def __init__(self, api: DiscordAPI):
        self.api = api
        self._completed_ids: set = set()
        self._ids_lock = threading.Lock()

    # ── Thread-safe completed_ids access ──────────────────────────────────────
    @property
    def completed_ids(self) -> set:
        return self._completed_ids

    def mark_completed(self, qid: str):
        with self._ids_lock:
            self._completed_ids.add(qid)

    def is_already_done(self, qid: str) -> bool:
        with self._ids_lock:
            return qid in self._completed_ids

    # ── Fetch quests ───────────────────────────────────────────────────────────
    def fetch_quests(self) -> list:
        """Fetch the current quest list, retrying on rate limits (iterative, no recursion)."""
        for attempt in range(1, MAX_FETCH_RETRIES + 1):
            try:
                r = self.api.get("/quests/@me")
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, dict):
                        quests   = data.get("quests", [])
                        excluded = data.get("excluded_quests", [])
                        blocked  = _get(data, "quest_enrollment_blocked_until")
                        if blocked:
                            log(f"Enrollment blocked until: {blocked}", "warn")
                        if excluded:
                            log(f"{len(excluded)} quest(s) excluded", "debug")
                        return quests
                    elif isinstance(data, list):
                        return data
                    return []
                elif r.status_code == 429:
                    if attempt >= MAX_FETCH_RETRIES:
                        log("Max fetch retries reached after rate limiting.", "error")
                        return []
                    _wait_for_rate_limit(r, context=f"fetch attempt {attempt}/{MAX_FETCH_RETRIES}")
                    # loop continues
                else:
                    log(f"Quest fetch error ({r.status_code}): {r.text[:200]}", "warn")
                    return []
            except Exception as e:
                log(f"Error fetching quests: {e}", "error")
                if DEBUG:
                    traceback.print_exc()
                return []
        return []

    # ── Enroll ─────────────────────────────────────────────────────────────────
    def enroll_quest(self, quest: dict) -> bool:
        """Enroll in a quest with up to 3 attempts, retrying on 429 or transient errors."""
        name = get_quest_name(quest)
        qid  = quest["id"]
        for attempt in range(1, 4):
            try:
                random_sleep(1.5, 4.0)
                r = self.api.post(f"/quests/{qid}/enroll", {
                    "location": ENROLL_LOCATION,
                    "is_targeted": False,
                    "metadata_raw": None,
                    "metadata_sealed": None,
                    "traffic_metadata_raw":    quest.get("traffic_metadata_raw"),
                    "traffic_metadata_sealed": quest.get("traffic_metadata_sealed"),
                })
                if r.status_code == 429:
                    if attempt >= 3:
                        log(f"Skipping \"{name}\" after 3 rate limits", "warn")
                        return False
                    _wait_for_rate_limit(r, context=f"enrolling \"{name}\" attempt {attempt}/3")
                    continue   # retry
                if r.status_code in (200, 201, 204):
                    log(f"Enrolled: {C.BOLD}{name}{C.RESET}", "ok")
                    return True
                log(f"Enroll \"{name}\" failed ({r.status_code}): {r.text[:200]}", "warn")
                return False
            except Exception as e:
                log(f"Error enrolling \"{name}\" (attempt {attempt}/3): {e}", "error")
                if attempt >= 3:
                    return False
                # brief back-off before retry on transient network errors
                time.sleep(random.uniform(1, 3))
        return False

    def auto_accept(self, quests: list) -> list:
        if not AUTO_ACCEPT:
            return quests
        unaccepted = [
            q for q in quests
            if not is_enrolled(q) and not is_completed(q) and is_completable(q)
        ]
        if not unaccepted:
            return quests
        log(f"Found {len(unaccepted)} unenrolled quest(s) – auto-accepting...", "info")
        for q in unaccepted:
            self.enroll_quest(q)
            random_sleep(2, 5)
        human_sleep(2)
        return self.fetch_quests()

    # ── Build QuestState from a raw quest dict ─────────────────────────────────
    def _make_state(self, quest: dict) -> Optional[QuestState]:
        task_type = get_task_type(quest)
        if not task_type:
            return None
        enrolled_at_str = get_enrolled_at(quest)
        enrolled_ts = (
            datetime.fromisoformat(enrolled_at_str.replace("Z", "+00:00")).timestamp()
            if enrolled_at_str else time.time()
        )
        seconds_done = get_seconds_done(quest)
        seconds_needed = get_seconds_needed(quest)
        return QuestState(
            quest          = quest,
            task_type      = task_type,
            seconds_needed = seconds_needed,
            seconds_done   = seconds_done,
            enrolled_ts    = enrolled_ts,
            name           = get_quest_name(quest),
            completed      = seconds_done >= seconds_needed,
        )

    # ── Video group: all WATCH_VIDEO* quests tick together every second ────────
    def _run_video_group(self, states: List[QuestState]):
        """
        Advance all video quests in a shared 1-second tick loop — exactly as the
        browser extension does with its videoPromise.  Each quest sends its own
        POST independently; they don't block each other.
        """
        log(f"🎬 Video group starting ({len(states)} quest(s))", "info")
        for s in states:
            log(f"   • {C.BOLD}{s.name}{C.RESET}  {s.seconds_done:.0f}/{s.seconds_needed}s", "info")

        while True:
            all_done = True
            for state in states:
                if state.completed:
                    continue
                all_done = False
                qid = state.quest["id"]

                max_allowed = (time.time() - state.enrolled_ts) + VIDEO_MAX_FUTURE
                diff        = max_allowed - state.seconds_done
                if diff < VIDEO_SPEED:
                    continue    # not enough real time has passed yet — skip this tick

                timestamp = min(
                    float(state.seconds_needed),
                    state.seconds_done + VIDEO_SPEED + random.uniform(0, 0.5)
                )
                try:
                    r = self.api.post(f"/quests/{qid}/video-progress", {"timestamp": timestamp})
                    if r.status_code == 200:
                        body = r.json()
                        state.advance(timestamp)
                        log(
                            f"  🎬 [{state.name}] {state.seconds_done:.0f}/{state.seconds_needed}s "
                            f"({state.pct:.0f}%)",
                            "progress"
                        )
                        if body.get("completed_at") or state.completed:
                            # Final flush
                            try:
                                self.api.post(
                                    f"/quests/{qid}/video-progress",
                                    {"timestamp": state.seconds_needed}
                                )
                            except Exception:
                                pass
                            log(f"✅ Video done: {C.BOLD}{state.name}{C.RESET}", "ok")
                            state.completed = True
                            self.mark_completed(qid)
                    elif r.status_code == 429:
                        _wait_for_rate_limit(r, context=state.name)
                    else:
                        log(f"  Video error ({r.status_code}) [{state.name}]: {r.text[:200]}", "warn")
                except Exception as e:
                    log(f"  Video error [{state.name}]: {e}", "error")

            if all_done:
                break
            time.sleep(VIDEO_TICK_INTERVAL)

    # ── Heartbeat group: one thread per quest, each on its own interval ────────
    def _run_heartbeat_group(self, states: List[QuestState]):
        """
        Spawn one worker thread per heartbeat quest so every quest gets a
        heartbeat every HEARTBEAT_INTERVAL seconds — independent of how many
        other quests are running.

        Old round-robin: 2 quests × 20s interval = each quest heartbeats every 40s.
        New per-thread:  2 quests × 20s interval = each quest heartbeats every 20s. ✓
        """
        log(f"🎮 Heartbeat group starting ({len(states)} quest(s), one thread each)", "info")

        def _worker(state: QuestState):
            qid = state.quest["id"]
            pid = random.randint(1000, 30000)
            channel_id = random.randint(10**17, 10**18 - 1)
            stream_key = f"call:{channel_id}:{pid}"
            emoji = "🕹️" if state.task_type == "PLAY_ACTIVITY" else "🎮"
            log(
                f"   {emoji} {C.BOLD}{state.name}{C.RESET}  "
                f"~{state.remaining // 60:.0f}m remaining  [{state.task_type}]",
                "info"
            )

            while not state.completed:
                try:
                    r = self.api.post(f"/quests/{qid}/heartbeat", {
                        "stream_key": stream_key,
                        "terminal":   False,
                    })
                    if r.status_code == 200:
                        body          = r.json()
                        progress_data = body.get("progress", {})
                        if progress_data and state.task_type in progress_data:
                            state.advance(progress_data[state.task_type].get("value", state.seconds_done))
                        log(
                            f"  🎮 [{state.name}] {state.seconds_done:.0f}/{state.seconds_needed}s "
                            f"({state.pct:.0f}%)",
                            "progress"
                        )
                        if body.get("completed_at") or state.completed:
                            try:
                                self.api.post(f"/quests/{qid}/heartbeat", {
                                    "stream_key": stream_key,
                                    "terminal":   True,
                                })
                            except Exception:
                                pass
                            log(f"✅ Heartbeat done: {C.BOLD}{state.name}{C.RESET}", "ok")
                            state.completed = True
                            self.mark_completed(qid)
                            return
                    elif r.status_code == 429:
                        _wait_for_rate_limit(r, context=state.name)
                        continue   # retry immediately without sleeping the full interval
                    else:
                        log(f"  Heartbeat error ({r.status_code}) [{state.name}]: {r.text[:200]}", "warn")
                except Exception as e:
                    log(f"  Heartbeat error [{state.name}]: {e}", "error")

                human_sleep(HEARTBEAT_INTERVAL, pct=0.15)

        workers = [
            threading.Thread(target=_worker, args=(s,), name=f"HB-{s.name[:20]}", daemon=True)
            for s in states
        ]
        for w in workers:
            w.start()
        for w in workers:
            w.join()

    # ── Complete: ACHIEVEMENT_IN_ACTIVITY ─────────────────────────────────────
    def _handle_achievement(self, quest: dict):
        """
        ACHIEVEMENT_IN_ACTIVITY quests are gated by the Discord Activities SDK.
        Progress is only accepted from a live in-game session — no REST endpoint
        exists to complete these externally. Skip and guide the user.
        """
        name       = get_quest_name(quest)
        info       = get_activity_quest_info(quest)
        app_id     = info.get("app_id", "?")
        event_name = info.get("event_name", "progress")
        target     = info.get("target", 1)
        us         = get_user_status(quest)
        already    = int(
            (us.get("progress") or {})
            .get("ACHIEVEMENT_IN_ACTIVITY", {})
            .get("value", 0)
        )
        log(f"⏭️  Skipping \"{C.BOLD}{name}{C.RESET}\" [ACHIEVEMENT_IN_ACTIVITY — manual only]", "warn")
        log(f"   Progress: {already}/{target}  |  event: {event_name}  |  app: {app_id}", "info")
        log(
            f"   ↳ Open Discord → find the Activity for this quest → "
            f"play until '{event_name}' fires {target - already} more time(s).",
            "info"
        )

    # ── Run all actionable quests in parallel groups ───────────────────────────
    def run_all_quests(self, quests: list):
        """
        Split actionable quests into video / heartbeat / achievement groups and
        run video + heartbeat concurrently in separate threads — mirroring the
        browser extension's Promise.all([videoPromise, heartbeatPromise]) design.
        """
        video_states      = []
        heartbeat_states  = []

        for quest in quests:
            qid       = quest.get("id")
            task_type = get_task_type(quest)
            name      = get_quest_name(quest)

            if self.is_already_done(qid):
                continue

            if not task_type:
                raw_keys = get_raw_task_keys(quest)
                if raw_keys:
                    log(f"❓ \"{name}\" — unknown task type(s): {raw_keys}, skipping", "warn")
                    log(
                        f"   Tip: add '{raw_keys[0]}' to SUPPORTED_TASKS + "
                        f"HEARTBEAT_TASKS or VIDEO_TASKS to enable it.",
                        "info"
                    )
                else:
                    log(f"❓ \"{name}\" — no tasks found, skipping", "warn")
                continue

            if task_type in ACHIEVEMENT_TASKS:
                self._handle_achievement(quest)
                continue    # don't track in completed_ids; re-check each scan

            state = self._make_state(quest)
            if state is None or state.completed:
                continue

            if task_type in VIDEO_TASKS:
                video_states.append(state)
            elif task_type in HEARTBEAT_TASKS:
                heartbeat_states.append(state)
            else:
                log(f"  No handler for {task_type} [{name}], skipping", "warn")

        if not video_states and not heartbeat_states:
            return

        threads = []

        if video_states:
            t = threading.Thread(
                target=self._run_video_group,
                args=(video_states,),
                name="VideoGroup",
                daemon=True,
            )
            threads.append(t)

        if heartbeat_states:
            t = threading.Thread(
                target=self._run_heartbeat_group,
                args=(heartbeat_states,),
                name="HeartbeatGroup",
                daemon=True,
            )
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        log("All quest groups finished.", "ok")

    # ── Main loop ──────────────────────────────────────────────────────────────
    def run(self):
        log("=" * 60, "info")
        log(f"{C.BOLD}Discord Quest Auto-Completer (multi-quest){C.RESET}", "info")
        log(f"Auto-accept: {'ON' if AUTO_ACCEPT else 'OFF'}  |  Poll: {POLL_INTERVAL}s", "info")
        log("=" * 60, "info")
        cycle = 0
        while True:
            cycle += 1
            log(f"── Scan #{cycle} ──", "info")
            quests = self.fetch_quests()
            if not quests:
                log("No quests found", "info")
            else:
                total             = len(quests)
                enrolled_count    = sum(1 for q in quests if is_enrolled(q))
                completed_count   = sum(1 for q in quests if is_completed(q))
                completable_count = sum(1 for q in quests if is_completable(q))
                log(
                    f"Total: {total} | Enrolled: {enrolled_count} | "
                    f"Completed: {completed_count} | Completable: {completable_count}",
                    "info"
                )
                for q in quests:
                    name  = get_quest_name(q)
                    task  = get_task_type(q)
                    task_label = task if task else (
                        f"? ({', '.join(get_raw_task_keys(q))})" if get_raw_task_keys(q) else "?"
                    )
                    if is_completed(q):
                        status = f"{C.GREEN}✓{C.RESET}"
                    elif is_enrolled(q):
                        status = f"{C.YELLOW}▶{C.RESET}"
                    else:
                        status = f"{C.DIM}○{C.RESET}"

                    # Expiry warning
                    expiry_tag = ""
                    expires = get_expires_at(q)
                    if expires and not is_completed(q):
                        try:
                            exp_dt    = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                            hours_left = (exp_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                            if hours_left < 1:
                                expiry_tag = f" {C.RED}⚠ expires in {hours_left*60:.0f}m!{C.RESET}"
                            elif hours_left < 6:
                                expiry_tag = f" {C.YELLOW}⚠ expires in {hours_left:.1f}h{C.RESET}"
                        except Exception:
                            pass

                    log(f"  {status} {name} [{task_label}]{expiry_tag}", "info")

                quests    = self.auto_accept(quests)
                actionable = [
                    q for q in quests
                    if is_enrolled(q)
                    and not is_completed(q)
                    and is_completable(q)
                    and not self.is_already_done(q.get("id"))
                ]
                if actionable:
                    log(f"\n{len(actionable)} quest(s) ready — running in parallel groups", "info")
                    t_start = time.time()
                    self.run_all_quests(actionable)
                    elapsed = time.time() - t_start
                    # ── Completion summary ────────────────────────────────────
                    done_this_run = [q for q in actionable if self.is_already_done(q.get("id"))]
                    log("─" * 50, "info")
                    log(f"Session summary  ({elapsed/60:.1f}m elapsed)", "info")
                    for q in actionable:
                        qid  = q.get("id")
                        name = get_quest_name(q)
                        mark = f"{C.GREEN}✅ done{C.RESET}" if self.is_already_done(qid) else f"{C.YELLOW}⏳ in progress{C.RESET}"
                        log(f"  {mark}  {name}", "info")
                    log("─" * 50, "info")
                else:
                    log("No quests need completion at this time", "info")

            wait = jitter(POLL_INTERVAL, 0.10)
            log(f"\nWaiting {wait:.0f}s... (Ctrl+C to stop)\n", "info")
            time.sleep(wait)


# ── Entry point ────────────────────────────────────────────────────────────────
def main():
    print(f"""
{C.BOLD}{C.CYAN}╔══════════════════════════════════════════════╗
║     Discord Quest Auto-Completer             ║
║  Auto‑scan · Auto‑enroll · Auto‑complete     ║
╚══════════════════════════════════════════════╝{C.RESET}
""")
    build_number = fetch_latest_build_number()
    api          = DiscordAPI(TOKEN, build_number)
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
