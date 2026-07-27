import pdfplumber, re, json, collections
NUM=re.compile(r'^\d{1,4}$'); ANS=re.compile(r'^[（(]?([1-3１２３])[）)]?$'); T=str.maketrans("１２３","123")
DOCS={"car": dict(path="/Users/chenliangyu/Downloads/汽車筆試題庫_公告115.06.9.pdf",
                  c1=89,c2=119,top=52,bot=790,minw=430),
      "moto":dict(path="/Users/chenliangyu/Downloads/機車駕照筆試題庫(全部804題)-1150218.pdf",
                  c1=71,c2=101,top=52,bot=790,minw=430)}
SKIP={"題目","題目內容","題","目","內容","圖示","分類","答","案","題號","▎題號","▎"}

def build(tag):
    cfg=DOCS[tag]; qs=[]; orph=[]
    with pdfplumber.open(cfg["path"]) as pdf:
        for pi,page in enumerate(pdf.pages,1):
            mid=lambda w:(w["top"]+w["bottom"])/2; xc=lambda w:(w["x0"]+w["x1"])/2
            ws=[w for w in page.extract_words() if cfg["top"]<mid(w)<cfg["bot"]]
            span=collections.defaultdict(lambda:[1e9,-1e9])
            for e in page.edges:
                if e["orientation"]=="h":
                    k=round(e["top"],1); span[k][0]=min(span[k][0],e["x0"]); span[k][1]=max(span[k][1],e["x1"])
            agg=collections.defaultdict(lambda:[1e9,-1e9])
            for k,(a,b) in span.items():
                kk=round(k/2.0)
                agg[kk][0]=min(agg[kk][0],a); agg[kk][1]=max(agg[kk][1],b)
            ys=sorted(min(k*2.0 for k in [kk]) for kk,(a,b) in agg.items() if b-a>cfg["minw"] and cfg["top"]-4<kk*2.0<cfg["bot"]+4)
            cuts=[]
            for y in ys:
                if not cuts or y-cuts[-1]>2.0: cuts.append(y)
            cuts=[cfg["top"]]+[y for y in cuts if y>cfg["top"]+1]+[cfg["bot"]+40]
            rows=[{"y0":cuts[i],"y1":cuts[i+1],"q":None,"ans":None,"words":[],"imgs":[]} for i in range(len(cuts)-1)]
            def band(m):
                for r in rows:
                    if r["y0"]<=m<r["y1"]: return r
            for w in ws:
                r=band(mid(w))
                if r is None: orph.append((pi,w["text"])); continue
                if xc(w)<cfg["c1"]:
                    if NUM.match(w["text"]) and r["q"] is None: r["q"]=int(w["text"])
                elif xc(w)<cfg["c2"]:
                    m2=ANS.match(w["text"])
                    if m2 and r["ans"] is None: r["ans"]=int(m2.group(1).translate(T))
                elif w["text"] not in SKIP: r["words"].append(((pi,round(w["top"]/3)),w["x0"],w["text"]))
            for im in [i for i in page.images if i["x1"]-i["x0"]>12 and i["bottom"]-i["top"]>12]:
                r=band((im["top"]+im["bottom"])/2)
                if r: r["imgs"].append([round(im["x0"],1),round(im["top"],1),round(im["x1"],1),round(im["bottom"],1)])
            for r in rows:
                if r["q"] is not None:
                    qs.append({"no":r["q"],"ans":r["ans"],"page":pi,"w":list(r["words"]),"imgs":list(r["imgs"])})
                elif qs:
                    qs[-1]["w"]+=r["words"]; qs[-1]["imgs"]+=r["imgs"]
                    if r["ans"] is not None and qs[-1]["ans"] is None: qs[-1]["ans"]=r["ans"]
                elif r["words"]: orph.append((pi,"".join(t for _,_,t in r["words"])[:25]))
    for q in qs:
        L=collections.defaultdict(list)
        for k,x,t in q.pop("w"): L[k].append((x,t))
        q["text"]="\n".join("".join(t for _,t in sorted(v)) for k,v in sorted(L.items()))
    return qs,orph

for tag in DOCS:
    qs,orph=build(tag); nos=[q["no"] for q in qs]
    print("="*62); print(tag,len(qs),"range",min(nos),max(nos))
    print("  dup",sorted(n for n,c in collections.Counter(nos).items() if c>1)[:8],
          "| missing",sorted(set(range(1,max(nos)+1))-set(nos))[:8],
          "| noans",[q["no"] for q in qs if q["ans"] is None][:8],
          "| empty",[q["no"] for q in qs if not q["text"].strip()][:8])
    bad=[q["no"] for q in qs if not all(len(re.findall(r'\(\s*%d\s*\)'%d,q["text"].translate(str.maketrans("１２３（）","123()"))))==1 for d in (1,2,3))]
    print("  option anomalies:",len(bad),bad[:10],"| orphans:",len(orph),orph[:3])
    print("  imgs:",sum(len(q['imgs']) for q in qs),"on",sum(1 for q in qs if q['imgs']))
    json.dump(qs,open(f"v13_{tag}.json","w"),ensure_ascii=False,indent=1)
