#!/usr/bin/env python3
"""catt-web: a local web remote for catt (Chromecast).

Run with the Python of the catt pipx venv, so that catt and pychromecast import.
The launcher script `catt-web` next to this file does that.
"""
from __future__ import annotations

import argparse
import hashlib
import http.cookies
import io
import ipaddress
import json
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pychromecast
from pychromecast.config import APP_YOUTUBE
from pychromecast.controllers.media import MediaStatusListener

from catt.controllers import get_app, get_controller
from catt.discovery import get_cast_with_ip, get_cast_with_name
from catt.stream_info import StreamInfo
from catt.util import get_local_ip, guess_mime

try:
    import segno
except ImportError:  # optional: only the QR codes need it
    segno = None

HOME = Path.home()
CONFIG_FILE = HOME / ".config/catt-web/config.json"
STATE_DIR = HOME / ".local/state/catt-web"
SESSION_FILE = STATE_DIR / "session.json"
SAVED_FILE = STATE_DIR / "saved.json"
SUBS_CACHE = HOME / ".cache/catt-web/subs"
CONVERT_DIR = Path("/tmp/catt-web")
STATIC_DIR = Path(__file__).resolve().parent / "static"

VIDEO_EXT = {".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".ts", ".m2ts"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wav"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
SUB_EXT = {".srt", ".vtt", ".ass", ".ssa"}
TEXT_SUB_CODECS = {"ass", "ssa", "subrip", "srt", "mov_text", "webvtt", "text"}
OK_VIDEO = {"h264", "hevc", "vp8", "vp9"}
# Audio codecs the Default Media Receiver plays on a Chromecast Ultra.
OK_AUDIO = {"aac", "mp3", "opus", "vorbis", "flac"}
# Audio codecs that can be copied into an MP4 without re-encoding.
MP4_COPY_AUDIO = {"aac", "mp3", "opus"}
PAUSE_INHIBIT_LIMIT = 30 * 60  # keep blocking sleep while paused, up to this many seconds


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- helpers

def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)


def parse_time(value: Any) -> float:
    """Accept seconds (number or string) or [hh:]mm:ss."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        raise ApiError("Empty time value")
    try:
        parts = [float(p) for p in text.split(":")]
    except ValueError:
        raise ApiError(f"Bad time value: {text}")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def lan_ip() -> str:
    """IP of the interface with the default route (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def qr_terminal(text: str) -> str:
    """QR code drawn with half blocks: one text row holds two module rows.
    Colors are set explicitly (black on white), so it scans on dark and light terminals."""
    rows = [list(r) for r in segno.make(text, error="m").matrix_iter(scale=1, border=2)]
    if len(rows) % 2:
        rows.append([0] * len(rows[0]))
    black, white = 16, 231
    lines = []
    for top, bottom in zip(rows[0::2], rows[1::2]):
        cells = "".join(f"\x1b[38;5;{black if t else white}m\x1b[48;5;{black if b else white}m▀"
                        for t, b in zip(top, bottom))
        lines.append("  " + cells + "\x1b[0m")
    return "\n".join(lines)


def qr_svg(text: str) -> bytes:
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=8, border=4, dark="#000", light="#fff",
                                     xmldecl=False, svgclass=None)
    return buf.getvalue()


def run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise ApiError(f"{cmd[0]} is not installed", 500)
    except subprocess.TimeoutExpired:
        raise ApiError(f"{cmd[0]} timed out", 500)


# ---------------------------------------------------------------- files, probe, subtitles

