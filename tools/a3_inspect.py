#!/usr/bin/env python3
"""Inspect the map captcha structure — channels, how text sits on the map."""
os_env=None
import os,sys,cv2,numpy as np
DATA="/home/kali/NeoSolver/data/real_captchas/grid"
img=cv2.imread(f"{DATA}/map_00000.png", cv2.IMREAD_UNCHANGED)
print("shape", img.shape, "dtype", img.dtype)
if img.shape[2]==4:
    b,g,r,a = cv2.split(img)
    for n,ch in [("B",b),("G",g),("R",r),("A",a)]:
        print(f"{n}: min={ch.min()} max={ch.max()} mean={ch.mean():.1f} std={ch.std():.1f}")
    print("alpha unique vals:", len(np.unique(a)))
    # where is alpha non-255? maybe text is in alpha vs map in rgb
    mask=a<250
    print("alpha<250 frac:", mask.mean())
# Try to view the luminance structure
for i in [0,15]:
    im=cv2.imread(f"{DATA}/map_{i:05d}.png", cv2.IMREAD_UNCHANGED)
    gray=cv2.cvtColor(im[:,:,:3],cv2.COLOR_BGR2GRAY)
    print(f"map_{i} gray min/max/mean/std", gray.min(),gray.max(),round(gray.mean(),1),round(gray.std(),1))
