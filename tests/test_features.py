"""Checks for alarms, timers, reminders, calendar, lists, memory, tutor, smart home, security, network watch..."""
import json
import socket
import threading
import time
from datetime import datetime, timedelta

import httpx
import numpy as np
import pytest

from jarvis import timeparse as tp
from jarvis import tools
from jarvis.brain import Brain
from jarvis.context import ctx
from jarvis.llm import LLMClient
from jarvis.scheduler import Scheduler


def run(tool_name, settings, **args):
    return json.loads(tools.run(tool_name, args, settings))


class FakeRinger:
    def __init__(self):
        self.rang = []
        self.ringing = None
        self.last_rang = None
        self.pending_briefing = None

    def ring(self, kind, label, item_id=None, seconds=None, speak=""):
        self.rang.append((kind, label, speak))
        self.ringing = {"kind": kind, "label": label, "id": item_id, "started": time.time()}

    def is_ringing(self):
        return self.ringing is not None

    def stop(self, reason="user"):
        info, self.ringing = self.ringing, None
        if info:
            self.last_rang = dict(info, stopped=time.time())
        return info


class FakeAnnouncer:
    def __init__(self):
        self.said = []

    def say(self, text, push_kind=None, chime=True):
        self.said.append(text)


@pytest.fixture
def world(settings, db):
    ctx.settings = settings
    ctx.ringer = FakeRinger()
    ctx.announcer = FakeAnnouncer()
    return Scheduler(settings, db)


# ---------------------------------------------------------------- timers & alarms
def test_timer_rings_and_status(settings, world):
    out = run("timer", settings, action="start", duration="2 minutes", name="pasta")
    assert out["length"] == "2 minutes"
    status = run("timer", settings, action="status")
    assert status["timers"][0]["name"] == "pasta"
    world.tick(time.time() + 121)
    assert ctx.ringer.rang[0][:2] == ("timer", "pasta")
    assert "pasta timer is done" in ctx.ringer.rang[0][2]
    assert run("timer", settings, action="status")["timers"] == "no timers running"


def test_timer_add_time_and_cancel(settings, world):
    run("timer", settings, action="start", duration="10 minutes", name="laundry")
    out = run("timer", settings, action="add_time", name="laundry", duration="5 minutes")
    assert out["time_left"].startswith("14 minutes") or out["time_left"].startswith("15 minutes")
    assert run("timer", settings, action="cancel", name="laundry")["cancelled"] == ["laundry"]


def test_alarm_one_time_and_repeating(settings, world, db):
    out = run("alarm", settings, action="set", time="in 1 minute", label="test")
    assert out["repeats"] == "once"
    world.tick(time.time() + 61)
    assert ctx.ringer.rang[-1][0] == "alarm"
    assert db.one("SELECT enabled FROM alarms WHERE id=?", (out["alarm_id"],))["enabled"] == 0

    rep = run("alarm", settings, action="set", time="6:30 am", days="weekdays")
    assert rep["repeats"] == "on weekdays"
    row = db.one("SELECT * FROM alarms WHERE id=?", (rep["alarm_id"],))
    first = datetime.fromtimestamp(row["next_fire"])
    assert (first.hour, first.minute) == (6, 30) and first.weekday() < 5
    world.tick(row["next_fire"] + 1)
    row2 = db.one("SELECT * FROM alarms WHERE id=?", (rep["alarm_id"],))
    assert row2["enabled"] == 1 and row2["next_fire"] > row["next_fire"]
    assert datetime.fromtimestamp(row2["next_fire"]).weekday() < 5


def test_alarm_snooze(settings, world, db):
    out = run("alarm", settings, action="set", time="in 1 minute")
    world.tick(time.time() + 61)
    assert ctx.ringer.is_ringing()
    snooze = run("alarm", settings, action="snooze", minutes=5)
    assert snooze["snoozed_minutes"] == 5 and not ctx.ringer.is_ringing()
    count = len(ctx.ringer.rang)
    world.tick(time.time() + 5 * 60 + 2)
    assert len(ctx.ringer.rang) == count + 1
    assert db.one("SELECT enabled FROM alarms WHERE id=?", (out["alarm_id"],))["enabled"] == 0


