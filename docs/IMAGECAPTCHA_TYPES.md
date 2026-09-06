# Image CAPTCHA Taxonomy & AI-Independent Local Solving

**Author:** NeoSolver / Solver research (R1)
**Date:** 2026-09-06
**Scope:** Full taxonomy of *image* CAPTCHA types and how each is solved **locally** (script / CPU / GPU, on-device models only). **No external AI API, no VISION_LLM_URL** — the `LO` rule. All methods described use self-contained, trainable-on-this-box models and classic CV. This document is deliberately vendor-neutral where possible but flags the specific model families that matter for each type (Deep-CAPTCHA, TileNet, RotNet, etc.).

---

## 0. Executive summary: how a local solver dispatches

A local solver does not "guess" which CAPTCHA it is facing — it **detects the modality** from the page DOM, the challenge payload, and image structure, then routes to a dedicated solver. The high-level dispatch pipeline used across every type below:

```
challenge captured (screenshot / DOM / network payload)
        │
        ▼
  detect_challenge_type(page)
        │   (iframe/class hints, tile count, presence of slider track,
        │    canvas 3D flag, audio <audio> tag, image+instruction string)
        ▼
  ┌─────────┬──────────┬──────────┬──────────┬──────────┬──────────┬─────────┐
  │ 1. Text │ 2. Grid  │ 3. Arkose│ 4. Slider│ 5. 2D    │ 6. Math  │ 7. Audio│
  │   (OCR) │  object  │   3D rot │   gap    │   puzzle │  / VQA   │  (ASR)  │
  └─────────┴──────────┴──────────┴──────────┴──────────┴──────────┴─────────┘
```

Real-world frequency (market share, sources differ — see §8): **reCAPTCHA (v2/v3) > Cloudflare Turnstile > hCaptcha > GeeTest > Arkose**. Within *challenge-bearing* (non-invisible, non-passive) providers that actually surface an image task, the practical encounter order is **reCAPTCHA v2 (grid) > hCaptcha (object tiles) > Arkose (3D rotation) > GeeTest (slider)**. That ordering is what the solver engineer should optimize first.

---

## 1. Text / Distorted-Text CAPTCHAs (Deep-CAPTCHA)

### What it looks like
A single image (typically ~200×70 px, but 240×80 / 300×100 are common) showing a short alphanumeric string — 4–6 digits/letters, rendered with classical anti-OCR defenses: random noise dots (`noise_points`), interference lines (`noise_lines`), geometric warping/twisting, per-character rotation, variable spacing, and character **overlap / connected-component touching (CCT)**. Background may be solid, gradient, or striped. The user must type the whole string in order. Classic generators: Python `ImageCaptcha`, `CaptchaMvc`, `Securimage`, reCAPTCHA v1-style, ARCaptcha, MTCaptcha single-image. This is the *smallest* part of the modern wild but remains the reference problem for CNNs.

### How a local solver detects it
- DOM: a `<img>` whose `src`/`data:image` loads a small image, plus a text `<input>` field with a placeholder like *"Type the text"* / *"Enter the characters you see."*
- Image shape: one contiguous stripe of glyphs (no tiles, no slider button, no 3D canvas).
- Model signature / endpoint is `text`/`image` (CaptchaAI) or a plain OCR-style challenge.
- Heuristic: aspect ratio >> 1 (wide), single object, character-like connected components after binarization.

### Concrete solve algorithm (AI-independent, local)
**Pipeline: preprocess → segment → classify → assemble → validate.**

1. **Preprocess**
   - Convert to grayscale.
   - **Median blur / bilateral filter** to kill salt-and-pepper noise.
   - **Otsu adaptive threshold** → binary map. For colored text on colored bg, drop the channel with maximal variance first.
   - Morphological **open** (small kernel) to strip isolated noise specks without touching glyphs.
   - Optional **deskew** (Hough line / Radon transform if strong rotation) and perspective-normalize to a fixed canvas (e.g. resize height to 60 px, preserve aspect).

