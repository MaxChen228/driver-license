import pdfplumber, re, json, collections
NUM=re.compile(r'^\d{1,4}$'); ANS=re.compile(r'^[（(]?([1-3１２３])[）)]?$'); T=str.maketrans("１２３","123")
P="/Users/chenliangyu/Downloads/汽車筆試題庫_公告115.06.9.pdf"; HDR=52; BOT=790
SKIP={"題目","題目內容","題","目","內容","圖示","分類","答","案","題號","▎題號","▎"}
def dedup(tabs):
    keep=[]
    for t in tabs:
        x0,y0,x1,y1=t.bbox
        if any(o is not t and o.bbox[0]-1<=x0 and o.bbox[1]-1<=y0 and x1<=o.bbox[2]+1 and y1<=o.bbox[3]+1
               and (o.bbox[2]-o.bbox[0])*(o.bbox[3]-o.bbox[1])>(x1-x0)*(y1-y0) for o in tabs): continue
        keep.append(t)
    return keep

qs=[]; orph=[]; claimed=0; total=0
with pdfplumber.open(P) as pdf:
    for pi,page in enumerate(pdf.pages,1):
        chars=[c for c in page.chars if HDR<(c["top"]+c["bottom"])/2<BOT and c["text"].strip()]
        total+=len(chars)
        tabs=sorted(dedup(page.find_tables()),key=lambda t:(t.bbox[1],t.bbox[0]))
        # build ordered list of (row_index_global, cells) with center-based char buckets
        units=[]   # (ytop, xleft, bucket)
        for t in tabs:
            for r in t.rows:
                cells=[c for c in r.cells if c]
                units.append({"y":r.bbox[1],"y1":r.bbox[3],"cells":sorted(cells,key=lambda c:c[0]),"buf":collections.defaultdict(list)})
        units.sort(key=lambda u:(u["y"],))
        for c in chars:
            cx=(c["x0"]+c["x1"])/2; cy=(c["top"]+c["bottom"])/2
            hit=None
            for ui,u in enumerate(units):
                for ci,cell in enumerate(u["cells"]):
                    if cell[0]-0.5<=cx<=cell[2]+0.5 and cell[1]-0.5<=cy<=cell[3]+0.5: hit=(ui,ci); break
                if hit: break
            if hit is None: orph.append((pi,c["text"])); continue
            claimed+=1
            units[hit[0]]["buf"][hit[1]].append((round(c["top"]/3),c["x0"],c["text"]))
        for u in units:
            cellstr=[]
            for ci in range(len(u["cells"])):
                L=collections.defaultdict(list)
                for k,x,t in u["buf"].get(ci,[]): L[k].append((x,t))
                cellstr.append("\n".join("".join(t for _,t in sorted(v)) for k,v in sorted(L.items())))
            nz=[s for s in cellstr if s.strip()]
            if not nz or nz[0] in ("題號","▎題號"): continue
            if NUM.match(nz[0].strip()) and len(nz[0].strip())<=4:
                m=ANS.match(nz[1].strip()) if len(nz)>1 else None
                qs.append({"no":int(nz[0]),"ans":int(m.group(1).translate(T)) if m else None,
                           "page":pi,"_uy":u["y"],"lines":[s for s in nz[(2 if m else 1):] if s not in SKIP],"imgs":[]})
            elif qs: qs[-1]["lines"]+=[s for s in nz if s not in SKIP]
        pg=[q for q in qs if q["page"]==pi]
        for im in [i for i in page.images if i["x1"]-i["x0"]>12 and i["bottom"]-i["top"]>12]:
            imy=(im["top"]+im["bottom"])/2; tgt=None
            for u in units:
                if u["y"]-0.5<=imy<=u["y1"]+0.5:
                    band=[q for q in pg if q.get("_uy") is not None and abs(q["_uy"]-u["y"])<0.6]
                    if band: tgt=band[0]; break
            if tgt is None:
                cand=[q for q in pg if q.get("_uy") is not None and q["_uy"]<=imy]
                tgt=cand[-1] if cand else (pg[-1] if pg else qs[-1])
            tgt["imgs"].append([round(im["x0"],1),round(im["top"],1),round(im["x1"],1),round(im["bottom"],1)])
for q in qs: q["text"]="\n".join(q.pop("lines")); q.pop("_uy",None)
nos=[q["no"] for q in qs]
print("car v9:",len(qs),"range",min(nos),max(nos))
print("  dup",sorted(n for n,c in collections.Counter(nos).items() if c>1)[:8],
      "| missing",sorted(set(range(1,max(nos)+1))-set(nos))[:8],
      "| noans",[q["no"] for q in qs if q["ans"] is None][:8],
      "| empty",[q["no"] for q in qs if not q["text"].strip()][:8])
bad=[q["no"] for q in qs if not all(len(re.findall(r'\(\s*%d\s*\)'%d,q["text"].translate(str.maketrans("１２３（）","123()"))))==1 for d in (1,2,3))]
print("  option anomalies:",len(bad),bad[:8])
print("  chars total",total,"claimed",claimed,"orphan",len(orph),collections.Counter(o[1] for o in orph).most_common(6))
json.dump(qs,open("v13_car.json","w"),ensure_ascii=False,indent=1)
