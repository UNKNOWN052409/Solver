# NeoSolver CAPTCHA Class Taxonomy & Real-Dataset Analysis

**Scope:** What each *real* CAPTCHA family in `data/real_captchas/` actually asks the human, what image it shows, and how to convert that solve task into a trainable classification / regression target for `solver/vision/`.
**Grounding:** every fact below was measured from the real files on disk (`cv2`), not assumed. See the per-family sections for the measured dims / color stats and the exact label sources (`tools/a3_measure_before.py` GT dict, rot filename angle, slider pixel-offset, hcaptcha tile class).

---

## 0. Dataset inventory (REAL, measured)

| Family | Dir | Count | Dims | Channels | Content type | Prompt (what it asks) |
|---|---|---|---|---|---|---|
| `text/map` | `data/real_captchas/grid/` (`map_*.png`) | 20 | 128×128 | RGBA (4) | **Text** — 5-char alphanumeric string on a map/scene background | "type the characters you see" |
| `rotation` | `data/real_captchas/rot/` (`*_rot<angle>_*.png`) | 18 | 152×152 | RGB (3) | **Abstract/scene** — a photo rotated away from upright | "rotate the image so it is upright" |
| `slider` | `data/real_captchas/slider/` (`challenge.png`, `panel.png`) | 2 | 960×540 / 960×660 | RGB (3) | **Object/scene** — a scene with a missing puzzle gap + drag piece | "drag the slider so the piece fills the gap" |
| `hcaptcha` | `data/real_captchas/hcaptcha/` (`1..4.jpg`) | 4 | 128×128 | RGB (3) | **Object tile** — independent photographs of a target object class | "select all images that contain [object]" |

**Total real files on disk: 44** (20 + 18 + 2 + 4). The task's "40 real images" scope maps to **grid+rot+slider = 40**; hCaptcha (4) is a bonus family present in the tree.

---

## 1. `text` / `map` family — distorted-text (grid dir)

**Prompt:** "Type the characters you see." The user reads a short alphanumeric string and enters all 5 characters in order.
**Image:** 128×128, 4 channels (RGBA). A map/scene background overlaid with a 5-character distorted text string. Anti-OCR noise: per-character rotation/position jitter, connected/touching glyphs, low-moderate color/saturation contrast.

**Measured color complexity (20 images):**
- unique colors (5-bit bucketed): 27–69, mean≈**51**
- mean saturation (HSV): 13–102, mean≈**46**
- gray std: 11–37, mean≈**27**
- edge fraction: 0.08–0.32, mean≈**0.19**

**Label / target:** 5-character string from a 24-char alphabet `3479ACDEFHJKLMNPQRTUVWXY` (**not** the full A–Z0–9 set — only those 24 letters+digits occur). GT is hardcoded in `tools/a3_measure_before.py` (and mirrored in `solver/vision/train_map_ocr.py`).

**Classification conversion → 5 multi-head classes:** build a CNN with one shared conv backbone + **5 parallel softmax heads**, each 24-way (one class per alphabet char per position). Input: grayscale 128×128 normalized /255.

```python
# target: per-position class id, e.g. label "4KTN9" -> idxs [CHAR2IDX['4'], ...]
ALPHABET = "3479ACDEFHJKLMNPQRTUVWXY"      # 24 classes
NUM_CHARS = 5                               # 5 heads -> classification_targets = 5*24 (per-img: 5)
```

The existing `solver/vision/map_cnn.py` + `train_map_ocr.py` already implement exactly this.

---

## 2. `rotation` family (rot dir)

**Prompt:** "Rotate the image to upright." The user must rotate the displayed photo back to its correct (upright) orientation.
**Image:** 152×152, 3 channels (RGB). A real photo (scene/subject) rotated by an arbitrary angle away from upright.

**Measured (18 images):**
- unique colors (bucketed): 358–3227, mean≈**1530** (photos, so much higher color complexity than text)
- mean saturation (HSV): 9–112, mean≈**74**
- gray std: 70–91, mean≈**81** (broad dynamic range — full photo)
- edge fraction: 0.29–0.67, mean≈**0.50**

