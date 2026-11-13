import pytest

from jarvis.config import Settings
from jarvis.context import ctx
from jarvis.storage import Database


@pytest.fixture(autouse=True)
def clean_context():
    ctx.reset()
    yield
    ctx.reset()


@pytest.fixture
def settings(tmp_path):
    s = Settings(tmp_path / "settings.json")
    s.update({"llm": {"api_style": "openai", "stream_replies": False}, "learning": {"tutor_enabled": False}})
    return s


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "jarvis.db")
    ctx.db = d
    return d
