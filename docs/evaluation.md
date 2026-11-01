# Model evaluation

This records what has been measured, what hasn't, and how the default settings were chosen.

## Recognition accuracy

**Dataset:** [LFW](https://vis-www.cs.umass.edu/lfw/) funneled images, using the standard protocol of 6,000 pairs in 10 folds (3,000 same-person and 3,000 different-person pairs). Every image had a detectable face.

**Model:** SFace (`face_recognition_sface_2021dec.onnx`), with YuNet detection, on OpenCV 5.0.

**Result:** 99.40% ± 0.38% accuracy.

| Threshold | False accepts | False rejects | Used as |
|---|---|---|---|
| 0.300 | 0.73% | 0.60% | |
| 0.363 | 0.10% | 1.47% | OpenCV's published default |
| **0.400** | **0.03%** | **2.37%** | **`match_threshold` (verify)** |
| **0.450** | **0% observed** | **4.30%** | **`identify_threshold`** |
| 0.500 | 0% observed | 7.60% | |
| 0.550 | 0% observed | 15.30% | |
| 0.600 | 0% observed | 26.43% | |

"0% observed" means none of the 3,000 different-person pairs matched. That only suggests the true rate is below about 0.1%.

To reproduce these figures, run [scripts/evaluate_lfw.py](../scripts/evaluate_lfw.py).

### Limits of these numbers

- **LFW isn't your users.** It holds press photos, mostly of public figures, and its demographics are skewed. Selfies, webcams and low light will perform differently.
- **No per-group results yet.** Error rates can differ across skin tone, age and gender, and this hasn't been measured for FacePass.
- **Identify risk grows with enrollment.** Each search compares against everyone, so a low pairwise false accept rate adds up. With 10,000 enrolled people, even a 0.01% pairwise rate means about one expected false candidate per unknown person. Test with your real gallery size before relying on identify.

**Before launch,** collect a consented test set from your real devices and users, then rerun this evaluation and report results by group.

## Liveness

**Status: not validated.** The MiniFASNet models load and return scores, but no real spoof data was available, so nothing here shows how well they stop attacks. Treat `liveness_threshold = 0.5` as a placeholder.

The only measurement is the score spread on LFW, which contains photographs rather than live captures: 1st percentile 0.01, 5th percentile 0.30, median 0.99. Scanned NASA portraits scored between 0.55 and 0.999, so **these photographs passed the check at the default threshold.** Digital image files sent directly aren't what this model was trained to catch, and it can't be relied on to stop them. A real camera recapture may behave differently, but that hasn't been tested.

**Before relying on liveness,** capture real faces, printed photos and screen replays on your target phones and webcams. Then measure both error rates and set the threshold from those results. For higher assurance, consider active challenges or a certified liveness vendor. Also note that a client can upload any file, so this API can't prove a photo came from a camera.

## Quality gates

Default values, compared with the distributions on LFW (face crop resized to 112×112 for sharpness and brightness):

| Setting | Default | 1st / 50th / 99th percentile on LFW |
|---|---|---|
| `min_detection_score` | 0.80 | 0.90 / 0.93 / 0.95 |
| `min_face_size` | 64 px | 78 / 96 / 122 px |
| `min_sharpness` | 15 | 22.5 / 84 / 309 |
| `min_brightness`, `max_brightness` | 40–220 | 74 / 127 / 177 |

These are set to reject clearly unusable images without blocking normal ones. They haven't been tuned on phone or webcam captures.

## After changing the model

Embeddings from different models can't be compared, and every template stores its `model_id`, so existing users must re-enroll. A model with a different embedding size also needs a migration. Rerun this evaluation and set new thresholds.
