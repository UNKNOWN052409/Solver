#!/usr/bin/env python3
"""Colab T4 GPU: REAL OCR training on 4,587 real labeled gib captchas."""
import os, json, time, random, tarfile
import numpy as np, torch, torch.nn as nn
os.chdir("/content")
import tarfile
tarfile.open("gib_real.tar.gz").extractall(".")
ROOT = "/content"
MAN = os.path.join(ROOT, "manifest.json")
ALPHA="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"; A2I={c:i for i,c in enumerate(ALPHA)}
NLEN=6
rows=[]
with open(MAN) as f:
    for path,label in json.load(f).items():
        p=os.path.join(ROOT,str(path))
        lab=str(label)
        if os.path.exists(p) and len(lab)>=6 and any(c.isalpha() for c in lab):
            rows.append((p,lab[:6].upper()))
print("[REAL] loaded",len(rows),flush=True)
random.seed(42); random.shuffle(rows)
nv=int(len(rows)*0.15); val_rows,tr_rows=rows[:nv],rows[nv:]

def pre(rs):
    Xs=[];Ys=[]
    for p,l in rs:
        try:
            im=Image.open(p).convert("L").resize((200,100))
            Xs.append(np.asarray(im,dtype=np.float32)/255.0)
            y=np.zeros(NLEN,dtype=np.int64)
            for j,c in enumerate(l):
                if c in A2I: y[j]=A2I[c]
            Ys.append(y)
        except Exception: pass
    return torch.from_numpy(np.stack(Xs)[:,None]), torch.from_numpy(np.stack(Ys))

from PIL import Image
Xtr,Ytr=pre(tr_rows); Xv,Yv=pre(val_rows)
print("[DATA]",Xtr.shape,Ytr.shape,"val",Xv.shape,Yv.shape,flush=True)
dev=torch.device("cuda" if torch.cuda.is_available() else "cpu")
print("[DEVICE]",dev,flush=True)
Xtr,Ytr,Xv,Yv=Xtr.to(dev),Ytr.to(dev),Xv.to(dev),Yv.to(dev)

class OCRNet(nn.Module):
    def __init__(s,vocab=36):
        super().__init__(); s.f=nn.Sequential(
            nn.Conv2d(1,32,3,padding=1),nn.ReLU(),nn.MaxPool2d(2),
            nn.Conv2d(32,64,3,padding=1),nn.ReLU(),nn.MaxPool2d(2),
            nn.Conv2d(64,128,3,padding=1),nn.ReLU(),nn.MaxPool2d(2),
            nn.Conv2d(128,256,3,padding=1),nn.ReLU(),nn.AdaptiveAvgPool2d((1,1)))
        s.hs=nn.ModuleList([nn.Linear(256,vocab) for _ in range(NLEN)])
    def forward(s,x):
        f=s.f(x).flatten(1); return torch.stack([h(f) for h in s.hs],1)

BS=256; EP=60
m=OCRNet().to(dev); opt=torch.optim.Adam(m.parameters(),1e-3); lf=nn.CrossEntropyLoss()
t0=time.time()
for ep in range(EP):
    m.train(); perm=torch.randperm(Xtr.shape[0]); tot=corr=0
    for st in range(0,Xtr.shape[0],BS):
        idx=perm[st:st+BS]; X=Xtr[idx]; Y=Ytr[idx]
        out=m(X); loss=sum(lf(out[:,j],Y[:,j]) for j in range(NLEN))/NLEN
        opt.zero_grad(); loss.backward(); opt.step()
        pr=out.argmax(-1); corr+=(pr==Y).all(-1).sum().item(); tot+=Y.shape[0]
    m.eval()
    with torch.no_grad():
        pr=m(Xv).argmax(-1)
        vacc=(pr==Yv).all(-1).sum().item()/Yv.shape[0]; vchar=(pr==Yv).sum().item()/Yv.numel()
        tracc=corr/max(tot,1)
    print(f"[EP {ep}] train_img={tracc:.4f} val_img={vacc:.4f} val_char={vchar:.4f} ({time.time()-t0:.0f}s)",flush=True)
    if ep>=5 and tracc>0.99:
        torch.save(m.state_dict(),"/content/real_ocr.pt"); print("[SAVE real_ocr.pt]",flush=True); break
torch.save(m.state_dict(),"/content/real_ocr.pt")
print("[DONE] real OCR trained on T4 -> /content/real_ocr.pt",flush=True)
