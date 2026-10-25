"""engram/vision/intent_vision.py without the network: request shape, image sizing, answer parsing, fail-closed."""

import base64
import io
import json
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from engram.vision import intent_vision as iv

KEY = "dummy-api-key-for-tests"  # not a real key; asserts below check it never leaks
REPO = Path(__file__).resolve().parents[1]


def png(w, h, color=(200, 30, 30)):
    """A solid-colour PNG built by hand, so these tests need neither Pillow nor OpenCV to make inputs. Four colour
    components (r, g, b, alpha) make an RGBA PNG."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    row = b"\x00" + bytes(color) * w
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6 if len(color) == 4 else 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * h)) + chunk(b"IEND", b""))


def mean_rgb(jpeg):
    """The average (r, g, b) of an image, read with whichever library is installed."""
    try:
        from PIL import Image, ImageStat
        return tuple(ImageStat.Stat(Image.open(io.BytesIO(jpeg)).convert("RGB")).mean)
    except ImportError:
        cv2 = pytest.importorskip("cv2")
        import numpy as np
        b, g, r = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR).reshape(-1, 3).mean(axis=0)
        return r, g, b


def image_size(jpeg):
    """(width, height, format) of an image, read with whichever library is installed."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(jpeg))
        return img.size[0], img.size[1], img.format
    except ImportError:
        cv2 = pytest.importorskip("cv2")
        import numpy as np
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        return img.shape[1], img.shape[0], "JPEG" if jpeg[:2] == b"\xff\xd8" else "?"


class FakeTransport:
    def __init__(self, reply=None, status=200, raises=None):
        self.reply, self.status, self.raises, self.calls = reply, status, raises, []

    def __call__(self, url, body, headers, timeout_s):
        self.calls.append((url, json.loads(body), headers, timeout_s))
        if self.raises:
            raise self.raises
        data = self.reply(json.loads(body)) if callable(self.reply) else self.reply
        return self.status, json.dumps(data).encode() if not isinstance(data, bytes) else data


def client(transport, **kw):
    return iv.VisionClient(api_key=KEY, model="test-model", transport=transport, **kw)


def hand_reply(signal="thumbs_up", p=0.9):
    probs = {k: (p if k == signal else (1 - p) / (len(iv.HAND_SIGNALS) - 1)) for k in iv.HAND_SIGNALS}
    return {"model": "m", "answers": {"signal": {"type": "choice", "choice": signal, "confidence": 0.88,
                                                 "probabilities": probs}}}


def test_request_shape_endpoint_header_and_single_referenced_asset():
    t = FakeTransport(hand_reply())
    out = iv.classify_hand_signal(png(640, 480), client(t))
    url, body, headers, timeout = t.calls[0]
    assert url == iv.ENDPOINT == "https://api.typesafe.ai/v1/systemone"
    assert headers[iv.IMAGES_HEADER] == "true" and headers["Authorization"] == f"Bearer {KEY}"
    assert body["model"] == "test-model"
    (ref, asset), = body["assets"].items()
    assert body["state"] == {"frame": ref}
    assert asset["type"] == "image" and asset["format"] == "jpg"
    jpeg = base64.b64decode(asset["data_base64"], validate=True)
    assert jpeg[:2] == b"\xff\xd8"  # a JPEG
    assert set(body["questions"]) == {"signal"}
    assert body["questions"]["signal"]["criteria"] == iv.HAND_SIGNALS
    assert out["signal"] == "thumbs_up" and out["probability"] == pytest.approx(0.9) and out["latency_ms"] >= 0


@pytest.mark.parametrize("size,expect_max,expect_min", [((4000, 3000), 768, 280), ((200, 150), None, 280),
                                                        ((640, 480), 768, 280)])
def test_prepare_jpeg_fits_the_api_limits(size, expect_max, expect_min):
    w, h, fmt = image_size(iv.prepare_jpeg(png(*size)))
    assert fmt == "JPEG"
    assert min(w, h) >= expect_min and max(w, h) <= (expect_max or iv.MAX_SIDE)


def test_prepare_jpeg_refuses_non_images_and_impossible_shapes():
    with pytest.raises(iv.VisionError):
        iv.prepare_jpeg(b"not an image")
    with pytest.raises(iv.VisionError):
        iv.prepare_jpeg(png(4000, 50))  # growing the short side to 280 would break the long-side limit


