#!/usr/bin/env python3
"""Bounded real CRNN+CTC training (3000 imgs, 12 epochs) — completes in Colab CPU timeout."""
import os, csv, glob, time, random, tarfile
import numpy as np, torch, torch.nn as nn, torch.nn.functional as F
from PIL import Image
os.chdir("/content")
tarfile.open("ronc_sample.tar.gz").extractall(".")
IMGD="/content/ronc_sample/images"; CSV="/content/ronc_sample/labels.csv"
lab={}
with open(CSV) as f:
    for row in csv.DictReader(f): lab[row["id"]]=row["captcha"].upper()
files=sorted(glob.glob(IMGD+"/*.jpg")); random.seed(42); random.shuffle(files)
files=files[:3000]
nv=int(3000*0.15); val_f=files[:nv]; tr_f=files[nv:]
CHARS="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"; C2I={c:i for i,c in enumerate(CHARS)}; NC=36
def load(p):
    im=Image.open(p).convert("L").resize((128,64))
    return torch.from_numpy(np.asarray(im,dtype=np.float32)/255.0)[None]
def prep(fl):
    X=[];Y=[]
    for p in fl:
        hid=os.path.splitext(os.path.basename(p))[0]; c=lab.get(hid,"")
        if len(c)==3:
            try: X.append(load(p)); Y.append(c)
            except Exception: pass
    return X,Y
TX,TY=prep(tr_f); VX,VY=prep(val_f)
print("[REAL] train",len(TX),"val",len(VX),flush=True)
class CRNN(nn.Module):
    def __init__(s,nc=NC):
        super().__init__(); s.cnn=nn.Sequential(
            nn.Conv2d(1,32,3,padding=1),nn.ReLU(),nn.MaxPool2d(2,2),
            nn.Conv2d(32,64,3,padding=1),nn.ReLU(),nn.MaxPool2d(2,2),
            nn.Conv2d(64,128,3,padding=1),nn.ReLU(),nn.MaxPool2d((2,1)),
            nn.Conv2d(128,128,3,padding=1),nn.ReLU(),nn.MaxPool2d((2,1)))
        s.bi=nn.LSTM(512,256,bidirectional=True,batch_first=True); s.fc=nn.Linear(512,nc+1)
    def forward(s,x):
        f=s.cnn(x); b=f.permute(0,3,2,1).flatten(2); out,_=s.bi(b); return s.fc(out)
dev=torch.device("cuda" if torch.cuda.is_available() else "cpu")
print("[DEVICE]",dev,flush=True)
m=CRNN().to(dev); opt=torch.optim.Adam(m.parameters(),1e-3)
def lof(logits,cps):
    lp=F.log_softmax(logits,-1).permute(1,0,2); tgt=[];tl=[]
    for c in cps: tgt.extend([C2I.get(ch,NC) for ch in c]); tl.append(len(c))
    return F.ctc_loss(lp,torch.tensor(tgt,device=dev),torch.tensor([logits.shape[1]]*len(cps),device=dev),torch.tensor(tl,device=dev),blank=NC,zero_infinity=True)
def dec(pr):  # greedy decode batch
    seqs=[]
    for row in pr:
        seq=[];last=-1
        for t in row:
            if int(t)!=last and int(t)!=NC: seq.append(int(t))
            last=int(t)
        seqs.append(''.join(CHARS[t] for t in seq if t<NC))
    return seqs
BS=64; EP=12; t0=time.time()
for ep in range(EP):
    m.train(); idx=list(range(len(TX))); random.shuffle(idx); tot=0;corr=0
    for st in range(0,len(idx),BS):
        sel=idx[st:st+BS]; X=torch.stack([TX[i].to(dev) for i in sel]); cps=[TY[i] for i in sel]
        out=m(X); loss=lof(out,cps); opt.zero_grad(); loss.backward(); opt.step()
        ds=dec(out.argmax(-1).detach().cpu().numpy())
        for d,c in zip(ds,cps):
            if d==c: corr+=1
            tot+=1
    m.eval(); vc=vt=0
    with torch.no_grad():
        for st in range(0,len(VX),BS):
            sel=list(range(st,min(st+BS,len(VX))))
            X=torch.stack([VX[i].to(dev) for i in sel]); cps=[VY[i] for i in sel]
            out=m(X); ds=dec(out.argmax(-1).detach().cpu().numpy())
            for d,c in zip(ds,cps):
                if d==c: vc+=1
                vt+=1
    print(f"[EP{ep}] loss={loss.item():.4f} train_acc={corr/max(tot,1):.4f} val_acc={vc/max(vt,1):.4f} ({time.time()-t0:.0f}s)",flush=True)
    if ep==11: torch.save(m.state_dict(),"/content/ronc_crnn.pt")
print("[DONE] -> /content/ronc_crnn.pt",flush=True)
