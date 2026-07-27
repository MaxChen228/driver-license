import json, re, collections

SECT={
 "car":[(1,126,"架構一 正確觀念與態度",None),(1,168,None,"禁止不當行為（酒駕、不使用手機、危險駕駛）"),
        (8,288,None,"其他（平交道、強制險、環保駕駛、特殊天候、駕駛道德）"),
        (32,60,"架構二 主動停讓文化",None),(32,102,None,"路口安全（有號誌路口、無號誌路口、停讓行人）"),
        (43,336,None,"轉彎（左右轉、迴轉）"),(47,231,None,"注意大型車行駛及轉彎（內輪差、視野死角、不並行）"),
        (50,60,"架構三 安全駕駛能力",None),(50,102,None,"行駛中應注意事項（保持安全車距、注意前車狀況）"),
        (62,285,None,"正確使用燈光（頭燈、霧燈、方向燈）"),(64,276,None,"貨物裝載（防止掉落或滲漏）"),
        (66,252,None,"事故預防及處理（預防國道二次事故、急救常識）"),(72,225,None,"行車檢查（設備、燈光）")],
 "moto":[(1,324,"正確觀念與態度",None),(1,384,None,"貨物裝載"),(3,132,None,"事故預防及處理"),
         (7,216,None,"禁止不當行為（酒駕、不使用手機、危險駕駛）"),(12,336,None,"行車檢查（設備、燈光）"),
         (17,390,None,"平交道、強制險、環保駕駛、特殊天候、駕駛道德"),
         (36,72,"主動停讓文化",None),(36,135,None,"路口安全（有號誌路口、無號誌路口、停讓行人）"),
         (43,321,None,"注意大型車行駛及轉彎（內輪差、視野死角、不並行）"),
         (45,72,"安全駕駛能力",None),(45,135,None,"轉彎（左右轉、迴轉）"),
         (48,381,None,"行駛中應注意事項（保持安全車距、注意前車狀況）"),(61,603,None,"正確使用燈光（頭燈、霧燈、方向燈）")],
}
META={"car":dict(title="新版汽車筆試題庫",version="115.5.29",file="汽車筆試題庫_公告115.06.9.pdf",pages=80,prefix="C"),
      "moto":dict(title="機車駕照筆試題庫",version="115.1",file="機車駕照筆試題庫(全部804題)-1150218.pdf",pages=63,prefix="M")}
HALF=str.maketrans("１２３４５６７８９０（）","1234567890()")

def split_q(text):
    t=re.sub(r'[ 　]+',' ',text.replace("\n","")).translate(HALF).strip()
    i=[t.find("(1)"),t.find("(2)"),t.find("(3)")]
    assert -1 not in i and i[0]<i[1]<i[2], t[:60]
    return t[:i[0]].strip(), [t[i[0]+3:i[1]].strip(), t[i[1]+3:i[2]].strip(), t[i[2]+3:].strip()]

for tag in ("car","moto"):
    qs=json.load(open(f"v13_{tag}.json")); m=META[tag]
    qs.sort(key=lambda q:q["no"])
    arch=cat=None; ptr=0; sec=SECT[tag]; out=[]
    for q in qs:
        if q["no"]==3 and tag=="car":
            q["text"]=re.sub(r'\n?▎\n?題號\s*$','',q["text"])
        while ptr<len(sec) and (sec[ptr][0]<q["page"] or (sec[ptr][0]==q["page"])):
            a,c=sec[ptr][2],sec[ptr][3]
            if a: arch=a; cat=None
            if c: cat=c
            ptr+=1
        stem,opts=split_q(q["text"])
        out.append({
            "id":f"{m['prefix']}{q['no']:04d}", "number":q["no"], "answer":q["ans"],
            "stem":stem, "stem_is_image": stem=="",
            "options":[{"n":i+1,"text":o,"is_image":o==""} for i,o in enumerate(opts)],
            "images":[{"bbox":b} for b in q["imgs"]],
            "architecture":arch, "category":cat, "page":q["page"]})
    doc={"source":{**{k:v for k,v in m.items() if k!="prefix"},"question_count":len(out),
                   "extracted_with":"pdfplumber, char-level lossless (EXTRA=0 verified)"},
         "questions":out}
    json.dump(doc,open(f"/Users/chenliangyu/Downloads/{tag}_questions.json","w"),ensure_ascii=False,indent=2)
    # validation
    assert [q["number"] for q in out]==list(range(1,len(out)+1))
    assert all(q["answer"] in (1,2,3) for q in out)
    assert all(q["stem"] or q["images"] for q in out)
    print(f"{tag}: {len(out)} questions -> ~/Downloads/{tag}_questions.json")
    print("   answers:",collections.Counter(q["answer"] for q in out))
    print("   image-stem:",sum(q["stem_is_image"] for q in out),
          "| image-options:",sum(1 for q in out if any(o["is_image"] for o in q["options"])),
          "| unassigned category:",sum(1 for q in out if not q["category"]))
    print("   per-architecture:",collections.Counter(q["architecture"] for q in out))
