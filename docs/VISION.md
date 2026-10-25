# Vision with the intent model

`engram/vision/intent_vision.py` asks TypeSafe's intent model typed questions about **one camera frame**:

| Question | Function | Answer |
|---|---|---|
| Which hand signal is shown? | `classify_hand_signal(jpeg)` | one of `open_palm`, `thumbs_up`, `thumbs_down`, `pointing`, `fist`, `peace`, `waving`, `none`, with its probability |
| Is someone there, on the floor, asking for help? What is their posture? | `scene_check(jpeg)` | three yes-probabilities and a posture (`standing`, `sitting`, `lying_in_bed`, `lying_on_floor`, `no_person`) |
| Is an object in view, and where? | `find_object(jpeg, "pill bottle")` | a presence probability and, only when present, a box as fractions of the image (`top`, `bottom`, `left`, `right`) |

The answers are **advice about one still frame**. They never move the robot, approve a plan or count as a
sensor reading; code decides what to do with them, through the brain's normal stop / propose / approve paths.
Images are a preview feature on TypeSafe's side (fine for prototypes; format and accuracy can change), so
check it on your own camera before you rely on a threshold.

## Set up (once per terminal)

```bash
read -rsp 'TypeSafe key: ' TYPESAFE_API_KEY; echo; export TYPESAFE_API_KEY
export TYPESAFE_INTENT_MODEL=<model id from the TypeSafe console>
```

The key is never printed or logged. It needs `httpx` (in `requirements.txt`) and Pillow or OpenCV
(`pip install pillow` if neither is installed).

## Try it

```bash
python scripts/vision_ask.py hand  --image photo.jpg
python scripts/vision_ask.py hand  --robot http://star-pi.local:8200 --every 1    # the robot's C920, once a second
python scripts/vision_ask.py hand  --webcam 0                                    # a laptop camera
python scripts/vision_ask.py scene --robot http://star-pi.local:8200
python scripts/vision_ask.py find  --what "pill bottle" --robot http://star-pi.local:8200
```

Each frame prints one JSON line, for example
`{"source": "...", "signal": "thumbs_up", "probability": 0.93, "confidence": 0.92, "latency_ms": 412, "model": "..."}`.
Stop a live run with Ctrl+C.

## Measured on the warden's PC (2026-10-25, public test photos, not the C920)

- **Speed:** 0.32 to 0.61 s per frame, end to end over the internet, frames resized to 768 px.
- **Hand signals:** 10 of 11 photos right (fist, open palm, peace, thumbs up, no signal). The miss was a stone
  sculpture of a pointing hand, read as a thumbs up. A wave and an open palm look the same in one still frame;
  both read as `open_palm`.
- **Care-room scene:** 6 of 6 right: a person lying on a sidewalk (on the floor, 0.88), two people standing, a
  patient in an ICU bed, an empty room (person 0.07), a person in a wheelchair (sitting), and a person waving
  (asking for help, 0.95).
- **Find an object:** a mug boxed correctly (present 0.94); no mug in an empty room (0.16); no pill bottle in the
  mug photo (0.02).

Not measured yet: the C920 on the robot, the Pi's network, hands at the robot's distance and lighting. Run
`vision_ask.py hand --robot ...` with each signal you plan to use and write down what it reads before the demo.