class Files:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.allowed = [self.root, CONVERT_DIR]
        self.probe_cache: dict[tuple[str, int], dict] = {}

    def check(self, path: str, want_file: bool = False) -> Path:
        p = Path(path).expanduser().resolve()
        if not any(p == a or p.is_relative_to(a) for a in self.allowed):
            raise ApiError(f"Path is outside the allowed folders: {p}", 403)
        if not p.exists():
            raise ApiError(f"Not found: {p}", 404)
        if want_file and not p.is_file():
            raise ApiError(f"Not a file: {p}")
        return p

    def browse(self, path: str | None) -> dict:
        default = HOME / "Downloads"
        p = self.check(path or str(default if default.is_dir() else self.root))
        if not p.is_dir():
            p = p.parent
        entries = []
        try:
            items = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except PermissionError:
            raise ApiError(f"No permission to read {p}", 403)
        for e in items:
            if e.name.startswith("."):
                continue
            try:
                if e.is_dir():
                    entries.append({"name": e.name, "path": str(e), "type": "dir"})
                    continue
                ext = e.suffix.lower()
                kind = ("video" if ext in VIDEO_EXT else "audio" if ext in AUDIO_EXT
                        else "image" if ext in IMAGE_EXT else "subs" if ext in SUB_EXT else None)
                if kind:
                    st = e.stat()
                    entries.append({"name": e.name, "path": str(e), "type": kind,
                                    "size": st.st_size, "mtime": st.st_mtime})
            except OSError:
                continue
        parent = None
        if any(p != a and p.is_relative_to(a) for a in self.allowed):
            parent = str(p.parent)
        return {"path": str(p), "parent": parent, "root": str(self.root),
                "converted_dir": str(CONVERT_DIR), "entries": entries}

    def probe(self, path: str) -> dict:
        p = self.check(path, want_file=True)
        st = p.stat()
        key = (str(p), st.st_mtime_ns)
        if key in self.probe_cache:
            result = dict(self.probe_cache[key])
            result["converted"] = converted_path_if_fresh(p)
            return result
        out = run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(p)], 60)
        if out.returncode != 0:
            raise ApiError(out.stderr.strip() or "ffprobe failed")
        data = json.loads(out.stdout)
        streams = data.get("streams", [])
        fmt = data.get("format", {})
        video = [s for s in streams if s.get("codec_type") == "video"
                 and not s.get("disposition", {}).get("attached_pic")]
        audio = [s for s in streams if s.get("codec_type") == "audio"]
        subs = [s for s in streams if s.get("codec_type") == "subtitle"]
        warnings: list[str] = []
        v_mode, a_mode = "copy", "copy"
        v = video[0] if video else None
        a = audio[0] if audio else None
        if v and v.get("codec_name") not in OK_VIDEO:
            warnings.append(f"Video codec {v.get('codec_name')} is not supported by Chromecast. "
                            "Convert re-encodes the video to H.264 (slow).")
            v_mode = "h264"
        if a:
            codec, ch = a.get("codec_name"), int(a.get("channels") or 2)
            if codec == "aac" and ch > 2:
                warnings.append(f"Audio is {ch}-channel AAC. Chromecast Gen 2/Ultra refuse multichannel AAC "
                                "(the cast loads, then stops). Convert makes a stereo AAC copy.")
                a_mode = "aac2"
            elif codec in ("ac3", "eac3"):
                warnings.append(f"Audio is {codec.upper()}. It plays only if the TV or receiver supports "
                                "passthrough. Convert makes a stereo AAC copy.")
                a_mode = "aac2"
            elif codec not in OK_AUDIO:
                warnings.append(f"Audio codec {codec} is not supported by Chromecast. Convert makes a stereo AAC copy.")
                a_mode = "aac2"
            elif codec not in MP4_COPY_AUDIO:
                a_mode = "aac2"  # playable, but cannot be copied into MP4
        if p.suffix.lower() in (".mkv", ".avi", ".ts", ".m2ts", ".mov") and v:
            warnings.append(f"{p.suffix.upper().lstrip('.')} container may fail on Chromecast. "
                            "MP4 is safer (Convert copies the streams into MP4).")
        result = {
            "path": str(p), "name": p.name, "size": st.st_size,
            "duration": float(fmt.get("duration") or 0),
            "format": fmt.get("format_name"),
            "video": {k: v.get(k) for k in ("index", "codec_name", "profile", "width", "height", "pix_fmt")} if v else None,
            "audio": [{"index": s.get("index"), "codec": s.get("codec_name"), "channels": s.get("channels"),
                       "lang": s.get("tags", {}).get("language"), "title": s.get("tags", {}).get("title")}
                      for s in audio],
            "subtitles": [{"track": i, "codec": s.get("codec_name"),
                           "text": s.get("codec_name") in TEXT_SUB_CODECS,
                           "lang": s.get("tags", {}).get("language"),
                           "title": s.get("tags", {}).get("title"),
                           "default": bool(s.get("disposition", {}).get("default")),
                           "forced": bool(s.get("disposition", {}).get("forced"))}
                          for i, s in enumerate(subs)],
            "external_subs": sorted(str(e) for e in p.parent.iterdir()
                                    if e.is_file() and e.suffix.lower() in SUB_EXT),
            "warnings": warnings,
            "convert": {"needed": bool(warnings), "video": v_mode, "audio": a_mode if a else None,
                        "video_index": v.get("index") if v else None,
                        "audio_index": a.get("index") if a else None,
                        "hevc": bool(v and v.get("codec_name") == "hevc")},
        }
        self.probe_cache[key] = result
        result = dict(result)
        result["converted"] = converted_path_if_fresh(p)
        return result


def converted_path(src: Path) -> Path:
    return CONVERT_DIR / f"{src.stem}.cast.mp4"


def converted_path_if_fresh(src: Path) -> str | None:
    out = converted_path(src)
    try:
        if out != src and out.stat().st_mtime >= src.stat().st_mtime:
            return str(out)
    except OSError:
        pass
    return None


