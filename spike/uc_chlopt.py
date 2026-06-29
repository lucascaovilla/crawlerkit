#!/usr/bin/env python3
"""GROUND TRUTH: run real Chrome (uc), let it solve, then dump the challenge iframe's _cf_chl_opt + the
event sequence + whether it did a flow POST. Lets us diff uc's server-issued challenge config vs ours, to
decide if the easy/hard fork is server-side (request fingerprint) or client-side (our jsdom VM diverging).

Site isolation OFF so we can read the cross-origin iframe's window._cf_chl_opt via JS. Dev/ground-truth
ONLY (never imported by crawlerkit)."""
import json
import sys
import time
import undetected_chromedriver as uc

PORTAL = "https://www.detran.pa.gov.br/"
FORM = ("https://sistemas-renavam.detran.pa.gov.br/sistransito/detran-web/"
        "servicos/infracao/indexConsultaInfracao.jsf")
REC = ("(function(){if(window.__m)return;window.__m=[];addEventListener('message',function(e){"
       "try{window.__m.push({t:Math.round(performance.now()),d:e.data})}catch(_){}},true);})();")
FIND_IFRAME = r"""
function find(root){var ifr=root.querySelector?root.querySelector('iframe[src*="challenge-platform"]'):null;
if(ifr)return ifr;var all=root.querySelectorAll?root.querySelectorAll('*'):[];
for(var i=0;i<all.length;i++){if(all[i].shadowRoot){var f=find(all[i].shadowRoot);if(f)return f;}}return null;}
return find(document);"""

# Decision-window + anti-tamper env probe. The challenge iframe lives in a CLOSED shadow root, so
# switch_to.frame can't reach it. Instead inject this via Page.addScriptToEvaluateOnNewDocument: it runs
# in EVERY new document — including the challenge iframe AND the VM's OWN pristine child iframe (depth>0)
# that it uses for anti-tamper — and is NON-DISRUPTIVE (reads only, creates no DOM). It captures env at a
# few delays (bracketing the ~3.5s decision) and postMessages each snapshot to `top`, where the REC
# listener stashes it in window.__m; we extract the `__ifenv` messages afterward. Diff vs our jsdom
# sidecar's iframe realm to find what flips uc onto the local-mint (easy) path.
INJECT_PROBE = r"""(function(){try{
// NB: this runs at document-creation when an iframe's location is still about:blank (before the real URL
// commits), so we must NOT gate-and-return here. Schedule captures unconditionally and re-check href in
// cap() (by then the challenge URL has committed). Capture challenge-platform realms AND about:blank child
// realms (depth>=1) = the VM's pristine anti-tamper iframe.
function srcOf(f){try{return Function.prototype.toString.call(f)}catch(e){return 'ERR:'+e}}
function depth(){var d=0,w=window;try{while(w&&w!==w.parent){d++;w=w.parent}}catch(e){}return d}
function cap(tag){try{var h='';try{h=location.href||''}catch(_){}
var dep=depth();if(h.indexOf('challenge-platform')<0 && !(h==='about:blank'&&dep>=1)) return;
var n=navigator,o={tag:tag,href:h.slice(0,90),depth:dep};
o.ua=n.userAgent;o.platform=n.platform;o.webdriver=n.webdriver;o.hwc=n.hardwareConcurrency;o.devmem=n.deviceMemory;o.maxTouch=n.maxTouchPoints;
o.gpu_tostr=Object.prototype.toString.call(n.gpu);o.gpu_keys=n.gpu?Object.keys(n.gpu):null;
try{o.gpu_chain=[];var g=n.gpu;for(var i=0;i<4&&g;i++){o.gpu_chain.push(Object.prototype.toString.call(g));g=Object.getPrototypeOf(g)}}catch(e){o.gpu_chain='E:'+e}
o.uadata=n.userAgentData?{brands:n.userAgentData.brands,mobile:n.userAgentData.mobile,platform:n.userAgentData.platform}:null;
o.readyState=document.readyState;o.compatMode=document.compatMode;
o.win_chrome=!!window.chrome;o.chrome_keys=window.chrome?Object.keys(window.chrome):null;
o.win_ownprops=Object.getOwnPropertyNames(window).length;
o.perf_entries=(performance.getEntries()||[]).map(function(e){return e.entryType+'|'+(e.name||'').slice(0,70)});
o.perf_res=(performance.getEntriesByType('resource')||[]).length;
o.subtle=!!(window.crypto&&window.crypto.subtle);
o.src_fetch=srcOf(window.fetch);o.src_xhrOpen=srcOf(XMLHttpRequest.prototype.open);
o.src_funcToStr=srcOf(Function.prototype.toString);o.src_getRandom=srcOf(window.crypto&&crypto.getRandomValues);
o.src_appendChild=srcOf(Node.prototype.appendChild);o.src_attachShadow=srcOf(Element.prototype.attachShadow);
o.src_createElement=srcOf(document.createElement);
try{top.postMessage({__ifenv:o},'*')}catch(e){}}catch(e){try{top.postMessage({__ifenv:{tag:tag,ERR:String(e)}},'*')}catch(_){}}}
[600,1500,2500,3300].forEach(function(t){setTimeout(function(){cap(t)},t)});
}catch(e){}})();"""


