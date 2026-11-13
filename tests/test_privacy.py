"""Privacy guard: fails if personal information sneaks into the project.

The blocked words are stored only as fingerprints (SHA-256), so this file can't leak them either.
To block another word, add the output of:
    python3 -c "import hashlib; print(hashlib.sha256('yourword'.lower().encode()).hexdigest())"
"""
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BLOCKED = {
    "a20c0df55915fc3d2f8ce2eb69f968989b50832937a96e8aaeebe181b53f05e3",
    "8519c6249323e9fe4582811f183569d16e37e484f4f2663163cadea71451ea45",
    "13a71ccb9e93e74e7494b32bb42d22032f7df16edff78ccdc076ec61ed136898",
    "4d304dfed178d8ac90f63964a01da0c7f1dc729ad932586ea19709ce10adae04",
    "73423c7707c42ee62b43044e3e417d14f09ddc92fdab633562a11ed50ff7ea1e",
}
ALLOWED_EMAILS = {"noreply@anthropic.com"}
TEXT_SUFFIXES = {".py", ".sh", ".md", ".txt", ".html", ".json", ".toml", ".cfg", ".yml", ".yaml", ""}


def project_files():
    for p in ROOT.rglob("*"):
        if p.is_file() and not any(part.startswith(".") or part == "__pycache__" for part in p.relative_to(ROOT).parts):
            yield p


def test_no_personal_words_or_emails():
    problems = []
    for p in project_files():
        data = p.read_bytes()
        text = data.decode("utf-8", "ignore") if p.suffix in TEXT_SUFFIXES else data.decode("latin-1")
        for word in set(re.findall(r"[a-z0-9]+", text.lower())):
            if hashlib.sha256(word.encode()).hexdigest() in BLOCKED:
                problems.append(f"{p.relative_to(ROOT)}: contains a blocked personal word")
        if p.suffix in TEXT_SUFFIXES:
            for email in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text):
                if email.lower() not in ALLOWED_EMAILS and not email.lower().endswith(("@example.com", "@example.org")):
                    problems.append(f"{p.relative_to(ROOT)}: contains an email address")
    assert not problems, "\n".join(sorted(set(problems)))


def test_no_private_network_details():
    """Only the documentation-style example addresses (192.168.1.x) are allowed."""
    for p in project_files():
        if p.suffix in TEXT_SUFFIXES:
            text = p.read_text("utf-8", "ignore")
            for ip in re.findall(r"\b(?:10|172|192)\.\d+\.\d+\.\d+\b", text):
                assert ip.startswith(("192.168.1.", "172.17.0.")), f"{p.relative_to(ROOT)}: {ip}"
