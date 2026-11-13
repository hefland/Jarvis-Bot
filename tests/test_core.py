"""Automatic checks. Run with:  .venv/bin/python -m pytest"""
import json

import httpx
import numpy as np
import pytest

from jarvis import tools
from jarvis.audio.recorder import CHUNK_SECONDS, Recording
from jarvis.audio.wakeword import find_phrase
from jarvis.assistant import is_cancel
from jarvis.brain import Brain, to_speech
from jarvis.config import SECRET_MASK, Settings
from jarvis.llm import LLMClient, normalize_message
from jarvis.tools import geo
from jarvis.tools.math_engine import convert, evaluate


# ---------------- math: must never be wrong ----------------
@pytest.mark.parametrize("expr, exact", [
    ("2^10", "1024"), ("15% * 80", "12"), ("5!", "120"), ("sin(30 deg)", "1/2"), ("0.1+0.2", "3/10"),
    ("1234 * 5678", "7006652"), ("integrate(x^2, (x, 0, 3))", "9"), ("limit(sin(x)/x, x, 0)", "1"),
    ("factor(x^2 - 4)", "(x - 2)*(x + 2)"), ("diff(x^3, x)", "3*x^2"), ("mean(3, 5, 10)", "6"),
    ("det([[1,2],[3,4]])", "-2"), ("7 mod 3", "1"), ("ln(e)", "1"), ("summation(k, (k, 1, 100))", "5050"),
    ("12345678987654321*98765432123456789", "1219326320073159566072245112635269"),
])
def test_math_exact(expr, exact):
    assert evaluate(expr)["exact"] == exact


def test_math_solve():
    assert evaluate("solve(x^2 - 5x + 6 = 0, x)")["exact"] == ["2", "3"]
    assert evaluate("solve([x + y = 10, x - y = 2], [x, y])")["exact"] == {"x": "6", "y": "4"}
    assert evaluate("2x + 3 = 11")["exact"] == ["4"]


def test_math_division_by_zero():
    assert "undefined" in evaluate("3/0")["exact"]


@pytest.mark.parametrize("bad", ['__import__("os")', "x.__class__", "().__class__", "exec(1)", "eval(1)",
                                 "globals()", "getattr(x, 1)"])
def test_math_rejects_code(bad):
    assert "error" in evaluate(bad)


def test_units():
    assert convert(5, "miles", "km")["value"] == pytest.approx(8.04672)
    assert convert(212, "fahrenheit", "celsius")["value"] == pytest.approx(100)
    assert "error" in convert(1, "meter", "second")


def test_calculator_tool_runs_in_sandbox(settings):
    out = json.loads(tools.run("calculate", {"expression": "17*23"}, settings))
    assert out["exact"] == "391"


# ---------------- settings ----------------
def test_settings_roundtrip(settings, tmp_path):
    changed = settings.update({"listening": {"end_of_speech_seconds": "3.5"}, "search": {"tavily_api_key": "tvly-1"}})
    assert ("listening", "end_of_speech_seconds") in changed
    again = Settings(tmp_path / "settings.json")
    assert again.get("listening", "end_of_speech_seconds") == 3.5
    masked = again.as_dict(mask_secrets=True)
    assert masked["search"]["tavily_api_key"] == SECRET_MASK
    again.update({"search": {"tavily_api_key": SECRET_MASK}})  # saving the masked value keeps the real key
    assert again.get("search", "tavily_api_key") == "tvly-1"


def test_settings_clamps_and_validates(settings):
    settings.update({"listening": {"end_of_speech_seconds": 99}})
    assert settings.get("listening", "end_of_speech_seconds") == 10
    with pytest.raises(ValueError):
        settings.update({"tts": {"engine": "nope"}})
    with pytest.raises(KeyError):
        settings.update({"tts": {"made_up": 1}})


# ---------------- brain + tool calling with a fake AI server ----------------
def fake_llm(responses):
    calls = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(200, json={"choices": [{"message": responses[len(calls) - 1]}]})
    return httpx.MockTransport(handler), calls


def test_brain_uses_calculator(settings):
    transport, calls = fake_llm([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "calculate", "arguments": '{"expression": "17*23"}'}}]},
        {"role": "assistant", "content": "**17 times 23 is 391.**"},
    ])
    brain = Brain(settings, LLMClient(settings, transport=transport))
    assert brain.ask("what's 17 times 23") == "17 times 23 is 391."
    tool_msg = calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and '"391"' in tool_msg["content"]
    assert "calculate" in [t["function"]["name"] for t in calls[0]["tools"]]
    assert "web_search" not in [t["function"]["name"] for t in calls[0]["tools"]]  # search is off by default


def test_brain_rescues_text_tool_call(settings):
    transport, calls = fake_llm([
        {"role": "assistant", "content": '{"name": "calculate", "arguments": {"expression": "2^8"}}'},
        {"role": "assistant", "content": "256."},
    ])
    brain = Brain(settings, LLMClient(settings, transport=transport))
    assert brain.ask("2 to the 8th") == "256."


def test_brain_custom_prompt_and_memory(settings):
    settings.update({"llm": {"custom_prompt": "Be sarcastic."}, "general": {"user_name": "Sam"}})
    transport, calls = fake_llm([{"role": "assistant", "content": "Hi."}, {"role": "assistant", "content": "Yes."}])
    brain = Brain(settings, LLMClient(settings, transport=transport))
    brain.ask("hello")
    brain.ask("again")
    system = calls[1]["messages"][0]["content"]
    assert "Be sarcastic." in system and "Sam" in system
    assert [m["role"] for m in calls[1]["messages"]] == ["system", "user", "assistant", "user"]


