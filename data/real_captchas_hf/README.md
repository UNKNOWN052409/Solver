# Real Labeled Captcha Dataset

**REAL data — NOT synthetic.** No generated/mock captchas here.

Two independent REAL labeled captcha sources were downloaded (no synthetic data).

## Source 1 — project-sloth/captcha-images (REAL)
- **HF:** https://huggingface.co/datasets/project-sloth/captcha-images | License: WTFPL
- Provenance: real CAPTCHA JPEGs collected in the open-source "sloth" project's
  captcha collection. Ground-truth text label is encoded in the **filename**: the
  string before the first `.` is the captcha answer (6-char alphanumeric).
- Example: `train/000073.e1270d42...jpg` → label `000073`

| Split       | # images | size   | label source   |
|-------------|----------|--------|----------------|
| train       | 6000     | 200x50 | filename prefix|
| test        | 2000     | 200x50 | filename prefix|
| validation  | 2000     | 200x50 | filename prefix|
| **Subtotal**| **10000**|        |                |

## Source 2 — Tuna0000/gib-captcha-labeled (REAL)
- **HF:** https://huggingface.co/datasets/Tuna0000/gib-captcha-labeled | License: apache-2.0
- Provenance: real labeled captcha corpus (image-to-text, 1K-10K size category).
  Labels in a `text` column; NOT synthetic-tagged on HF.
- Images extracted from parquet into `gib_captcha/{train,validation}/*.jpg`,
  dimensions 200x100.

| Split       | # images | label source |
|-------------|----------|--------------|
| train       | 4086     | `text` column|
| validation  | 510      | `text` column|
| **Subtotal**| **4596** |              |

## Totals
| Source                            | # real labeled |
|-----------------------------------|----------------|
| project-sloth/captcha-images      | 10000          |
| Tuna0000/gib-captcha-labeled      | 4596           |
| **GRAND TOTAL**                   | **14596**      |

## Manifest
`manifest.json` maps every image path to its label:
```json
{ "originals/train/000073.<hash>.jpg": "000073", "gib_captcha/train/01100.jpg": "YZMTBA", ... }
```
14,596 entries.

## Real vs Synthetic clarity (from HF search)
- **REAL (used):** `project-sloth/captcha-images`, `Tuna0000/gib-captcha-labeled`
- **REJECTED (SYNTHETIC):** `AvinashRicky/CaptchaOCR-500K` — explicitly tagged
  `synthetic-data` on HF. Not used.
- Other HF candidates were **models, not datasets** (e.g.
  `aman-agrawal/image-to-text-captcha`, `dragonstar/image-text-captcha[-v2]`,
  `verytuffcat/captcha_image`, `tcsenpai/Captchot-...`). Not applicable.
- **No synthetic data used.** Deliberately rejected the only synthetic-tagged
  option rather than fall back.
