"""engram/vision/intent_vision.py without the network: request shape, image sizing, answer parsing, fail-closed."""

import base64
import io
import json

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from engram.vision import intent_vision as iv  # noqa: E402

KEY = "dummy-api-key-for-tests"  # not a real key; asserts below check it never leaks


def png(w, h, color=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


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
    out = Image.open(io.BytesIO(iv.prepare_jpeg(png(*size))))
    assert out.format == "JPEG"
    assert min(out.size) >= expect_min and max(out.size) <= (expect_max or iv.MAX_SIDE)


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
