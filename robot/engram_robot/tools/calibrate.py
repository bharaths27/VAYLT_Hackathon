"""R04: calibrate the camera focal length and grasp standoff against a running adapter.

1. Put a tag at a tape-measured distance straight ahead (camera lens -> tag face), e.g. 0.50 m.
2. python tools/calibrate.py focal --tag 7 --true-m 0.50
   -> prints focal_px_override; put it in calib.json and restart the server.
3. Hand-place the object where the claw grasps it reliably (claw open, centred), then:
   python tools/calibrate.py standoff --tag 7
   -> prints grasp_standoff_m for calib.json.
4. For the safety envelope (Deniz): see README "Deniz: stop test".
"""
import argparse
import json
import statistics
import urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("mode", choices=["focal", "standoff"])
ap.add_argument("--tag", type=int, required=True)
ap.add_argument("--true-m", type=float)
ap.add_argument("--url", default="http://localhost:8200")
ap.add_argument("--n", type=int, default=20)
a = ap.parse_args()


def observe():
    import time
    body = json.dumps({"op": "observe", "args": {}, "operation_id": f"calib-{time.time_ns()}"}).encode()
    req = urllib.request.Request(a.url + "/robot/execute", body, {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req))


caps = json.load(urllib.request.urlopen(a.url + "/robot/capabilities"))
dists, sides = [], []
for _ in range(a.n):
    for t in observe()["data"]["tags"]:
        if t["tag_id"] == a.tag:
            dists.append(t["distance_m"])
            sides.append(t["side_px"])
if len(dists) < a.n // 2:
    raise SystemExit(f"tag {a.tag} seen in only {len(dists)}/{a.n} frames; fix lighting/position")
d, s = statistics.median(dists), statistics.median(sides)
print(f"tag {a.tag}: median distance {d:.4f} m (spread {max(dists) - min(dists):.4f}), side {s:.1f}px")
if a.mode == "focal":
    if not a.true_m:
        raise SystemExit("--true-m required")
    # current estimate d = f * size / side  ->  f_true = true_m * side / size
    # estimated distance scales linearly with focal length, so f_true = f_current * true / estimated
    f_current = caps["camera"]["focal_px"]
    print("put this in calib.json:")
    print(json.dumps({"focal_px_override": round(f_current * a.true_m / d, 1)}))
else:
    print(json.dumps({"grasp_standoff_m": round(d, 3)}))
