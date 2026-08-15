#!/usr/bin/env python3
"""
Velocity X – Discord Quest Auto-Completer
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
import shutil
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List

# ── Configuration ──────────────────────────────────────────────────────────────
CONFIG_FILE = "config.json"

# ── Constants ──────────────────────────────────────────────────────────────────
BUILD_NUMBER_FALLBACK = 504649
ENROLL_LOCATION       = 11
MAX_RATE_LIMIT_WAITS  = 5
MAX_FETCH_RETRIES     = 3
VIDEO_TICK_INTERVAL   = 1.0
VIDEO_SPEED           = 7.0
VIDEO_MAX_FUTURE      = 10.0

# ── Anti-detection pools ───────────────────────────────────────────────────────
_LOCALES = ["en-US", "en-GB", "en-CA", "en-AU"]
_TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Denver",
    "America/Los_Angeles", "America/Toronto", "Europe/London",
    "Europe/Paris", "Europe/Berlin",
]
_SCREEN_SIZES = [
    (1920, 1080), (2560, 1440), (1440, 900), (1366, 768), (1280, 800),
]

# Pick once per session so headers are consistent within a run
_SESSION_LOCALE   = random.choice(_LOCALES)
_SESSION_TIMEZONE = random.choice(_TIMEZONES)
_SESSION_SCREEN   = random.choice(_SCREEN_SIZES)

# ─────────────────────────────────────────────────────────────────────────────
#  ANSI palette  (auto-stripped when not a TTY)
# ─────────────────────────────────────────────────────────────────────────────
_TTY = sys.stdout.isatty()

def _a(code: str) -> str:
    return f"\033[{code}m" if _TTY else ""

class A:
    RST   = _a("0");  BOLD  = _a("1");  DIM   = _a("2")
    BLK   = _a("30"); RED   = _a("91"); GRN   = _a("92")
    YLW   = _a("93"); BLU   = _a("94"); MAG   = _a("95")
    CYN   = _a("96"); WHT   = _a("97"); BBLK  = _a("40")
    HOME  = "\033[H"      if _TTY else ""
    CLR   = "\033[2J"     if _TTY else ""
    ELINE = "\033[K"      if _TTY else ""
    HIDE  = "\033[?25l"   if _TTY else ""
    SHOW  = "\033[?25h"   if _TTY else ""
    ALT   = "\033[?1049h" if _TTY else ""
    NORM  = "\033[?1049l" if _TTY else ""

# ── Banner ─────────────────────────────────────────────────────────────────────
BANNER_TEXT = "⚡ VELOCITY X"

# ─────────────────────────────────────────────────────────────────────────────
#  QuestState – shared across threads
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class QuestState:
    quest:          dict
    task_type:      str
    seconds_needed: int
    seconds_done:   float
    enrolled_ts:    float
    name:           str
    reward:         str   = "—"
    completed:      bool  = False
    status:         str   = "queued"
    last_update:    float = field(default_factory=time.time)
    lock:           threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def advance(self, value: float):
        with self.lock:
            self.seconds_done = value
            self.status = "running"
            if self.seconds_done >= self.seconds_needed:
                self.completed = True
                self.status    = "done"

    @property
    def remaining(self) -> float:
        return max(0.0, self.seconds_needed - self.seconds_done)

    @property
    def pct(self) -> float:
        if self.seconds_needed == 0:
            return 100.0
        return min(100.0, self.seconds_done / self.seconds_needed * 100)


# ─────────────────────────────────────────────────────────────────────────────
#  Dashboard
# ─────────────────────────────────────────────────────────────────────────────
LOG_LINES  = 5
RENDER_HZ  = 0.5

@dataclass
class _LogEntry:
    ts:    str
    level: str
    msg:   str

class Dashboard:
    """Live terminal dashboard. Thread-safe."""

    def __init__(self):
        self._lock        = threading.Lock()
        self._rows: List[QuestState] = []
        self._logs        = deque(maxlen=200)
        self._username    = "—"
        self._user_id     = "—"
        self._status_msg  = "INITIALIZING..."
        self._status_ok   = False
        self._next_scan   = 0.0
        self._cycle       = 0
        self._running     = False
        self._thread: Optional[threading.Thread] = None
        self._start_ts    = time.time()
        self._spin_frame  = 0

    # ── Public API ─────────────────────────────────────────────────────────
    def set_user(self, username: str, user_id: str):
        with self._lock:
            self._username = username; self._user_id = user_id

    def set_status(self, msg: str, ok: bool = True):
        with self._lock:
            self._status_msg = msg; self._status_ok = ok

    def set_next_scan(self, ts: float):
        with self._lock: self._next_scan = ts

    def set_cycle(self, n: int):
        with self._lock: self._cycle = n

    def set_rows(self, states: List[QuestState]):
        with self._lock: self._rows = list(states)

    def add_log(self, ts: str, level: str, msg: str):
        with self._lock: self._logs.append(_LogEntry(ts, level, msg))

    # ── Render thread ──────────────────────────────────────────────────────
    def start(self):
        if not _TTY:
            return
        self._running = True
        sys.stdout.write(A.ALT + A.HIDE)
        sys.stdout.flush()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="Dashboard")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        if _TTY:
            sys.stdout.write(A.NORM + A.SHOW)
            sys.stdout.flush()

    def _loop(self):
        while self._running:
            try:
                self._spin_frame += 1
                self._render()
            except Exception:
                pass
            time.sleep(RENDER_HZ)

    # ── Drawing helpers ────────────────────────────────────────────────────
    @staticmethod
    def _tw() -> int:
        return shutil.get_terminal_size((100, 30)).columns

    @staticmethod
    def _th() -> int:
        return shutil.get_terminal_size((100, 30)).lines

    def _line(self, txt: str = "", fill: str = " ") -> str:
        tw    = self._tw()
        plain = re.sub(r'\033\[[^m]*m', '', txt)
        pad   = max(0, tw - len(plain))
        return txt + fill * pad + A.ELINE

    def _hline(self, char: str = "─", left: str = "├", right: str = "┤",
               color: str = "") -> str:
        tw  = self._tw()
        mid = char * (tw - 2)
        return self._line(f"{color}{left}{mid}{right}{A.RST}")

    def _render(self):
        with self._lock:
            rows  = list(self._rows)
            logs  = list(self._logs)
            uname = self._username
            uid   = self._user_id
            smsg  = self._status_msg
            sok   = self._status_ok
            nscan = self._next_scan
            cycle = self._cycle
        spin = self._spin_frame

        tw = self._tw()
        th = self._th()
        out: List[str] = []
        W = lambda t="", f=" ": self._line(t, f)

        SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

        # ── Header bar ────────────────────────────────────────────────────
        scol   = A.GRN if sok else A.YLW
        sicon  = "☑" if sok else "☐"
        elapsed = int(time.time() - self._start_ts)
        h, rem  = divmod(elapsed, 3600)
        m, s    = divmod(rem, 60)
        uptime  = f"{h:02d}:{m:02d}:{s:02d}"
        eta = ""
        if nscan > time.time():
            eta = f"  scan #{cycle}  next in {int(nscan - time.time())}s"

        brand  = f"{A.GRN}{A.BOLD} {BANNER_TEXT}{A.RST}"
        status = f"  {scol}{sicon} {smsg}{A.RST}{A.DIM}{eta}{A.RST}"
        right  = f"{A.DIM}{uname}  {uid}  up {uptime} {A.RST}"

        plain_left  = re.sub(r'\033\[[^m]*m', '', brand + status)
        plain_right = re.sub(r'\033\[[^m]*m', '', right)
        gap = max(1, tw - len(plain_left) - len(plain_right))
        out.append(W(f"{brand}{status}{' ' * gap}{right}"))
        out.append(W(f"{A.DIM}{'─' * tw}{A.RST}"))

        # ── Quest table ───────────────────────────────────────────────────
        NO_W   = 3
        TIME_W = 9
        STAT_W = 11
        REW_W  = min(22, max(10, tw // 5))
        NAME_W = max(10, tw - NO_W - REW_W - TIME_W - STAT_W - 6)

        hdr = (
            f"{A.DIM}│{A.RST}{A.BOLD}{A.WHT}{'#':>{NO_W}}{A.RST}{A.DIM}│{A.RST}"
            f"{A.WHT} {'Quest':<{NAME_W}}{A.RST}{A.DIM}│{A.RST}"
            f"{A.WHT} {'Reward':<{REW_W}}{A.RST}{A.DIM}│{A.RST}"
            f"{A.WHT} {'Time Left':<{TIME_W}}{A.RST}{A.DIM}│{A.RST}"
            f"{A.WHT} {'Status':<{STAT_W}}{A.RST}{A.DIM}│{A.RST}"
        )
        top = f"{A.DIM}┌{'─'*NO_W}┬{'─'*(NAME_W+1)}┬{'─'*(REW_W+1)}┬{'─'*(TIME_W+1)}┬{'─'*(STAT_W+1)}┐{A.RST}"
        sep = f"{A.DIM}├{'─'*NO_W}┼{'─'*(NAME_W+1)}┼{'─'*(REW_W+1)}┼{'─'*(TIME_W+1)}┼{'─'*(STAT_W+1)}┤{A.RST}"
        bot = f"{A.DIM}└{'─'*NO_W}┴{'─'*(NAME_W+1)}┴{'─'*(REW_W+1)}┴{'─'*(TIME_W+1)}┴{'─'*(STAT_W+1)}┘{A.RST}"

        out.append(W(top))
        out.append(W(hdr))

        if not rows:
            out.append(W(sep))
            out.append(W(
                f"{A.DIM}│{'':>{NO_W}}│{A.RST}"
                f" {A.DIM}{'Waiting for quests...':<{NAME_W}}{A.RST}{A.DIM}│{A.RST}"
                f" {'':>{REW_W}}{A.DIM}│ {'':>{TIME_W}}│ {'':>{STAT_W}}│{A.RST}"
            ))
        else:
            for i, st in enumerate(rows, 1):
                out.append(W(sep))
                name_d = (st.name[:NAME_W-1] + "…") if len(st.name) > NAME_W else st.name
                rew_d  = (st.reward[:REW_W-1] + "…") if len(st.reward) > REW_W else st.reward

                if st.status == "done":
                    tl_col, tl_str = A.GRN, "✓ DONE"
                    st_col, st_str = A.GRN, "✓ Done"
                elif st.status == "running":
                    mm, ss = divmod(int(st.remaining), 60)
                    sp     = SPIN[spin % len(SPIN)]
                    tl_col, tl_str = A.YLW, f"{sp} {mm:02d}:{ss:02d}"
                    st_col, st_str = A.YLW, f"▶ {st.task_type[:STAT_W-3]}"
                elif st.status == "error":
                    tl_col, tl_str = A.RED, "✗ ERROR"
                    st_col, st_str = A.RED, "✗ Error"
                elif st.status == "skipped":
                    tl_col, tl_str = A.DIM, "— SKIP"
                    st_col, st_str = A.DIM, "— Skip"
                else:
                    tl_col, tl_str = A.DIM, "queued"
                    st_col, st_str = A.DIM, "○ Queued"

                out.append(W(
                    f"{A.DIM}│{A.RST}{A.DIM}{i:>{NO_W}}{A.RST}{A.DIM}│{A.RST}"
                    f" {A.CYN}{name_d:<{NAME_W}}{A.RST}{A.DIM}│{A.RST}"
                    f" {A.MAG}{rew_d:<{REW_W}}{A.RST}{A.DIM}│{A.RST}"
                    f" {tl_col}{tl_str:<{TIME_W}}{A.RST}{A.DIM}│{A.RST}"
                    f" {st_col}{st_str:<{STAT_W}}{A.RST}{A.DIM}│{A.RST}"
                ))

        out.append(W(bot))

        # ── Log panel (fills remaining terminal height) ────────────────────
        fixed_rows = len(out) + 3
        avail_logs = max(2, th - fixed_rows)
        show_logs  = min(avail_logs, LOG_LINES)

        out.append(W(f"{A.DIM}{'─' * tw}{A.RST}"))

        level_fmt = {
            "ok":       (A.GRN, "OK  "),
            "warn":     (A.YLW, "WARN"),
            "error":    (A.RED, "ERR "),
            "progress": (A.DIM, "PROG"),
            "debug":    (A.DIM, "DBG "),
            "info":     (A.CYN, "INFO"),
        }
        visible_logs = list(logs)[-show_logs:]
        for entry in visible_logs:
            col, lbl = level_fmt.get(entry.level, (A.WHT, entry.level[:4].upper()))
            msg_w    = tw - 16
            out.append(W(
                f" {A.DIM}{entry.ts}{A.RST} "
                f"{col}{lbl}{A.RST} "
                f"{entry.msg[:msg_w]}"
            ))
        for _ in range(show_logs - len(visible_logs)):
            out.append(W())

        out.append(W(f"{A.DIM}{'─' * tw}{A.RST}"))
        out.append(W(f" {A.DIM}Ctrl+C to stop{A.RST}"))

        sys.stdout.write(A.HOME + "\n".join(out))
        sys.stdout.flush()


# ── Global dashboard instance ──────────────────────────────────────────────────
_dash: Optional[Dashboard] = None


# ─────────────────────────────────────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────────────────────────────────────
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

# Support both single token (legacy) and multi-account list
_raw_tokens = config.get("TOKENS", None)
if _raw_tokens is None:
    # legacy single-token key
    _single = config.get("TOKEN_DISCORD", "")
    _raw_tokens = [_single] if _single else []

TOKENS: List[str] = [t.strip() for t in _raw_tokens if isinstance(t, str) and t.strip()]
if not TOKENS:
    print("❌ No tokens found in config.json. Add \"TOKENS\": [\"token1\", \"token2\"]")
    sys.exit(1)

POLL_INTERVAL      = config.get("POLL_INTERVAL", 60)
HEARTBEAT_INTERVAL = config.get("HEARTBEAT_INTERVAL", 20)
AUTO_ACCEPT        = config.get("AUTO_ACCEPT", True)
LOG_PROGRESS       = config.get("LOG_PROGRESS", True)
DEBUG              = config.get("DEBUG", False)

SUPPORTED_TASKS = [
    "WATCH_VIDEO", "PLAY_ON_DESKTOP", "STREAM_ON_DESKTOP",
    "PLAY_ACTIVITY", "WATCH_VIDEO_ON_MOBILE", "PLAY_ON_MOBILE",
    "WATCH_VIDEO_ON_DESKTOP", "ACHIEVEMENT_IN_ACTIVITY",
    "PLAY_ON_PLAYSTATION", "PLAY_ON_XBOX",
]
HEARTBEAT_TASKS   = {"PLAY_ON_DESKTOP", "STREAM_ON_DESKTOP", "PLAY_ACTIVITY", "PLAY_ON_MOBILE", "PLAY_ON_PLAYSTATION", "PLAY_ON_XBOX"}
ACHIEVEMENT_TASKS = {"ACHIEVEMENT_IN_ACTIVITY"}
VIDEO_TASKS       = {"WATCH_VIDEO", "WATCH_VIDEO_ON_MOBILE", "WATCH_VIDEO_ON_DESKTOP"}


# ─────────────────────────────────────────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────────────────────────────────────────
def log(msg: str, level: str = "info", account: str = ""):
    if level == "debug" and not DEBUG:
        return
    if level == "progress" and not LOG_PROGRESS:
        return
    clean = re.sub(r'\033\[[^m]*m', '', msg)
    ts = datetime.now().strftime("%H:%M:%S")
    prefix = f"[{account}] " if account else ""
    if _dash:
        _dash.add_log(ts, level, f"{prefix}{clean}")
    else:
        pfx = {
            "info":     f"{A.CYN}[INFO]{A.RST}",
            "ok":       f"{A.GRN}[  OK]{A.RST}",
            "warn":     f"{A.YLW}[WARN]{A.RST}",
            "error":    f"{A.RED}[ ERR]{A.RST}",
            "progress": f"{A.DIM}[PROG]{A.RST}",
            "debug":    f"{A.DIM}[ DBG]{A.RST}",
        }.get(level, f"[{level.upper()}]")
        acc_tag = f"{A.MAG}[{account}]{A.RST} " if account else ""
        print(f"{A.DIM}{ts}{A.RST} {pfx} {acc_tag}{msg}")


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────
def jitter(base: float, pct: float = 0.20) -> float:
    return base + random.uniform(-base * pct, base * pct)

def human_sleep(base: float, pct: float = 0.25):
    time.sleep(max(0.5, jitter(base, pct)))

def random_sleep(lo: float, hi: float):
    time.sleep(random.uniform(lo, hi))

def _wait_for_rate_limit(response: requests.Response, context: str = "") -> float:
    try:
        retry_after = response.json().get("retry_after", 10)
    except Exception:
        retry_after = 10
    wait = retry_after + random.uniform(1.0, 3.0)
    log(f"Rate limited{f' ({context})' if context else ''} – waiting {wait:.1f}s", "warn")
    time.sleep(wait)
    return wait


# ─────────────────────────────────────────────────────────────────────────────
#  Build number
# ─────────────────────────────────────────────────────────────────────────────
def fetch_latest_build_number() -> int:
    try:
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )
        r = requests.get("https://discord.com/app", headers={"User-Agent": ua}, timeout=15)
        if r.status_code != 200:
            log(f"Discord page returned {r.status_code}, using fallback", "warn")
            return BUILD_NUMBER_FALLBACK
        scripts = re.findall(r'/assets/([a-f0-9]+)\.js', r.text)
        if not scripts:
            alts    = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
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
                    log(f"Build number: {bn}", "ok")
                    return bn
            except Exception:
                continue
        log(f"Build number not found, using fallback {BUILD_NUMBER_FALLBACK}", "warn")
        return BUILD_NUMBER_FALLBACK
    except Exception as e:
        log(f"Error fetching build number: {e}, using fallback", "warn")
        return BUILD_NUMBER_FALLBACK


# ─────────────────────────────────────────────────────────────────────────────
#  Super-properties  (consistent per session, varied per run)
# ─────────────────────────────────────────────────────────────────────────────
def make_super_properties(build_number: int) -> str:
    sw, sh = _SESSION_SCREEN
    obj = {
        "os":                   "Windows",
        "browser":              "Discord Client",
        "release_channel":      "stable",
        "client_version":       "1.0.9175",
        "os_version":           "10.0.26100",
        "os_arch":              "x64",
        "app_arch":             "x64",
        "system_locale":        _SESSION_LOCALE,
        "browser_user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) discord/1.0.9175 Chrome/128.0.6613.186 "
            "Electron/32.2.7 Safari/537.36"
        ),
        "browser_version":      "32.2.7",
        "client_build_number":  build_number,
        "native_build_number":  59498,
        "client_event_source":  None,
        "client_launch_id":     str(random.randint(10**15, 10**16 - 1)),
        "screen_width":         sw,
        "screen_height":        sh,
    }
    return base64.b64encode(json.dumps(obj, separators=(',', ':')).encode()).decode()


# ─────────────────────────────────────────────────────────────────────────────
#  Discord API
# ─────────────────────────────────────────────────────────────────────────────
class DiscordAPI:
    def __init__(self, token: str, build_number: int):
        self.token   = token
        self.session = requests.Session()
        self._lock   = threading.Lock()
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) discord/1.0.9175 Chrome/128.0.6613.186 "
            "Electron/32.2.7 Safari/537.36"
        )
        lang = _SESSION_LOCALE + ",en;q=0.9" if _SESSION_LOCALE != "en-US" else "en-US,en;q=0.9"
        self.session.headers.update({
            "Authorization":    token,
            "Content-Type":     "application/json",
            "Accept":           "*/*",
            "Accept-Language":  lang,
            "Accept-Encoding":  "gzip, deflate, br",
            "User-Agent":       ua,
            "X-Super-Properties":  make_super_properties(build_number),
            "X-Discord-Locale":    _SESSION_LOCALE,
            "X-Discord-Timezone":  _SESSION_TIMEZONE,
            "X-Debug-Options":     "bugReporterEnabled",
            "Origin":              "https://discord.com",
            "Referer":             "https://discord.com/channels/@me",
            "DNT":                 "1",
            "Sec-Fetch-Dest":      "empty",
            "Sec-Fetch-Mode":      "cors",
            "Sec-Fetch-Site":      "same-origin",
            "Connection":          "keep-alive",
        })

    def _delay(self):
        """Human-like inter-request delay with occasional longer pauses."""
        base = random.uniform(0.3, 0.9)
        if random.random() < 0.05:   # 5% chance of a longer thinking pause
            base += random.uniform(1.0, 2.5)
        time.sleep(base)

    def get(self, path: str, timeout: int = 30, **kw) -> requests.Response:
        log(f"GET {path}", "debug")
        with self._lock:
            self._delay()
            r = self.session.get(f"https://discord.com/api/v9{path}", timeout=timeout, **kw)
        log(f"  -> {r.status_code}", "debug")
        return r

    def post(self, path: str, payload: Optional[dict] = None, timeout: int = 30, **kw) -> requests.Response:
        log(f"POST {path}", "debug")
        with self._lock:
            self._delay()
            r = self.session.post(f"https://discord.com/api/v9{path}", json=payload, timeout=timeout, **kw)
        log(f"  -> {r.status_code}", "debug")
        return r

    def validate_token(self) -> Optional[str]:
        """Returns username on success, None on failure."""
        try:
            r = self.get("/users/@me")
            if r.status_code == 200:
                user  = r.json()
                uname = user.get("username", "?")
                uid   = str(user.get("id", "?"))
                log(f"Logged in as: {uname} (ID: {uid})", "ok")
                if _dash:
                    _dash.set_status("SYSTEM RUNNING...", ok=True)
                return uname
            log(f"Invalid token (status {r.status_code}) — skipping this account", "error")
            return None
        except Exception as e:
            log(f"Cannot connect to Discord: {e}", "error")
            return None


# ─────────────────────────────────────────────────────────────────────────────
#  Quest helpers
# ─────────────────────────────────────────────────────────────────────────────
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
    cfg  = quest.get("config", {})
    msgs = cfg.get("messages", {})
    for key in ("questName", "quest_name", "gameTitle", "game_title", "title"):
        v = msgs.get(key)
        if v:
            return v.strip()
    app_name = cfg.get("application", {}).get("name")
    if app_name:
        return app_name
    return f"Quest#{quest.get('id', '?')}"

def _dump_quest_debug(quests: list):
    """
    Write raw quest JSON to quest_debug.json on first fetch.
    Delete that file to re-dump next run.
    """
    path = "quest_debug.json"
    if os.path.exists(path):
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(quests, f, indent=2, default=str)
        log(f"Raw quest data saved → {path}  (delete to re-dump)", "warn")
    except Exception as e:
        log(f"Could not write {path}: {e}", "warn")


def _orb_label(item: dict) -> Optional[str]:
    """
    Discord Orb rewards look like {"type": "DISCORD_PRODUCT", "amount": 700}.
    Detect any integer/float amount field and return "N Discord Orbs".
    Also handles {"currency": "orbs", "quantity": 700} and similar shapes.
    """
    amount = None
    for k in ("amount", "quantity", "count", "value"):
        v = item.get(k)
        if isinstance(v, (int, float)) and v > 0:
            amount = int(v); break
    if amount is None:
        return None
    # confirm it looks like an orb/currency reward, not e.g. a duration
    rtype = str(item.get("type", "") or item.get("currency", "") or "").upper()
    is_orb = any(x in rtype for x in ("ORB", "DISCORD_PRODUCT", "CREDIT", "COIN", "TOKEN"))
    if is_orb or amount >= 100:   # ≥100 is almost certainly orbs not seconds
        return f"{amount} Discord Orbs"
    return None


# Keys that belong to the application/game metadata — never the reward
_APP_KEYS = {"application", "app", "game", "publisher", "developer", "guild"}

# Keys whose string values ARE reward names
_REWARD_NAME_KEYS = {
    "rewardDescription", "reward_description",
    "rewardTitle",       "reward_title",
    "rewardName",        "reward_name",
    "prizeDescription",  "prize_title",
    "displayName",       "display_name",
    "item_name",         "itemName",
    "label",
}

# Keys that are reward containers (walk these first)
_REWARD_CONTAINERS = {
    "reward", "rewards", "rewardItems", "reward_items",
    "prize", "prizes", "items", "entitlements",
}


def _walk_for_reward(obj, depth: int = 0, _skip_app: bool = True) -> Optional[str]:
    """
    Recursively walk a dict/list looking for an orb amount or a string
    on a reward-name key.  Skips application/game subtrees so we never
    accidentally return the game title as the reward.
    """
    if depth > 6:
        return None

    if isinstance(obj, dict):
        # ── orb / integer amount check first ──────────────────────────────
        orb = _orb_label(obj)
        if orb:
            return orb

        # ── string values on explicit reward-name keys ────────────────────
        for k, v in obj.items():
            if k in _REWARD_NAME_KEYS and isinstance(v, str) and v.strip():
                return v.strip()

        # ── recurse into reward containers (priority) ─────────────────────
        for k in _REWARD_CONTAINERS:
            if k in obj:
                hit = _walk_for_reward(obj[k], depth + 1)
                if hit:
                    return hit

        # ── recurse into everything else, skipping app/game keys ──────────
        for k, v in obj.items():
            if k in _REWARD_CONTAINERS or (_skip_app and k in _APP_KEYS):
                continue
            hit = _walk_for_reward(v, depth + 1)
            if hit:
                return hit

    elif isinstance(obj, list):
        for item in obj[:5]:
            hit = _walk_for_reward(item, depth + 1)
            if hit:
                return hit

    return None


def get_quest_reward(quest: dict) -> str:
    """
    Extracts the reward label from a Discord quest.

    Real Discord structure (confirmed from API):
      config.rewards_config.rewards[0].messages.name  →  "700 Orbs"
      config.rewards_config.rewards[0].orb_quantity   →  700
    """
    cfg = quest.get("config", {})

    # ── 1. rewards_config.rewards  (confirmed real Discord path) ────────────
    rc = cfg.get("rewards_config", {})
    for item in rc.get("rewards", [])[:1]:
        if not isinstance(item, dict):
            continue
        # best: messages.name inside the reward item
        item_msgs = item.get("messages", {})
        name = item_msgs.get("name") or item_msgs.get("name_with_article")
        if name and isinstance(name, str):
            return name.strip()
        # fallback: orb_quantity integer
        qty = item.get("orb_quantity") or item.get("premium_orb_quantity")
        if isinstance(qty, (int, float)) and qty > 0:
            return f"{int(qty)} Orbs"

    # ── 2. Legacy / alternate reward list fields ─────────────────────────────
    for key in ("rewardItems", "reward_items", "rewards", "prize", "prizes"):
        items = cfg.get(key)
        if not items or not isinstance(items, list):
            continue
        item = items[0]
        if not isinstance(item, dict):
            if isinstance(item, str) and item.strip():
                return item.strip()
            continue
        item_msgs = item.get("messages", {})
        name = item_msgs.get("name") or item_msgs.get("name_with_article")
        if name and isinstance(name, str):
            return name.strip()
        orb = _orb_label(item)
        if orb:
            return orb
        for f in ("label", "item_name", "itemName", "displayName", "display_name"):
            v = item.get(f)
            if v and isinstance(v, str):
                return v.strip()

    # ── 3. Top-level config messages ─────────────────────────────────────────
    msgs = cfg.get("messages", {})
    for key in ("rewardDescription", "reward_description", "rewardTitle",
                "reward_title", "rewardName", "reward_name"):
        v = msgs.get(key)
        if v and isinstance(v, str):
            return v.strip()

    # ── 4. Recursive tree-walk (last resort, skips application subtree) ──────
    hit = _walk_for_reward(rc) or _walk_for_reward(cfg)
    if hit and len(hit) < 80:
        return hit

    return "—"

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
            if datetime.fromisoformat(expires.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                return False
        except Exception:
            pass
    tc = get_task_config(quest)
    if not tc or "tasks" not in tc:
        return False
    return any(tc["tasks"].get(t) is not None for t in SUPPORTED_TASKS)

def is_enrolled(quest: dict) -> bool:
    return bool(_get(get_user_status(quest), "enrolledAt", "enrolled_at"))

def is_completed(quest: dict) -> bool:
    return bool(_get(get_user_status(quest), "completedAt", "completed_at"))

def get_task_type(quest: dict) -> Optional[str]:
    tc = get_task_config(quest)
    if not tc or "tasks" not in tc:
        return None
    for t in SUPPORTED_TASKS:
        if tc["tasks"].get(t) is not None:
            return t
    return None

def get_raw_task_keys(quest: dict) -> list:
    tc = get_task_config(quest)
    if not tc or "tasks" not in tc:
        return []
    return list(tc["tasks"].keys())

def get_activity_quest_info(quest: dict) -> dict:
    tc = get_task_config(quest)
    if not tc:
        return {}
    task_data = tc.get("tasks", {}).get("ACHIEVEMENT_IN_ACTIVITY", {})
    app_id = None
    apps = task_data.get("applications") or []
    if apps and isinstance(apps, list):
        app_id = apps[0].get("id")
    if not app_id:
        app_id = quest.get("config", {}).get("application", {}).get("id")
    return {
        "app_id":     app_id,
        "event_name": task_data.get("event_name", "progress"),
        "target":     task_data.get("target", 1),
    }

def get_seconds_needed(quest: dict) -> int:
    tc = get_task_config(quest)
    tt = get_task_type(quest)
    if not tc or not tt:
        return 0
    return tc["tasks"][tt].get("target", 0)

def get_seconds_done(quest: dict) -> float:
    tt = get_task_type(quest)
    if not tt:
        return 0
    us = get_user_status(quest)
    return (us.get("progress") or {}).get(tt, {}).get("value", 0)

def get_enrolled_at(quest: dict) -> Optional[str]:
    return _get(get_user_status(quest), "enrolledAt", "enrolled_at")


# ─────────────────────────────────────────────────────────────────────────────
#  Core logic
# ─────────────────────────────────────────────────────────────────────────────
class QuestAutocompleter:
    def __init__(self, api: DiscordAPI, account: str = ""):
        self.api             = api
        self.account         = account   # display name for log prefixing
        self._completed_ids: set = set()
        self._ids_lock       = threading.Lock()

    def mark_completed(self, qid: str):
        with self._ids_lock:
            self._completed_ids.add(qid)

    def is_already_done(self, qid: str) -> bool:
        with self._ids_lock:
            return qid in self._completed_ids

    # ── Fetch ──────────────────────────────────────────────────────────────
    def fetch_quests(self) -> list:
        for attempt in range(1, MAX_FETCH_RETRIES + 1):
            try:
                r = self.api.get("/quests/@me")
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, dict):
                        blocked = _get(data, "quest_enrollment_blocked_until")
                        if blocked:
                            log(f"Enrollment blocked until: {blocked}", "warn", account=self.account)
                        quests_raw = data.get("quests", [])
                        _dump_quest_debug(quests_raw)
                        return quests_raw
                    result = data if isinstance(data, list) else []
                    _dump_quest_debug(result)
                    return result
                elif r.status_code == 429:
                    if attempt >= MAX_FETCH_RETRIES:
                        log("Max fetch retries reached.", "error", account=self.account); return []
                    _wait_for_rate_limit(r, f"fetch {attempt}/{MAX_FETCH_RETRIES}")
                else:
                    log(f"Quest fetch error ({r.status_code}): {r.text[:200]}", "warn", account=self.account)
                    return []
            except Exception as e:
                log(f"Error fetching quests: {e}", "error", account=self.account)
                if DEBUG:
                    traceback.print_exc()
                return []
        return []

    # ── Enroll ────────────────────────────────────────────────────────────
    def enroll_quest(self, quest: dict) -> bool:
        name = get_quest_name(quest)
        qid  = quest["id"]
        for attempt in range(1, 4):
            try:
                random_sleep(2.0, 5.0)   # slightly longer pre-enroll pause
                r = self.api.post(f"/quests/{qid}/enroll", {
                    "location":               ENROLL_LOCATION,
                    "is_targeted":            False,
                    "metadata_raw":           None,
                    "metadata_sealed":        None,
                    "traffic_metadata_raw":    quest.get("traffic_metadata_raw"),
                    "traffic_metadata_sealed": quest.get("traffic_metadata_sealed"),
                })
                if r.status_code == 429:
                    if attempt >= 3:
                        log(f'Skipping "{name}" after 3 rate limits', "warn", account=self.account); return False
                    _wait_for_rate_limit(r, f'enrolling "{name}" {attempt}/3'); continue
                if r.status_code in (200, 201, 204):
                    log(f"Enrolled: {name}", "ok", account=self.account); return True
                log(f'Enroll "{name}" failed ({r.status_code}, account=self.account): {r.text[:200]}', "warn")
                return False
            except Exception as e:
                log(f'Error enrolling "{name}" ({attempt}/3, account=self.account): {e}', "error")
                if attempt >= 3: return False
                time.sleep(random.uniform(1, 3))
        return False

    def auto_accept(self, quests: list) -> list:
        if not AUTO_ACCEPT:
            return quests
        unaccepted = [q for q in quests
                      if not is_enrolled(q) and not is_completed(q) and is_completable(q)]
        if not unaccepted:
            return quests
        log(f"Auto-accepting {len(unaccepted)} quest(s)...", "info", account=self.account)
        for q in unaccepted:
            self.enroll_quest(q)
            random_sleep(2, 5)
        human_sleep(2)
        return self.fetch_quests()

    # ── State factory ──────────────────────────────────────────────────────
    def _make_state(self, quest: dict) -> Optional[QuestState]:
        tt = get_task_type(quest)
        if not tt:
            return None
        ea = get_enrolled_at(quest)
        enrolled_ts = (
            datetime.fromisoformat(ea.replace("Z", "+00:00")).timestamp()
            if ea else time.time()
        )
        sd = get_seconds_done(quest)
        sn = get_seconds_needed(quest)
        return QuestState(
            quest=quest, task_type=tt, seconds_needed=sn, seconds_done=sd,
            enrolled_ts=enrolled_ts, name=get_quest_name(quest),
            reward=get_quest_reward(quest),
            completed=sd >= sn, status="done" if sd >= sn else "queued",
        )

    # ── Video group ────────────────────────────────────────────────────────
    def _run_video_group(self, states: List[QuestState]):
        log(f"Video group starting ({len(states)} quest(s))", "info", account=self.account)
        for s in states:
            log(f"  • {s.name}  {s.seconds_done:.0f}/{s.seconds_needed}s", "info", account=self.account)
            s.status = "running"

        while True:
            all_done    = True   # True only when every state is .completed
            any_active  = False  # True when at least one state sent a request this tick
            for state in states:
                if state.completed:
                    continue
                all_done = False
                qid         = state.quest["id"]
                max_allowed = (time.time() - state.enrolled_ts) + VIDEO_MAX_FUTURE
                if max_allowed - state.seconds_done < VIDEO_SPEED:
                    continue
                any_active = True
                # slight random offset to avoid a perfectly mechanical pattern
                timestamp = min(
                    float(state.seconds_needed),
                    state.seconds_done + VIDEO_SPEED + random.uniform(-0.3, 0.8)
                )
                try:
                    r = self.api.post(f"/quests/{qid}/video-progress", {"timestamp": timestamp})
                    if r.status_code == 200:
                        body = r.json()
                        # Prefer server-authoritative progress; fall back to our timestamp
                        server_progress = (
                            body.get("progress", {})
                            .get(state.task_type, {})
                            .get("value")
                        )
                        state.advance(server_progress if server_progress is not None else timestamp)
                        log(
                            f"[{state.name}] {state.seconds_done:.0f}/"
                            f"{state.seconds_needed}s ({state.pct:.0f}%)", "progress",
                            account=self.account
                        )
                        if body.get("completed_at") or state.completed:
                            try:
                                self.api.post(
                                    f"/quests/{qid}/video-progress",
                                    {"timestamp": state.seconds_needed}
                                )
                            except Exception:
                                pass
                            log(f"Video done: {state.name}", "ok", account=self.account)
                            state.status = "done"; state.completed = True
                            self.mark_completed(qid)
                    elif r.status_code == 429:
                        _wait_for_rate_limit(r, state.name)
                    else:
                        log(f"Video error ({r.status_code}, account=self.account) [{state.name}]: {r.text[:200]}", "warn")
                except Exception as e:
                    log(f"Video error [{state.name}]: {e}", "error", account=self.account)
            if all_done:
                break
            # If no quest was active this tick (all still within rate-cap window),
            # sleep a bit longer to avoid a busy-wait spin until the window opens.
            time.sleep(VIDEO_TICK_INTERVAL if any_active else min(3.0, VIDEO_SPEED / 2))

    # ── Heartbeat group ────────────────────────────────────────────────────
    def _run_heartbeat_group(self, states: List[QuestState]):
        log(f"Heartbeat group starting ({len(states)} quest(s), one thread each)", "info", account=self.account)

        def _worker(state: QuestState):
            qid = state.quest["id"]
            # Generate a realistic-looking stream key
            guild_id   = random.randint(10**17, 10**18 - 1)
            channel_id = random.randint(10**17, 10**18 - 1)
            uid_fake   = random.randint(10**17, 10**18 - 1)
            stream_key = f"guild:{guild_id}:{channel_id}:{uid_fake}"
            state.status = "running"
            log(f"{state.name}  ~{state.remaining // 60:.0f}m remaining [{state.task_type}]", "info", account=self.account)

            while not state.completed:
                try:
                    r = self.api.post(
                        f"/quests/{qid}/heartbeat",
                        {"stream_key": stream_key, "terminal": False}
                    )
                    if r.status_code == 200:
                        body      = r.json()
                        prog_data = body.get("progress", {})
                        if prog_data and state.task_type in prog_data:
                            state.advance(prog_data[state.task_type].get("value", state.seconds_done))
                        log(
                            f"[{state.name}] {state.seconds_done:.0f}/"
                            f"{state.seconds_needed}s ({state.pct:.0f}%)", "progress",
                            account=self.account
                        )
                        if body.get("completed_at") or state.completed:
                            try:
                                self.api.post(
                                    f"/quests/{qid}/heartbeat",
                                    {"stream_key": stream_key, "terminal": True}
                                )
                            except Exception:
                                pass
                            log(f"Heartbeat done: {state.name}", "ok", account=self.account)
                            state.status = "done"; state.completed = True
                            self.mark_completed(qid); return
                    elif r.status_code == 429:
                        _wait_for_rate_limit(r, state.name); continue
                    else:
                        log(
                            f"Heartbeat error ({r.status_code}) [{state.name}]: {r.text[:200]}",
                            "warn"
                        )
                except Exception as e:
                    log(f"Heartbeat error [{state.name}]: {e}", "error", account=self.account)
                human_sleep(HEARTBEAT_INTERVAL, pct=0.15)

        workers = [
            threading.Thread(target=_worker, args=(s,), name=f"HB-{s.name[:20]}", daemon=True)
            for s in states
        ]
        for w in workers: w.start()
        for w in workers: w.join()

    # ── Achievement (manual only) ──────────────────────────────────────────
    def _handle_achievement(self, quest: dict):
        name    = get_quest_name(quest)
        info    = get_activity_quest_info(quest)
        us      = get_user_status(quest)
        already = int((us.get("progress") or {})
                      .get("ACHIEVEMENT_IN_ACTIVITY", {}).get("value", 0))
        target  = info.get("target", 1)
        ename   = info.get("event_name", "progress")
        log(f'Skipping "{name}" [ACHIEVEMENT — manual only] {already}/{target}', "warn", account=self.account)
        log(f"  ↳ Play Discord Activity until '{ename}' fires {target - already}x", "info", account=self.account)

    # ── Run all ────────────────────────────────────────────────────────────
    def run_all_quests(self, quests: list):
        video_states, hb_states = [], []
        all_states: List[QuestState] = []

        for quest in quests:
            qid  = quest.get("id")
            tt   = get_task_type(quest)
            name = get_quest_name(quest)
            if self.is_already_done(qid):
                continue
            if not tt:
                raw = get_raw_task_keys(quest)
                log(f'"{name}" — unknown task {raw}, skipping', "warn", account=self.account); continue
            if tt in ACHIEVEMENT_TASKS:
                self._handle_achievement(quest); continue
            state = self._make_state(quest)
            if state is None or state.completed:
                continue
            all_states.append(state)
            if tt in VIDEO_TASKS:
                video_states.append(state)
            elif tt in HEARTBEAT_TASKS:
                hb_states.append(state)
            else:
                log(f"No handler for {tt} [{name}], skipping", "warn", account=self.account)

        if not all_states:
            return

        if _dash:
            _dash.set_rows(all_states)

        threads = []
        if video_states:
            threads.append(threading.Thread(
                target=self._run_video_group,
                args=(video_states,), name="VideoGroup", daemon=True
            ))
        if hb_states:
            threads.append(threading.Thread(
                target=self._run_heartbeat_group,
                args=(hb_states,), name="HeartbeatGroup", daemon=True
            ))
        for t in threads: t.start()
        for t in threads: t.join()
        log("All quest groups finished.", "ok", account=self.account)

    # ── Main loop ──────────────────────────────────────────────────────────
    def run(self):
        log(f"Velocity X started — account: {self.account}", "ok", account=self.account)
        log(f"Auto-accept: {'ON' if AUTO_ACCEPT else 'OFF'}  Poll: {POLL_INTERVAL}s", "info", account=self.account)
        cycle = 0

        while True:
            cycle += 1
            if _dash: _dash.set_cycle(cycle)
            log(f"Scan #{cycle}", "info", account=self.account)
            if _dash: _dash.set_status("SCANNING...", ok=True)

            quests = self.fetch_quests()

            if not quests:
                log("No quests found", "info", account=self.account)
                if _dash: _dash.set_rows([])
            else:
                total     = len(quests)
                enrolled  = sum(1 for q in quests if is_enrolled(q))
                completed = sum(1 for q in quests if is_completed(q))
                log(f"Total: {total}  Enrolled: {enrolled}  Completed: {completed}", "info", account=self.account)

                for q in quests:
                    name   = get_quest_name(q)
                    tt     = get_task_type(q)
                    reward = get_quest_reward(q)
                    mark   = "✓" if is_completed(q) else ("▶" if is_enrolled(q) else "○")
                    expires    = get_expires_at(q)
                    expiry_note = ""
                    if expires and not is_completed(q):
                        try:
                            h = (
                                datetime.fromisoformat(expires.replace("Z", "+00:00"))
                                - datetime.now(timezone.utc)
                            ).total_seconds() / 3600
                            if h < 1:   expiry_note = f" ⚠ {h*60:.0f}m left!"
                            elif h < 6: expiry_note = f" ⚠ {h:.1f}h left"
                        except Exception:
                            pass
                    rew_note = f" [{reward}]" if reward != "—" else ""
                    log(f"  {mark} {name}{rew_note} [{tt or '?'}]{expiry_note}", "info", account=self.account)

                quests     = self.auto_accept(quests)
                actionable = [
                    q for q in quests
                    if is_enrolled(q) and not is_completed(q)
                    and is_completable(q) and not self.is_already_done(q.get("id"))
                ]

                if actionable:
                    log(f"{len(actionable)} quest(s) ready — launching parallel groups", "info", account=self.account)
                    if _dash: _dash.set_status("RUNNING QUESTS...", ok=True)
                    t0 = time.time()
                    self.run_all_quests(actionable)
                    elapsed = time.time() - t0
                    done_n  = sum(1 for q in actionable if self.is_already_done(q.get("id")))
                    log(f"Session done: {done_n}/{len(actionable)} in {elapsed/60:.1f}m", "ok", account=self.account)
                    for q in actionable:
                        mark = "✅" if self.is_already_done(q.get("id")) else "⏳"
                        log(f"  {mark} {get_quest_name(q)}", "info", account=self.account)
                else:
                    log("No quests need completion right now", "info", account=self.account)
                    if _dash: _dash.set_rows([])

            wait = jitter(POLL_INTERVAL, 0.10)
            if _dash:
                _dash.set_status("SYSTEM RUNNING...", ok=True)
                _dash.set_next_scan(time.time() + wait)
            log(f"Waiting {wait:.0f}s...", "info", account=self.account)
            time.sleep(wait)


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────
def _run_account(token: str, build_number: int, failed_accounts: list, lock: threading.Lock):
    """Worker for one account. Runs until KeyboardInterrupt or fatal error."""
    api      = DiscordAPI(token, build_number)
    username = api.validate_token()
    if username is None:
        short = token[:20] + "..."
        with lock:
            if short not in failed_accounts:
                failed_accounts.append(short)
        log(f"Invalid token ({short}) — will keep retrying every {POLL_INTERVAL}s", "warn")
        while True:
            time.sleep(POLL_INTERVAL)
            username = api.validate_token()
            if username is not None:
                log(f"Token recovered: {username}", "ok")
                with lock:
                    if short in failed_accounts:
                        failed_accounts.remove(short)
                break
            log(f"Still invalid ({short}) — retrying...", "warn")
    if _dash:
        _dash.set_user(username, "")
    completer = QuestAutocompleter(api, account=username)
    try:
        completer.run()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log(f"Fatal error: {e}", "error", account=username)
        if DEBUG:
            traceback.print_exc()


def main():
    global _dash

    _dash = Dashboard()
    _dash.set_status("INITIALIZING...", ok=False)
    _dash.start()

    try:
        log(f"Fetching build number... ({len(TOKENS)} account(s))", "info")
        build_number    = fetch_latest_build_number()
        failed_accounts: list = []
        lock            = threading.Lock()

        workers = [
            threading.Thread(
                target=_run_account,
                args=(token, build_number, failed_accounts, lock),
                name=f"Account-{i+1}",
                daemon=True,
            )
            for i, token in enumerate(TOKENS)
        ]

        for w in workers:
            w.start()
            # Stagger account startup slightly to avoid simultaneous token validation
            if len(workers) > 1:
                time.sleep(random.uniform(1.5, 3.0))

        for w in workers:
            w.join()

    except KeyboardInterrupt:
        pass
    except Exception as e:
        log(f"Unexpected error: {e}", "error")
        if DEBUG:
            traceback.print_exc()
    finally:
        _dash.stop()
        if failed_accounts:
            print(f"\n{A.RED}X Failed accounts ({len(failed_accounts)}): {', '.join(failed_accounts)}{A.RST}")
        print(f"\n{A.GRN}Stopped.{A.RST}")

if __name__ == "__main__":
    main()
