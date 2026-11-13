"""Settings for Jarvis.

All settings live in one JSON file (default: ~/.config/jarvis-bot/settings.json).
The SCHEMA list below describes every setting: its default value, the label
shown on the settings web page, and a short help text. To add a new setting,
add one entry to SCHEMA -- the web page picks it up automatically.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

SECRET_MASK = "••••••••"

# Each field: (key, type, default, label, help, extra)
# type is one of: text, password, textarea, number, bool, select
SCHEMA: list[dict[str, Any]] = [
    {
        "id": "general",
        "title": "General",
        "fields": [
            {"key": "assistant_name", "type": "text", "default": "Jarvis", "label": "Assistant name",
             "help": "What the assistant calls itself."},
            {"key": "user_name", "type": "text", "default": "", "label": "Your name",
             "help": "Optional. The assistant will call you this."},
            {"key": "location", "type": "text", "default": "", "label": "Home location",
             "help": "City for weather and local time, e.g. 'Dallas, TX' or 'Toronto'."},
            {"key": "units", "type": "select", "default": "imperial", "label": "Units",
             "options": ["imperial", "metric"], "help": "Imperial = °F / mph. Metric = °C / km/h."},
            {"key": "time_format", "type": "select", "default": "12h", "label": "Clock style",
             "options": ["12h", "24h"]},
            {"key": "theme", "type": "select", "default": "dark", "label": "Settings page theme",
             "options": ["dark", "light", "system"]},
        ],
    },
    {
        "id": "llm",
        "title": "AI model (brain)",
        "fields": [
            {"key": "base_url", "type": "text", "default": "http://localhost:11434/v1", "label": "API address",
             "help": "Any OpenAI-compatible server. Ollama: http://localhost:11434/v1 . "
                     "LM Studio: http://localhost:1234/v1 . llama.cpp server: http://localhost:8080/v1"},
            {"key": "model", "type": "text", "default": "qwen2.5:7b", "label": "Model name",
             "help": "Must be a model your server has. Click 'Load models' to pick from the list. "
                     "Choose one that supports 'tools'.", "extra": "models"},
            {"key": "api_style", "type": "select", "default": "auto", "label": "Server type",
             "options": ["auto", "ollama", "openai"],
             "help": "auto = detect (Ollama's port 11434 uses Ollama's own API so the context size below works)."},
            {"key": "context_length", "type": "number", "default": 16384, "label": "Context size (tokens, Ollama)",
             "min": 2048, "max": 131072, "step": 1024,
             "help": "How much text the model can see at once. Jarvis has many tools, so keep this at 8192 or more."},
            {"key": "stream_replies", "type": "bool", "default": True, "label": "Start speaking before the answer is finished",
             "help": "Faster replies: speaks the first sentence while the rest is still being written."},
            {"key": "api_key", "type": "password", "default": "", "label": "API key",
             "help": "Leave empty for Ollama. Only needed for servers that require a key."},
            {"key": "temperature", "type": "number", "default": 0.4, "label": "Creativity (temperature)",
             "min": 0, "max": 2, "step": 0.1, "help": "Lower = more focused, higher = more creative."},
            {"key": "custom_prompt", "type": "textarea", "default": "", "label": "Your extra instructions",
             "help": "Added on top of the built-in instructions. Example: 'Be witty and a bit sarcastic. "
                     "Keep answers under two sentences.'"},
            {"key": "memory_turns", "type": "number", "default": 10, "label": "Conversation memory (turns)",
             "min": 0, "max": 50, "step": 1, "help": "How many back-and-forths it remembers."},
            {"key": "forget_after_minutes", "type": "number", "default": 5, "label": "Forget conversation after (minutes)",
             "min": 0, "max": 240, "step": 1, "help": "Start fresh after this long without talking. 0 = never."},
            {"key": "timeout_seconds", "type": "number", "default": 120, "label": "Give up after (seconds)",
             "min": 5, "max": 600, "step": 5},
        ],
    },
    {
        "id": "listening",
        "title": "Listening (microphone)",
        "fields": [
            {"key": "end_of_speech_seconds", "type": "number", "default": 2.5,
             "label": "Seconds of silence before I answer",
             "min": 0.5, "max": 10, "step": 0.1,
             "help": "Raise this if it cuts you off while you are still talking."},
            {"key": "wait_for_speech_seconds", "type": "number", "default": 6, "label": "Wait for you to start talking (seconds)",
             "min": 1, "max": 30, "step": 0.5, "help": "After the wake word, how long to wait for you to begin."},
            {"key": "max_recording_seconds", "type": "number", "default": 45, "label": "Longest question (seconds)",
             "min": 5, "max": 180, "step": 1},
            {"key": "followup_seconds", "type": "number", "default": 5, "label": "Follow-up listening (seconds)",
             "min": 0, "max": 30, "step": 0.5,
             "help": "After answering, keep listening this long so you can reply without the wake word. 0 = off."},
            {"key": "vad_aggressiveness", "type": "select", "default": "2", "label": "Voice detection strictness",
             "options": ["0", "1", "2", "3"],
             "help": "How sure I must be that a sound is a human voice (music, alarms and noise are ignored). "
                     "0 = easiest, 3 = strictest. Lower it if I stop listening while you're still talking quietly."},
            {"key": "mic_device", "type": "text", "default": "", "label": "Microphone",
             "help": "Leave empty for the system default. Click 'Load devices' to choose.", "extra": "input_devices"},
            {"key": "cancel_enabled", "type": "bool", "default": True, "label": "Listen for the cancel phrase",
             "help": "While I'm talking, listening, ringing or playing anything, say the cancel phrase (no wake word "
                     "needed) to stop everything: my voice, music, alarms, timers, the briefing, browser videos."},
            {"key": "cancel_phrase", "type": "text", "default": "cancel that", "label": "Cancel phrase"},
            {"key": "media_while_listening", "type": "select", "default": "pause", "label": "Music while I listen",
             "options": ["pause", "lower", "leave"],
             "help": "pause = pause music and browser videos while you talk to me, then resume (most reliable). "
                     "lower = turn my music down. leave = do nothing."},
            {"key": "chimes", "type": "bool", "default": True, "label": "Play chimes",
             "help": "A sound when it starts listening and when it got your question."},
        ],
    },
    {
        "id": "wake_word",
        "title": "Wake word",
        "fields": [
            {"key": "engine", "type": "select", "default": "openwakeword", "label": "Wake word engine",
             "options": ["openwakeword", "whisper", "none"],
             "help": "openwakeword = fast and very reliable, but only for its built-in wake words (or a model you train). "
                     "whisper = say ANY word or phrase you type below (uses a bit more of the graphics card). "
                     "none = no voice wake word (use the 'Talk' button on this page)."},
            {"key": "openwakeword_model", "type": "select", "default": "hey_jarvis", "label": "Built-in wake word",
             "options": ["hey_jarvis", "alexa", "hey_mycroft", "hey_rhasspy", "custom"],
             "help": "Used with the openwakeword engine. Pick 'custom' to use a model file you trained."},
            {"key": "custom_model_path", "type": "text", "default": "", "label": "Custom wake word model file",
             "help": "Full path to a .onnx file (only for 'custom')."},
            {"key": "threshold", "type": "number", "default": 0.6, "label": "Wake word strictness",
             "min": 0.1, "max": 0.95, "step": 0.05,
             "help": "Works for both engines. Lower = wakes more easily (more false alarms). Higher = stricter. "
                     "With the whisper engine, the whole phrase must be said (e.g. both 'hey' and the name) unless "
                     "this is below 0.4."},
            {"key": "phrase", "type": "text", "default": "Jarvis", "label": "Wake phrase (whisper engine)",
             "help": "Any word or phrase, e.g. 'Jarvis', 'Hey Computer', 'Friday'."},
            {"key": "whisper_model", "type": "select", "default": "base.en", "label": "Wake phrase listener model",
             "options": ["tiny.en", "base.en", "small.en"], "help": "Bigger = more accurate, slightly slower."},
        ],
    },
    {
        "id": "speech_to_text",
        "title": "Speech to text",
        "fields": [
            {"key": "model", "type": "select", "default": "large-v3-turbo", "label": "Whisper model",
             "options": ["tiny.en", "base.en", "small.en", "medium.en", "distil-large-v3", "large-v3-turbo", "large-v3"],
             "help": "large-v3-turbo is accurate and fast on your 3090 Ti. Pick small.en if running without a graphics card."},
            {"key": "device", "type": "select", "default": "auto", "label": "Run on",
             "options": ["auto", "cuda", "cpu"], "help": "cuda = graphics card. auto = graphics card if it works."},
            {"key": "language", "type": "text", "default": "en", "label": "Language code",
             "help": "en = English. Leave empty to auto-detect."},
        ],
    },
    {
        "id": "tts",
        "title": "Voice (text to speech)",
        "fields": [
            {"key": "engine", "type": "select", "default": "kokoro", "label": "Voice engine",
             "options": ["kokoro", "openai_compatible", "piper", "espeak", "none"],
             "help": "kokoro = Kokoro built in. openai_compatible = any voice server with an OpenAI-style API "
                     "(e.g. Kokoro-FastAPI, AllTalk, OpenedAI-Speech). piper = Piper command. espeak = robotic backup."},
            {"key": "speed", "type": "number", "default": 1.1, "label": "Speaking speed",
             "min": 0.5, "max": 2.0, "step": 0.05},
            {"key": "volume", "type": "number", "default": 1.0, "label": "Volume",
             "min": 0.1, "max": 2.0, "step": 0.1},
            {"key": "kokoro_voice", "type": "text", "default": "af_heart", "label": "Kokoro voice",
             "help": "e.g. af_heart, af_bella, am_michael, am_fenrir, bm_george, bf_emma."},
            {"key": "kokoro_lang", "type": "select", "default": "a", "label": "Kokoro accent",
             "options": ["a", "b"], "help": "a = American English, b = British English."},
            {"key": "http_url", "type": "text", "default": "http://localhost:8880/v1", "label": "Voice server address",
             "help": "For openai_compatible. Kokoro-FastAPI default is http://localhost:8880/v1"},
            {"key": "http_model", "type": "text", "default": "kokoro", "label": "Voice server model"},
            {"key": "http_voice", "type": "text", "default": "af_heart", "label": "Voice server voice"},
            {"key": "http_api_key", "type": "password", "default": "", "label": "Voice server API key"},
            {"key": "piper_command", "type": "text", "default": "piper", "label": "Piper program"},
            {"key": "piper_model", "type": "text", "default": "", "label": "Piper voice file (.onnx)"},
            {"key": "output_device", "type": "text", "default": "", "label": "Speaker",
             "help": "Leave empty for the system default.", "extra": "output_devices"},
        ],
    },
    {
        "id": "search",
        "title": "Web search",
        "fields": [
            {"key": "provider", "type": "select", "default": "none", "label": "Search engine",
             "options": ["none", "searxng", "tavily", "brave"],
             "help": "Only your search words are sent out -- never your voice or conversation."},
            {"key": "searxng_url", "type": "text", "default": "http://localhost:8080", "label": "SearXNG address",
             "help": "Your SearXNG server. JSON output must be enabled in its settings.yml."},
            {"key": "tavily_api_key", "type": "password", "default": "", "label": "Tavily API key"},
            {"key": "brave_api_key", "type": "password", "default": "", "label": "Brave Search API key"},
            {"key": "max_results", "type": "number", "default": 5, "label": "Results to read",
             "min": 1, "max": 10, "step": 1},
        ],
    },
    {
        "id": "alarms",
        "title": "Alarms & timers",
        "fields": [
            {"key": "snooze_minutes", "type": "number", "default": 9, "label": "Snooze length (minutes)",
             "min": 1, "max": 60, "step": 1},
            {"key": "ring_minutes", "type": "number", "default": 10, "label": "Stop ringing by itself after (minutes)",
             "min": 1, "max": 60, "step": 1},
            {"key": "volume", "type": "number", "default": 0.8, "label": "Alarm volume", "min": 0.1, "max": 1.5, "step": 0.05},
            {"key": "gentle_seconds", "type": "number", "default": 30, "label": "Gentle wake: fade in over (seconds)",
             "min": 0, "max": 300, "step": 5, "help": "Alarm starts quiet and gets louder. 0 = full volume right away."},
            {"key": "sound_file", "type": "text", "default": "", "label": "Custom alarm sound file (optional)",
             "help": "Full path to a song or sound, e.g. /home/you/Music/wake.mp3. Empty = built-in chime."},
            {"key": "timer_ring_seconds", "type": "number", "default": 60, "label": "Timer rings for (seconds)",
             "min": 5, "max": 600, "step": 5},
            {"key": "morning_briefing", "type": "bool", "default": True, "label": "Morning briefing after my first alarm",
             "help": "After you stop a morning alarm: time, weather, today's schedule, reminders and a word of the day."},
            {"key": "briefing_news", "type": "bool", "default": True, "label": "Include news headlines in the briefing"},
        ],
    },
    {
        "id": "reminders",
        "title": "Reminders & calendar",
        "fields": [
            {"key": "event_heads_up_minutes", "type": "number", "default": 15, "label": "Warn me before calendar events (minutes)",
             "min": 0, "max": 240, "step": 5, "help": "0 = no warning."},
            {"key": "nag_minutes", "type": "number", "default": 10, "label": "Repeat unconfirmed reminders every (minutes)",
             "min": 1, "max": 120, "step": 1, "help": "For reminders that need a 'yes, I did it' (like medication)."},
            {"key": "nag_max", "type": "number", "default": 6, "label": "Give up repeating after (times)",
             "min": 1, "max": 50, "step": 1},
            {"key": "google_enabled", "type": "bool", "default": False, "label": "Use my Google Calendar",
             "help": "Set up once: see 'Google Calendar' in the README, then click 'Connect Google' on the Dashboard."},
            {"key": "google_credentials_file", "type": "text", "default": "", "label": "Google credentials file (.json)",
             "help": "The file you downloaded from Google Cloud, e.g. /home/you/Downloads/client_secret.json"},
            {"key": "google_calendar_id", "type": "text", "default": "primary", "label": "Google calendar ID",
             "help": "'primary' = your main calendar."},
            {"key": "new_events_go_to", "type": "select", "default": "google", "label": "Save new events to",
             "options": ["google", "jarvis"], "help": "google = your Google Calendar (if connected). jarvis = only on this PC."},
        ],
    },
    {
        "id": "learning",
        "title": "Learning & vocabulary",
        "fields": [
            {"key": "tutor_enabled", "type": "bool", "default": True, "label": "Vocabulary tutor",
             "help": "Now and then, while you're already talking to me, I quiz you on words or teach a new one."},
            {"key": "chance_percent", "type": "number", "default": 20, "label": "How often (% of conversations)",
             "min": 0, "max": 100, "step": 5},
            {"key": "min_minutes_between", "type": "number", "default": 45, "label": "At least this many minutes apart",
             "min": 0, "max": 1440, "step": 5},
            {"key": "max_per_day", "type": "number", "default": 4, "label": "At most this many per day",
             "min": 0, "max": 50, "step": 1},
            {"key": "teach_new_words", "type": "bool", "default": True, "label": "Teach me new words"},
            {"key": "sentence_practice", "type": "bool", "default": True, "label": "Ask me to use words in a sentence"},
            {"key": "challenges", "type": "bool", "default": True, "label": "Brain challenges (riddles & mental math)"},
            {"key": "online_dictionary", "type": "bool", "default": True, "label": "Use online dictionary as a backup",
             "help": "If a word isn't in the offline dictionary, look it up online (only the word is sent)."},
        ],
    },
    {
        "id": "smarthome",
        "title": "Smart home",
        "fields": [
            {"key": "govee_mode", "type": "select", "default": "lan", "label": "Govee lights",
             "options": ["off", "lan", "cloud", "lan+cloud"],
             "help": "lan = control over your Wi-Fi, private and fast (turn on 'LAN Control' for each light in the Govee Home app). "
                     "cloud = through Govee's internet service (needs an API key; for models without LAN Control)."},
            {"key": "govee_api_key", "type": "password", "default": "", "label": "Govee API key (cloud only)",
             "help": "Govee Home app > Profile > Settings > Apply for API Key. It arrives by email."},
            {"key": "govee_names", "type": "textarea", "default": "", "label": "Light names",
             "help": "Easiest: click 'Find Govee lights' at the bottom of this page, type a name, click Add. "
                     "Or type one per line: name = IP address (or device ID), e.g. TV strip = 192.168.1.42"},
            {"key": "scenes", "type": "textarea", "default": "Movie = all: purple 15\nBright = all: white 100\nRelax = all: warm white 40",
             "label": "Scenes", "help": "One per line: Scene name = light: color brightness, light: off ... "
                                         "'all' means every light. Example: Movie = TV strip: blue 20, Desk lamp: off"},
            {"key": "homeassistant_url", "type": "text", "default": "", "label": "Home Assistant address (optional)",
             "help": "e.g. http://localhost:8123 -- controls any device Home Assistant knows."},
            {"key": "homeassistant_token", "type": "password", "default": "", "label": "Home Assistant token",
             "help": "Home Assistant > your profile > Security > Long-lived access tokens > Create token."},
        ],
    },
    {
        "id": "music",
        "title": "Music & sleep sounds",
        "fields": [
            {"key": "music_folder", "type": "text", "default": "", "label": "Music folder",
             "help": "e.g. /home/you/Music, or a network share like smb://nas/Music/Rock (open the share once in "
                     "the Files app and tick 'Remember password forever'). Songs in sub-folders are found too."},
            {"key": "sleep_sound_file", "type": "text", "default": "", "label": "Sleep sound file",
             "help": "e.g. /home/you/Music/rain.mp3 or smb://nas/Sounds/rain.mp3 -- plays on a loop when you say "
                     "'play my sleep sound' or 'play rain'."},
            {"key": "sleep_minutes", "type": "number", "default": 0, "label": "Stop sleep sound after (minutes)",
             "min": 0, "max": 720, "step": 15, "help": "0 = play all night until you say stop."},
            {"key": "music_volume", "type": "number", "default": 60, "label": "Music volume (%)", "min": 0, "max": 130, "step": 5},
            {"key": "sleep_volume", "type": "number", "default": 40, "label": "Sleep sound volume (%)", "min": 0, "max": 130, "step": 5},
            {"key": "duck", "type": "bool", "default": True, "label": "Lower the music when I make announcements"},
        ],
    },
    {
        "id": "traffic",
        "title": "Traffic",
        "fields": [
            {"key": "tomtom_api_key", "type": "password", "default": "", "label": "TomTom API key",
             "help": "Free: create an account at developer.tomtom.com, then copy your key. 2,500 free lookups per day."},
            {"key": "home_address", "type": "text", "default": "", "label": "Home address"},
            {"key": "work_address", "type": "text", "default": "", "label": "Work address"},
            {"key": "places", "type": "textarea", "default": "", "label": "Other places",
             "help": "One per line: name = address. Example: Gym = 123 Main St, Dallas, TX"},
        ],
    },
    {
        "id": "phone",
        "title": "Phone notifications",
        "fields": [
            {"key": "enabled", "type": "bool", "default": False, "label": "Send notifications to my phone",
             "help": "Install the free 'ntfy' app on your phone, tap +, and subscribe to the same topic name as below."},
            {"key": "server", "type": "text", "default": "https://ntfy.sh", "label": "ntfy server",
             "help": "https://ntfy.sh is free. For full privacy you can run your own ntfy server in Docker."},
            {"key": "topic", "type": "text", "default": "", "label": "Topic (secret channel name)",
             "help": "Make it long and hard to guess, like a password -- e.g. jarvis-7k2m9x4q. Anyone who knows it can read your alerts."},
            {"key": "token", "type": "password", "default": "", "label": "Access token (optional)"},
            {"key": "send_reminders", "type": "bool", "default": True, "label": "Send my reminders too"},
            {"key": "send_security", "type": "bool", "default": True, "label": "Send security alerts"},
            {"key": "send_network", "type": "bool", "default": True, "label": "Send network alerts"},
            {"key": "send_alarms", "type": "bool", "default": True, "label": "Tell me about alarms that ring while I'm away"},
        ],
    },
    {
        "id": "security",
        "title": "Security (away mode)",
        "fields": [
            {"key": "schedule_enabled", "type": "bool", "default": False, "label": "Turn on away mode automatically",
             "help": "On the days and hours below. You can also say 'I'm leaving' / 'I'm home', or use the Security page."},
            {"key": "days", "type": "text", "default": "weekdays", "label": "Away days",
             "help": "e.g. weekdays, every day, mon tue wed thu fri"},
            {"key": "start", "type": "text", "default": "08:00", "label": "Away from (24-hour time)"},
            {"key": "end", "type": "text", "default": "17:30", "label": "Away until (24-hour time)"},
            {"key": "sensitivity", "type": "number", "default": 6, "label": "Sensitivity (1-10)", "min": 1, "max": 10, "step": 1,
             "help": "Higher = picks up quieter sounds (typing, mouse clicks). Lower = only louder sounds and voices."},
            {"key": "transcribe", "type": "bool", "default": True, "label": "Write down what voices said",
             "help": "Uses Whisper on this PC, only on recorded clips. The words appear in the alert."},
            {"key": "attach_audio", "type": "bool", "default": False, "label": "Attach the recording to the phone alert",
             "help": "Off = recordings stay on this PC only (most private)."},
            {"key": "alert_every_minutes", "type": "number", "default": 10, "label": "Alert at most every (minutes)",
             "min": 1, "max": 240, "step": 1, "help": "Keeps recording in between; you get a summary."},
            {"key": "keep_days", "type": "number", "default": 30, "label": "Keep recordings for (days)", "min": 1, "max": 365, "step": 1},
            {"key": "disarm_phrase", "type": "text", "default": "I'm home", "label": "Disarm phrase",
             "help": "While away mode is on I stay completely silent. Say the wake word, then this phrase, to turn it off."},
            {"key": "netwatch_enabled", "type": "bool", "default": True, "label": "Alert when a device connects to this PC",
             "help": "e.g. someone opens a shared folder, remote desktop, SSH, or this settings page from another device."},
            {"key": "netwatch_new_devices", "type": "bool", "default": True, "label": "Alert when a new device joins my Wi-Fi"},
            {"key": "netwatch_ignore", "type": "text", "default": "", "label": "Ignore these devices (IP addresses)",
             "help": "Separate with commas, e.g. your own phone: 192.168.1.20"},
            {"key": "netwatch_ignore_jarvis", "type": "bool", "default": False, "label": "Don't alert for the Jarvis settings page"},
            {"key": "netwatch_cooldown_minutes", "type": "number", "default": 60, "label": "Same device alerts at most every (minutes)",
             "min": 1, "max": 1440, "step": 5},
        ],
    },
    {
        "id": "pc",
        "title": "PC control",
        "fields": [
            {"key": "allow_open_apps", "type": "bool", "default": True, "label": "Let me open apps"},
            {"key": "allow_power", "type": "bool", "default": True, "label": "Let me sleep / restart / shut down the PC",
             "help": "I always ask you to confirm first."},
        ],
    },
    {
        "id": "knowledge",
        "title": "News & offline Wikipedia",
        "fields": [
            {"key": "news_feeds", "type": "textarea",
             "default": "Top news = https://feeds.npr.org/1001/rss.xml\nWorld = https://feeds.bbci.co.uk/news/world/rss.xml\n"
                        "Technology = https://feeds.arstechnica.com/arstechnica/index\nSports = https://www.espn.com/espn/rss/news",
             "label": "News feeds", "help": "One per line: name = website. A normal site like techcrunch.com works -- "
                                            "I find its news feed automatically. No account needed."},
            {"key": "kiwix_url", "type": "text", "default": "", "label": "Offline Wikipedia (Kiwix) address",
             "help": "e.g. http://localhost:8090 -- see README for the 2-minute Docker setup. Empty = off."},
            {"key": "kiwix_book", "type": "text", "default": "", "label": "Kiwix book name (optional)",
             "help": "Leave empty to search all books."},
        ],
    },
    {
        "id": "history",
        "title": "Conversation history",
        "fields": [
            {"key": "enabled", "type": "bool", "default": True, "label": "Save my conversations",
             "help": "Saved only on this PC. See them on the History page."},
            {"key": "keep_days", "type": "number", "default": 90, "label": "Keep for (days)", "min": 1, "max": 3650, "step": 1},
        ],
    },
    {
        "id": "web",
        "title": "Settings page access",
        "fields": [
            {"key": "allow_network", "type": "bool", "default": False, "label": "Allow phones/other computers on my Wi-Fi",
             "help": "Off = only this PC can open the settings page (most private). Takes effect after restart."},
            {"key": "port", "type": "number", "default": 8765, "label": "Port", "min": 1024, "max": 65535, "step": 1,
             "help": "Takes effect after restart."},
        ],
    },
]

# Settings whose change requires the voice pipeline (mic, wake word, whisper) to restart.
VOICE_RESTART_KEYS = {
    ("listening", "mic_device"), ("listening", "vad_aggressiveness"),
    ("wake_word", "engine"), ("wake_word", "openwakeword_model"), ("wake_word", "custom_model_path"),
    ("wake_word", "phrase"), ("wake_word", "whisper_model"), ("wake_word", "threshold"),
    ("listening", "cancel_phrase"), ("listening", "cancel_enabled"),
    ("speech_to_text", "model"), ("speech_to_text", "device"),
}


def defaults() -> dict[str, dict[str, Any]]:
    return {sec["id"]: {f["key"]: copy.deepcopy(f["default"]) for f in sec["fields"]} for sec in SCHEMA}


def _field(section: str, key: str) -> dict[str, Any] | None:
    for sec in SCHEMA:
        if sec["id"] == section:
            for f in sec["fields"]:
                if f["key"] == key:
                    return f
    return None


def _coerce(field: dict[str, Any], value: Any) -> Any:
    t = field["type"]
    if t == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if t == "number":
        num = float(value)
        if "min" in field:
            num = max(float(field["min"]), num)
        if "max" in field:
            num = min(float(field["max"]), num)
        if isinstance(field["default"], int) and not isinstance(field["default"], bool):
            return int(round(num))
        return num
    if t == "select":
        value = str(value)
        if value not in field["options"]:
            raise ValueError(f"'{value}' is not one of {field['options']}")
        return value
    return "" if value is None else str(value)


def config_dir() -> Path:
    env = os.environ.get("JARVIS_BOT_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".config" / "jarvis-bot"


def data_dir() -> Path:
    env = os.environ.get("JARVIS_BOT_DATA_DIR")
    path = Path(env) if env else Path.home() / ".local" / "share" / "jarvis-bot"
    path.mkdir(parents=True, exist_ok=True)
    return path


class Settings:
    """Thread-safe settings store backed by a JSON file."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else config_dir() / "settings.json"
        self._lock = threading.RLock()
        self._data = defaults()
        self._listeners: list[Callable[[set[tuple[str, str]]], None]] = []
        self.load()

    # ---- file I/O -------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            self._data = defaults()
            if not self.path.exists():
                self.save()
                return
            try:
                stored = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as exc:  # broken file -> keep defaults, keep a backup
                log.error("Could not read %s (%s). Using defaults; old file saved as .bak", self.path, exc)
                self.path.rename(self.path.with_suffix(".json.bak"))
                self.save()
                return
            for section, values in stored.items():
                if section not in self._data or not isinstance(values, dict):
                    continue
                for key, value in values.items():
                    field = _field(section, key)
                    if field is None:
                        continue
                    try:
                        self._data[section][key] = _coerce(field, value)
                    except (ValueError, TypeError):
                        log.warning("Ignoring bad saved value %s.%s=%r", section, key, value)

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            os.chmod(tmp, 0o600)  # file contains API keys: only you can read it
            tmp.replace(self.path)

    # ---- access ---------------------------------------------------------
    def get(self, section: str, key: str) -> Any:
        with self._lock:
            return self._data[section][key]

    def section(self, section: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._data[section])

    def as_dict(self, mask_secrets: bool = False) -> dict[str, dict[str, Any]]:
        with self._lock:
            data = copy.deepcopy(self._data)
        if mask_secrets:
            for sec in SCHEMA:
                for f in sec["fields"]:
                    if f["type"] == "password" and data[sec["id"]][f["key"]]:
                        data[sec["id"]][f["key"]] = SECRET_MASK
        return data

    def update(self, changes: dict[str, dict[str, Any]]) -> set[tuple[str, str]]:
        """Apply changes like {"listening": {"end_of_speech_seconds": 3}}. Returns changed keys."""
        changed: set[tuple[str, str]] = set()
        with self._lock:
            for section, values in changes.items():
                if not isinstance(values, dict):
                    continue
                for key, value in values.items():
                    field = _field(section, key)
                    if field is None:
                        raise KeyError(f"Unknown setting {section}.{key}")
                    if field["type"] == "password" and value == SECRET_MASK:
                        continue  # unchanged masked secret
                    new = _coerce(field, value)
                    if self._data[section][key] != new:
                        self._data[section][key] = new
                        changed.add((section, key))
            if changed:
                self.save()
        for listener in list(self._listeners):
            try:
                listener(changed)
            except Exception:
                log.exception("Settings listener failed")
        return changed

    def on_change(self, callback: Callable[[set[tuple[str, str]]], None]) -> None:
        self._listeners.append(callback)
