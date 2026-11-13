import httpx
from fastapi.testclient import TestClient

from jarvis.brain import Brain
from jarvis.config import Settings
from jarvis.llm import LLMClient
from jarvis.web.server import create_app


class FakeSpeaker:
    def __init__(self):
        self.said = []

    def say(self, text):
        self.said.append(text)

    def stop(self):
        pass


def make_client(tmp_path):
    settings = Settings(tmp_path / "s.json")
    settings.update({"llm": {"api_style": "openai"}})

    def handler(request: httpx.Request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen2.5:7b"}, {"id": "llama3.1:8b"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Hello there."}}]})
    brain = Brain(settings, LLMClient(settings, transport=httpx.MockTransport(handler)))
    return TestClient(create_app(settings, brain, FakeSpeaker())), settings


def test_page_and_settings(tmp_path):
    client, settings = make_client(tmp_path)
    assert "<title>Jarvis</title>" in client.get("/").text
    assert any(s["id"] == "listening" for s in client.get("/api/schema").json())
    r = client.post("/api/settings", json={"listening": {"end_of_speech_seconds": 4}})
    assert r.json()["changed"] == ["listening.end_of_speech_seconds"]
    assert settings.get("listening", "end_of_speech_seconds") == 4.0
    assert client.post("/api/settings", json={"tts": {"engine": "bogus"}}).status_code == 400


def test_chat_and_models(tmp_path):
    client, _ = make_client(tmp_path)
    assert client.post("/api/chat", json={"text": "hi"}).json()["reply"] == "Hello there."
    assert client.get("/api/options/models").json()["options"] == ["llama3.1:8b", "qwen2.5:7b"]
    assert client.get("/api/status").json()["state"] == "off"


def test_dashboard_security_vocab_history(tmp_path):
    from jarvis.context import ctx
    from jarvis.security import SecurityMonitor
    from jarvis.storage import Database
    from jarvis.tutor import Tutor
    client, settings = make_client(tmp_path)
    ctx.settings = settings
    ctx.db = Database(tmp_path / "j.db")
    ctx.security = SecurityMonitor(settings, ctx.db)
    ctx.tutor = Tutor(settings, ctx.db)
    from jarvis import tools
    tools.run("alarm", {"action": "set", "time": "7am", "days": "weekdays"}, settings)
    tools.run("lists", {"action": "add", "list_name": "shopping", "items": "milk"}, settings)
    d = client.get("/api/dashboard").json()
    assert d["alarms"][0]["repeats"] == "on weekdays" and d["lists"]["shopping"][0]["item"] == "milk"
    assert client.post("/api/delete", json={"kind": "list_item", "id": d["lists"]["shopping"][0]["id"]}).json()["ok"]
    assert client.get("/api/dashboard").json()["lists"] == {}
    assert client.post("/api/away", json={"on": True}).json()["away"] is True
    assert client.get("/api/security").json()["away"] is True
    client.post("/api/away", json={"on": False})
    assert client.get("/api/vocab").json()["word_of_the_day"]["word"]
    client.post("/api/chat", json={"text": "hi"})
    items = client.get("/api/history").json()["items"]
    assert items[0]["user"] == "hi" and items[0]["source"] == "typed"
    ctx.reset()


def test_phone_voice_endpoint(tmp_path):
    import base64
    import io

    import numpy as np
    import soundfile as sf

    from jarvis.context import ctx

    class FakeSTT:
        def transcribe(self, f):
            assert f.read(4) == b"RIFF"
            return "hello from my phone"

    class Engine:
        def synth(self, text):
            return np.zeros(2400, np.float32), 24000

    class Voice:
        stt = FakeSTT()

    client, settings = make_client(tmp_path)
    FakeSpeaker._get_engine = lambda self: Engine()
    ctx.voice = Voice()
    buf = io.BytesIO()
    sf.write(buf, np.zeros(16000, np.float32), 16000, format="WAV")
    d = client.post("/api/voice", content=buf.getvalue(), headers={"Content-Type": "audio/wav"}).json()
    assert d["heard"] == "hello from my phone" and d["reply"] == "Hello there."
    assert base64.b64decode(d["audio"])[:4] == b"RIFF"
    del FakeSpeaker._get_engine
    ctx.reset()


def test_stop_everything_button(tmp_path):
    from jarvis.context import ctx
    client, settings = make_client(tmp_path)
    calls = []

    class Media:
        def stop_everything(self):
            calls.append("media")

    class Ringer:
        def is_ringing(self):
            return True

        def stop(self, reason="user"):
            calls.append("ringer")
    ctx.media, ctx.ringer = Media(), Ringer()
    assert client.post("/api/cancel").json()["ok"]
    assert calls == ["ringer", "media"]
    ctx.reset()


def test_phone_voice_real_decoding(tmp_path):
    """Real phone formats go through our own decoder (faster-whisper's breaks with new PyAV)."""
    import io

    import av
    import numpy as np

    from jarvis.audio.stt import SpeechToText
    from jarvis.context import ctx

    def make(fmt, codec):
        buf = io.BytesIO()
        out = av.open(buf, mode="w", format=fmt)
        st = out.add_stream(codec, rate=48000)
        st.layout = "mono"
        x = (np.sin(2 * np.pi * 440 * np.arange(96000) / 48000) * 0.3).astype(np.float32)
        fr = av.AudioFrame.from_ndarray(x.reshape(1, -1), format="flt", layout="mono")
        fr.sample_rate = 48000
        for p in list(st.encode(fr)) + list(st.encode(None)):
            out.mux(p)
        out.close()
        return buf.getvalue()

    got = []

    class FakeModel:
        def transcribe(self, audio, **kw):
            got.append(len(audio))
            return iter([]), None

    stt = SpeechToText.__new__(SpeechToText)
    stt.model, stt.language, stt.hint = FakeModel(), "en", ""

    class Voice:
        pass
    Voice.stt = stt
    client, settings = make_client(tmp_path)
    ctx.voice = Voice()
    for fmt, codec in (("webm", "libopus"), ("mp4", "aac")):
        r = client.post("/api/voice", content=make(fmt, codec), headers={"Content-Type": "audio/" + fmt})
        assert r.status_code == 200, r.text
    assert len(got) == 2 and all(31000 < n < 33500 for n in got)   # ~2 s at 16 kHz each
    ctx.reset()