def prepare_subs(src: Path, track: int | None) -> Path:
    """Convert an external subtitle file or an embedded track to WebVTT (cached)."""
    st = src.stat()
    key = hashlib.sha1(f"{src}|{st.st_mtime_ns}|{track}".encode()).hexdigest()[:16]
    out = SUBS_CACHE / f"{key}.vtt"
    if out.is_file() and out.stat().st_size > 0:
        return out
    SUBS_CACHE.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part.vtt")
    base = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y"]
    tail = (["-map", f"0:s:{track}"] if track is not None else []) + ["-f", "webvtt", str(tmp)]
    res = run(base + ["-i", str(src)] + tail, 900)
    if res.returncode != 0 and track is None:
        # Retry with a legacy encoding for old .srt files.
        res = run(base + ["-sub_charenc", "CP1252", "-i", str(src)] + tail, 900)
    if res.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise ApiError("Subtitle conversion failed: " + (res.stderr.strip()[-300:] or "unknown error"))
    tmp.replace(out)
    return out


# ---------------------------------------------------------------- convert jobs

class Jobs:
    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.procs: dict[str, subprocess.Popen] = {}
        self.lock = threading.Lock()

    def public(self) -> list[dict]:
        with self.lock:
            return [dict(j) for j in self.jobs.values()]

    def start(self, info: dict) -> dict:
        src = Path(info["path"])
        out = converted_path(src)
        with self.lock:
            for j in self.jobs.values():
                if j["source"] == str(src) and j["state"] == "running":
                    return dict(j)
        fresh = converted_path_if_fresh(src)
        job_id = secrets.token_hex(4)
        job = {"id": job_id, "source": str(src), "output": str(out), "state": "running",
               "progress": 0.0, "error": None, "started": time.time()}
        if fresh:
            job.update(state="done", progress=1.0)
            with self.lock:
                self.jobs[job_id] = job
            return dict(job)
        CONVERT_DIR.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(CONVERT_DIR).free
        need = int(info["size"] * 1.1) + 100 * 2**20
        if free < need:
            raise ApiError(f"Not enough free space in {CONVERT_DIR}: need {need / 2**30:.1f} GB, "
                           f"have {free / 2**30:.1f} GB")
        conv = info["convert"]
        part = out.with_suffix(".part.mp4")
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", str(src)]
        if conv["video_index"] is not None:
            cmd += ["-map", f"0:{conv['video_index']}"]
            if conv["video"] == "copy":
                cmd += ["-c:v", "copy"] + (["-tag:v", "hvc1"] if conv["hevc"] else [])
            else:
                cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
        if conv["audio_index"] is not None:
            cmd += ["-map", f"0:{conv['audio_index']}"]
            cmd += ["-c:a", "copy"] if conv["audio"] == "copy" else ["-c:a", "aac", "-ac", "2", "-b:a", "192k"]
        cmd += ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", "-f", "mp4", str(part)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        with self.lock:
            self.jobs[job_id] = job
            self.procs[job_id] = proc
        threading.Thread(target=self._watch, args=(job_id, proc, info["duration"], part, out),
                         daemon=True).start()
        return dict(job)

    def _watch(self, job_id: str, proc: subprocess.Popen, duration: float, part: Path, out: Path) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            key, _, val = line.strip().partition("=")
            if key == "out_time_us" and duration > 0:
                try:
                    with self.lock:
                        self.jobs[job_id]["progress"] = min(0.999, int(val) / 1e6 / duration)
                except ValueError:
                    pass
        err = proc.stderr.read() if proc.stderr else ""
        code = proc.wait()
        with self.lock:
            job = self.jobs[job_id]
            self.procs.pop(job_id, None)
            if job["state"] == "cancelled":
                part.unlink(missing_ok=True)
            elif code == 0:
                part.replace(out)
                job.update(state="done", progress=1.0)
            else:
                part.unlink(missing_ok=True)
                job.update(state="failed", error=err.strip()[-400:] or f"ffmpeg exit code {code}")

    def cancel(self, job_id: str) -> None:
        with self.lock:
            proc = self.procs.get(job_id)
            if not proc:
                raise ApiError("No running job with that id")
            self.jobs[job_id]["state"] = "cancelled"
        proc.terminate()

    def stop_all(self) -> None:
        with self.lock:
            procs = list(self.procs.values())
        for p in procs:
            p.terminate()


# ---------------------------------------------------------------- sleep inhibitor