@pytest.mark.parametrize("reply,status", [
    ({"answers": {}}, 200),
    ({"answers": {"signal": {"type": "noul", "noul": 0.5}}}, 200),
    ({"answers": {"signal": {"type": "choice", "choice": "dance", "confidence": 1, "probabilities": {"dance": 1}}}}, 200),
    ({"answers": {"signal": {"type": "choice", "choice": "dance", "confidence": 1,
                             "probabilities": {k: 0.125 for k in iv.HAND_SIGNALS}}}}, 200),
    (b"<html>gateway</html>", 200),
    ({"error": "bad"}, 400),
    ({"error": "nope"}, 500),
])
def test_unusable_replies_fail_closed(reply, status):
    with pytest.raises(iv.VisionError):
        iv.classify_hand_signal(png(640, 480), client(FakeTransport(reply, status)))


def test_network_errors_fail_closed_and_never_leak_the_key():
    t = FakeTransport(raises=OSError(f"connect failed for Bearer {KEY}"))
    with pytest.raises(iv.VisionError) as e:
        iv.classify_hand_signal(png(640, 480), client(t))
    assert KEY not in str(e.value)
    assert KEY not in repr(client(t))


def test_missing_configuration_is_a_config_error():
    with pytest.raises(iv.VisionConfigError):
        iv.VisionClient.from_env({"TYPESAFE_API_KEY": "", "TYPESAFE_INTENT_MODEL": "m"})
    with pytest.raises(iv.VisionConfigError):
        iv.VisionClient.from_env({"TYPESAFE_API_KEY": "k"})


def test_scene_check_reads_every_answer():
    def reply(body):
        assert set(body["questions"]) == {"person_present", "person_on_floor", "asking_for_help", "posture"}
        posture = {k: 0.0 for k in iv.POSTURES}
        posture["lying_on_floor"] = 1.0
        return {"model": "m", "answers": {
            "person_present": {"type": "noul", "noul": 0.99}, "person_on_floor": {"type": "noul", "noul": 0.9},
            "asking_for_help": {"type": "noul", "noul": 0.1},
            "posture": {"type": "choice", "choice": "lying_on_floor", "confidence": 1.0, "probabilities": posture}}}
    out = iv.scene_check(png(640, 480), client(FakeTransport(reply)))
    assert out["person_on_floor"] == 0.9 and out["posture"] == "lying_on_floor"


@pytest.mark.parametrize("present,expect_box", [(0.94, True), (0.16, False)])
def test_find_object_returns_a_box_only_when_the_object_is_there(present, expect_box):
    reply = {"model": "m", "answers": {"present": {"type": "noul", "noul": present},
                                       "where": {"type": "box", "confidence": 0.9,
                                                 "box": {"top": 0.26, "bottom": 0.73, "left": 0.2, "right": 0.92}}}}
    out = iv.find_object(png(640, 480), "pill bottle", client(FakeTransport(reply)))
    assert (out["box"] is not None) is expect_box


def test_find_object_rejects_out_of_range_box_edges():
    reply = {"model": "m", "answers": {"present": {"type": "noul", "noul": 0.9},
                                       "where": {"type": "box", "box": {"top": -1, "bottom": 2, "left": 0, "right": 1}}}}
    with pytest.raises(iv.VisionError):
        iv.find_object(png(640, 480), "cup", client(FakeTransport(reply)))


def test_error_replies_never_echo_the_body_or_the_key():
    t = FakeTransport({"error": f"bad token Bearer {KEY}"}, status=401)
    with pytest.raises(iv.VisionError) as e:
        iv.classify_hand_signal(png(640, 480), client(t))
    assert str(e.value) == "HTTP 401" and KEY not in str(e.value)


def _choice_reply(**overrides):
    answer = {"type": "choice", "choice": "fist", "confidence": 0.9,
              "probabilities": {k: (0.93 if k == "fist" else 0.01) for k in iv.HAND_SIGNALS}}
    answer.update(overrides)
    return {"model": "m", "answers": {"signal": answer}}


@pytest.mark.parametrize("overrides", [
    {"confidence": 1.5}, {"confidence": -0.1}, {"confidence": None}, {"confidence": "0.9"}, {"confidence": True},
    {"confidence": float("nan")},
    {"probabilities": {k: (None if k == "fist" else 0.01) for k in iv.HAND_SIGNALS}},
    {"probabilities": {k: ("0.5" if k == "fist" else 0.01) for k in iv.HAND_SIGNALS}},
    {"probabilities": {k: (2.0 if k == "fist" else 0.01) for k in iv.HAND_SIGNALS}},
    {"choice": 3},
    {"choice": ["fist"]},
])
def test_out_of_range_or_wrong_type_values_fail_closed(overrides):
    with pytest.raises(iv.VisionError):
        iv.classify_hand_signal(png(640, 480), client(FakeTransport(_choice_reply(**overrides))))