def test_brain_reports_unreachable_server(settings):
    def handler(request):
        raise httpx.ConnectError("refused")
    brain = Brain(settings, LLMClient(settings, transport=httpx.MockTransport(handler)))
    assert "Ollama" in brain.ask("hi")


def test_strip_thinking():
    assert normalize_message({"content": "<think>hmm</think>Hello"})["content"] == "Hello"


def test_to_speech():
    assert to_speech("# Title\n- **bold** item\n`code`") == "Title\nbold item\ncode"


# ---------------- weather with a fake internet ----------------
def test_weather(settings, monkeypatch):
    def handler(request: httpx.Request):
        if "geocoding" in request.url.host:
            return httpx.Response(200, json={"results": [
                {"name": "Springfield", "latitude": 39.8, "longitude": -89.6, "admin1": "Illinois",
                 "country": "United States", "country_code": "US", "timezone": "America/Chicago"}]})
        assert request.url.params["temperature_unit"] == "fahrenheit"
        return httpx.Response(200, json={
            "current": {"temperature_2m": 71.4, "apparent_temperature": 70, "relative_humidity_2m": 40,
                        "weather_code": 2, "wind_speed_10m": 8},
            "daily": {"time": ["2026-11-13"], "weather_code": [61], "temperature_2m_max": [75.2],
                      "temperature_2m_min": [50.1], "precipitation_probability_max": [60],
                      "sunrise": ["2026-11-13T06:40"], "sunset": ["2026-11-13T16:45"]}})
    monkeypatch.setattr(geo, "_transport", httpx.MockTransport(handler))
    geo.geocode.cache_clear()
    out = json.loads(tools.run("get_weather", {"location": "Springfield, IL"}, settings))
    assert out["now"]["temperature"] == "71°F" and out["now"]["conditions"] == "partly cloudy"
    assert out["forecast"][0]["chance_of_rain"] == "60%"


def test_weather_without_location(settings):
    assert "No home location" in tools.run("get_weather", {}, settings)


def test_time_tools(settings):
    out = json.loads(tools.run("date_difference", {"start": "2026-11-13", "end": "2026-12-25"}, settings))
    assert out["days"] == 42
    assert "time" in json.loads(tools.run("get_time", {}, settings))


# ---------------- listening ----------------
def run_recording(pattern, end_silence=2.5, wait=6, max_s=45):
    """pattern: list of (is_speech, seconds)"""
    rec = Recording(lambda c: c[0] == 1, end_silence, wait, max_s)
    chunk_speech, chunk_quiet = np.ones(1280, np.int16), np.zeros(1280, np.int16)
    t = 0.0
    for speech, seconds in pattern:
        for _ in range(int(round(seconds / CHUNK_SECONDS))):
            t += CHUNK_SECONDS
            if rec.add(chunk_speech if speech else chunk_quiet):
                return rec, t
    return rec, None


def test_slow_talker_pauses_are_not_cut_off():
    # 2-second pauses mid-sentence must not end the recording when the setting is 2.5 s
    rec, t = run_recording([(1, 1.5), (0, 2.0), (1, 1.0), (0, 2.0), (1, 1.0), (0, 5)], end_silence=2.5)
    assert rec.finished_reason == "silence"
    assert t == pytest.approx(1.5 + 2 + 1 + 2 + 1 + 2.5, abs=0.1)


def test_no_speech_times_out():
    rec, t = run_recording([(0, 10)], wait=4)
    assert rec.finished_reason == "no_speech" and t == pytest.approx(4, abs=0.1)


def test_wake_phrase_matching():
    assert find_phrase("Jarvis, what time is it?", "Jarvis") == "what time is it"
    assert find_phrase("Jervis what's the weather", "Jarvis") == "what's the weather"
    assert find_phrase("I was talking to my friend", "Jarvis") is None
    assert find_phrase("Hey computer, lights off", "hey computer") == "lights off"


def test_cancel():
    assert is_cancel("Never mind.") and is_cancel("Cancel that") and not is_cancel("cancel my alarm")


def test_streamed_wav_with_placeholder_sizes():
    import struct

    from jarvis.audio.tts import read_loose_wav
    pcm = (np.sin(np.arange(2400) / 10) * 10000).astype("<i2").tobytes()
    fmt = struct.pack("<HHIIHH", 1, 1, 24000, 48000, 2, 16)
    wav = b"RIFF" + b"\xff\xff\xff\xff" + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + \
        b"data" + b"\xff\xff\xff\xff" + pcm
    audio, rate = read_loose_wav(wav)
    assert rate == 24000 and len(audio) == 2400


def test_http_voice_engine(monkeypatch):
    import io

    import soundfile as sf

    from jarvis.audio.tts import HttpEngine
    buf = io.BytesIO()
    sf.write(buf, np.zeros(2400, np.float32), 24000, format="WAV")
    sent = {}

    def fake_post(url, headers, timeout, json):
        sent.update(json, url=url)
        return httpx.Response(200, content=buf.getvalue())
    monkeypatch.setattr(httpx, "post", fake_post)
    audio, rate = HttpEngine("http://localhost:8880/v1/", "kokoro", "af_heart", "", 1.1).synth("Hi")
    assert rate == 24000 and len(audio) == 2400
    assert sent["url"] == "http://localhost:8880/v1/audio/speech" and sent["stream"] is False
