# Jarvis-Bot — a private voice assistant for your home PC

**AI Assistant to replace the big name assistants.**

*Free and open source (MIT license). Runs on Linux Mint / Ubuntu.*

Jarvis replaces "Hey Google" with an assistant that runs **on your own computer**.
Your voice and conversations never leave your PC. Only these things ever go out, and only if you set them up:
weather lookups (the city name), web searches (the search words), Google Calendar (calendar data),
traffic (the two addresses), and phone notifications (the alert text).

## What it can do

| Area | Try saying… |
|---|---|
| **Time & date** | "What time is it?" · "…in Tokyo?" · "How many days until Christmas?" |
| **Weather** | "What's the weather?" · "Will it rain this weekend in Chicago?" |
| **Exact math** | "What's 17% of 2,350?" · "Solve x squared minus 5x plus 6" · "Convert 350 Fahrenheit to Celsius" |
| **Alarms** | "Wake me up at 6:30 on weekdays" · "Snooze" · "Stop" · "What alarms do I have?" |
| **Timers** | "Set a pasta timer for 10 minutes" · "How long left on the laundry?" · "Add 5 minutes to the pasta timer" |
| **Reminders** | "Remind me at 5 to call Mom" · "Remind me every day at 8pm to take my pills, and make sure I did" |
| **Calendar** | "What's on my schedule tomorrow?" · "Add dentist Tuesday at 2pm" (Google Calendar optional) |
| **Morning briefing** | Plays after your first morning alarm, or say "Give me my briefing" |
| **Dictionary** | "What does ubiquitous mean?" · "How do you spell necessary?" |
| **Vocabulary tutor** | It sometimes quizzes you, teaches a new word, or gives a riddle — only while you're already chatting. "Quiz me" · "Teach me a word" · "What's my vocabulary level?" |
| **Lights** | "Turn the desk lamp blue" · "Lights to 30 percent" · "Movie mode" · "Turn off all the lights" |
| **Music** | "Play music" · "Play Comfortably Numb" · "Play my rain sound" · "Next song" · "Pause" · "Volume up" |
| **YouTube & web** | "Play the latest NASA video" · "Play cyberpunk music on YouTube" · "Play cyberpunk on Pandora" · "Open Netflix" |
| **Lists** | "Add milk and eggs to my shopping list" · "What's on my to-do list?" |
| **Memory** | "Remember the spare key is in the blue drawer" · "Where's the spare key?" |
| **Traffic** | "How's traffic to work?" · "How long to drive to the gym?" |
| **News** | "What's in the news?" · "Any tech news?" |
| **PC control** | "Lock the computer" · "Open Firefox" · "How hot is my graphics card?" · "Put the computer to sleep" |
| **Security** | "I'm leaving" (away mode on) · wake word + "I'm home" (off) |
| **Web search** | "Who won the game last night?" (SearXNG, Tavily or Brave) |
| **Offline Wikipedia** | "Tell me about the Roman Empire" (optional, see below) |

Also: an adjustable **"seconds of silence before I answer"** for slow talkers, **follow-up mode** (keep talking
without the wake word), **interrupt** it by saying the wake word, and the **cancel phrase** — say **"cancel that"**
(no wake word needed) while it's talking, listening, ringing or playing anything, and it stops *everything*: its
voice, music, browser videos, alarms, timers, the briefing. Saying it at the end of a question drops the question.
The **✋ Stop everything** button at the top of the settings page does the same as the cancel phrase.
It tells **voices apart from music, alarms and noise**, and **pauses your music and videos while you talk**, then
resumes them.
It **starts speaking before the answer is finished** and **turns your music down** while you talk.