def test_alarm_list_and_cancel(settings, world):
    run("alarm", settings, action="set", time="7am", label="gym")
    run("alarm", settings, action="set", time="9pm", label="bed")
    assert len(run("alarm", settings, action="list")["alarms"]) == 2
    assert "gym" in run("alarm", settings, action="cancel", label="gym")["cancelled"][0]
    assert len(run("alarm", settings, action="list")["alarms"]) == 1


# ---------------------------------------------------------------- reminders
def test_reminder_fires_and_repeats(settings, world, db):
    out = run("reminder", settings, action="set", text="stretch", when="in 1 minute", repeat="every 2 hours")
    assert out["repeats"] == "every 2 hours"
    world.tick(time.time() + 61)
    assert ctx.announcer.said[-1].startswith("Reminder: stretch")
    row = db.one("SELECT * FROM reminders WHERE id=?", (out["reminder_id"],))
    assert row["active"] == 1 and row["next_fire"] > time.time() + 3600


def test_medication_reminder_nags_until_confirmed(settings, world, db):
    settings.update({"reminders": {"nag_minutes": 1}})
    run("reminder", settings, action="set", text="take your pills", when="in 1 minute", needs_confirmation=True)
    t0 = time.time() + 61
    world.tick(t0)
    assert "Let me know when you've done it" in ctx.announcer.said[-1]
    world.tick(t0 + 61)
    assert ctx.announcer.said[-1].startswith("Just checking: did you take your pills")
    assert "Waiting for the user to confirm" in Brain(settings).system_prompt()
    assert run("reminder", settings, action="confirm")["confirmed"] == "take your pills"
    n = len(ctx.announcer.said)
    world.tick(t0 + 200)
    assert len(ctx.announcer.said) == n


def test_reminder_weekly_days():
    from jarvis.tools.reminders import next_repeat, normalize_repeat
    assert normalize_repeat("weekdays") == "days:0,1,2,3,4"
    assert normalize_repeat("every day") == "daily"
    assert normalize_repeat("every 4 hours") == "every 4 hours"
    fri = datetime(2026, 11, 13, 9, 0).astimezone().timestamp()
    nxt = datetime.fromtimestamp(next_repeat(fri, "days:0,1,2,3,4"))
    assert nxt.weekday() == 0 and nxt.hour == 9


# ---------------------------------------------------------------- calendar
def test_calendar_add_and_schedule(settings, world):
    settings.update({"reminders": {"new_events_go_to": "jarvis", "event_heads_up_minutes": 15}})
    out = run("calendar", settings, action="add", title="Dentist", when="tomorrow at 2pm", duration_minutes=60)
    assert out["saved_to"] == "Jarvis calendar"
    sched = run("calendar", settings, action="schedule", day="tomorrow")
    assert "Dentist" in json.dumps(sched)
    start = tp.parse_when("tomorrow at 2pm").timestamp()
    world.tick(start - 10 * 60)
    assert "Dentist starts in 10 minutes" in ctx.announcer.said[-1]
    assert run("calendar", settings, action="delete", title="dentist")["deleted"] == "Dentist"


# ---------------------------------------------------------------- lists & memory
def test_lists(settings, db):
    out = run("lists", settings, action="add", list_name="grocery", items="milk, eggs and bread")
    assert out["list"] == "shopping" and out["added"] == ["milk", "eggs", "bread"]
    assert run("lists", settings, action="show", list_name="shopping list")["items"] == ["milk", "eggs", "bread"]
    assert run("lists", settings, action="remove", list_name="shopping", items="egg")["removed"] == ["eggs"]
    run("lists", settings, action="add", list_name="to do", items="call plumber")
    assert run("lists", settings, action="show", list_name="todo")["items"] == ["call plumber"]


