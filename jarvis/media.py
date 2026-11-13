"""Music + sleep sound player built on mpv (sudo apt install mpv), controlled over its IPC socket.

Also controls other players (Spotify, browsers, VLC...) through playerctl, and the system volume.
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import random
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from .config import data_dir

log = logging.getLogger(__name__)

AUDIO_EXT = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".wma", ".alac", ".aiff", ".webm"}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()


def resolve_path(text: str) -> Path:
    """Turn what the user typed into a real folder/file path.

    Handles normal paths, ~, file:// links, and network shares: smb://server/share/folder or \\\\server\\share.
    Network shares are opened through Linux Mint's own network-drive system (gvfs, the same thing the Files app
    uses) and live under /run/user/<id>/gvfs/. Saved passwords from the Files app are reused."""
    from urllib.parse import unquote
    t = (text or "").strip().strip('"').strip("'")
    if t.lower().startswith("file://"):
        t = unquote(t[7:])
    if t.startswith("\\\\"):
        t = "smb://" + t.lstrip("\\").replace("\\", "/")
    m = re.match(r"^smb://([^/]+)/([^/]+)/?(.*)$", t, re.I)
    if not m:
        return Path(t).expanduser()
    host, share, rest = m.group(1).split("@")[-1], unquote(m.group(2)), unquote(m.group(3)).strip("/")
    root = _find_gvfs_share(host, share)
    if root is None:
        try:
            subprocess.run(["gio", "mount", f"smb://{host}/{share}"], capture_output=True, timeout=30,
                           stdin=subprocess.DEVNULL, env=_session_env())
        except Exception as exc:
            log.warning("Couldn't open network share smb://%s/%s: %s", host, share, exc)
        root = _find_gvfs_share(host, share)
    if root is None:
        raise FileNotFoundError(f"Couldn't open the network share smb://{host}/{share}. Open it once in the Files app "
                                f"(and tick 'Remember password forever'), then try again.")
    return root / rest if rest else root


def _session_env() -> dict:
    env = dict(os.environ)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return env


def _find_gvfs_share(host: str, share: str, base: Path | None = None) -> Path | None:
    gvfs = base or Path(f"/run/user/{os.getuid()}/gvfs")
    if not gvfs.is_dir():
        return None
    want = f"smb-share:server={host},share={share}".lower()
    for d in gvfs.iterdir():
        name = d.name.lower()
        if name == want or name.startswith(want + ","):
            return d
    for d in gvfs.iterdir():  # some systems put the user or domain first
        name = d.name.lower()
        if name.startswith("smb-share:") and f"server={host.lower()}" in name and f"share={share.lower()}" in name:
            return d
    return None


def external_players() -> list[str]:
    """Names of other media players (Firefox/YouTube, Spotify, VLC...) that are playing right now."""
    if not shutil.which("playerctl"):
        return []
    try:
        names = subprocess.run(["playerctl", "-l"], capture_output=True, text=True, timeout=3,
                               env=_session_env()).stdout.split()
        playing = []
        for n in names:
            if n.startswith("mpv"):  # our own player is handled separately
                continue
            st = subprocess.run(["playerctl", "-p", n, "status"], capture_output=True, text=True, timeout=3,
                                env=_session_env()).stdout.strip()
            if st == "Playing":
                playing.append(n)
        return playing
    except Exception:
        return []


def _player_cmd(names: list[str], cmd: str) -> None:
    for n in names:
        try:
            subprocess.run(["playerctl", "-p", n, cmd], capture_output=True, timeout=3, env=_session_env())
        except Exception:
            pass


class MusicIndex:
    """A list of every song in the music folder, built in the background and saved to disk.

    Scanning a big network folder can take minutes, so it never happens while you wait: playback starts
    from whatever has been found so far, and the saved list makes the next start instant."""

    RESCAN_HOURS = 6

    def __init__(self, settings):
        self.settings = settings
        self.path = data_dir() / "music_index.json"
        self.root: Path | None = None
        self.files: list[Path] = []
        self.scanning = False
        self.scanned_at = 0.0
        self.error = ""
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._load()

    def _setting_root(self) -> str:
        return self.settings.get("music", "music_folder").strip()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
            if data.get("folder") == self._setting_root():
                self.root = Path(data["root"])
                self.files = [self.root / f for f in data["files"]]
                self.scanned_at = data.get("scanned_at", 0)
        except Exception:
            pass

    def _save(self) -> None:
        try:
            rel = [str(f.relative_to(self.root)) for f in self.files]
            self.path.write_text(json.dumps({"folder": self._setting_root(), "root": str(self.root),
                                             "scanned_at": self.scanned_at, "files": rel}))
        except Exception as exc:
            log.warning("Couldn't save the music index: %s", exc)

    def ensure(self) -> None:
        """Start a background scan if there's no up-to-date list (cheap to call often)."""
        text = self._setting_root()
        if not text:
            return
        stale = time.time() - self.scanned_at > self.RESCAN_HOURS * 3600
        if self.root is None or not self.files or stale:
            self.rescan()

    def rescan(self) -> None:
        with self._lock:
            if self.scanning:
                return
            self.scanning = True
        self._thread = threading.Thread(target=self._scan, name="music-scan", daemon=True)
        self._thread.start()

    def _scan(self) -> None:
        text = self._setting_root()
        try:
            root = resolve_path(text)
            if not root.is_dir():
                raise FileNotFoundError(f"The music folder '{text}' doesn't exist.")
            try:
                sleep_file = resolve_path(self.settings.get("music", "sleep_sound_file"))
            except Exception:
                sleep_file = None
            log.info("Indexing music in %s ...", root)
            start = time.time()
            found: list[Path] = []
            fresh = self.root != root or not self.files
            if fresh:
                self.root, self.files = root, found  # let playback use songs as they're found
            # os.walk only reads folder listings (no per-file checks), which is much faster on network drives
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in filenames:
                    if os.path.splitext(name)[1].lower() in AUDIO_EXT:
                        p = Path(dirpath) / name
                        if p != sleep_file:
                            found.append(p)
            found.sort()
            self.root, self.files, self.scanned_at, self.error = root, found, time.time(), ""
            self._save()
            log.info("Music index ready: %d songs (%.0f s).", len(found), time.time() - start)
        except Exception as exc:
            self.error = str(exc)
            log.error("Music indexing failed: %s", exc)
        finally:
            self.scanning = False

    def wait_for_some(self, minimum: int = 1, timeout: float = 15.0) -> list[Path]:
        """Return songs now; if none are known yet, wait briefly for the scan to find a few."""
        self.ensure()
        end = time.time() + timeout
        while len(self.files) < minimum and self.scanning and time.time() < end:
            time.sleep(0.2)
        return list(self.files)