2. **Segment characters** (when glyphs don't touch)
   - Find **connected components**; split the image into per-glyph crops by CC bounding boxes.
   - For strings with a *known fixed length* (most text CAPTCHAs emit a fixed count), if CC count ≠ expected length, apply **vertical projection segmentation**: sum column intensities, find valleys between characters, cut at local minima (a horizontal scanline where ink density is lowest). For **touching glyphs (CCT)**, use **water-shed / single-char regression detectors** (trained to output per-char bounding boxes) or a **CTC/RNN whole-string model** that sidesteps hard segmentation entirely.

3. **Classify each glyph** with a small CNN trained locally
   - **Deep-CAPTCHA architecture** (Noury et al., arXiv:2006.08296): 3× (Conv2D 5×5 → ReLU → 2×2 MaxPool, channels 32→48→64) → Flatten → Dense 512 ReLU + 30% dropout → **L = number-of-characters parallel Softmax heads** (input is `D×L` where D = alphabet size, e.g. 36 for A–Z0–9; using *L parallel softmaxes* instead of one giant `D^L` class set avoids combinatorial blowup). Trained with Adam @ lr 1e-4, binary-cross-entropy on the one-hot-per-position targets. Reported 98.94% (numeric) / 98.31% (alphanumeric) on its test set.
   - The repo's own `solver/vision/model.py` + `train.py` is the direct analogue: a multi-output classifier (one softmax head per character position) trained with `--synthetic` image generation (python-imagecaptcha-style renderer) so **no real corpus is needed** to bootstrap.
   - Better: a **CRNN (CNN + 2 conv + LSTM, CTC loss)** replaces the FC heads to model inter-character correlation and handles variable length for free (MDPI 2024 Adaptive-CAPTCHA reduced params ~70% and lifted accuracy >10 pts over Deep-CAPTCHA).

4. **Assemble + validate**
   - Concatenate per-position argmax classes into the candidate string.
   - Optional dictionary / checksum validation (many text CAPTCHAs have a fixed expected answer length; some embed a checksum). Cross-check with a second cheap pass (e.g. template match against a normalized font atlas for low-confidence glyphs).

**CPU/GPU local notes:** Deep-CAPTCHA is ~5 conv+pool layers — trivially runs on CPU via `solver/device.py`'s CPU path; ONNX export in `train.py` lets it run without torch too. When no torch is present, `solve_with_fallback.py` already ships an OCR-ensemble fallback (exact-string accuracy ~1/10 — honest low-confidence, never fake).

**Failure modes:** CCT-heavy strings, extreme RBI (rotated/bent/intertwined) glyphs, adversarial noise. Mitigate with CTC/CRNN (no hard segmentation) and adaptive fusion filters that denoise *without* eroding stroke.

---

## 2. Object / Grid Selection (reCAPTCHA v2, hCaptcha, BLS)

### 2a. reCAPTCHA v2 — image grid
**What it looks like:** A single photo split into a **3×3 (9 tiles)** or **4×4 (16 tiles)** grid, with an instruction such as *"Select all squares with **traffic lights / crosswalks / buses / bicycles / fire hydrants / stairs / bridges / boats / taxis**"*. Tiles numbered left→right, top→bottom. May be **single-step** (one selection round) or **multi-step / dynamic** (after clicking, selected tiles fade out and are replaced; you must re-classify until none match). Objects can **span tile boundaries** (a traffic light across tiles 2 and 5). Shown as a fallback when the v2 checkbox risk score is too high.

**How a local solver detects it:**
- DOM: the `g-recaptcha` / `g-recaptcha-response` widget; challenge surfaces inside a Google `iframe[title*=challenge]`. Count `<td>` or image tiles → 9 (3×3) or 16 (4×4).
- Read the **instruction text** (e.g. sentence "Select all squares with…") → that noun is the **prompt-vocab class** to detect.
- Single large image with a visible grid overlay / equal-size tile srcs.

**Concrete solve algorithm (local):**
1. **Detect grid geometry:** read tile count from DOM or split the full screenshot into equal grid cells (cols=3/4, rows=3/4 for 9/16; confirm by counting distinct `src` URLs).
2. **Crop each tile** → N small images (N = 9 or 16).
3. **Classify each tile** into the prompt-vocab with a dedicated **tile classifier** (`TileNet` in this repo: multi-label CNN — outputs, per tile, whether it *contains* the target object; the same net can be trained on a fixed reCAPTCHA category vocabulary). 3×3 grids: classify the tiled 3×3 image directly with a small CNN (e.g. MobileNetV3-small → ONNX). 4×4 grids: run an object detector (e.g. YOLO-world) on the full image and map detected boxes → tile indices.
4. **Decision rule:** a tile is selected iff its object-class probability ≥ confidence threshold (e.g. 0.8). Because objects span boundaries, a *partial* object in a tile still counts → use a model trained on partial/occluded crops, or decide from the detector box overlapping the tile.
5. **Multi-step:** after each click, poll for tile src changes (CSS fade-in via computed `opacity > 0.95`); re-classify any replaced tiles; click again; stop when no tile exceeds threshold.
6. Return the **tile-index array** `[1,3,6,9]` to the click controller (select typically 2–5 of 9, or 3–6 of 16; "select all / select none" is virtually never correct — use that as a sanity rejection).
7. Click with **human-paced random delays**; submit `g-recaptcha-response` token.

### 2b. hCaptcha — object tiles
**What it looks like:** A grid (commonly **9 tiles** in a 3×3, sometimes 12) of **independent photographs** (not one photo cut up), with an instruction *"Select all images that contain a **bus** / **motorcycle** / **door** / **kitchen appliance**…"*. Tiles are separate images placed side-by-side (unlike reCAPTCHA's single photo sliced). Vocabulary is much larger and more abstract than reCAPTCHA; images are often real-world photos with varied lighting/angle/occlusion.

**How a local solver detects it:**
- DOM: `h-captcha` widget, `iframe[title*=hCaptcha]`; tiles as separate `<img>` elements (distinct srcs), instruction text present.
- Reactive deployment: hCaptcha is often **rate-triggered** (fires after a burst of requests) rather than always-on — be ready for it to appear only after several page loads.

**Concrete solve algorithm (local):**
1. Enumerate distinct tile images (by `src`), crop each.
2. Classify each tile into the prompt vocabulary. Because hCaptcha's vocabulary is **large and open**, a fixed small classifier underperforms; use a **zeroshot object detector** (YOLO-world style, ONNX-exportable) with the instruction noun as the free-text prompt, or a **CLIP-style vision-language encoder + text-embedding prompt** comparing each tile's image embedding to the prompt embedding. Both run fully on-device (GPU via `device.py`, or CPU slow-path).
3. Select tiles whose similarity / object-presence score ≥ threshold; return indices.
4. Submit; if the challenge is multi-round (hCaptcha iterates "one more try"-style rounds), loop re-classification until accepted.

### 2c. BLS / similar standalone grids
Grid of separate images (like hCaptcha). Same per-tile classification approach; no single-photo slicing, so **no cross-boundary problem** — each tile is independent.

---

## 3. Arkose / FunCaptcha — 3D Object Rotation (3D rolling ball / "rotate the animal")

### What it looks like
Arkose Labs FunCaptcha (now **Arkose MatchKey**, dominant on X.com/login, Roblox, EA) presents an **interactive 3D game** rather than a static image. The most common challenge type is **`3d_rollball_objects`**: a 3D-rendered animal/object (owl, seahorse "made of clouds", etc.) inside an interactive sphere, with **left / right arrow buttons**; each click rotates the object by a fixed **increment angle** (typ. 45°, but the increment is exposed as `challenge.increment`). The goal is to rotate the object so it faces **forward / upright**. Other Arkose games: pick 1 of 6 tiles (`gameType 3`), dice-sum puzzles, maze, matchkey. The puzzle difficulty, wave count, and time pressure (≈15 s timeout) are all **set by a risk engine**, not fixed — the fingerprint (`bda`) and the per-session `dapib` proof-of-work bind the answer to the session and prevent replays. Because each object uses **novel generated art**, a classifier trained on "the owl" does not transfer to "the seahorse" — the solver must target **geometry/orientation**, not the object's identity.

### How a local solver detects it
- DOM: `iframe[src*=arkose]` / `funcaptcha` frame; game canvas; a "rotate" instruction.
- Presence of arrow buttons (`[aria-label*=left/right]`, `.challenge-button-*`), a submit/verify button, and GL/canvas rendering (WebGL context) signals a 3D game.
- Challenge payload (`/fc/gfct/`) exposes `game_type` (e.g. `1` = rotate circle, `3` = pick tile), `variant` (e.g. `3d_rollball_animals`), `increment`, and `waves`.

### Concrete solve algorithm (local) — AI-independent
The archetype is **RotNet** (d4nst et al.): treat the **rotation-to-upright angle as a 360-way classification** (angle bin regression).

1. Grab the rendered object (screenshot the game canvas, or fetch the `challengeURL` image asset).
2. **Predict the current absolute rotation** with a *rotation classifier*:
   - `RotNet` (ResNet50 backbone, **360 classes**, cross-entropy) — the classic. Mainline refinement: **Circular Smooth Label (CSL)** so `dist(1°,2°) < dist(1°,180°)` (plain one-hot makes all angle pairs equidistant, which hurts); or switch to **regression with SmoothL1 / angle-error loss**.
   - This repo's `solver/vision/model.py` already trains a **RotNet** (angle-bin head), so this is a drop-in.
3. **Compute the corrective rotations:** knowing current angle θ and the wheel's `increment` (e.g. 45°), the number of arrow clicks N and direction = `argmin` over k∈{0..7} of `|rot(θ + sign·k·increment) − target (upright/forward)|`. Map to click count + direction.
4. **Click the arrow N times** (human-paced rhythm: 0.3–0.5 s between clicks), then click **Submit/Verify**.
5. **Multi-wave:** repeat per wave; several wrong waves or timeout → Arkose swaps the puzzle or issues a failing verdict, so cap attempts (e.g. `_MAX_ATTEMPTS`) and be willing to walk away.

**Local-only caveat:** RotNet trained on the COCO-unlabeled set reaches ~7° mean error for rotation estimation — good enough for the coarse 45°-multiple targets Arkose uses. The identity-free orientation prior is what makes it transfer across novel art. (Coordinate precision of a VLM is not needed because the solve is *direction + count*, not pixel-perfect clicking.)

---

## 4. Slider / Puzzle (GeeTest gap offset)

### What it looks like
Geetest (dominant anti-bot in CN; used globally on TikTok etc. as GeeTest v3/v4) shows a background image with a **missing fragment** (a cut-out shape) and a **separate puzzle piece**: the user drags a **slider button** until the piece fits exactly into the gap. A drag-velocity curve is also collected server-side. **GeeTest is the most common challenge** offered by Geetest (both v3 and v4). Variants: click-the-blurred-gap (no drag), 3D rotating objects, Yidun uses similar slide puzzles.

### How a local solver detects it
- DOM: `.geetest_slider` / `.geetest_slider_button` / `.geetest_canvas_slice`; a slider track; two images — **background with hole** and **piece (slice)**.
- Challenge payload: `gt` + `challenge` params; two image URLs (`pic` = full bg, `slice` = puzzle piece).

### Concrete solve algorithm (local) — CV, no ML needed
The answer is the **horizontal pixel offset** where the piece's shape matches the gap, plus **Y is usually fixed**.

**Method A — template matching on edges (simplest):**
```python
bg    = cv2.imread("captcha_bg.png", 0)       # full background, has a hole
piece = cv2.imread("captcha_piece.png", 0)    # the puzzle fragment
bg_edges    = cv2.Canny(bg,    50, 150)
piece_edges = cv2.Canny(piece, 50, 150)
res   = cv2.matchTemplate(bg_edges, piece_edges, cv2.TM_CCOEFF_NORMED)
_, max_val, _, max_loc = cv2.minMaxLoc(res)
gap_x, gap_y = max_loc          # gap_x is the drag distance in px
```
(The piece's own edge is masked/zeroed so it doesn't match itself; use only the *shape outline*.)

**Method B — pixel-diff gap scan (Geetest often gives two renderings of the bg: with and without the hole):**
1. Diff the two images pixel-wise → the hole region lights up.
2. Morph-close → connected blob → centroid x = **gap_x**.
3. The piece's initial position (offset `0` = left edge) → **drag distance = gap_x − piece_origin_x**.

**Method C — contour/edge scan:** binarize bg, find largest "hole" contour via connected components; take its bounding-box left edge. For colored blur-gaps, threshold by color distance to the surrounding region first, then Canny.

**Drag with human-like motion (critical — the server validates the curve):**
```python
slider = driver.find_element(By.CSS_SELECTOR, ".geetest_slider_button")
actions = ActionChains(driver); actions.click_and_hold(slider).perform()
distance = match_x
moved = 0
while moved < distance:
    step = random.randint(5, 8)              # irregular steps
    if moved + step > distance: step = distance - moved
    actions.move_by_offset(xoffset=step, yoffset=0).perform()
    moved += step
    time.sleep(random.uniform(0.01, 0.03))
actions.move_by_offset(xoffset=-3, yoffset=0).perform()  # micro-adjust tremor
time.sleep(0.1)
actions.move_by_offset(xoffset=3,  yoffset=0).perform()
actions.release().perform()
```
**WindMouse** (parametric human cursor motion) yields ~95% pass rates once tuned (a naive linear jump ≈ <10%). The dragged distance from CV feeds the endpoint; then submit `validate`/`seccode`.

**Local notes:** pure OpenCV (Canny + matchTemplate + connected components) — **no model, no torch**, runs on any CPU. Exact offset in px is the whole game; the JS/`geetest_`/encrypted `w` payload encoding is a separate reverse-engineering problem, not a vision one.

---

## 5. 2D Image Puzzles (jigsaw, drag-to-fit, tap-order)

### What it looks like
A family of 2D interaction puzzles, distinct from the Geetest slide:
- **Jigsaw:** an image split into shuffled tiles; drag each into its correct position.
- **Drag-to-gap:** a puzzle piece must be placed into the matching hole (like Geetest but vertical/2D alignment matters, or the gap is irregular).
- **Tap-in-order:** "tap the pictures in **ascending / alphabetical / color-spectrum** order" (e.g. sort objects by size, tap numbers 1→N, tap letters A→Z).
- **Rotate a 2D dial** to align a pattern (2D cousin of Arkose).

### How a local solver detects it
- DOM: draggable tile elements, a target region, or a tap-order instruction string with ordering keywords (ascending/biggest-to-smallest/A-to-Z).
- Multiple small images arranged in a grid with a visible "sort by ___" prompt.

### Concrete solve algorithm (local)
- **Jigsaw / drag-to-gap:** same **template matching** as §4 — `cv2.matchTemplate(piece, bg_slot)` over each candidate slot; pick highest `TM_CCOEFF_NORMED`; drag piece to that slot. For a shuffled full image, compute a **global patch hash / perceptual hash** per tile and place by reconstructing overlapping edge continuity (maximizing matching along shared borders).
- **Tap-in-order:** classify each tile (object classifier / OCR for digits-letters) → extract the sort key (size estimate from contour area, digit value from OCR, category rank from a hand-built ordinal) → sort the tile indices → click in that order.
- **2D dial rotation:** reuse the **RotNet** angle-bin head from §3: predict the dial's current angle and the target alignment angle; compute the required rotation delta; drag the handle accordingly.

**Local notes:** cv2-heavy; ordering tasks add a small OCR/classifier. No external AI.

---

## 6. VQA / Math / Natural-Language CAPTCHAs

### What it looks like
CAPTCHAs that ask for a *reasoned answer*, not raw transcription:
- **Arithmetic:** "What is **75 − 26 = ?**" or **`96−41=?`** rendered (often with distortion/noise), requiring computation. Corpus format: `96-41=?` → answer `55`. Sizes ~200×70 px.
- **Natural-language Q:** "Click the **biggest** object", "Select the **reddest** one", "Which number is **prime**?".
- **Icon-equation puzzles:** objects/icons stand for variables, coefficients inferred by counting (e.g. `🍎🍎 + 🍎 = 12`).

### How a local solver detects it
- DOM: instruction contains arithmetic symbols, "=", "?", or comparative/ordering natural language; single image of a math expression or a question + selectable answers.

### Concrete solve algorithm (local)
**Math expression: OCR + arithmetic evaluation (no LLM):**
1. **OCR the expression** — the repo's local OCR ensemble / Deep-CAPTCHA digit network extracts digits and operator (`+ − × ÷`). Optionally a dedicated detection of `+`/`−` via template match on the operator glyph.
2. **Validate** the transcript against the deterministic grammar `num op num = ?` (reject/retry otherwise — don't feed junk to the answer).
3. **Evaluate arithmetically** in Python (`eval` on the sanitized two-term expr, or hand-rolled int arithmetic) → integer answer → submit.
4. **Natural-language/ordering:** prep a small classifier per task type — e.g. object-size estimator (contour area), category-ordinal table, or color-dominance metric — then apply the comparator ("biggest" → max), submit the chosen tile. For icon-equation VQA, the local solver must **count** repeated icons per row then solve linear equations symbolically (counting is the hard part even for giant VLMs — a local connected-component / template-match counter on a known icon set handles it deterministically).

**Local notes:** pure OCR + integer math. Guard against OCR noise: because the *answer* is what's validated, small misreads flip the result — run 2–3 OCR passes and majority-vote the expression before computing.

---

## 7. Audio CAPTCHA (accessibility fallback)

### What it looks like
Audio is the **accessibility alternative** to a visual challenge (reCAPTCHA v2's audio button — the headphones icon). The user hears a spoken sequence of digits/letters/words (over background noise, variable tempo/pitch, sometimes "one ... two ... thirty four") and must type what was said. Wild audio is increasingly distorted specifically to defeat ASR, but modern local ASR still cracks classic audio reCAPTCHA with **>90–97% accuracy**.

### How a local solver detects it
- DOM: an `<audio>`/`<source>` element (or a fetchable `.mp3`/`.wav` URL) and a response text input; triggered by switching the challenge to audio mode (`#recaptcha-audio-button`).

### Concrete solve algorithm (local) — ASR, no external API
1. **Fetch / catch the audio asset** (intercept the download from `<audio>` src via Playwright `page.request.get` or a network hook).
2. **Transcribe locally** with an on-device ASR model:
   - **Whisper** (`faster-whisper`, `WhisperModel("base", compute_type="int8")`) — tiny/base models are enough for clear challenge speech; runs on CPU (int8) or GPU. Alternative: `WhisperModel("tiny.en")`.
   - Lighter: a tiny **speech-to-digit CTC** model trained on synthetic spoken digits. DeepSpeech as a fallback.
   - Research (HAL) shows Whisper tiny.en / base.en hit **97% / 93%** on audio reCAPTCHA, solved in ≈1 s.
3. **Normalize** the transcript: strip the word-form numbers ("thirty four" → "34"), drop filler words, keep the digit/letter sequence the challenge asks for.
4. **Fill the answer field** and submit.
5. **Retry loop:** audio challenges often say "multiple correct solutions required" or just fail; re-fetch + re-transcribe up to N rounds before giving up.

**Local notes:** Whisper local (no cloud) satisfies the LO rule. Guard: some audio challenges pose a **mix** where the answer is only part of the spoken content ("answer the *first* three numbers"), so parse the instruction, not just the audio.

---

## 8. Frequency ranking (real-world evidence)

Measured deployment shares vary by crawler and methodology — worth knowing because it drives solver priority:

| Source | reCAPTCHA | Cloudflare Turnstile | hCaptcha | GeeTest | Arkose |
|---|---|---|---|---|---|
| Zyte SOWA 2026 (landing pages) | 49.4% | 39.4% | 1.9% | 7.7% | 1.5% |
| wmtips 2026 (3.5M sites) | 76.3% | — | 20.9% | ~0.07% | ~#19 |
| Aguko 2026 | 37.9% | (HSTS 30%) | 0.30% | 0.00% | 0.02% |

**Engineering ordering** (what a solver actually encounters among *challenge-surfacing* image tasks, which is how effort should be allocated):

1. **reCAPTCHA v2** — grid/object; checkbox + grid fallback; most common challenge by volume.
2. **hCaptcha** — object tiles; #2 provider overall, larger/abstracter vocabulary.
3. **Arkose (FunCaptcha)** — 3D rotation; concentrated on high-value properties (X.com, gaming/logins) so *disproportionately frequent* for a scraper targeting those; low global share but high encounter rate in bot-heavy verticals.
4. **GeeTest** — slider & related; dominant specialists outside the two giants; very common on CN + TikTok-family properties.
5. **Cloudflare Turnstile / Friendly / Altcha** — largely **passive/invisible** (no image task) — solved by browser-behavior/fingerprint evasion, not vision; outside this document's scope but worth noting so the solver doesn't waste a vision pass on them.

The task's requested shorthand ranking **"reCAPTCHA v2 > hCaptcha > Arkose > GeeTest"** matches the ordering of *challenge-burden per provider* priority (reCAPTCHA #1 globally; hCaptcha #2 provider with real image grids; Arkose ahead of GeeTest in Western bot-heavy targets, GeeTest ahead of Arkose in CN-heavy targets). Optimize the solver in that order.

---

## 9. Cross-cutting solver design notes

- **One dispatch entry point, many backends.** `solver/vision/model.py` already bundles the two reusable nets: **TileNet** (grid multi-label / per-tile object clf) and **RotNet** (angle-bin rotation). The same ONNX-exporter (`train.py --target onnx`) covers text (Deep-CAPTCHA heads), grid, rotation, and math-OCR.
- **Local-only invariant (LO rule):** every backend is trainable on this box (`train.py --synthetic` generates text/grid/math data; `--data` ingests harvested real tiles via `harvest.py`) and runs on CPU (slow path) or GPU/MPS via `device.py`'s `pick_device`/`batch_size_for`/`amp_enabled`. No `VISION_LLM_URL`, no remote API — the ONNX/CPU fallback keeps everything solvable even with torch absent.
- **Confidence + honesty:** `solve_with_fallback.py`'s ethos applies everywhere — a low-confidence result is reported as "low-confidence / likely-wrong", never silently accepted. A wrong grid tile or a mis-read arithmetic expression should trigger a documented retry, not a fabricated success.
- **Anti-detection is orthogonal to vision.** Getting the *answer* right is only half; human-paced interaction (random delays, wind-mouse curves, tile fade-in waits, capped attempts) is what lets a correct answer actually pass. Behavior engineering (§4, §2a) is shared across all types.

---

## 10. Source map (web research, 2026-09-06)

- **Text:** Deep-CAPTCHA (Noury et al., arXiv:2006.08296) — 3× Conv+Pool → Dense512 → parallel softmax heads, 98.94% numeric / 98.31% alphanumeric. MDPI *Appl. Sci.* 14:5016 (2024) Adaptive-CAPTCHA — CRNN (2×conv + LSTM/CTC) + Adaptive Fusion Filtering Networks, −70% params, +10 pts. End-to-end CNN-RNN attention recognizer (github.com/wilbertharriman/tf2-attention-captcha-recognizer).
- **Grid:** blog.captchaai.com "How Grid Image CAPTCHAs Work"; *reCAPTCHA Grid Challenge Explained*; docs.captchaai.com/guides/grid-image; github.com/v6ctor/Google-reCaptcha-v2-ML-Solver (MobileNetV3 3×3 tile clf + YOLO-world 4×4, fade-in waits, multi-step re-classify). Zyte SOWA 2026 for hCaptcha reactive (rate-triggered) deployment stats.
- **Arkose:** github.com/DFGANDP/Rotnet-Captcha-Solver; github.com/lumina37/rotate-captcha-crack (RotNet ResNet50 360-cls, CSL circular-smooth-label, RotNetR RegNet ~7° mean err); github.com/Nader96x/funcaptcha (gameType/variant/increment/waves); blog.crawlex.net "Arkose Labs FunCaptcha internals" (risk engine, bda fingerprint, dapib proof-of-work, ~15 s timeout, novel-art anti-transfer).
- **Slider:** github.com/nttrung9x/solver-captcha-slider (OpenCV PuzleSolver); github.com/ToliaGuy/geetest-solver (pixel-diff, WindMouse ~95%); habr.com/en/articles/903870 (Canny + matchTemplate Snippet, human-motion drag); github.com/rish-hyun/geetest-slider-captcha-bypass.
- **Audio:** github.com/yfe404/recaptcha-audio-solver & github.com/saifyxpro/recaptcha-v2-audio-solver (local faster-whisper, no API); HAL hal-05489792v1 "Bypassing Audio reCAPTCHA with ASR" (Whisper tiny 97% / base 93%, solved ~1 s, <$0.01); dev.to colony0ai Playwright+faster-whisper.
- **Math/VQA:** huggingface.co/datasets/atalaydenknalbant/MathCaptcha10k (`96-41=?`→55; evaluation); ACL 2025 EMNLP "Can Vision-Language Models Solve Visual Math Equations?" (counting is the bottleneck even for VLMs → motivates local deterministic counting).
- **Frequency:** zyte.com/sowa/2026/barriers/captcha (22.5% of pages, reCAPTCHA 49.4% / Turnstile 39.4% / GeeTest 7.7% / hCaptcha 1.9% / Arkose 1.5%); wmtips.com captcha market share (reCAPTCHA 76.3%, hCaptcha 20.9%); aguko.com/cat/captchas.
