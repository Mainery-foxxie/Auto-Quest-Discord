# Discord Quest Auto‑Completer

Automatically enrolls and completes Discord quests (watch video, play on desktop, etc.) using your account token.

## Features

- Automatically scans for available quests.
- Auto‑enrolls in new quests (optional).
- Completes supported task types:
  - WATCH_VIDEO / WATCH_VIDEO_ON_MOBILE
  - PLAY_ON_DESKTOP / STREAM_ON_DESKTOP
  - PLAY_ACTIVITY
- Configurable polling and heartbeat intervals.
- Respects rate limits and retries.

## Setup

### Desktop (Windows / Linux / macOS)

1. Clone the repository  
   git clone https://github.com/Mainery-foxxie/Auto-Quest-Discord
   
   cd Auto-Quest-Discord

3. Install dependencies  
   pip install -r requirements.txt

4. Configure  
   Copy config.json.example to config.json and edit it:  
   cp config.json.example config.json  
   Fill in your Discord token in "TOKEN_DISCORD".  
   Adjust other settings as needed (poll interval, auto‑accept, etc.).

   How to get your token:  
   - Open Discord in your browser (or desktop app with dev tools).  
   - Press Ctrl+Shift+I (or F12) to open developer tools.  
   - Go to the Network tab, find any request to discord.com/api/v9/..., look for the Authorization header, and copy its value.  
   - Never share your token with anyone!

5. Run  
   python main.py

### Mobile (Android with Termux)

1. Install Termux,dependencies and extension get token
   Download and install the following APKs (allow installation from unknown sources if needed):

## Termux :
   - Termux: https://f-droid.org/repo/com.termux_1022.apk  
   - Termux:Boot: https://f-droid.org/repo/com.termux.boot_1000.apk (optional, for auto‑start)

## Extension :
   - https://chromewebstore.google.com/detail/discord-get-user-token/accgjfooejbpdchkfpngkjjdekkcbnfd?hl=id
   
   Open Termux and run:  
   - pkg install python-pip
   - pkg install git
   
2. Clone the repository  
   - git clone https://github.com/Mainery-foxxie/Auto-Quest-Discord
   - cd Auto-Quest-Discord

3. Install Python dependencies  
   - pip install requests

4. Configure and how to get ur token

   Download link extension on upper link one you can use `kiwi browser` or another extension browser for mobile
   Go link https://discord.com/channels/@me ``logging ur acc in website``in to extension ``goto the profile`` then open extension is should show click Button get token now do
   
   ``nano config.json``  
   Paste your token into the file (use Ctrl+X+Y+Enter to save and exit).

6. Run  
   - python main.py

   The script will run continuously. To keep it running in the background, you can use tmux or run it with & (e.g., python main.py &).

## Configuration Options

| Option                | Type      | Default | Description |
|-----------------------|-----------|---------|-------------|
| TOKEN_DISCORD         | string    | (none)  | Your Discord user token. |
| POLL_INTERVAL         | integer   | 60      | Seconds between quest scans. |
| HEARTBEAT_INTERVAL    | integer   | 20      | Seconds between heartbeat requests for play/stream tasks. |
| AUTO_ACCEPT           | boolean   | true    | Automatically enroll in new quests. |
| LOG_PROGRESS          | boolean   | true    | Show progress updates (seconds done). |
| DEBUG                 | boolean   | true    | Show detailed debug logs. |

## Disclaimer

- This tool interacts with Discord’s internal API. Use at your own risk.
- Automating quest completion may violate Discord’s Terms of Service.  
- The author is not responsible for any account actions taken by Discord.

## 💬 Discord Server
[Join our Discord](https://discord.gg/BG9bYyK9nA)
