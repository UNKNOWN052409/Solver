#!/usr/bin/env python3
"""A3 AFTER — improved preprocessing + ensemble for real map captchas.

Strategy:
  * Many preprocessing variants (grayscale/adaptive/threshold/edge/color
    channel/CLAHE/resize), each read by tesseract at several psm+oem combos,
    restricted to the real 24-char alphabet (3479ACDEFHJKLMNPQRTUVWXY).
  * Weighted positional majority-vote across all variant reads.
Ground truth: upstream metadata.csv (hardcoded for map_00000..00019).
"""
import os, sys, json, subprocess, tempfile
os.environ["SOLVER_TESS_ROOT"]="/tmp/tesseract-root"
sys.path.insert(0,"/home/kali/NeoSolver")
import cv2, numpy as np
from collections import Counter

DATA="/home/kali/NeoSolver/data/real_captchas/grid"
CHARSET="3479ACDEFHJKLMNPQRTUVWXY"
GT={0:"4KTN9",1:"7UTUP",2:"D37JF",3:"HTJA9",4:"JX7CL",5:"JYRJX",6:"KK4EK",
 7:"KWNVJ",8:"PY3WU",9:"TH9TQ",10:"TJKN9",11:"WELDP",12:"UPVAP",13:"FYEVU",
 14:"Q9DHQ",15:"3WCE7",16:"LDWC7",17:"QR939",18:"R3AWX",19:"WTVRY"}

# ---------- tesseract invocation (rootless tree) ----------
TESS="/tmp/tesseract-root/usr/bin/tesseract"
LIB="/tmp/tesseract-root/usr/lib/aarch64-linux-gnu"
TDIR="/tmp/tesseract-root/usr/share/tesseract-ocr/5/tessdata"
LOADER="/lib/ld-linux-aarch64.so.1"
PREFIX=[LOADER,"--library-path",LIB,TESS]
def tess(image, psm, oem):
    try:
        with tempfile.NamedTemporaryFile(suffix=".png") as t:
            cv2.imwrite(t.name, image)
            cmd=[*PREFIX,t.name,"stdout","--oem",str(oem),"--psm",str(psm),
                 "-c",f"tessedit_char_whitelist={CHARSET}","--tessdata-dir",TDIR]
            r=subprocess.run(cmd,capture_output=True,text=True,timeout=30)
            return r.stdout.strip().replace(" ","").replace("\n","")
    except Exception:
        return ""

# ---------- preprocessing variants (return list of (name,image)) ----------
def gray(img):
    if img.ndim==3: return cv2.cvtColor(img,cv2.COLOR_BGR2GRAY)
    return img
def prep_variants(bgr):
    out=[]
    g=gray(bgr)
    out.append(("gray",g))
    cl=cv2.createCLAHE(clipLimit=3.0,tileGridSize=(8,8)).apply(g)
    out.append(("clahe",cl))
    # adaptive thresholds
    for bs,const in [(11,2),(15,2),(11,-2),(15,5),(31,7)]:
        a=cv2.adaptiveThreshold(cl,255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY,bs,const)
        out.append((f"adapt{bs}_{const}",a))
        # inverted
        out.append((f"adapt{bs}_{const}_inv",cv2.bitwise_not(a)))
    # otsu on clahe
    _,o=cv2.threshold(cl,0,255,cv2.THRESH_BINARY+cv2.THRESH_OTSU)
    out.append(("otsu",o))
    _,o2=cv2.threshold(cl,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)
    out.append(("otsu_inv",o2))
    # per-channel
    if bgr.ndim==3:
        b_,g_,r_=cv2.split(bgr)
        for nm,ch in [("B",b_),("G",g_),("R",r_)]:
            out.append((nm,ch))
            a=cv2.adaptiveThreshold(ch,255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                    cv2.THRESH_BINARY,15,2)
            out.append((nm+"_adapt",a))
        # saturation channel (text color vs pastel bg)
        hsv=cv2.cvtColor(bgr,cv2.COLOR_BGR2HSV)
        out.append(("sat",hsv[:,:,1]))
        out.append(("val",hsv[:,:,2]))
        # redness as before
        f=bgr.astype(np.int16)
        red=np.clip(f[:,:,2]-np.maximum(f[:,:,0],f[:,:,1]),0,255).astype(np.uint8)
        out.append(("red",red))
    # edges
    e=cv2.Canny(cl,50,150)
    out.append(("canny",e))
    e2=cv2.Canny(cl,30,100)
    out.append(("canny2",e2))
    # morphology-open to kill thin noise
    open3=cv2.morphologyEx(cl,cv2.MORPH_OPEN,np.ones((2,2),np.uint8))
    out.append(("open3",open3))
    # a = dark-stroke extraction via Otsu on (255-cl-gaussianminus)
    return out

def upscale(img,fx=2,interp=cv2.INTER_CUBIC):
    return cv2.resize(img,None,fx=fx,fy=fx,interpolation=interp)

# ---------- run full matrix per image ----------
def variants_solve(bgr):
    """Return list of (name, text)."""
    results=[]
    for pname,pimg in prep_variants(bgr):
        for up in (False,True):
            im=upscale(pimg) if up else pimg
            for psm in (7,13):
                for oem in (1,3):
                    t=tess(im,psm,oem)
                    if t:
                        results.append((f"{pname}|up{int(up)}|psm{psm}|oem{oem}",t))
    return results

def vote(raws):
    """majority positional vote over variant strings."""
    lens=Counter(len(s) for s in raws if s)
    if not lens: return ""
    n=lens.most_common(1)[0][0]
    if n==0: return ""
    out=[]
    for pos in range(n):
        tally=Counter()
        for s in raws:
            if pos<len(s) and s[pos]: tally[s[pos]]+=1
        if tally: out.append(tally.most_common(1)[0][0])
    return "".join(out)

def char_acc(p,t): return sum(1 for a,b in zip(p,t) if a==b) if p else 0

# keep raw list lengths manageable but per-image print top
raws_by_img={}
total_before=0
for i in range(20):
    bgr=cv2.imread(f"{DATA}/map_{i:05d}.png")
    if bgr is None: print("MISS",i); continue
    if bgr.ndim==3 and bgr.shape[2]==4:
        bgr=bgr[:,:,:3]
    r=variants_solve(bgr)
    raws_by_img[i]=[s for _,s in r]

import pickle
pickle.dump(raws_by_img, open("/tmp/raws_by_img.pkl","wb"))
print("stored raw reads per image; total reads:", sum(len(v) for v in raws_by_img.values()))