@pytest.mark.parametrize("box,conf", [({"top": 0.8, "bottom": 0.2, "left": 0.1, "right": 0.9}, 0.9),
                                      ({"top": 0.1, "bottom": 0.2, "left": 0.1, "right": 0.9}, 1.7),
                                      ({"top": 0.1, "bottom": 0.2, "left": 0.1}, 0.9)])
def test_malformed_boxes_fail_closed(box, conf):
    reply = {"model": "m", "answers": {"present": {"type": "noul", "noul": 0.9},
                                       "where": {"type": "box", "confidence": conf, "box": box}}}
    with pytest.raises(iv.VisionError):
        iv.find_object(png(640, 480), "cup", client(FakeTransport(reply)))


def _serve(handler_body):
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            handler_body(self)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/"


def test_the_deadline_covers_the_whole_exchange_even_when_the_server_drips():
    import time

    def drip(h):
        h.send_response(200)
        h.send_header("Content-Length", "100")
        h.end_headers()
        for _ in range(100):  # one byte every 0.1 s: each read is quick, the whole reply takes 10 s
            try:
                h.wfile.write(b"x")
                h.wfile.flush()
            except OSError:
                return
            time.sleep(0.1)

    srv, url = _serve(drip)
    try:
        t0 = time.monotonic()
        with pytest.raises(iv.VisionError, match="deadline"):
            iv._default_transport(url, b"{}", {"Content-Type": "application/json"}, 1.0)
        assert time.monotonic() - t0 < 1.6
    finally:
        srv.shutdown()


def test_the_default_transport_returns_a_normal_reply():
    def ok(h):
        data = b'{"answers": {}}'
        h.send_response(200)
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)

    srv, url = _serve(ok)
    try:
        assert iv._default_transport(url, b"{}", {}, 3.0) == (200, b'{"answers": {}}')
    finally:
        srv.shutdown()


@pytest.mark.parametrize("bad", [-0.1, 1.5, float("nan"), True, "0.5", None])
def test_find_object_refuses_a_nonsense_threshold_before_asking(bad):
    t = FakeTransport({"answers": {}})
    with pytest.raises(ValueError):
        iv.find_object(png(640, 480), "cup", client(t), min_present=bad)
    assert t.calls == []


def test_a_very_narrow_image_meets_the_minimum_without_passing_the_api_limit():
    w, h, _ = image_size(iv.prepare_jpeg(png(1600, 300)))
    assert min(w, h) >= iv.MIN_SIDE and max(w, h) <= iv.MAX_SIDE


def test_the_opencv_path_works_and_fails_closed_without_pillow(monkeypatch):
    pytest.importorskip("cv2")
    monkeypatch.setitem(sys.modules, "PIL", None)  # "from PIL import Image" now raises ImportError
    w, h, fmt = image_size(iv.prepare_jpeg(png(4000, 3000)))
    assert fmt == "JPEG" and max(w, h) <= iv.DEFAULT_SIDE and min(w, h) >= iv.MIN_SIDE
    for bad in (b"", b"not an image"):
        with pytest.raises(iv.VisionError):
            iv.prepare_jpeg(bad)


def _cli(*args, env_extra=None):
    import os
    env = {k: v for k, v in os.environ.items() if not k.startswith("TYPESAFE_")}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(REPO / "scripts" / "vision_ask.py"), *args],
                          capture_output=True, text=True, env=env, timeout=60)