def test_memory(settings, db):
    run("memory", settings, action="remember", text="The spare key is in the blue drawer")
    run("memory", settings, action="remember", text="Oil change was at 82,000 miles")
    out = run("memory", settings, action="recall", text="where did I put the spare key")
    assert out["found"] and "blue drawer" in out["memories"][0]
    assert "82,000" in run("memory", settings, action="recall", text="when was my oil change")["memories"][0]


# ---------------------------------------------------------------- tutor
def test_tutor_teaches_quizzes_and_grades(settings, db):
    from jarvis.tutor import Tutor
    settings.update({"learning": {"tutor_enabled": True, "chance_percent": 100, "min_minutes_between": 0,
                                  "max_per_day": 10, "challenges": False, "sentence_practice": False}})
    ctx.tutor = t = Tutor(settings, db)
    t.add_word("concatenate", "to link things together in a chain", "looked_up")
    db.execute("UPDATE vocab SET next_review=0")
    question = t.maybe_followup("what's the weather like", "It's sunny.", force="quiz")
    assert "concatenate" in question and t.pending["type"] == "define"
    assert "correct meaning: to link things together" in Brain(settings).system_prompt()
    assert t.record("concatenate", "correct")["next_review_in_days"] >= 2
    assert t.pending is None
    new = t.maybe_followup("tell me something", "Sure.", force="teach")
    assert "Here's a new word for you" in new


def test_tutor_through_brain(settings, db):
    """The tutor's question is appended after the answer, then graded on the next turn."""
    from jarvis.tutor import Tutor
    settings.update({"learning": {"tutor_enabled": True, "chance_percent": 100, "min_minutes_between": 0,
                                  "max_per_day": 10, "challenges": False, "teach_new_words": False,
                                  "sentence_practice": False}})
    ctx.tutor = t = Tutor(settings, db)
    t.add_word("ubiquitous", "found everywhere", "looked_up")
    db.execute("UPDATE vocab SET next_review=0")
    replies = iter([
        {"content": "Glad to hear it."},
        {"content": "", "tool_calls": [{"id": "1", "type": "function", "function": {
            "name": "learning", "arguments": json.dumps({"action": "quiz_result", "word": "ubiquitous",
                                                          "result": "correct"})}}]},
        {"content": "That's right!"},
    ])
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": next(replies)}]})
    brain = Brain(settings, LLMClient(settings, transport=httpx.MockTransport(handler)))
    first = brain.ask("I had a good day at work")
    assert first.startswith("Glad to hear it.") and "ubiquitous" in first
    assert brain.ask("it means it's everywhere") == "That's right!"
    assert "TUTOR" in seen[1]["messages"][0]["content"]
    assert db.one("SELECT correct FROM vocab WHERE word='ubiquitous'")["correct"] == 1


def test_tutor_respects_limits(settings, db):
    from jarvis.tutor import Tutor
    settings.update({"learning": {"tutor_enabled": True, "chance_percent": 100, "min_minutes_between": 60,
                                  "max_per_day": 5}})
    t = Tutor(settings, db)
    assert t.maybe_followup("hello there friend", "Hi.") is not None
    t.pending = None
    assert t.maybe_followup("hello again friend", "Hi.") is None  # too soon


