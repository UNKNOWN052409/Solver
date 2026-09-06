#!/usr/bin/env python3
"""A3 BEFORE measurement — current solve_with_fallback / EnsembleEngine on the
20 real map_*.png captchas. Ground truth from upstream metadata.csv (24-char
alphabet 3479ACDEFHJKLMNPQRTUVWXY, 5 chars)."""
import os, sys, json
os.environ["SOLVER_TESS_ROOT"] = "/tmp/tesseract-root"
sys.path.insert(0, "/home/kali/NeoSolver")
import cv2
from solver.engines.ensemble_engine import EnsembleEngine

DATA = "/home/kali/NeoSolver/data/real_captchas/grid"
GT = {
 0:"4KTN9",1:"7UTUP",2:"D37JF",3:"HTJA9",4:"JX7CL",5:"JYRJX",6:"KK4EK",
 7:"KWNVJ",8:"PY3WU",9:"TH9TQ",10:"TJKN9",11:"WELDP",12:"UPVAP",13:"FYEVU",
 14:"Q9DHQ",15:"3WCE7",16:"LDWC7",17:"QR939",18:"R3AWX",19:"WTVRY"}

eng = EnsembleEngine(charset="3479ACDEFHJKLMNPQRTUVWXY")
def char_acc(pred, truth):
    return sum(1 for p,t in zip(pred,truth) if p==t) if pred else 0

total=0; full5=0; rows=[]
for i in range(20):
    img = cv2.imread(f"{DATA}/map_{i:05d}.png")
    pred = eng.solve(img)
    truth = GT[i]
    ca = char_acc(pred, truth)
    total += ca
    if pred == truth and len(pred)==5:
        full5 += 1
    rows.append((i, pred, truth, ca))
for i,pred,t,ca in rows:
    print(f"map_{i:05d} pred={pred!r:12} gt={t!r} chars={ca}")
print(f"\nAVG_CHARS={total/20:.2f}  full5_wins={full5}")