def _vision_ask():
    """scripts/vision_ask.py as a module, for the tests that swap one of its parts."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("vision_ask", REPO / "scripts" / "vision_ask.py")
    va = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(va)
    return va


def test_the_cli_exits_nonzero_when_a_file_fails_and_two_without_configuration(tmp_path):
    missing = str(tmp_path / "no-such-photo.jpg")
    r = _cli("hand", "--image", missing, env_extra={"TYPESAFE_API_KEY": KEY, "TYPESAFE_INTENT_MODEL": "m"})
    assert r.returncode == 1 and '"error"' in r.stdout and KEY not in r.stdout + r.stderr
    r = _cli("hand", "--image", missing)
    assert r.returncode == 2


@pytest.mark.parametrize("flag,value", [("--every", "nan"), ("--every", "inf"), ("--every", "-1"),
                                        ("--every", "3601"), ("--timeout", "nan"), ("--timeout", "0"),
                                        ("--timeout", "16")])
def test_the_cli_refuses_an_interval_or_timeout_that_is_not_a_sane_number_of_seconds(flag, value):
    # luna round 6: `--every nan` passed `every <= 0` and slept 0 s, so the live loop called the paid API back to back
    r = _cli("hand", "--robot", "http://robot.invalid:8200", flag, value,
             env_extra={"TYPESAFE_API_KEY": KEY, "TYPESAFE_INTENT_MODEL": "m"})
    assert r.returncode == 2 and flag in r.stderr and r.stdout == ""


def test_the_cli_takes_zero_as_one_frame_and_a_fraction_as_an_interval():
    va = _vision_ask()
    every, timeout = va._seconds(0.0, 3600.0, low_ok=True), va._seconds(0.0, iv.MAX_TIMEOUT_S, low_ok=False)
    assert every("0") == 0.0 and every("0.5") == 0.5 and every("3600") == 3600.0
    assert timeout("4") == 4.0 and timeout(str(iv.MAX_TIMEOUT_S)) == iv.MAX_TIMEOUT_S


def test_a_truncated_robot_frame_is_a_failed_frame_not_a_crash(monkeypatch):
    import http.client

    va = _vision_ask()

    class Truncated:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            raise http.client.IncompleteRead(b"partial", 100)

    monkeypatch.setattr(va.urllib.request, "urlopen", lambda *a, **k: Truncated())
    with pytest.raises(iv.VisionError):
        va.robot_frame("http://robot.invalid:8200")


def _fake_cv2(opened=True, frame=(True, "a frame"), encode=(True, b"jpeg bytes")):
    """Just enough of OpenCV for the webcam path, so these tests never open a real camera."""
    import types

    class Cap:
        released = False

        def isOpened(self):
            return opened

        def read(self):
            if isinstance(frame, Exception):
                raise frame
            return frame

        def release(self):
            Cap.released = True

    cv2 = types.SimpleNamespace(IMWRITE_JPEG_QUALITY=1, error=type("error", (Exception,), {}), Cap=Cap)
    cv2.VideoCapture = lambda index: Cap()

    def imencode(ext, img, params):
        if encode == "raise":
            raise cv2.error("bad frame")
        return encode
    cv2.imencode = imencode
    return cv2


class _Encoded(bytes):
    def tobytes(self):
        return bytes(self)


@pytest.mark.parametrize("encode", [(False, None), (True, _Encoded(b"")), "raise"])
def test_a_webcam_frame_that_cannot_be_encoded_is_a_failed_frame_not_a_crash(monkeypatch, encode):
    # luna round 7: imencode's status was never checked and cv2.error is not an OSError, so the CLI died mid-loop
    fake = _fake_cv2(encode=encode)
    monkeypatch.setitem(sys.modules, "cv2", fake)
    va = _vision_ask()
    with pytest.raises(iv.VisionError):
        va.webcam_frame(fake.VideoCapture(0))


def test_a_good_webcam_frame_is_returned_as_its_jpeg_bytes(monkeypatch):
    fake = _fake_cv2(encode=(True, _Encoded(b"jpeg bytes")))
    monkeypatch.setitem(sys.modules, "cv2", fake)
    assert _vision_ask().webcam_frame(fake.VideoCapture(0)) == b"jpeg bytes"


@pytest.mark.parametrize("cv2_module,expect", [(None, "needs OpenCV"), ("closed", "did not open"),
                                               ("unencodable", "could not be encoded")])
def test_the_cli_prints_one_error_line_and_exits_1_when_the_webcam_fails(monkeypatch, capsys, cv2_module, expect):
    if cv2_module == "closed":
        cv2_module = _fake_cv2(opened=False)
    elif cv2_module == "unencodable":
        cv2_module = _fake_cv2(encode=(False, None))
    monkeypatch.setitem(sys.modules, "cv2", cv2_module)  # None makes `import cv2` raise ImportError
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)
    monkeypatch.setenv("TYPESAFE_INTENT_MODEL", "m")
    monkeypatch.setattr(sys, "argv", ["vision_ask.py", "hand", "--webcam", "0"])
    va = _vision_ask()
    assert va.main() == 1
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1 and expect in json.loads(lines[0])["error"] and KEY not in lines[0]


@pytest.mark.parametrize("pillow", [True, False])
def test_transparent_pixels_are_sent_as_white_never_as_their_hidden_colour(monkeypatch, pillow):
    # luna round 8: convert("RGB") / IMREAD_COLOR dropped the alpha, so a fully transparent PNG whose hidden pixels
    # are red reached the model as a red image
    if not pillow:
        pytest.importorskip("cv2")
        monkeypatch.setitem(sys.modules, "PIL", None)
    r, g, b = mean_rgb(iv.prepare_jpeg(png(400, 300, (255, 0, 0, 0))))
    assert min(r, g, b) > 245, (r, g, b)
    r, g, b = mean_rgb(iv.prepare_jpeg(png(400, 300, (255, 0, 0, 255))))  # opaque red stays red
    assert r > 200 and g < 60 and b < 60, (r, g, b)
    r, g, b = mean_rgb(iv.prepare_jpeg(png(400, 300, (255, 0, 0, 128))))  # half transparent: pink on white
    assert r > 230 and 100 < g < 160 and 100 < b < 160, (r, g, b)


@pytest.mark.parametrize("url", ["", "star-pi.local:8200", "ftp://star-pi.local", "http://"])
def test_the_cli_refuses_a_robot_address_that_is_not_an_http_url(url):
    # luna round 8: --robot "" satisfied the required source group, read as false, and fell through to the webcam
    r = _cli("hand", "--robot", url, env_extra={"TYPESAFE_API_KEY": KEY, "TYPESAFE_INTENT_MODEL": "m"})
    assert r.returncode == 2 and "--robot" in r.stderr and r.stdout == ""


class _Reply:
    """A stand-in urlopen reply: hands out `chunks` one read at a time (then b""), sleeping `pause` s before each."""

    def __init__(self, chunks, pause=0.0):
        self.chunks, self.pause = iter(chunks), pause

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        import time
        time.sleep(self.pause)
        return next(self.chunks, b"")


def test_a_robot_frame_is_read_whole_and_returned(monkeypatch):
    va = _vision_ask()
    monkeypatch.setattr(va.urllib.request, "urlopen", lambda *a, **k: _Reply([b"jpeg ", b"bytes"]))
    assert va.robot_frame("http://robot.invalid:8200") == b"jpeg bytes"


def test_a_robot_server_that_drips_bytes_is_a_failed_frame_at_the_deadline(monkeypatch):
    # luna round 9: urlopen's timeout bounds each read, not the reply, so a dripping server hung a one-shot run
    import itertools
    import time

    va = _vision_ask()
    monkeypatch.setattr(va.urllib.request, "urlopen", lambda *a, **k: _Reply(itertools.repeat(b"x"), pause=0.05))
    started = time.monotonic()
    with pytest.raises(iv.VisionError, match="deadline"):
        va.robot_frame("http://robot.invalid:8200", deadline_s=0.4)
    assert time.monotonic() - started < 2.0


def test_an_abandoned_robot_fetch_lets_go_of_a_real_dripping_server():
    # luna round 10: read(64 KiB) waits for 64 KiB, so after the deadline the abandoned worker kept reading a server
    # that drips a byte at a time (in a live loop, one leaked socket per frame). The server here notices the client
    # hanging up: a send fails once the fetch has let go.
    import socket
    import threading
    import time

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    released = threading.Event()

    def drip():
        conn, _ = srv.accept()
        with conn:
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: 100000\r\n\r\n")
            try:
                for _ in range(400):  # 20 s at most
                    conn.sendall(b"x")
                    time.sleep(0.05)
            except OSError:
                released.set()

    threading.Thread(target=drip, daemon=True).start()
    va = _vision_ask()
    with pytest.raises(iv.VisionError, match="deadline"):
        va.robot_frame(f"http://127.0.0.1:{srv.getsockname()[1]}", deadline_s=0.5)
    assert released.wait(5.0), "the abandoned fetch kept reading the dripping server"
    srv.close()


def test_a_robot_frame_over_the_size_cap_is_a_failed_frame(monkeypatch):
    import itertools

    va = _vision_ask()
    monkeypatch.setattr(va.urllib.request, "urlopen", lambda *a, **k: _Reply(itertools.repeat(b"x" * 65536)))
    with pytest.raises(iv.VisionError, match="larger than"):
        va.robot_frame("http://robot.invalid:8200", max_bytes=200_000)


@pytest.mark.parametrize("pillow", [True, False])
def test_a_phone_photo_is_sent_the_right_way_up(monkeypatch, pillow):
    # luna round 9: the Pillow path re-encoded without applying the EXIF orientation, so a photo stored sideways
    # (as phones store portrait shots) reached the model sideways; OpenCV's decode already applies it
    Image = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    exif = Image.Exif()
    exif[0x0112] = 6  # EXIF orientation 6: turn 90 degrees clockwise to view
    Image.new("RGB", (600, 300), (10, 120, 200)).save(buf, "JPEG", exif=exif)
    if not pillow:
        pytest.importorskip("cv2")
        monkeypatch.setitem(sys.modules, "PIL", None)
    w, h, fmt = image_size(iv.prepare_jpeg(buf.getvalue()))
    assert fmt == "JPEG" and h > w, (w, h)