def main():
    o = uc.ChromeOptions()
    o.add_argument("--window-size=1920,1080")
    o.add_argument("--lang=pt-BR,pt")
    o.add_argument("--disable-features=IsolateOrigins,site-per-process")  # read iframe realm via JS
    o.add_argument("--disable-site-isolation-trials")  # force the cf iframe in-process so injected probe reaches it
    o.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    d = uc.Chrome(options=o, version_main=144)
    try:
        d.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": REC})
        d.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": INJECT_PROBE})
        d.execute_cdp_cmd("Network.enable", {})
        d.get(PORTAL); time.sleep(2.5)
        d.get(FORM)
        tok = ""
        iframe_html = ""
        seen = set()
        ifenv = None
        for _ in range(150):
            # scan CDP log for the iframe response, grab its body before eviction
            for entry in d.get_log("performance"):
                try:
                    msg = json.loads(entry["message"])["message"]
                    if msg.get("method") == "Network.responseReceived":
                        u = msg["params"]["response"]["url"]
                        rid = msg["params"]["requestId"]
                        if "turnstile/f/ov2" in u and rid not in seen:
                            seen.add(rid)
                            try:
                                body = d.execute_cdp_cmd("Network.getResponseBody", {"requestId": rid})
                                iframe_html = body.get("body", "") or iframe_html
                                if iframe_html:
                                    print(f"[uc] got iframe body len={len(iframe_html)}")
                            except Exception as e:
                                print("[uc] getBody err", str(e)[:60])
                except Exception:
                    pass
            # (iframe-realm env arrives via the injected probe's postMessages -> window.__m; extracted below.)
            tok = d.execute_script("var i=document.querySelector('input[name=cf-turnstile-response]');return i?i.value:'';") or ""
            if len(tok) > 20:
                break
            time.sleep(0.4)
        print(f"[uc] token_len={len(tok)} {'*** MINTED ***' if tok else 'NO TOKEN'}")
        if iframe_html:
            import re
            m = re.search(r"window\._cf_chl_opt\s*=\s*(\{.*?\});", iframe_html, re.S)
            open("spike/_uc_iframe.html", "w").write(iframe_html)
            print("[uc] saved spike/_uc_iframe.html ; _cf_chl_opt match:", bool(m))
            if m:
                open("spike/_uc_chlopt_raw.txt", "w").write(m.group(1))
                print("[uc] _cf_chl_opt raw len", len(m.group(1)))
        # network: did it do a flow POST?
        flow = []
        for entry in d.get_log("performance"):
            try:
                m = json.loads(entry["message"])["message"]
                if m.get("method") == "Network.requestWillBeSent":
                    u = m["params"]["request"]["url"]
                    if "challenge-platform" in u:
                        flow.append((m["params"]["request"]["method"], u.split("/cdn-cgi")[-1][:60]))
            except Exception:
                pass
        print("[uc] CF requests:", json.dumps(flow, indent=0)[:600])
        msgs = d.execute_script("return window.__m||[];") or []
        # event sequence (top)
        evs = [(x["d"].get("event")) for x in msgs
               if isinstance(x.get("d"), dict) and x["d"].get("event") not in (None, "meow", "food")]
        print("[uc] events:", evs)
        # iframe-realm env snapshots (from the injected probe's postMessages)
        ifenvs = [x["d"]["__ifenv"] for x in msgs
                  if isinstance(x.get("d"), dict) and isinstance(x["d"].get("__ifenv"), dict)]
        if ifenvs:
            json.dump(ifenvs, open("spike/_uc_ifenv.json", "w"), indent=1, default=str)
            tags = [(e.get("tag"), e.get("depth"), e.get("readyState")) for e in ifenvs]
            print(f"[uc] saved spike/_uc_ifenv.json ({len(ifenvs)} snapshots) tags(t,depth,rs)={tags}")
        else:
            print("[uc] WARNING: no __ifenv snapshots captured")
        # iframe _cf_chl_opt (best-effort; closed shadow root usually hides the iframe element from us)
        ifr = d.execute_script(FIND_IFRAME)
        if ifr:
            try:
                d.switch_to.frame(ifr)
                opt = d.execute_script("try{return JSON.parse(JSON.stringify(window._cf_chl_opt||{}))}catch(e){return {ERR:String(e)}}")
                d.switch_to.default_content()
                json.dump(opt, open("spike/_uc_chlopt.json", "w"), indent=1, default=str)
                print(f"[uc] iframe _cf_chl_opt keys={len(opt)}; saved spike/_uc_chlopt.json")
            except Exception as e:
                try: d.switch_to.default_content()
                except Exception: pass
                print("[uc] _cf_chl_opt read failed:", str(e)[:80])
        else:
            print("[uc] challenge iframe element not reachable (closed shadow root) — _cf_chl_opt skipped")
        return 0 if tok else 1
    finally:
        try: d.quit()
        except Exception: pass


if __name__ == "__main__":
    sys.exit(main())