class MediaPlayer:
    def __init__(self, settings):
        self.settings = settings
        rt = os.environ.get("XDG_RUNTIME_DIR")
        self.sock_path = str(Path(rt) / "jarvis-mpv.sock") if rt else str(data_dir() / "mpv.sock")
        self.proc: subprocess.Popen | None = None
        self.mode = ""                # "music" or "sleep"
        self._lock = threading.RLock()
        self._duck_depth = 0
        self._volume_before_duck: float | None = None
        self._paused_self = False
        self._paused_external: list[str] = []
        self._busy = False
        self._busy_checked = 0.0
        self._sleep_timer: threading.Timer | None = None
        self.index = MusicIndex(settings)
        self._names_cache: list[tuple[str, str]] = []
        self._folders: list[tuple[str, list[int]]] = []
        self._names_key = None
        settings.on_change(lambda changed: self.index.rescan()
                           if ("music", "music_folder") in changed else None)

    # ---------------------------------------------------------------- mpv process
    def available(self) -> bool:
        return shutil.which("mpv") is not None

    def _ensure(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return
        if not self.available():
            raise RuntimeError("The music player (mpv) isn't installed. Run: sudo apt install mpv")
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass
        self.proc = subprocess.Popen(
            ["mpv", "--idle=yes", "--no-video", "--no-terminal", "--force-window=no", "--audio-display=no",
             f"--input-ipc-server={self.sock_path}", "--volume-max=130"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            if os.path.exists(self.sock_path):
                return
            time.sleep(0.05)
        raise RuntimeError("The music player didn't start.")

    def _cmd(self, *args, start: bool = True):
        with self._lock:
            if start:
                self._ensure()
            elif self.proc is None or self.proc.poll() is not None:
                return None
            req = random.randint(1, 1_000_000)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(2)
                s.connect(self.sock_path)
                s.sendall((json.dumps({"command": list(args), "request_id": req}) + "\n").encode())
                buf = b""
                end = time.time() + 2
                while time.time() < end:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    for line in buf.split(b"\n"):
                        try:
                            msg = json.loads(line)
                        except Exception:
                            continue
                        if msg.get("request_id") == req:
                            return msg.get("data")
            return None

    def _get(self, prop: str):
        try:
            return self._cmd("get_property", prop, start=False)
        except Exception:
            return None

    def is_playing(self) -> bool:
        if self.proc is None or self.proc.poll() is not None:
            return False
        return self._get("idle-active") is False and self._get("pause") is False

    def is_active(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and self._get("idle-active") is False

    # ---------------------------------------------------------------- library
    def music_root(self) -> Path | None:
        return self.index.root

    def library(self) -> list[Path]:
        """Songs known so far (never blocks for long; the folder is indexed in the background)."""
        if self.index.error and not self.index.files:
            raise FileNotFoundError(self.index.error)
        return self.index.wait_for_some()

    def _names(self, files: list[Path], root: Path) -> list[tuple[str, str]]:
        """Cleaned-up song names and every artist/album folder, worked out once per index (fast searching)."""
        key = (len(files), str(root), self.index.scanned_at)
        if self._names_key != key:
            self._names_cache, folders = [], {}
            for i, f in enumerate(files):
                stem = _norm(f.stem)
                try:
                    parts = f.relative_to(root).parts[:-1]
                except ValueError:
                    parts = ()
                self._names_cache.append((stem, _norm(" ".join(parts)) + " " + stem))
                for depth in range(1, len(parts) + 1):  # Artist, Artist/Album, ...
                    folders.setdefault(parts[:depth], []).append(i)
            self._folders = [(_norm(k[-1]), idx) for k, idx in folders.items()]
            self._names_key = key
        return self._names_cache

    def find(self, query: str) -> tuple[str, list[Path]]:
        """Return ('song'|'folder'|'none', files). Matches song titles, artists and album folders."""
        files = self.library()
        q = _norm(query)
        root = self.music_root() if files else None
        if not files or not q or root is None:
            return "none", []
        names = self._names(files, root)
        best_file, best_fs = None, 0.0
        for f, (stem, rel) in zip(files, names):
            s = 1.0 if q == stem else 0.92 if q in stem else 0.85 if q in rel else 0.0
            if s < 0.9:  # fuzzy compare only when it could beat the best so far (fast on big libraries)
                sm = difflib.SequenceMatcher(None, q, stem)
                if sm.real_quick_ratio() > max(s, best_fs) and sm.quick_ratio() > max(s, best_fs):
                    s = max(s, min(0.95, sm.ratio()))
            if s > best_fs:
                best_file, best_fs = f, s
        best_folder, best_ds = None, 0.0
        for name, idx in self._folders:
            d = 1.0 if q == name else 0.93 if q in name else difflib.SequenceMatcher(None, q, name).ratio()
            if d > best_ds or (d == best_ds and best_folder is not None and len(idx) > len(best_folder)):
                best_folder, best_ds = idx, d
        if best_folder is not None and best_ds >= 0.6 and best_ds >= best_fs:
            return "folder", [files[i] for i in best_folder]
        if best_file is not None and best_fs >= 0.55:
            return "song", [best_file]
        return "none", []

    # ---------------------------------------------------------------- playing
    def _load(self, paths: list[Path], loop: bool, volume: float) -> None:
        self._cancel_sleep_timer()
        playlist = data_dir() / "playlist.m3u"
        playlist.write_text("\n".join(str(p) for p in paths), encoding="utf-8")
        self._cmd("set_property", "loop-playlist", "inf" if loop else "no")
        self._cmd("set_property", "loop-file", "no")
        self._cmd("loadlist", str(playlist), "replace")
        self._cmd("set_property", "volume", volume)
        self._cmd("set_property", "pause", False)
        self.forget_paused()

    def play_music(self, query: str = "") -> dict:
        try:
            files = self.library()
        except FileNotFoundError as exc:
            return {"error": str(exc)}
        if not files:
            return {"error": "Your music folder isn't set, can't be opened, or has no songs (Settings > Music & sleep sounds)."}
        vol = float(self.settings.get("music", "music_volume"))
        if query:
            kind, found = self.find(query)
            if kind == "none":
                err = {"error": f"I couldn't find '{query}' in your music.", "songs_in_library": len(files)}
                if self.index.scanning:
                    err["note"] = "I'm still indexing your music folder; try again in a few minutes."
                return err
            if kind == "song":
                rest = [f for f in files if f != found[0]]
                random.shuffle(rest)
                self._load(found + rest, True, vol)
            else:
                self._load(found, True, vol)
            self.mode = "music"
            out = {"ok": True, "playing": found[0].stem if kind == "song" else found[0].parent.name,
                   "type": kind, "songs_queued": len(found) if kind == "folder" else len(files)}
            if self.index.scanning:
                out["note"] = f"I'm still indexing your music ({len(files)} songs found so far)."
            return out
        shuffled = files[:]
        random.shuffle(shuffled)
        self._load(shuffled, True, vol)
        self.mode = "music"
        out = {"ok": True, "playing": "shuffle", "songs": len(files), "first_song": shuffled[0].stem}
        if self.index.scanning:
            out["note"] = "Your music is still being indexed, so I'm shuffling the songs found so far."
        return out

    def play_sleep(self, minutes: float | None = None) -> dict:
        text = self.settings.get("music", "sleep_sound_file").strip()
        try:
            path = resolve_path(text) if text else None
        except FileNotFoundError as exc:
            return {"error": str(exc)}
        if path is None or not path.is_file():
            return {"error": "No sleep sound file is set, or the file wasn't found (Settings > Music & sleep sounds)."}
        self._load([path], False, float(self.settings.get("music", "sleep_volume")))
        self._cmd("set_property", "loop-file", "inf")
        self.mode = "sleep"
        mins = float(minutes if minutes is not None else self.settings.get("music", "sleep_minutes"))
        if mins > 0:
            self._sleep_timer = threading.Timer(mins * 60, self.fade_out_and_stop)
            self._sleep_timer.daemon = True
            self._sleep_timer.start()
        from .timeparse import say_duration
        return {"ok": True, "playing": path.stem, "stops_after": say_duration(mins * 60) if mins > 0 else "plays all night"}

    def _cancel_sleep_timer(self) -> None:
        if self._sleep_timer is not None:
            self._sleep_timer.cancel()
            self._sleep_timer = None

    def fade_out_and_stop(self, seconds: float = 30) -> None:
        vol = self._get("volume") or 40
        steps = 30
        for i in range(steps):
            try:
                self._cmd("set_property", "volume", vol * (1 - (i + 1) / steps), start=False)
            except Exception:
                break
            time.sleep(seconds / steps)
        self.stop()

    def control(self, action: str, value: float | None = None) -> dict:
        if not self.is_active() and action not in ("volume",):
            return {"error": "nothing playing in Jarvis's player"}
        if action == "pause":
            self._cmd("set_property", "pause", True)
        elif action == "resume":
            self._cmd("set_property", "pause", False)
        elif action == "next":
            self._cmd("playlist-next", "force")
        elif action == "previous":
            self._cmd("playlist-prev", "force")
        elif action == "stop":
            self.stop()
        elif action == "volume" and value is not None:
            self._cmd("set_property", "volume", max(0.0, min(130.0, value)))
        return {"ok": True, "action": action}

    def stop(self) -> None:
        self._cancel_sleep_timer()
        try:
            self._cmd("stop", start=False)
        except Exception:
            pass
        self.mode = ""

    def now_playing(self) -> dict:
        if not self.is_active():
            return {"playing": False}
        title = re.sub(r"\.(mp3|flac|ogg|opus|m4a|aac|wav|wma|aiff|webm)$", "", self._get("media-title") or "", flags=re.I)
        return {"playing": True, "paused": bool(self._get("pause")), "title": title,
                "volume": round(self._get("volume") or 0), "mode": self.mode}

    def duck(self, on: bool, mode: str | None = None) -> None:
        """Make room for talking: pause or lower music (and pause browser videos), then restore afterwards.
        mode: 'pause', 'lower' or 'leave' (default: the 'Music while I listen' setting)."""
        mode = mode or self.settings.get("listening", "media_while_listening")
        try:
            with self._lock:
                if on:
                    self._duck_depth += 1
                    if self._duck_depth != 1 or mode == "leave":
                        return
                    if self.is_playing():
                        if mode == "pause":
                            self._cmd("set_property", "pause", True, start=False)
                            self._paused_self = True
                        else:
                            self._volume_before_duck = self._get("volume")
                            if self._volume_before_duck:
                                self._cmd("set_property", "volume", self._volume_before_duck * 0.15, start=False)
                    if mode == "pause":
                        self._paused_external = external_players()
                        _player_cmd(self._paused_external, "pause")
                else:
                    self._duck_depth = max(0, self._duck_depth - 1)
                    if self._duck_depth:
                        return
                    if self._paused_self:
                        self._cmd("set_property", "pause", False, start=False)
                    if self._volume_before_duck is not None:
                        self._cmd("set_property", "volume", self._volume_before_duck, start=False)
                    _player_cmd(self._paused_external, "play")
                    self._paused_self, self._volume_before_duck, self._paused_external = False, None, []
        except Exception as exc:
            log.debug("Ducking failed: %s", exc)

    def forget_paused(self) -> None:
        """The user stopped/paused things on purpose -- don't resume them after the conversation."""
        self._paused_self, self._volume_before_duck, self._paused_external = False, None, []

    def stop_everything(self) -> None:
        """Cancel phrase: stop our music and pause any other player (browser videos, Spotify...)."""
        self.forget_paused()
        self.stop()
        _player_cmd(external_players(), "pause")

    def busy(self) -> bool:
        """Is any sound playing (ours or another player's)? Cached for 2 seconds."""
        now = time.time()
        if now - self._busy_checked > 2:
            self._busy_checked = now
            try:
                self._busy = (self.is_playing() or self._paused_self or bool(self._paused_external)
                              or bool(external_players()))
            except Exception:
                self._busy = False
        return self._busy

    def shutdown(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()


# ---------------------------------------------------------------- other players & system volume
def playerctl(action: str) -> bool:
    if not shutil.which("playerctl"):
        return False
    cmd = {"pause": "pause", "resume": "play", "next": "next", "previous": "previous", "stop": "stop",
           "toggle": "play-pause"}.get(action)
    if not cmd:
        return False
    return subprocess.run(["playerctl", cmd], capture_output=True, timeout=5).returncode == 0


def playerctl_status() -> str:
    if not shutil.which("playerctl"):
        return ""
    r = subprocess.run(["playerctl", "metadata", "--format", "{{artist}} - {{title}} ({{status}})"],
                       capture_output=True, text=True, timeout=5)
    return r.stdout.strip() if r.returncode == 0 else ""


def system_volume(action: str, value: float | None = None) -> dict:
    """action: up, down, set, mute, unmute, get"""
    if shutil.which("wpctl"):
        sink = "@DEFAULT_AUDIO_SINK@"
        if action == "up":
            cmd = ["wpctl", "set-volume", "-l", "1.5", sink, f"{int(value or 10)}%+"]
        elif action == "down":
            cmd = ["wpctl", "set-volume", sink, f"{int(value or 10)}%-"]
        elif action == "set":
            cmd = ["wpctl", "set-volume", "-l", "1.5", sink, f"{int(value or 50)}%"]
        elif action in ("mute", "unmute"):
            cmd = ["wpctl", "set-mute", sink, "1" if action == "mute" else "0"]
        else:
            cmd = None
        if cmd:
            subprocess.run(cmd, capture_output=True, timeout=5)
        out = subprocess.run(["wpctl", "get-volume", sink], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"([\d.]+)", out)
        return {"ok": True, "volume_percent": round(float(m.group(1)) * 100) if m else None, "muted": "MUTED" in out}
    if shutil.which("pactl"):
        sink = "@DEFAULT_SINK@"
        if action == "up":
            subprocess.run(["pactl", "set-sink-volume", sink, f"+{int(value or 10)}%"], timeout=5)
        elif action == "down":
            subprocess.run(["pactl", "set-sink-volume", sink, f"-{int(value or 10)}%"], timeout=5)
        elif action == "set":
            subprocess.run(["pactl", "set-sink-volume", sink, f"{int(value or 50)}%"], timeout=5)
        elif action in ("mute", "unmute"):
            subprocess.run(["pactl", "set-sink-mute", sink, "1" if action == "mute" else "0"], timeout=5)
        out = subprocess.run(["pactl", "get-sink-volume", sink], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"(\d+)%", out)
        return {"ok": True, "volume_percent": int(m.group(1)) if m else None}
    return {"error": "Couldn't find a volume control program (wpctl or pactl)."}