# ---------------------------------------------------------------- streaming + ollama dialect
def test_ollama_streaming_and_context_size(settings, db):
    settings.update({"llm": {"api_style": "ollama", "stream_replies": True, "context_length": 12288}})
    bodies = []

    def handler(req):
        body = json.loads(req.content)
        bodies.append(body)
        assert req.url.path == "/api/chat"
        if len(bodies) == 1:
            lines = [{"message": {"content": "", "tool_calls": [{"function": {"name": "calculate",
                                                                                "arguments": {"expression": "6*7"}}}]}},
                     {"done": True}]
        else:
            lines = [{"message": {"content": "Six times seven "}}, {"message": {"content": "is 42. Anything "}},
                     {"message": {"content": "else?"}}, {"done": True}]
        return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines).encode())
    sentences = []
    brain = Brain(settings, LLMClient(settings, transport=httpx.MockTransport(handler)))
    answer = brain.ask("six times seven", on_sentence=sentences.append)
    assert answer == "Six times seven is 42. Anything else?"
    assert sentences == ["Six times seven is 42.", "Anything else?"]
    assert bodies[0]["options"]["num_ctx"] == 12288
    tool_msg = bodies[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_name"] == "calculate"
    assert isinstance(bodies[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"], dict)


def test_history_saved(settings, db):
    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": "Hi!"}}]})
    Brain(settings, LLMClient(settings, transport=httpx.MockTransport(handler))).ask("hello", source="voice")
    row = db.one("SELECT * FROM history")
    assert row["user"] == "hello" and row["reply"] == "Hi!" and row["source"] == "voice"


# ---------------------------------------------------------------- smart home
def test_govee_lan_commands(settings):
    from jarvis import smarthome as sh
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        rx.bind(("127.0.0.1", 4003))
    except OSError:
        pytest.skip("port 4003 busy")
    rx.settimeout(2)
    got = []

    def listen():
        try:
            while True:
                got.append(json.loads(rx.recv(4096)))
        except Exception:
            pass
    th = threading.Thread(target=listen, daemon=True)
    th.start()
    settings.update({"smarthome": {"govee_mode": "lan", "govee_names": "Desk lamp = 127.0.0.1"}})
    out = run("smart_home", settings, action="color", target="the desk lamp", value="purple")
    assert out["devices"] == ["Desk lamp"]
    run("smart_home", settings, action="brightness", target="desk", value="30%")
    run("smart_home", settings, action="off", target="all")
    th.join(3)
    rx.close()
    cmds = [g["msg"]["cmd"] for g in got]
    assert "colorwc" in cmds and "brightness" in cmds and cmds[-1] == "turn"
    color = next(g for g in got if g["msg"]["cmd"] == "colorwc")["msg"]["data"]["color"]
    assert color == {"r": 140, "g": 0, "b": 255}
    assert got[-1]["msg"]["data"]["value"] == 0
    assert sh.parse_color("warm white") == (None, 2700)


def test_scenes_parse():
    from jarvis.smarthome import parse_scenes
    s = parse_scenes("Movie = TV strip: blue 20, Desk lamp: off\nBright = all: white 100")
    assert s["movie"] == [("TV strip", "blue 20"), ("Desk lamp", "off")]


# ---------------------------------------------------------------- security
def _chunks(audio):
    for i in range(0, len(audio) - 1279, 1280):
        yield audio[i:i + 1280]


def test_security_records_clicks_and_ignores_quiet(settings, db, tmp_path, monkeypatch):
    from jarvis import security as secmod
    monkeypatch.setattr(secmod, "data_dir", lambda: tmp_path)
    sent = []

    class FakeNotifier:
        def send(self, msg, **kw):
            sent.append(msg)
    ctx.notifier = FakeNotifier()
    sec = secmod.SecurityMonitor(settings, db)
    sec.folder = tmp_path / "security"
    rng = np.random.default_rng(1)
    quiet = (rng.normal(0, 40, 16000 * 10)).astype(np.int16)        # 10 s of room hum
    for c in _chunks(quiet):
        sec.process(c)
    assert sec._clip is None
    clicks = quiet.copy()
    for k in range(20):                                              # 20 keyboard-like clicks over 5 s
        pos = 16000 * 2 + k * 4000
        clicks[pos:pos + 200] = (rng.normal(0, 9000, 200)).astype(np.int16)
    for c in _chunks(clicks):
        sec.process(c)
    for c in _chunks(quiet):                                         # quiet again -> clip ends and is saved
        sec.process(c)
    for _ in range(50):
        if db.all("SELECT * FROM security_events"):
            break
        time.sleep(0.1)
    rows = db.all("SELECT * FROM security_events")
    assert len(rows) == 1 and rows[0]["kind"] == "sounds"
    assert (tmp_path / "security").exists()
    assert sent and "human activity" in sent[0]


def test_away_mode_schedule_and_override(settings, db):
    from jarvis.security import SecurityMonitor
    sec = SecurityMonitor(settings, db)
    settings.update({"security": {"schedule_enabled": True, "days": "every day", "start": "00:00", "end": "23:59"}})
    assert sec.away()
    sec.set_away(False, "test")       # "I'm home" during the schedule
    assert not sec.away()
    sec.set_away(True, "test")
    assert sec.away()
    friday_9 = datetime(2026, 11, 13, 9, 0).astimezone()
    settings.update({"security": {"days": "weekdays", "start": "08:00", "end": "17:30"}})
    assert sec.scheduled_now(friday_9) and not sec.scheduled_now(friday_9 + timedelta(days=1))


# ---------------------------------------------------------------- network watch
def test_netwatch_inbound_detection(settings, db, monkeypatch):
    from jarvis import netwatch as nw
    outputs = {
        ("ip", "-4", "route", "show", "default"): "default via 192.168.1.1 dev wlp5s0 proto dhcp\n",
        ("ip", "-4", "-o", "addr", "show", "dev", "wlp5s0"): "3: wlp5s0    inet 192.168.1.10/24 brd 192.168.1.255\n",
        ("ss", "-Htln"): "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\nLISTEN 0 50 *:445 *:*\nLISTEN 0 4096 127.0.0.1:11434 0.0.0.0:*\n",
        ("ss", "-Htn", "state", "established"):
            "0 0 192.168.1.10:22 192.168.1.23:51514\n"          # phone SSH in -> alert
            "0 0 192.168.1.10:51000 192.168.1.50:4003\n"        # we talk to a light -> no alert
            "0 0 [::ffff:192.168.1.10]:445 [::ffff:192.168.1.77]:50000\n"  # file sharing -> alert
            "0 0 172.17.0.1:11434 172.17.0.2:40000\n",          # docker container -> no alert
        ("ip", "neigh"): "192.168.1.23 dev wlp5s0 lladdr aa:bb:cc:dd:ee:ff REACHABLE\n",
    }
    monkeypatch.setattr(nw, "_run", lambda cmd: outputs.get(tuple(cmd), ""))
    monkeypatch.setattr(nw, "device_name", lambda ip: "Pixel-7" if ip.endswith(".23") else "")
    sent = []

    class FakeNotifier:
        def send(self, msg, **kw):
            sent.append(msg)
    ctx.notifier = FakeNotifier()
    w = nw.NetWatch(settings, db)
    events = w.check_connections()
    assert len(events) == 2
    assert "Pixel-7 (192.168.1.23)" in sent[0] and "SSH" in sent[0] and "aa:bb:cc:dd:ee:ff" in sent[0]
    assert "file sharing" in sent[1]
    assert w.check_connections() == []                                # cooldown


# ---------------------------------------------------------------- music library
def test_music_find(settings, tmp_path):
    from jarvis.media import MediaPlayer
    root = tmp_path / "Music"
    (root / "Pink Floyd" / "The Wall").mkdir(parents=True)
    (root / "Misc").mkdir()
    for p in ["Pink Floyd/The Wall/Comfortably Numb.mp3", "Pink Floyd/The Wall/Hey You.mp3",
              "Misc/Bohemian Rhapsody.flac", "Misc/notes.txt"]:
        (root / p).write_bytes(b"x")
    settings.update({"music": {"music_folder": str(root)}})
    mp = MediaPlayer(settings)
    mp.index._scan()
    assert len(mp.library()) == 3
    kind, files = mp.find("comfortably numb")
    assert kind == "song" and files[0].stem == "Comfortably Numb"
    kind, files = mp.find("the wall")
    assert kind == "folder" and len(files) == 2
    assert mp.find("bohemian rhapsody")[1][0].suffix == ".flac"
    assert mp.find("zzzz")[0] == "none"


def test_briefing(settings, db, monkeypatch):
    from jarvis import briefing
    settings.update({"general": {"user_name": "Sam"}, "alarms": {"briefing_news": False}})
    db.execute("INSERT INTO events(title, start, end, created) VALUES(?,?,?,?)",
               ("Team call", time.time() + 3600, time.time() + 7200, time.time()))
    text = briefing.build_briefing(settings)
    assert "Sam" in text and "Team call" in text


# ---------------------------------------------------------------- regression: model "pretends" instead of using tools
def test_model_must_use_timer_tool_even_after_copying_history(settings, world, db):
    """Reproduces the bug: the 2nd timer request was answered from memory ('I have started a timer')
    without a tool call, so no timer existed. Now Jarvis sends it back to use the tool."""
    replies = []

    def handler(req):
        body = json.loads(req.content)
        last = body["messages"][-1]
        if last["role"] == "tool":
            msg = {"content": "Done, timer started."}
        elif last["role"] == "system":  # our nudge -> the model now uses the tool
            msg = {"content": "", "tool_calls": [{"id": "t2", "type": "function", "function": {
                "name": "timer", "arguments": json.dumps({"action": "start", "duration": "15 seconds"})}}]}
        else:  # first try: pretends, copying an earlier answer
            msg = {"content": "I have started a timer for fifteen seconds."}
        replies.append(msg)
        return httpx.Response(200, json={"choices": [{"message": msg}]})
    spoken = []
    brain = Brain(settings, LLMClient(settings, transport=httpx.MockTransport(handler)))
    answer = brain.ask("Set a timer for 15 seconds.", on_sentence=spoken.append)
    assert answer == "Done, timer started."
    assert spoken == ["Done, timer started."]          # the made-up answer was never spoken
    assert len(db.all("SELECT * FROM timers")) == 1     # and a real timer exists
    roles = [m["role"] for m in brain.history]
    assert roles == ["user", "assistant", "tool", "assistant"]  # tool steps kept in memory, nudge is not


def test_time_question_always_checks_clock(settings, db):
    calls = []

    def handler(req):
        body = json.loads(req.content)
        last = body["messages"][-1]
        calls.append(last["role"])
        if last["role"] == "tool":
            t = json.loads(last["content"])["time"]
            msg = {"content": f"It's {t}."}
        elif last["role"] == "system":
            msg = {"content": "", "tool_calls": [{"id": "c", "type": "function",
                                                  "function": {"name": "get_time", "arguments": "{}"}}]}
        else:
            msg = {"content": "The current time is nine forty am."}  # stale answer copied from earlier
        return httpx.Response(200, json={"choices": [{"message": msg}]})
    brain = Brain(settings, LLMClient(settings, transport=httpx.MockTransport(handler)))
    answer = brain.ask("And what time is it right now?")
    assert answer == f"It's {tp.say_time(tp.now_local())}."
    assert calls == ["user", "system", "tool"]


def test_no_nudge_for_chitchat(settings, db):
    n = []

    def handler(req):
        n.append(1)
        return httpx.Response(200, json={"choices": [{"message": {"content": "I'm doing great!"}}]})
    Brain(settings, LLMClient(settings, transport=httpx.MockTransport(handler))).ask("How are you today?")
    assert len(n) == 1


def test_asking_for_briefing_cancels_automatic_one(settings, world, db):
    ctx.ringer.pending_briefing = time.time()
    run("briefing", settings)
    assert ctx.ringer.pending_briefing is None


def test_wake_phrase_cleanup():
    from jarvis.audio.wakeword import find_phrase, strip_wake_phrase
    assert find_phrase("[www.nova.com](https://www.nova.com)", "Nova") == ""
    assert find_phrase("Nova Nova", "Nova") == ""
    assert strip_wake_phrase("nova What time is it?", "Nova") == "what time is it"
    assert strip_wake_phrase("What time is it?", "Nova") == "What time is it?"


def test_news_feed_discovery(monkeypatch):
    from jarvis.tools import news
    rss = b'<?xml version="1.0"?><rss><channel><item><title>Big AI news</title><description>x</description></item></channel></rss>'
    pages = {
        "https://techcrunch.com": (200, b'<html><head><link rel="alternate" type="application/rss+xml" href="/feed/"></head></html>'),
        "https://techcrunch.com/feed/": (200, rss),
        "https://plain.example": (200, b"<html><head></head><body>hi</body></html>"),
        "https://plain.example/feed/": (404, b""), "https://plain.example/feed": (404, b""),
        "https://plain.example/rss": (200, rss),
    }

    def fake_get(url):
        code, body = pages.get(url, (404, b""))
        return httpx.Response(code, content=body, request=httpx.Request("GET", url))
    monkeypatch.setattr(news, "_get", fake_get)
    news._discovered.clear()
    news._cache.clear()
    assert news.find_feed("techcrunch.com") == "https://techcrunch.com/feed/"
    assert news.fetch_feed("techcrunch.com")[0]["title"] == "Big AI news"
    assert news.find_feed("https://plain.example") == "https://plain.example/rss"


def test_youtube_latest_picks_channel(monkeypatch):
    from jarvis.tools import browser as br
    opened = []
    monkeypatch.setattr(br, "open_url", opened.append)

    class FakeYDL:
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def extract_info(self, q, download=False):
            if q.startswith("ytsearch10:"):
                assert q == "ytsearch10:NASA"
                return {"entries": [
                    {"id": "a1", "title": "Reaction to NASA", "channel": "Some Fan", "channel_id": "UCfan"},
                    {"id": "b2", "title": "Old episode", "channel": "NASA", "channel_id": "UCnasa"}]}
            if q == "https://www.youtube.com/channel/UCnasa/videos":
                return {"entries": [{"id": "NEW123", "title": "Today's show"}, {"id": "old", "title": "Yesterday"}]}
            if q.startswith("ytsearch1:"):
                return {"entries": [{"id": "MIX9", "title": "Cyberpunk mix", "channel": "Synth"}]}
            raise AssertionError(q)
    monkeypatch.setattr(br, "_ydl", lambda: FakeYDL())
    out = br.browser(None, "youtube", "the latest NASA video")
    assert out["playing"] == "Today's show" and opened[-1] == "https://www.youtube.com/watch?v=NEW123"
    br.browser(None, "youtube", "cyberpunk music")
    assert opened[-1].endswith("MIX9")
    br.browser(None, "pandora", "cyberpunk")
    assert opened[-1] == "https://www.pandora.com/search/cyberpunk/all"
    br.browser(None, "website", "techcrunch.com")
    assert opened[-1] == "https://techcrunch.com"


def test_cancel_phrase_at_end():
    from jarvis.assistant import is_cancel
    assert is_cancel("Set a timer for, uh, cancel that.", "cancel that")
    assert not is_cancel("what is cancel culture", "cancel that")


def test_strict_whisper_wake_phrase():
    from jarvis.audio.wakeword import find_phrase, threshold_to_cutoff
    c = threshold_to_cutoff(0.6)
    assert find_phrase("Nova", "Hey Nova", c) is None          # 'hey' is required now
    assert find_phrase("Hey Nova, what time is it?", "Hey Nova", c) == "what time is it"
    assert find_phrase("Hey Nova what", "Hey Nova", c) == "what"  # never swallows the next word
    assert find_phrase("a novelist", "Hey Nova", c) is None


def test_network_share_paths(tmp_path, monkeypatch):
    from jarvis import media
    (tmp_path / "smb-share:server=nas,share=music" / "Rock").mkdir(parents=True)
    real = media._find_gvfs_share
    monkeypatch.setattr(media, "_find_gvfs_share", lambda h, s, base=None: real(h, s, tmp_path))
    assert media.resolve_path("smb://NAS/Music/Rock") == tmp_path / "smb-share:server=nas,share=music" / "Rock"
    assert media.resolve_path("\\\\nas\\music\\Rock") == tmp_path / "smb-share:server=nas,share=music" / "Rock"


def test_govee_discover_status_blink(monkeypatch):
    """A fake light that follows Govee's LAN API document (scan on 4001, replies to 4002, commands on 4003)."""
    import struct

    from jarvis import smarthome as sh
    state = {"onOff": 0, "brightness": 80, "color": {"r": 255, "g": 0, "b": 0}, "colorTemInKelvin": 0}
    seen = []
    try:
        scan = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        scan.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        scan.bind(("", 4001))
        scan.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                        struct.pack("4sl", socket.inet_aton("239.255.255.250"), socket.INADDR_ANY))
        ctl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ctl.bind(("127.0.0.1", 4003))
    except OSError:
        pytest.skip("Govee ports busy or multicast unavailable")
    scan.settimeout(5)
    ctl.settimeout(8)

    def reply(obj):
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(json.dumps(obj).encode(), ("127.0.0.1", 4002))

    def light():
        try:
            data, addr = scan.recvfrom(4096)
            reply({"msg": {"cmd": "scan", "data": {"ip": "127.0.0.1", "device": "AA:BB", "sku": "H6144"}}})
            while True:
                m = json.loads(ctl.recvfrom(4096)[0])["msg"]
                seen.append(m["cmd"])
                if m["cmd"] == "turn":
                    state["onOff"] = m["data"]["value"]
                if m["cmd"] == "devStatus":
                    reply({"msg": {"cmd": "devStatus", "data": state}})
        except Exception:
            pass
    threading.Thread(target=light, daemon=True).start()
    monkeypatch.setattr(sh, "_lan_ip", lambda: None)
    found = sh.govee_discover(timeout=2)
    assert found == [{"ip": "127.0.0.1", "device": "AA:BB", "model": "H6144"}]
    assert sh.govee_status("127.0.0.1")["brightness"] == 80
    sh.govee_blink("127.0.0.1")
    time.sleep(0.5)  # let the last message arrive
    assert seen.count("turn") == 5 and state["onOff"] == 0   # blinked, then left off like it was
    scan.close()
    ctl.close()


def test_youtube_press_play_only_when_not_playing(monkeypatch):
    from jarvis.tools import browser as br
    cmds = []
    state = {"playing": False}

    def fake_run(cmd, **kw):
        cmds.append(cmd[:2])
        out = "12345\n" if cmd[:2] == ["xdotool", "search"] else ""
        if cmd[:2] == ["xdotool", "key"]:
            state["playing"] = True
        return subprocess.CompletedProcess(cmd, 0, out, "")
    import subprocess
    monkeypatch.setattr(br.subprocess, "run", fake_run)
    monkeypatch.setattr(br.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(br, "_browser_playing", lambda: state["playing"])
    monkeypatch.setattr(br.time, "sleep", lambda s: None)
    assert br.press_play_when_loaded(wait=0.01) == "pressed play"
    assert ["xdotool", "key"] in cmds
    cmds.clear()
    assert br.press_play_when_loaded(wait=0.01) == "started by itself"   # already playing: never pressed
    assert ["xdotool", "key"] not in cmds