**Label / target:** the rotation angle is encoded in the filename `<idx>_rot<angle>_00_<idx>.png`, e.g. `100_rot60_00_100.png` is image index 100 currently rotated **60°**. 18 distinct angles measured: {7,60,62,82,85,86,92,99,136,144,146,161,207,231,255,276,284,300}.

**Classification conversion → 360-way (or binned) angle class:** predict current rotation θ ∈ [0,360). Class target = round(angle) for a 360-way classifier, or a coarser bin (e.g. 8×45° or 24×15°) for stability. The "answer" is −θ (or 360−θ) = degrees to rotate back to upright.

```python
# per image: target class = int(angle_from_filename)  # e.g. 100_rot60 -> 60
# 360-way: classification_targets = 360 ;  or 8-way (45° bins): 8
```
`RotNet`-style backbone (ResNet-ish conv stack + single 360-way softmax) in `solver/vision/model.py`.

---

## 3. `slider` family

**Prompt:** "Drag the slider so the puzzle piece fills the gap." The user drags a button horizontally until a missing fragment snaps into its cut-out in the background scene.
**Image (2 files):**
- `challenge.png`: 960×540 RGB — the background scene **with** the missing-gap fragment cut out (high saturation scene, 3928 unique bucketed colors).
- `panel.png`: 960×660 RGB — a larger control panel (grab handle / track region, 700 unique bucketed colors, low saturation ≈22 — mostly neutral chrome/panel).

**Label / target:** the answer is the **horizontal pixel offset** `x` where the piece's silhouette matches the gap. Measured gap detection = `cv2.matchTemplate(challenge_edges, piece_edges)` → `max_loc[0]`.

**Classification conversion → regression (not classification):** the solution is a continuous x-offset; the natural target is **regression** (Smooth-L1 on normalized x), not a class. For the 2 real samples there is no stored per-sample GT offset, so the dataset script leaves the slider family un-labeled for classification (it is CV-solved with template matching, per `docs/IMAGECAPTCHA_TYPES.md` §4).

---

## 4. `hcaptcha` family (object tiles)

**Prompt:** "Select all images that contain [object]." Each tile is an independent photo; select every tile matching the prompt noun (bus, motorcycle, boat, etc.).
**Image:** 128×128 RGB, real-world photographs. Measured: 870–1901 unique bucketed colors, saturation 38–64, edge fraction 0.58–0.69 (dense detail).

**Label / target:** one class per object category. Upstream source = `drandule/hcaptcha_dataset`, categories {airplane, bicycle, boat, motorbus, motorcycle, seaplane, train, truck} (+fire hydrant / crosswalk / staircase upstream). The 4 local files were flattened to `1..4.jpg` losing their category filename, so their exact class is not recoverable from disk — **classification target = per-tile object class** over the vocabulary.

```python
# target: per tile = the object class id in the prompt vocabulary
VOCAB = ["airplane","bicycle","boat","motorbus","motorcycle","seaplane","train","truck", ...]
```

---

## 5. Family → classification-target summary

| Family | What it asks | Image | Target shape | class count |
|---|---|---|---|---|
| `text` (map/grid) | type 5 chars | 128×128 distorted text on map | 5 × 24-way softmax heads | 24 (per head) |
| `rotation` | rotate to upright | 152×152 photo at angle θ | single 360-way softmax (or binned) | 360 (or 8/24) |
| `slider` | drag piece into gap | 960×W scene + panel | regression x-offset (no class) | — |
| `hcaptcha` | select all [object] tiles | 128×128 object tile | per-tile object class | N_categories |

**Classification targets currently derivable from real labels:** text → 24 classes × 5 heads; rotation → 360 angle classes (18 real samples have distinct exact angles). Slider is regression, hCaptcha classes are recoverable only if re-fetched with category filenames.

---

## 6. Notes / caveats
- **Content type mix:** the repo holds *text*, *scene/abstract* (rotation), *object/scene* (slider), and *object-tile* (hCaptcha) — not a single modality. The dataset builder must route per-family (they cannot share a classifier head).
- **RGB(A) nuance:** grid images are RGBA but the alpha channel carries no label signal; classifiers consume the BGR composite (as `train_map_ocr.py` does).
- All counts in this file are **real, measured** from `data/real_captchas/` — nothing synthetic.
