import json, sys
rows = [json.loads(l) for l in open(sys.argv[1])]
def idx(mark):
    return next((i for i, e in enumerate(rows) if e.get('mark') == mark), None)
ex = idx('msg:execute'); fp = idx('flow-post')
print(f"execute=i[{ex}] flow-post=i[{fp}]  window={fp-ex if (ex and fp) else '?'} entries\n")
for e in rows[ex+1:fp]:
    if e.get('mark'):
        print(f"  --- MARK {e['mark']} ---"); continue
    o = e.get('o', ''); k = e.get('k', ''); kind = e.get('kind', ''); v = str(e.get('v', ''))[:46]
    extra = ''
    for f in ('args', 'dim', 'keys', 'out', 'algo'):
        if f in e: extra += f" {f}={str(e[f])[:40]}"
    print(f"  {kind:5} {o}.{k} = {v}{extra}")
