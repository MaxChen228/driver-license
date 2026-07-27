import pdfplumber, json, os
CFG={"car":("/Users/chenliangyu/Downloads/汽車筆試題庫_公告115.06.9.pdf","C"),
     "moto":("/Users/chenliangyu/Downloads/機車駕照筆試題庫(全部804題)-1150218.pdf","M")}
for tag,(path,pre) in CFG.items():
    doc=json.load(open(f"/Users/chenliangyu/Downloads/{tag}_questions.json"))
    outdir=f"/Users/chenliangyu/Downloads/{tag}_images"; os.makedirs(outdir,exist_ok=True)
    bypage={}
    for q in doc["questions"]:
        for i,im in enumerate(q["images"]): bypage.setdefault(q["page"],[]).append((q["id"],i,im))
    n=0
    with pdfplumber.open(path) as pdf:
        for pg,items in sorted(bypage.items()):
            page=pdf.pages[pg-1]
            for qid,i,im in items:
                x0,t,x1,b=im["bbox"]; pad=2
                crop=page.crop((max(0,x0-pad),max(0,t-pad),min(page.width,x1+pad),min(page.height,b+pad)))
                fn=f"{qid}_{i+1}.png"
                crop.to_image(resolution=220).save(os.path.join(outdir,fn))
                im["file"]=f"{tag}_images/{fn}"; n+=1
    json.dump(doc,open(f"/Users/chenliangyu/Downloads/{tag}_questions.json","w"),ensure_ascii=False,indent=2)
    print(tag,"exported",n,"images ->",outdir)
