#!/usr/bin/env python3
"""Analyze color structure of map captchas to find text-vs-background signal."""
import cv2, numpy as np
DATA="/home/kali/NeoSolver/data/real_captchas/grid"
for i in [0,4,13,18]:
    im=cv2.imread(f"{DATA}/map_{i:05d}.png", cv2.IMREAD_UNCHANGED)[:,:,:3]
    b,g,r=cv2.split(im.astype(np.int16))
    gray=cv2.cvtColor(im,cv2.COLOR_BGR2GRAY)
    # look at local gradient magnitude (edges) - text has high gradient
    gx=cv2.Sobel(gray,cv2.CV_32F,1,0,ksize=3); gy=cv2.Sobel(gray,cv2.CV_32F,0,1,ksize=3)
    mag=np.sqrt(gx**2+gy**2)
    print(f"map_{i}: gray mean {gray.mean():.0f} std {gray.std():.0f} | gradmean {mag.mean():.0f} gradmax {mag.max():.0f} | sat mean {cv2.cvtColor(im,cv2.COLOR_BGR2HSV)[:,:,1].mean():.0f}")
    # Find the most 'outlier' pixels (glyph strokes should cluster in color space away from bg)
    # count pixels far from the median color
    med=np.median(gray)
    far=(np.abs(gray.astype(int)-med)>45).mean()
    print(f"   frac pixels >45 from median gray: {far:.3f}")