class Inhibitor:
    """Hold a systemd-inhibit block lock. The child is `cat` reading our pipe,
    so the lock is released even if this process is killed."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.why = ""

    @property
    def active(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def set(self, want: bool, why: str = "") -> None:
        if want and not self.active:
            try:
                self.proc = subprocess.Popen(
                    ["systemd-inhibit", "--what=sleep:handle-lid-switch", "--mode=block",
                     "--who=catt-web", f"--why={why or 'Casting to Chromecast'}", "cat"],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.why = why
            except FileNotFoundError:
                self.proc = None
        elif not want and self.proc is not None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()
            self.proc = None


# ---------------------------------------------------------------- device

class Listener(MediaStatusListener):
    def __init__(self, app: "App") -> None:
        self.app = app

    def new_media_status(self, status) -> None:
        pass

    def load_media_failed(self, queue_item_id: int, error_code: int) -> None:
        self.app.last_error = (f"The TV failed to load the media (error {error_code}). "
                               "The format or codec may not be supported.")


class Device:
    def __init__(self, app: "App") -> None:
        self.app = app
        self.cast: pychromecast.Chromecast | None = None
        self.controllers: dict[str, Any] = {}
        self.lock = threading.RLock()
        self.connecting = False
        self.error: str | None = None

    def require(self) -> pychromecast.Chromecast:
        if self.cast is None:
            raise ApiError("No device connected" + (" (connecting...)" if self.connecting else ""), 409)
        return self.cast

    def connect(self, ip: str | None = None, name: str | None = None) -> None:
        self.connecting = True
        try:
            if ip:
                cast = get_cast_with_ip(ip)
            elif name:
                cast = get_cast_with_name(name)
            else:
                raise ApiError("Give a device name or IP")
            if cast is None:
                raise ApiError(f"Device not found: {ip or name}", 404)
            cast.media_controller.register_status_listener(Listener(self.app))
            with self.lock:
                old, self.cast, self.controllers = self.cast, cast, {}
            self.error = None
            if old is not None:
                threading.Thread(target=old.disconnect, kwargs={"timeout": 3}, daemon=True).start()
        except Exception as e:
            self.error = str(e)
            raise
        finally:
            self.connecting = False

    def controller(self, app_name: str | None = None):
        """catt controller for the named app, or for the app running now."""
        c = self.require()
        if app_name is None:
            app = get_app(c.app_id or "default", c.cast_type)
        else:
            app = get_app(app_name, c.cast_type, strict=app_name != "default")
        if app.name not in self.controllers:
            self.controllers[app.name] = get_controller(c, app)
        return self.controllers[app.name]


def scan_devices() -> list[dict]:
    infos, browser = pychromecast.discovery.discover_chromecasts(timeout=5)
    browser.stop_discovery()
    return sorted(({"name": i.friendly_name, "ip": i.host, "model": i.model_name,
                    "type": i.cast_type, "uuid": str(i.uuid)} for i in infos),
                  key=lambda d: d["name"] or "")


# ---------------------------------------------------------------- app state

class App:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.port: int = args.port
        self.config = load_json(CONFIG_FILE, {})
        if not self.config.get("token"):
            self.config["token"] = secrets.token_urlsafe(16)
            save_json(CONFIG_FILE, self.config)
        self.token: str = self.config["token"]
        self.files = Files(Path(args.root).expanduser())
        self.jobs = Jobs()
        self.inhibitor = Inhibitor()
        self.device = Device(self)
        self.shares: dict[str, tuple[Path, str]] = {}  # token -> (file, content type)
        self.session: dict | None = load_json(SESSION_FILE, None)
        self.resume_dismissed = False
        self.last_error: str | None = None
        self.paused_since: float | None = None
        self.woke_at: float | None = None
        self.stop_event = threading.Event()

    # --- sharing files with the TV

    def share(self, path: Path, ctype: str) -> str:
        tok = secrets.token_urlsafe(12)
        self.shares[tok] = (path, ctype)
        while len(self.shares) > 16:
            self.shares.pop(next(iter(self.shares)))
        return tok

    def base_url(self, cast_host: str) -> str:
        ip = get_local_ip(cast_host) or lan_ip()
        return f"http://{ip}:{self.port}"

    def phone_url(self) -> str | None:
        """URL (with token) that a phone on the same network can open. None with --local-only."""
        if self.args.local_only:
            return None
        return f"http://{lan_ip()}:{self.port}/?t={self.token}"

    # --- status

    def ours(self) -> bool:
        c = self.device.cast
        return bool(c and self.session and self.session.get("media_url")
                    and c.media_controller.status.content_id == self.session["media_url"])

    def status(self) -> dict:
        c = self.device.cast
        d: dict[str, Any] = {
            "device": None, "connecting": self.device.connecting, "device_error": self.device.error,
            "app": None, "media": None, "volume": None, "session": self.session,
            "ours": self.ours(), "inhibit": self.inhibitor.active, "jobs": self.jobs.public(),
            "last_error": self.last_error, "woke_at": self.woke_at, "resume_offer": None,
            "phone_url": self.phone_url(), "qr": segno is not None,
        }
        if c is not None:
            cs = c.status
            ms = c.media_controller.status
            d["device"] = {"name": c.cast_info.friendly_name, "ip": c.cast_info.host,
                           "model": c.cast_info.model_name, "type": c.cast_type,
                           "connected": c.socket_client.is_connected}
            d["app"] = {"id": c.app_id, "name": c.app_display_name, "youtube": c.app_id == APP_YOUTUBE}
            if cs is not None:
                d["volume"] = {"level": round(cs.volume_level * 100), "muted": cs.volume_muted}
            tracks = [{"id": t.get("trackId"), "name": t.get("name"), "lang": t.get("language")}
                      for t in (ms.subtitle_tracks or []) if t.get("type") == "TEXT"]
            d["media"] = {"state": ms.player_state, "title": ms.title, "time": ms.adjusted_current_time,
                          "duration": ms.duration, "content_id": ms.content_id,
                          "content_type": ms.content_type, "idle_reason": ms.idle_reason,
                          "stream_type": ms.stream_type, "tracks": tracks,
                          "active_tracks": ms.current_subtitle_tracks,
                          "can_seek": ms.supports_seek, "can_pause": ms.supports_pause,
                          "can_skip": ms.supports_skip_forward}
        s = self.session
        if (s and not self.resume_dismissed and not s.get("finished") and (s.get("position") or 0) > 5
                and not self.ours()):
            d["resume_offer"] = {"title": s.get("title"), "path": s.get("path"),
                                 "position": s.get("position"), "subs": s.get("subs")}
        return d

    # --- casting

    def cast_file(self, path: str, subs: dict | None = None, start: float = 0, title: str | None = None,
                  origin: str | None = None) -> None:
        p = self.files.check(path, want_file=True)
        origin_p = self.files.check(origin, want_file=True) if origin else p
        sub_src: Path | None = None
        track: int | None = None
        if subs:
            if subs.get("file"):
                sub_src = self.files.check(subs["file"], want_file=True)
            elif subs.get("track") is not None:
                # "source" is the file that holds the track (the original, when casting a converted copy).
                sub_src = self.files.check(subs.get("source") or str(p), want_file=True)
                track = int(subs["track"])
        with self.device.lock:
            c = self.device.require()
            base = self.base_url(c.cast_info.host)
            ctype = guess_mime(p.name)
            media_url = f"{base}/media/{self.share(p, ctype)}/{urllib.parse.quote(p.name)}"
            sub_url = None
            if sub_src is not None:
                vtt = prepare_subs(sub_src, track)
                sub_url = f"{base}/subs/{self.share(vtt, 'text/vtt')}.vtt"
            ctl = self.device.controller("default")
            ctl.prep_app()
            self.last_error = None
            ctl.play_media_url(media_url, content_type=ctype, title=title or p.stem,
                               current_time=start or None, subtitles=sub_url)
            self.session = {"kind": "file", "path": str(p), "origin": str(origin_p),
                            "title": title or p.stem, "subs": subs or None,
                            "media_url": media_url, "position": start or 0, "finished": False,
                            "updated": time.time()}
            self.resume_dismissed = False
            save_json(SESSION_FILE, self.session)

    def cast_url(self, url: str, force_default: bool = False) -> None:
        with self.device.lock:
            c = self.device.require()
            try:
                stream = StreamInfo(url, cast_info=c.cast_info)
            except Exception as e:
                raise ApiError(f"Could not resolve URL: {e}")
            if stream.is_local_file:
                raise ApiError("Use the file browser for local files")
            app_name = "default" if force_default else get_app(stream.extractor, c.cast_type).name
            ctl = self.device.controller(app_name)
            ctl.prep_app()
            self.last_error = None
            if stream.is_playlist:
                if stream.playlist_length == 0:
                    raise ApiError("Playlist is empty")
                if ctl.playlist_capability and stream.playlist_all_ids:
                    ctl.play_playlist(stream.playlist_id, video_id=stream.video_id or stream.playlist_all_ids[0])
                    return
                stream.set_playlist_entry(0)
            if ctl.info_type == "url":
                ctl.play_media_url(stream.video_url, title=stream.video_title,
                                   content_type=stream.guessed_content_type, thumb=stream.video_thumbnail,
                                   stream_type=stream.stream_type, media_info=stream.media_info)
            else:
                ctl.play_media_id(stream.video_id)

    def cast_site(self, url: str) -> None:
        with self.device.lock:
            ctl = self.device.controller("dashcast")
            ctl.prep_app()
            ctl.load_url(url)

    def youtube(self, action: str, url: str | None) -> None:
        with self.device.lock:
            c = self.device.require()
            if c.app_id != APP_YOUTUBE:
                raise ApiError("The YouTube app is not active. Cast a YouTube URL first.")
            ctl = self.device.controller("youtube")
            if action == "clear":
                ctl.clear()
                return
            if not url:
                raise ApiError("Give a YouTube URL")
            stream = StreamInfo(url, cast_info=c.cast_info)
            if stream.extractor != "youtube" or not stream.video_id:
                raise ApiError("Not a YouTube video URL")
            if action == "add":
                ctl.add(stream.video_id)
            elif action == "add_next":
                ctl.add_next(stream.video_id)
            elif action == "remove":
                ctl.remove(stream.video_id)
            else:
                raise ApiError(f"Unknown YouTube action: {action}")

    def control(self, action: str, value: Any = None) -> None:
        with self.device.lock:
            c = self.device.require()
            if action == "volume":
                c.set_volume(max(0.0, min(1.0, float(value) / 100)))
                return
            if action in ("volumeup", "volumedown"):
                delta = float(value if value is not None else 5) / 100
                (c.volume_up if action == "volumeup" else c.volume_down)(delta)
                return
            if action == "mute":
                muted = (not c.status.volume_muted) if value is None else bool(value)
                c.set_volume_muted(muted)
                return
            ctl = self.device.controller()
            if action == "stop":
                ctl.kill()
                if self.session:
                    self.session["finished"] = True
                    save_json(SESSION_FILE, self.session)
                return
            ctl.prep_control()
            if action == "play":
                ctl.play()
            elif action == "pause":
                ctl.pause()
            elif action == "toggle":
                ctl.play_toggle()
            elif action == "skip":
                ctl.skip()
            elif action == "seek":
                ctl.seek(parse_time(value))
            elif action == "rewind":
                ctl.rewind(parse_time(value if value is not None else 10))
            elif action == "ffwd":
                ctl.ffwd(parse_time(value if value is not None else 10))
            else:
                raise ApiError(f"Unknown action: {action}")

    def set_subs(self, subs: dict | None) -> None:
        s = self.session
        if not (s and self.ours()):
            raise ApiError("Subtitles can only be changed for a file that catt-web is casting")
        c = self.device.require()
        ms = c.media_controller.status
        if not subs:
            if ms.current_subtitle_tracks:
                with self.device.lock:
                    c.media_controller.disable_subtitle()
            s["subs"] = None
            save_json(SESSION_FILE, s)
            return
        if subs.get("track") is not None and not subs.get("source"):
            subs = {**subs, "source": s.get("origin") or s["path"]}
        # The TV reads subtitle tracks only when media loads, so reload at the current position.
        self.cast_file(s["path"], subs, start=ms.adjusted_current_time or 0, title=s.get("title"),
                       origin=s.get("origin"))

    def info(self) -> dict:
        with self.device.lock:
            ctl = self.device.controller()
            ctl.prep_info()
            return json.loads(json.dumps(ctl.info, default=str))

    def save_state(self) -> dict:
        if self.session and self.ours():
            data = {"kind": "file", **{k: self.session.get(k) for k in ("path", "origin", "title", "subs")},
                    "position": self.device.require().media_controller.status.adjusted_current_time}
        else:
            with self.device.lock:
                ctl = self.device.controller()
                ctl.prep_control()
                if not ctl.save_capability and ctl.name != "default":
                    raise ApiError(f"Saving is not supported for the {ctl.name} app")
                data = {"kind": "remote", "app": ctl.name, "data": ctl.media_info}
        data["saved_at"] = time.time()
        save_json(SAVED_FILE, data)
        return data

    def restore_state(self) -> None:
        data = load_json(SAVED_FILE, None)
        if not data:
            raise ApiError("Nothing saved")
        if data["kind"] == "file":
            self.cast_file(data["path"], data.get("subs"), start=data.get("position") or 0, title=data.get("title"),
                           origin=data.get("origin"))
            return
        with self.device.lock:
            ctl = self.device.controller(data["app"])
            ctl.prep_app()
            ctl.restore(data["data"])

    def resume(self) -> None:
        s = self.session
        if not s:
            raise ApiError("No session to resume")
        self.cast_file(s["path"], s.get("subs"), start=max(0.0, (s.get("position") or 0) - 3), title=s.get("title"),
                       origin=s.get("origin"))

    # --- background work

    def connect_default(self) -> None:
        dev = self.args.device or self.config.get("device", {}).get("ip") or self.config.get("device", {}).get("name")
        try:
            if dev:
                is_ip = True
                try:
                    ipaddress.ip_address(dev)
                except ValueError:
                    is_ip = False
                self.device.connect(ip=dev if is_ip else None, name=None if is_ip else dev)
            else:
                found = scan_devices()
                if len(found) == 1:
                    self.device.connect(ip=found[0]["ip"])
                elif not found:
                    self.device.error = "No Chromecast found. Use Scan."
                else:
                    self.device.error = "Several devices found. Pick one."
                    return
            c = self.device.cast
            if c is not None:
                self.config["device"] = {"name": c.cast_info.friendly_name, "ip": c.cast_info.host}
                save_json(CONFIG_FILE, self.config)
        except Exception as e:
            self.device.error = f"Could not connect: {e}"

    def monitor(self) -> None:
        """Every 2 s: detect suspend/resume, track the session, drive the sleep inhibitor."""
        last_offset = time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()
        last_save = 0.0
        last_refresh = 0.0
        while not self.stop_event.wait(2):
            offset = time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()
            if offset - last_offset > 5:
                self.woke_at = time.time()
                self.resume_dismissed = False
                threading.Thread(target=self._reconnect_after_wake, daemon=True).start()
            last_offset = offset
            want_inhibit = False
            why = ""
            try:
                c = self.device.cast
                if c is not None:
                    ms = c.media_controller.status
                    now = time.time()
                    if (c.app_id and ms.player_state in ("PLAYING", "BUFFERING", "PAUSED")
                            and now - last_refresh > 30):
                        last_refresh = now
                        try:
                            c.media_controller.update_status()
                        except Exception:
                            pass
                    if self.ours() and self.session is not None:
                        state = ms.player_state
                        if state == "PAUSED":
                            self.paused_since = self.paused_since or now
                        else:
                            self.paused_since = None
                        pos = ms.adjusted_current_time
                        if pos is not None:
                            self.session["position"] = pos
                        self.session["duration"] = ms.duration
                        want_inhibit = state in ("PLAYING", "BUFFERING") or (
                            state == "PAUSED" and now - (self.paused_since or now) < PAUSE_INHIBIT_LIMIT)
                        why = f"Casting {self.session.get('title')}"
                        if state == "IDLE" and ms.idle_reason == "FINISHED":
                            self.session["finished"] = True
                        if state == "IDLE" and ms.idle_reason == "ERROR" and not self.session.get("error_shown"):
                            self.session["error_shown"] = True
                            self.last_error = (f"The TV stopped “{self.session.get('title')}” with an error. "
                                               "The format or codec is probably not supported. "
                                               "Open the file in Files to see warnings and Convert.")
                        if now - last_save > 5:
                            self.session["updated"] = now
                            save_json(SESSION_FILE, self.session)
                            last_save = now
            except Exception:
                pass
            self.inhibitor.set(want_inhibit, why)

    def _reconnect_after_wake(self) -> None:
        time.sleep(3)  # give the network time to come back
        c = self.device.cast
        ip = c.cast_info.host if c else (self.config.get("device") or {}).get("ip")
        for _ in range(10):
            try:
                if ip:
                    self.device.connect(ip=ip)
                else:
                    self.connect_default()
                return
            except Exception:
                time.sleep(5)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "catt-web"
    app: App  # set in main()

    def log_message(self, format: str, *args: Any) -> None:
        if self.app.args.verbose:
            super().log_message(format, *args)

    # --- responses

    def send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text: str, status: int = 200, ctype: str = "text/plain; charset=utf-8") -> None:
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- auth

    def client_is_local(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def authed(self, query: dict) -> bool:
        if self.app.args.local_only and not self.client_is_local():
            return False
        candidates = [query.get("t", [""])[0], self.headers.get("X-Token", "")]
        cookie = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        if "catt_web" in cookie:
            candidates.append(cookie["catt_web"].value)
        return any(c and secrets.compare_digest(c, self.app.token) for c in candidates)

    # --- routing

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_HEAD(self) -> None:
        self.do_GET(head=True)

    def do_GET(self, head: bool = False) -> None:
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        path = url.path
        if path.startswith("/media/") or path.startswith("/subs/"):
            tok = path.split("/")[2].removesuffix(".vtt")
            return self.serve_share(tok, head)
        if path == "/":
            # The page has no secrets; only /api/ needs the token. The page reads the token from
            # its URL and sends it as a header, so it does not depend on a cookie that browsers
            # may drop when a QR/camera app opens the link (cross-site navigation).
            if self.app.args.local_only and not self.client_is_local():
                return self.send_text("Forbidden\n", 403)
            cookie = None
            if "t" in query and self.authed(query):
                cookie = f"catt_web={self.app.token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000"
            return self.serve_static("index.html", cookie)
        if not self.authed(query):
            return self.send_text("catt-web: open the URL with ?t=<token> that the terminal printed.\n", 403)
        if path == "/api/qr.svg":
            url = self.app.phone_url()
            if not url or segno is None:
                return self.send_text("QR code not available\n", 404)
            body = qr_svg(url)
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/api/"):
            return self.api("GET", path[5:], {k: v[0] for k, v in query.items()})
        self.send_text("Not found\n", 404)

    def do_POST(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        if not self.authed(urllib.parse.parse_qs(url.query)):
            return self.send_text("Forbidden\n", 403)
        if not url.path.startswith("/api/"):
            return self.send_text("Not found\n", 404)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except ValueError:
            return self.send_json({"error": "Bad JSON"}, 400)
        self.api("POST", url.path[5:], body)

    def serve_static(self, name: str, cookie: str | None = None) -> None:
        body = (STATIC_DIR / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def serve_share(self, tok: str, head: bool) -> None:
        entry = self.app.shares.get(tok)
        if not entry or not entry[0].is_file():
            return self.send_text("Not found\n", 404)
        fpath, ctype = entry
        size = fpath.stat().st_size
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        if rng:
            try:
                unit, _, spec = rng.partition("=")
                first, _, last = spec.split(",")[0].strip().partition("-")
                if unit.strip() != "bytes":
                    raise ValueError
                if first:
                    start = int(first)
                    end = min(int(last), size - 1) if last else size - 1
                else:
                    start = max(0, size - int(last))
                if start > end or start >= size:
                    raise ValueError
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(end - start + 1))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head:
            return
        try:
            with fpath.open("rb") as fh:
                fh.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    buf = fh.read(min(256 * 1024, remaining))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    remaining -= len(buf)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass  # the TV closes connections when it seeks or stops

    def api(self, method: str, name: str, p: dict) -> None:
        app = self.app
        try:
            if method == "GET":
                if name == "status":
                    return self.send_json(app.status())
                if name == "devices":
                    return self.send_json({"devices": scan_devices()})
                if name == "browse":
                    return self.send_json(app.files.browse(p.get("path")))
                if name == "probe":
                    return self.send_json(app.files.probe(p["path"]))
                if name == "jobs":
                    return self.send_json({"jobs": app.jobs.public()})
                if name == "info":
                    return self.send_json(app.info())
                if name == "saved":
                    return self.send_json({"saved": load_json(SAVED_FILE, None)})
            else:
                if name == "device":
                    app.device.connect(ip=p.get("ip"), name=p.get("name"))
                    c = app.device.require()
                    app.config["device"] = {"name": c.cast_info.friendly_name, "ip": c.cast_info.host}
                    save_json(CONFIG_FILE, app.config)
                    return self.send_json({"ok": True})
                if name == "cast":
                    app.cast_file(p["path"], p.get("subs"), parse_time(p.get("start") or 0), p.get("title"),
                                  p.get("origin"))
                    return self.send_json({"ok": True})
                if name == "cast_url":
                    app.cast_url(p["url"], bool(p.get("force_default")))
                    return self.send_json({"ok": True})
                if name == "cast_site":
                    app.cast_site(p["url"])
                    return self.send_json({"ok": True})
                if name == "control":
                    app.control(p["action"], p.get("value"))
                    return self.send_json({"ok": True})
                if name == "subs":
                    app.set_subs(p.get("subs"))
                    return self.send_json({"ok": True})
                if name == "youtube":
                    app.youtube(p["action"], p.get("url"))
                    return self.send_json({"ok": True})
                if name == "convert":
                    return self.send_json({"job": app.jobs.start(app.files.probe(p["path"]))})
                if name == "convert_cancel":
                    app.jobs.cancel(p["id"])
                    return self.send_json({"ok": True})
                if name == "save":
                    return self.send_json({"saved": app.save_state()})
                if name == "restore":
                    app.restore_state()
                    return self.send_json({"ok": True})
                if name == "resume":
                    if p.get("dismiss"):
                        app.resume_dismissed = True
                    else:
                        app.resume()
                    return self.send_json({"ok": True})
                if name == "clear_error":
                    app.last_error = None
                    return self.send_json({"ok": True})
            self.send_json({"error": f"Unknown endpoint: {method} /api/{name}"}, 404)
        except ApiError as e:
            self.send_json({"error": str(e)}, e.status)
        except KeyError as e:
            self.send_json({"error": f"Missing parameter: {e}"}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(prog="catt-web", description="Local web remote for catt (Chromecast).")
    ap.add_argument("--port", type=int, default=8765, help="HTTP port (default 8765)")
    ap.add_argument("--device", help="Chromecast name or IP (default: last used, or the only one found)")
    ap.add_argument("--root", default=str(HOME), help="Top folder the file browser can open (default ~)")
    ap.add_argument("--local-only", action="store_true",
                    help="Allow the control page only from this computer (the TV can still fetch media)")
    ap.add_argument("--no-browser", action="store_true", help="Do not open a browser at start")
    ap.add_argument("--verbose", action="store_true", help="Log every HTTP request")
    args = ap.parse_args()

    app = App(args)
    Handler.app = app
    try:
        server = Server(("0.0.0.0", args.port), Handler)
    except OSError as e:
        sys.exit(f"catt-web: cannot listen on port {args.port}: {e}")

    def shutdown(*_: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, shutdown)
    threading.Thread(target=app.connect_default, daemon=True).start()
    threading.Thread(target=app.monitor, daemon=True).start()

    local_url = f"http://127.0.0.1:{args.port}/?t={app.token}"
    print(f"catt-web running\n  this computer: {local_url}")
    phone_url = app.phone_url()
    if phone_url:
        print(f"  phone (same Wi-Fi): {phone_url}")
        if segno is None:
            print("  (install segno for a QR code: pipx inject catt segno)")
        elif sys.stdout.isatty():
            print("  Scan with your phone:")
            print(qr_terminal(phone_url))
    print("Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        webbrowser.open(local_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("\nStopping catt-web...")
        app.stop_event.set()
        app.inhibitor.set(False)
        app.jobs.stop_all()
        if app.session:
            save_json(SESSION_FILE, app.session)
        server.server_close()


if __name__ == "__main__":
    main()