The settings page (http://localhost:8765) has a **Dashboard** (alarms, timers, reminders, calendar, lists, memories),
a **Security** page (away mode, recordings you can play, network alerts, devices on your Wi-Fi),
**Vocabulary** (your level, word of the day, your words) and **History** (searchable past conversations).

---

## Installing (one time, about 20 minutes)

You need: Linux Mint 21 or 22, an internet connection for the downloads, and a microphone + speakers.

**1. Install Ollama** (skip if you already have it). Open **Terminal** and paste:

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

**2. Download Jarvis:**

```bash
cd ~
git clone https://github.com/hefland/Jarvis-Bot.git
cd Jarvis-Bot
```

**3. Run the installer:**

```bash
./install.sh
```

It asks for your password once and a few yes/no questions — press **Enter** for the recommended answer.
(If you run Kokoro in Docker, answer **n** to the built-in Kokoro question.)

**4. Open the settings page:** click **Jarvis** in the Mint menu, or go to **http://localhost:8765**.
When the dot at the top is **green**, say: **"Hey Jarvis"** … *(beep)* … **"what time is it?"**

**Already installed?** Just update: `cd ~/Jarvis-Bot && ./manage.sh update`

---

## Setting up each feature

### 📱 Phone notifications (needed for security alerts and reminders on the go)
1. Install the free **ntfy** app on your phone (Play Store / App Store).
2. Make up a long secret channel name, like `jarvis-7k2m9x4q` (anyone who knows it can read your alerts).
3. In the ntfy app: tap **+**, type that name, **Subscribe**.
4. Jarvis settings → **Phone notifications**: turn it on, paste the same name in **Topic**, click **Save & send test**.

### 🎵 Music and the rain/sleep sound
Settings → **Music & sleep sounds**: put your music folder (e.g. `/home/you/Music`) and the full path of your rain file
(e.g. `/home/you/Music/rain.mp3`). Tip: in the Files app, right-click a file → **Copy**, then paste into the box —
it pastes the full path. Leave "Stop sleep sound after" at 0 to play all night.

**Big libraries are fine** — no playlists or shortcuts needed. Jarvis indexes your music folder in the background
(the first time can take a few minutes for a big network folder; music starts right away with the songs found so far)
and saves the list, so after that "play music" is instant. It re-checks for new songs every 6 hours, and right away
when you change the folder setting.

**Network drives (smb://) work too**, e.g. `smb://nas/Music` or `smb://192.168.1.50/Media/Music`. Do this once: open
the share in the **Files** app, and when it asks for a password choose **Remember forever**. After that Jarvis can open
it by itself, even after a restart. (Big network libraries are scanned in the background and refreshed every 30 minutes.)

### 📺 YouTube, Pandora and websites
Say "play the latest *channel* video", "play *anything* on YouTube", "play *genre* on Pandora", or "open *website*".
Jarvis opens your default browser (Firefox). If the video doesn't start by itself within a few seconds, Jarvis brings
Firefox to the front and presses YouTube's play key for you (it checks first, so it never pauses a video that already
started). It also helps to allow autoplay once: Firefox → ☰ → **Settings** → **Privacy & Security** → **Permissions** →
**Autoplay** → **Settings…** → add `https://www.youtube.com` → **Allow Audio and Video** → Save. Pandora doesn't let other programs start a station, so it
opens Pandora's search for what you asked — one click on the station you want. "Cancel that" pauses browser videos too.

### 💡 Govee lights
1. **Turn on LAN Control (in the Govee Home app on your phone):** tap your light → tap the
   **⚙️ gear** at the top right → turn on **LAN Control**. If you don't see that switch, unplug the light, plug it back
   in, wait about 30 minutes, and look again.
2. **In Jarvis:** Settings → **Smart home** → scroll down → click **🔎 Find Govee lights**.
3. For each light it finds: type a name (like `TV strip`) → click **Add**. Not sure which is which? Click **💡 Blink** and
   watch which light flashes.
4. Done — say "turn the TV strip purple", "TV strip to 30 percent", or "movie mode".

Nothing found? Make sure the light and the PC are on the same router. If you turned on the Linux Mint firewall, run
`sudo ufw allow 4002/udp` in Terminal (the lights answer on that port) and click Find again.
(Everything else on Govee's "LAN API" page — multicast, ports 4001/4002/4003, scan, colorwc — is what Jarvis does for you.)

Make **Scenes** like `Movie = TV strip: purple 15, Desk lamp: off`. For Govee models without LAN Control: get an API
key (Govee Home app → Profile → ⚙️ → **Apply for API Key**), choose `lan+cloud`, and paste the key. Your router may give
lights new addresses after a power cut — just click Find and Add again (or reserve their addresses in your router).

### 📅 Google Calendar (about 10 minutes, one time)
1. Go to **console.cloud.google.com** and sign in. Create a new project called **Jarvis**.
2. Search the top bar for **Google Calendar API** → **Enable**.
3. Go to **Google Auth Platform** (or "OAuth consent screen") → **Get started**: app name `Jarvis`, your email,
   audience **External**, finish. Under **Audience**, add your Gmail address as a **test user**, then click
   **Publish app** (otherwise Google makes you log in again every 7 days).
4. Go to **Clients** → **Create client** → type **Desktop app** → **Create** → **Download JSON**.
5. Jarvis settings → **Reminders & calendar**: turn on **Use my Google Calendar**, paste the downloaded file's path
   (e.g. `/home/you/Downloads/client_secret_123.json`), click **Save & connect Google**.
6. A Google page opens. Pick your account. It warns "Google hasn't verified this app" — that's normal because
   *you* made it: click **Continue** and **Allow**. The Dashboard will say "Google Calendar: connected".

### 🚗 Traffic
Create a free account at **developer.tomtom.com**, copy your API key into Settings → **Traffic**, and fill in your
home and work addresses (plus any other places, like `Gym = 123 Main St, Dallas, TX`).

### 🛡️ Security: away mode and network alerts
- **Away mode** turns on when you say "I'm leaving", click the button on the **Security** page, or automatically on a
  schedule (Settings → **Security** → "Turn on away mode automatically", e.g. weekdays 08:00–17:30).
- While it's on, Jarvis is **completely silent** — no chimes, no talking — and saves short recordings whenever it hears
  voices or nearby sounds like typing or clicking. Your phone gets an alert (at most every 10 minutes; adjustable), with
  the words it heard if you allow that. Recordings stay on your PC; play them on the **Security** page.
- To turn it off when you get home: say the wake word, then **"I'm home"** (you can change this phrase). It tells
  you how many recordings it made.
- **Network alerts**: your phone is notified when another device on your Wi-Fi connects to this PC (shared folders,
  remote desktop, SSH, this settings page…), and when a brand-new device joins your Wi-Fi. Add your own phone's
  address to "Ignore these devices" if it checks the settings page often.
- ⚠️ In some places it's illegal to record other people's conversations without their consent — check your local law
  and let people who live with you know.

### 📱 Use Jarvis from your phone (with Tailscale)
[Tailscale](https://tailscale.com) (free) is perfect for this: it lets your phone reach Jarvis privately from anywhere, without
opening anything to the internet. On the Jarvis PC, run once in Terminal:

```bash
sudo tailscale serve --bg --https=8443 8765
```

The `--https=8443` part gives Jarvis its **own** address, so it doesn't replace anything else you already share this
way (like Open WebUI on the main address). Check with `tailscale serve status`. To stop sharing Jarvis:
`sudo tailscale serve --https=8443 off`.

Jarvis's address is `https://your-pc.your-tailnet.ts.net:8443` (note the `:8443`). Open it on your phone (with Tailscale on), and tap
**Share → Add to Home Screen** to get a Jarvis icon. On the **Chat** page, tap **🎙️ Tap to talk**, speak, tap again —
Jarvis at home hears you, answers with your full setup (same AI, alarms, lights, music, memory), and the reply plays on
your phone in Jarvis's voice. Leave "Allow phones/other computers on my Wi-Fi" **off** — Tailscale handles it more safely.
(A phone browser can't listen for a wake word in your pocket, so it's tap-to-talk. The https address is required
because phones only allow the microphone on secure pages.)

**About ntfy** (the phone alerts app): it's free and **open source** (code at github.com/binwiederhier/ntfy), one of the most popular
notification tools in the self-hosting community, and needs no account. Anyone who knows your topic
name can read it, so make it long and random. For full privacy you can run your own ntfy server in Docker and reach it
over Tailscale, then point Jarvis's "ntfy server" setting and the phone app at it.

### 📚 Vocabulary tutor
On by default. Settings → **Learning & vocabulary** controls how often it speaks up (default: 20% of conversations,
at least 45 minutes apart, at most 4 times a day), and whether it teaches new words, asks for sentences, or gives riddles.

### 📖 Offline Wikipedia (optional, private)
Download a Wikipedia file (`.zim`) from **download.kiwix.org/zim/wikipedia/** — `wikipedia_en_all_nopic` (about 50 GB)
is a good choice; `wikipedia_en_all_maxi` (about 110 GB) has pictures. Put it in a folder called `kiwix` in your home
folder, then run once in Terminal:

```bash
docker run -d --name kiwix --restart always -p 8090:8080 -v ~/kiwix:/data ghcr.io/kiwix/kiwix-serve '*.zim'
```

Then Settings → **News & offline Wikipedia** → Kiwix address `http://localhost:8090`.

### 📰 News
Settings → **News & offline Wikipedia**: one line per source, `name = website`. A normal website works
(`Technology = techcrunch.com`) — Jarvis finds the site's news feed by itself. You can also paste a feed address
directly (`thepostmillennial.com/feed`).

### 🏠 Home Assistant (optional)
If you ever add other smart devices, Home Assistant connects to almost all of them. Put its address and a
long-lived token in Settings → **Smart home**, and Jarvis can control them by name.

---

## Everyday controls

Open Terminal, type `cd ~/Jarvis-Bot`, then:

| Command | What it does |
|---|---|
| `./manage.sh status` | Is Jarvis running? |
| `./manage.sh restart` | Restart Jarvis |
| `./manage.sh stop` / `./manage.sh start` | Stop / start |
| `./manage.sh logs` | Watch what it's doing (Ctrl+C to stop watching) |
| `./manage.sh update` | Get the newest version from GitHub and restart |
| `./manage.sh autostart-off` / `autostart-on` | Don't / do start when you log in |

---

## Wake word, model and voice

- **Wake word** (Settings → Wake word): `openwakeword` is the most reliable (hey jarvis, alexa, hey mycroft, hey rhasspy).
  `whisper` lets you type **any** word, like just `Jarvis`. Pause for a split second after the wake word.
- **AI model** (Settings → AI model): any Ollama model with "tools" support — `qwen2.5:7b`, `llama3.1:8b`, or
  `qwen2.5:14b` (smarter, still fast on a 3090 Ti). Avoid "thinking" models (like deepseek-r1) — they're slow for voice.
  Keep **Context size** at 16384: Jarvis has many abilities and needs the room.
- **Voice** (Settings → Voice): `kokoro` (built in), `openai_compatible` for Kokoro in Docker
  (`http://localhost:8880/v1`, model `kokoro`, voice e.g. `af_heart`; all voices: http://localhost:8880/v1/audio/voices),
  `piper`, or `espeak`.

## Troubleshooting

| Problem | Fix |
|---|---|
| It cuts me off | Settings → Listening → raise **Seconds of silence before I answer** (try 3–4) |
| It stops listening while I'm still talking quietly | Settings → Listening → lower **Voice detection strictness** to 1 |
| It doesn't hear the wake word / wakes by itself | Lower / raise **Wake word strictness** (0.6 is a good start; it works for both engines) |
| "cancel that" isn't heard | Settings → Listening: check **Listen for the cancel phrase**; speak it clearly as its own sentence |
| "Can't reach the AI server" | Ollama isn't running: `systemctl status ollama` |
| It picks the wrong ability or forgets instructions | Make sure **Context size** is 16384, or try a bigger model (qwen2.5:14b) |
| Music doesn't play | Check the folder path; make sure `mpv` is installed: `sudo apt install mpv` |
| Lights don't respond | Turn on LAN Control in the Govee app; click **Find Govee lights** again (addresses can change) |
| Security alerts don't arrive | Security page → **Send a test notification**. Check the topic matches the ntfy app |
| Away mode records the fridge/fan | Settings → Security → lower **Sensitivity** |
| Something else | Activity tab, or `./manage.sh logs` — the red lines say what went wrong |

Your data lives in `~/.local/share/jarvis-bot/` (database, recordings, log) and settings in `~/.config/jarvis-bot/`.

---

## For the curious: how it's built

```
jarvis/
  __main__.py      starts everything            config.py     every setting + the settings page layout
  brain.py         instructions, memory, tools  llm.py        Ollama / OpenAI-style servers, streaming
  assistant.py     the voice loop               scheduler.py  alarms, timers, reminders, event warnings
  ringer.py        alarm sounds                 announcer.py  speaks reminders etc. at the right moment
  tutor.py         vocabulary tutor             briefing.py   morning briefing
  security.py      away mode recordings         netwatch.py   network alerts
  media.py         music player (mpv)           smarthome.py  Govee + Home Assistant
  gcal.py          Google Calendar              notify.py     phone notifications (ntfy)
  timeparse.py     understands "tomorrow at 5"  storage.py    the database
  audio/           microphone, wake word, speech-to-text, voices, built-in sounds
  tools/           one file per ability (add a file = add an ability)
  data/words.py    the tutor's word list and riddles (add your own!)
  web/             the settings page
tests/             automatic checks:  .venv/bin/python -m pytest
```

## License

MIT — free to use, change and share. See [LICENSE](LICENSE).
