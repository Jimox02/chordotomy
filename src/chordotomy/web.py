"""Loopback-only browser interface for the live detector (standard-library HTTP)."""

from __future__ import annotations

import json
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from queue import Empty
from urllib.parse import urlsplit

import numpy as np

from .realtime import BLOCK, RATE, Capture, coalesce_blocks, make_detector, speakers
from .score import Score


class Session:
    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.stop_event = threading.Event()
        self.status = "idle"
        self.error = None
        self.events = deque(maxlen=600)
        self.latest = None
        self.dropped = 0
        self.sequence = 0
        self.engine = "dsp"
        self.score = Score()

    def start(self, device=None, demo=False, engine="dsp"):
        if engine not in ("dsp", "model"):
            raise ValueError("engine must be dsp or model")
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError("监听仍在运行或停止中，请稍后重试")
            self.stop_event.clear()
            self.events.clear()
            self.score.reset()
            self.latest, self.error, self.dropped = None, None, 0
            self.sequence += 1
            self.engine = engine
            self.status = "starting"
            self.thread = threading.Thread(
                target=self._run, args=(device, demo, engine), daemon=True
            )
            self.thread.start()

    def stop(self):
        with self.lock:
            self.stop_event.set()
            if self.thread and self.thread.is_alive():
                self.status = "stopping"

    def snapshot(self):
        with self.lock:
            return dict(
                status=self.status,
                error=self.error,
                latest=self.latest,
                history=list(self.events),
                dropped=self.dropped,
                session=self.sequence,
                engine=self.engine,
                score_revision=self.score.revision,
            )

    def _publish(self, event):
        if event is None:
            return
        item = event.to_dict()
        with self.lock:
            if not self.events or self.events[-1]["chord"] != event.chord:
                self.events.append(item)
            self.latest = item
            self.score.add(item)

    def _run(self, device, demo, engine):
        try:
            detector = make_detector(engine)
            if demo:
                with self.lock:
                    self.status = "demo"
                start = 0
                notes = [(60, 64, 67), (57, 60, 64), (53, 57, 60), (55, 59, 62, 65)]
                while not self.stop_event.is_set():
                    chord = notes[(start // (RATE * 3)) % len(notes)]
                    t = (np.arange(BLOCK) + start) / RATE
                    audio = sum(
                        0.04 * np.sin(2 * np.pi * 440 * 2 ** ((n - 69) / 12) * h * t) / h
                        for n in (*chord, chord[0] - 24)
                        for h in range(1, 5)
                    )
                    phase = t % 0.5
                    audio *= np.minimum(phase / 0.005, 1) * np.exp(-phase / 0.25)
                    self._publish(detector.feed(audio, start))
                    start += BLOCK
                    self.stop_event.wait(BLOCK / RATE)
            else:
                with Capture(device, queue_blocks=100 if engine == "model" else 10) as capture:
                    with self.lock:
                        self.status = "listening"
                    while not self.stop_event.is_set():
                        if capture.error:
                            raise RuntimeError(f"音频采集失败：{capture.error}")
                        try:
                            start, audio = capture.queue.get(timeout=0.25)
                        except Empty:
                            continue
                        if engine == "model":
                            start, audio = coalesce_blocks(capture.queue, (start, audio))
                        self._publish(detector.feed(audio, start))
                        with self.lock:
                            self.dropped = capture.dropped
            with self.lock:
                self.status = "stopped"
                self.score.analyze()
        except Exception as exc:
            with self.lock:
                self.status, self.error = "error", str(exc)


def make_handler(session):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def allowed(self):
            host = self.headers.get("Host", "")
            valid = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            origin = self.headers.get("Origin")
            return host in valid and (origin is None or origin == f"http://{host}")

        def send(self, data, status=200, content_type="application/json; charset=utf-8"):
            body = data.encode() if isinstance(data, str) else json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.allowed():
                return self.send({"error": "Invalid host or origin"}, 403)
            path = urlsplit(self.path).path
            if path == "/":
                return self.send(
                    files("chordotomy").joinpath("live.html").read_text("utf-8"),
                    content_type="text/html; charset=utf-8",
                )
            if path == "/score.js":
                return self.send(
                    files("chordotomy").joinpath("score.js").read_text("utf-8"),
                    content_type="text/javascript; charset=utf-8",
                )
            if path == "/api/state":
                return self.send(session.snapshot())
            if path == "/api/score":
                with session.lock:
                    score = session.score.snapshot()
                return self.send(score)
            if path == "/api/devices":
                try:
                    return self.send({"devices": speakers()})
                except Exception as exc:
                    return self.send({"error": str(exc)}, 503)
            return self.send({"error": "Not found"}, 404)

        def do_POST(self):
            if not self.allowed() or self.headers.get("Content-Type") != "application/json":
                return self.send({"error": "Invalid host, origin or content type"}, 403)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("Invalid body size")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("Expected JSON object")
                if self.path == "/api/start":
                    device, demo = body.get("device"), body.get("demo", False)
                    if (device is not None and not isinstance(device, str)) or type(
                        demo
                    ) is not bool:
                        raise ValueError("Invalid device or demo")
                    session.start(device, demo, body.get("engine", "dsp"))
                elif self.path in ("/api/score/config", "/api/score/edit", "/api/score/analyze"):
                    with session.lock:
                        if self.path == "/api/score/config":
                            session.score.configure(body)
                        elif self.path == "/api/score/edit":
                            session.score.edit(
                                body.get("beat"), body.get("chord", ""), body.get("reset", False)
                            )
                        elif session.thread and session.thread.is_alive():
                            raise ValueError("请先停止监听再补全和声分析")
                        if not session.thread or not session.thread.is_alive():
                            session.score.analyze()
                        score = session.score.snapshot()
                    return self.send(score)
                elif self.path == "/api/stop":
                    session.stop()
                else:
                    return self.send({"error": "Not found"}, 404)
                return self.send(session.snapshot())
            except (ValueError, json.JSONDecodeError) as exc:
                return self.send({"error": str(exc)}, 400)

    return Handler


def serve(port=8765):
    session = Session()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(session))
    server.daemon_threads = True
    print(f"Open http://127.0.0.1:{server.server_port} — Ctrl+C to exit", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        session.stop()
        server.server_close()
        if session.thread:
            session.thread.join(timeout=3)
