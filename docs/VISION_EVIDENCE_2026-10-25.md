# Vision with the intent model: the 2026-10-25 measurement

The record behind the numbers in `docs/VISION.md`. One run per photo, nothing repeated or averaged.

## Method

- **When and where:** 2026-10-25, about 09:40 to 09:50 UTC, from the warden's PC (Windows, home broadband) to
  TypeSafe's hosted API. The robot, the Pi and the C920 were not involved.
- **Model:** the version the API reported in every reply was 1.14.0 (`TYPESAFE_INTENT_MODEL` set to the
  provider's latest alias for the run).
- **Code:** this branch's `engram/vision/intent_vision.py` through `scripts/vision_ask.py` (`hand`, `scene` and
  `find` modes), default settings: each photo re-encoded as JPEG with its longest side at 768 px.
- **Timing:** `latency_ms` is wall-clock time measured inside `VisionClient.ask`, from just before the request is
  sent until the reply is read. It includes the upload, not the local resize.
- **Ground truth:** a person looked at each photo and wrote down what it shows before reading the model's answer
  as right or wrong. The photo's search label was not trusted: two of them were wrong (marked below).
- **Photos:** Wikimedia Commons, downloaded at 800 or 1024 px wide. They are listed, not included in the repo.
  Open one with `https://commons.wikimedia.org/wiki/<title>`.

## Hand signals (`vision_ask.py hand`)

| Photo (Commons title) | What it shows | Answer | p | ms | Right? |
|---|---|---|---|---|---|
| File:Clenched fist.jpg | a clenched fist | fist | 0.65 | 593 | yes |
| File:YellowFist.png | a raised-fist icon | fist | 0.97 | 324 | yes |
| File:St Margaret holding a palm in her raised left hand, a dragon at her right ... (engraving) | no hand signal (search said open palm) | none | 0.97 | 482 | yes |
| File:Osogbo Ogun Festival 2026 (Open Hand, Open Blessing).jpg | a raised open hand | open_palm | 0.98 | 355 | yes |
| File:Havana - Cuba - Man giving a V sign - 1326.jpg | a V sign | peace | 0.99 | 346 | yes |
| File:Mrs. Constance B. Motley, first woman Senator, 21st Senatorial District, N... | a V sign | peace | 0.96 | 583 | yes |
| File:The Big Index Finger - Flickr - mikecogh.jpg | a stone sculpture of a pointing index finger | thumbs_up | 0.80 | 541 | **no** |
| File:Finger-counting to 15.png | an open hand, palm to the camera (search said pointing) | open_palm | 0.98 | 412 | yes |
| File:Thumbs up floral arrangement.png | a thumbs up made of flowers | thumbs_up | 0.93 | 412 | yes |
| File:Thumb up.JPG | a thumbs up | thumbs_up | 1.00 | 328 | yes |
| File:BIBI 2024 TV10 waving hand 0.png | a person waving with an open palm | open_palm | 0.87 | 395 | see note |

**10 of 11** when a wave counts as an open palm (in one still frame they look the same), **9 of 11** if it had
to say `waving`. If a wave and a stop sign must mean different things, they need motion, not one frame.

## Care-room scene (`vision_ask.py scene`)

| Photo (Commons title) | What it shows | person | on floor | help | posture | ms | Right? |
|---|---|---|---|---|---|---|---|
| File:Lying on the sidewalk - 100 Metcalfe - 01.jpg | a person lying on a sidewalk | 0.99 | 0.88 | 0.05 | lying_on_floor | 606 | yes |
| File:Expérimentation sur les risques de chute des personnes âgées avec le LBMC.jpg | two people standing in a lab | 0.99 | 0.03 | 0.08 | standing | 358 | yes |
| File:Patient lying in hospital bed in intensive care unit in Germany in 2015.jpg | a patient in an ICU bed, a visitor beside | 0.98 | 0.02 | 0.20 | lying_in_bed | 366 | yes |
| File:Private room in the Elliot Community Hospital (2436610752).jpg | an empty hospital room | 0.07 | 0.03 | 0.05 | no_person | 424 | yes |
| File:Affection in wheelchair.jpeg | a woman in a wheelchair | 0.99 | 0.02 | 0.04 | sitting | 356 | yes |
| File:BIBI 2024 TV10 waving hand 0.png | a person waving | 0.98 | 0.01 | 0.95 | standing | 423 | yes |

## Find an object (`vision_ask.py find`)

| Photo | Asked for | present | box (top, bottom, left, right) | ms | Right? |
|---|---|---|---|---|---|
| File:Coffee mug on table.jpg | coffee mug | 0.94 | 0.26, 0.73, 0.20, 0.92 | 487 | yes: the box fits the mug |
| File:Private room in the Elliot Community Hospital (2436610752).jpg | coffee mug | 0.16 | none (not present) | 387 | yes |
| File:Coffee mug on table.jpg | pill bottle | 0.02 | none (not present) | 524 | yes |

## Limits

One run per photo; public photos, not the robot's C920; one network; no repeated trials, so no spread or
confidence interval. The speed on the Pi's network and the accuracy on the team's own hands, lighting and
distance are not measured: run `vision_ask.py hand --robot ...` for each signal before relying on it.
