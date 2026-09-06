# Captcha OCR Trainer — honest capability status
Date: 2026-09-06

## VALID DATA (confirmed)
- roncuevas/Captchas: 88,847 real captcha images, 3-char alphanumeric labels (4RJ, N34, YHP).
  - Published CRNN+CTC model on this dataset: 99.49% exact-match val accuracy.
  - Local: 21,146 images extracted + full labels_clean.csv. **0 image-label mismatches** (verified).
  - Signal confirmed: a simple CNN on this data drove val_char 0.038 -> 0.095 (3.4x random),
    proving the labels correspond to image content (wrong/random labels would stay pinned at random).

## ARCHITECTURE (correct, bug-fixed)
- solver/vision/colab_crnn_bounded.py: CRNN (CNN->BiLSTM 512->256x2 ->Linear) + CTC loss,
  blank=36 (last class) — matches roncuevas' proven 99.49% trainer. 128x64 input.
- Bug fixed during this session: LSTM input_size was 128 but CNN flattens to 512 -> now 512.
  (Local smoke: loss computes, no shape crash.)

## WHY HIGH ACCURACY IS NOT YET DEMONSTRATED (honest)
- Training is compute-bound, not data/arch-bound. On this box:
  - Colab CPU: ~400s/epoch. To match the 99.49% reference (30 epochs) needs ~3.3h >
    the 1500s colab exec timeout. Only 3 epochs fit -> val_acc 0 (still at random start).
  - Colab T4 GPU quota: EXHAUSTED (TooManyAssignmentsError / Service Unavailable) this session.
  - Local CPU: too slow + background procs killed (signal 1).
- Therefore NO real serve-accuracy number is claimed yet. The pipeline (valid data + correct
  CRNN+CTC) is ready; it must run 30+ epochs on GPU to demonstrate the accuracy.

## NEXT STEP
- When Colab GPU quota resets (or LO provides the M400/M800/TITAN/CodeSphere GPUs),
  run solver/vision/colab_crnn_bounded.py with EP=30 on cuda. Benchmark real val_acc,
  then live-test the solver on 5sim.net / captcha demo sites and report REAL success rate.
- NO mock: do not claim accuracy until a real val_acc + real live solve are measured.
