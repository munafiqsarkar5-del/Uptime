
import os
import time
import uuid
import socket
import ipaddress
import threading
import urllib.request
import urllib.error
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

CHECK_INTERVAL = 15
MAX_WORKERS = 5
lock = threading.RLock()
sites = {}
pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)
scheduler_started = False


def validate_url(url):
    try:
        p = urlparse(url.strip())
        if p.scheme not in ("http", "https") or not p.hostname:
            return False, "Enter a valid HTTP/HTTPS URL."
        if p.username or p.password:
            return False, "URLs containing credentials are not allowed."
        if p.port and p.port not in (80, 443, 8080, 8443):
            return False, "Unsupported port."
        if p.hostname.lower() == "localhost":
            return False, "Local addresses are not allowed."

        addresses = socket.getaddrinfo(
            p.hostname, p.port or (443 if p.scheme == "https" else 80),
            type=socket.SOCK_STREAM
        )
        if not addresses:
            return False, "Domain could not be resolved."

        for item in addresses:
            ip = ipaddress.ip_address(item[4][0])
            if not ip.is_global:
                return False, "Private IP addresses are blocked."

        return True, ""
    except Exception:
        return False, "Invalid URL or DNS lookup failed."


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = urllib.request.build_opener(NoRedirect)


def check_site(site_id, url):
    start = time.monotonic()
    ok, reason = validate_url(url)

    if not ok:
        online, ms, message = False, 0, reason
    else:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "SufyanUptime/1.0"}
            )
            with opener.open(req, timeout=4) as response:
                code = response.status
                online = 200 <= code < 400
                message = f"HTTP {code}"
        except urllib.error.HTTPError as e:
            online = False
            message = f"HTTP {e.code}"
        except Exception as e:
            online = False
            message = type(e).__name__

        ms = round((time.monotonic() - start) * 1000)

    with lock:
        site = sites.get(site_id)
        if site is None:
            return

        site["checks"] += 1
        site["success"] += int(online)
        site["failures"] += int(not online)
        site["online"] = online
        site["response_ms"] = ms
        site["message"] = message
        site["last_check"] = time.strftime("%Y-%m-%d %H:%M:%S")
        site["history"].append({
            "ok": online,
            "time": time.strftime("%H:%M:%S")
        })
        site["history"] = site["history"][-20:]
        site["busy"] = False
        site["next_check"] = time.monotonic() + CHECK_INTERVAL


def scheduler():
    while True:
        now = time.monotonic()
        jobs = []

        with lock:
            for sid, site in sites.items():
                if not site["busy"] and now >= site["next_check"]:
                    site["busy"] = True
                    jobs.append((sid, site["url"]))

        for sid, url in jobs:
            try:
                pool.submit(check_site, sid, url)
            except RuntimeError:
                with lock:
                    if sid in sites:
                        sites[sid]["busy"] = False

        time.sleep(1)


PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SUFYAN UPTIME</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#070b12;color:#eef4ff;font-family:Arial,sans-serif}
header{text-align:center;padding:30px 12px;background:linear-gradient(140deg,#101d31,#080b12);border-bottom:1px solid #23344a}
.logo{font-size:30px;font-weight:900;letter-spacing:2px;color:#00ffc3}
header p{color:#8799b3;font-size:12px;letter-spacing:2px}
main{max-width:1000px;margin:24px auto;padding:0 14px}
.card{background:#101724;border:1px solid #27354a;border-radius:15px;padding:18px;margin-bottom:15px}
input{width:100%;padding:14px;border-radius:9px;background:#080e18;border:1px solid #35455d;color:white;margin:9px 0}
button{padding:11px 14px;border:0;border-radius:8px;font-weight:bold;cursor:pointer}
.add{background:#00ffc3;color:#05140f}
.remove{background:#ff4765;color:white}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;margin-top:15px}
.stat{background:#080e18;border-radius:9px;padding:12px}
small{color:#8da0ba}
.num{font-size:20px;font-weight:bold;margin-top:7px;overflow-wrap:anywhere}
.green{color:#00ffc3}.red{color:#ff526d}.muted{color:#92a2ba}
.sitehead{display:flex;justify-content:space-between;align-items:start;gap:10px}
.siteurl{font-weight:bold;overflow-wrap:anywhere;word-break:break-word}
.status{font-size:12px;font-weight:bold;margin-top:8px}
.history{display:flex;gap:4px;margin-top:12px}
.tick{height:7px;flex:1;border-radius:4px;background:#3b4659}
.tick.good{background:#00dca7}.tick.bad{background:#ff4765}
#notice{color:#ffce78;font-size:13px;margin-top:10px}
@media(max-width:500px){.stats{grid-template-columns:repeat(2,1fr)}.logo{font-size:25px}}
</style>
</head>
<body>
<header>
 <div class="logo">⚡ SUFYAN UPTIME</div>
 <p>UNLIMITED URL MONITORING DASHBOARD</p>
</header>
<main>
 <section class="card">
  <h3>＋ Add Website</h3>
  <form id="addform">
   <input id="url" type="url" placeholder="https://your-website.com" required>
   <button class="add" type="submit">ADD WEBSITE</button>
  </form>
  <p class="muted">Add multiple public websites. Checks run approximately every 15 seconds per site when resources are available.</p>
  <div id="notice"></div>
 </section>
 <div class="sitehead" style="padding:5px 3px 12px">
  <b>YOUR MONITORS</b><span class="muted" id="count">0 websites</span>
 </div>
 <div id="sites"></div>
</main>
<script>
const $ = id => document.getElementById(id);
async function api(path, body){
 const r=await fetch(path,{
  method:body?'POST':'GET',
  headers:body?{'Content-Type':'application/json'}:{},
  body:body?JSON.stringify(body):undefined
 });
 const d=await r.json();
 if(!r.ok)throw Error(d.error||'Request failed');
 return d;
}
$('addform').onsubmit=async e=>{
 e.preventDefault();
 $('notice').textContent='Adding website...';
 try{
  await api('/api/add',{url:$('url').value.trim()});
  $('url').value='';
  $('notice').textContent='Website added!';
  refresh();
 }catch(err){$('notice').textContent=err.message;}
};
async function removeSite(id){
 if(!confirm('Remove this website?'))return;
 try{await api('/api/remove',{id});refresh();}
 catch(e){$('notice').textContent=e.message;}
}
function safeText(tag, cls, value){
 const el=document.createElement(tag);
 el.className=cls||'';
 el.textContent=value;
 return el;
}
function makeCard(s){
 const card=document.createElement('section');
 card.className='card';
 const head=document.createElement('div');
 head.className='sitehead';
 const left=document.createElement('div');
 left.style.flex='1';
 left.appendChild(safeText('div','siteurl',s.url));
 left.appendChild(safeText('div','status '+(s.online?'green':s.checks?'red':'muted'),
   s.checks?(s.online?'● ONLINE':'● OFFLINE'):'◌ WAITING'));
 left.appendChild(safeText('div','muted',s.message));
 const btn=safeText('button','remove','REMOVE');
 btn.onclick=()=>removeSite(s.id);
 head.append(left,btn);
 card.appendChild(head);

 const stats=document.createElement('div');
 stats.className='stats';
 [
  ['UPTIME',s.checks?((s.success/s.checks)*100).toFixed(2)+'%':'—'],
  ['RESPONSE',s.checks?s.response_ms+' ms':'—'],
  ['CHECKS',s.checks],
  ['SUCCESSES',s.success],
  ['FAILURES',s.failures],
  ['LAST CHECK',s.last_check||'Waiting']
 ].forEach(([label,value])=>{
  const box=document.createElement('div');box.className='stat';
  box.append(safeText('small','',label),safeText('div','num',String(value)));
  stats.appendChild(box);
 });
 card.appendChild(stats);
 const hist=document.createElement('div');hist.className='history';
 s.history.forEach(h=>{
  const tick=safeText('div','tick '+(h.ok?'good':'bad'),'');
  tick.title=h.time+(h.ok?' Online':' Failed');hist.appendChild(tick);
 });
 card.appendChild(hist);
 return card;
}
async function refresh(){
 try{
  const d=await api('/api/sites');
  $('count').textContent=d.sites.length+' websites';
  const root=$('sites');root.replaceChildren();
  if(!d.sites.length)root.appendChild(safeText('section','card muted','No websites added yet.'));
  d.sites.forEach(s=>root.appendChild(makeCard(s)));
 }catch(e){}
}
refresh();
setInterval(refresh,3000);
</script>
</body>
</html>
"""


@app.get("/")
def home():
    return render_template_string(PAGE)


@app.get("/api/sites")
def list_sites():
    with lock:
        result = []
        for sid, site in sites.items():
            item = dict(site)
            item["id"] = sid
            item["history"] = list(site["history"])
            result.append(item)
    return jsonify(sites=result)


@app.post("/api/add")
def add_site():
    body = request.get_json(silent=True) or {}
    url = str(body.get("url", "")).strip()

    if len(url) > 2048:
        return jsonify(error="URL is too long."), 400

    ok, reason = validate_url(url)
    if not ok:
        return jsonify(error=reason), 400

    with lock:
        if any(s["url"] == url for s in sites.values()):
            return jsonify(error="This URL is already added."), 409

        sid = uuid.uuid4().hex[:12]
        sites[sid] = {
            "url": url,
            "online": False,
            "checks": 0,
            "success": 0,
            "failures": 0,
            "response_ms": 0,
            "last_check": None,
            "message": "Waiting for first check",
            "history": [],
            "busy": False,
            "next_check": time.monotonic()
        }

    return jsonify(ok=True)


@app.post("/api/remove")
def remove_site():
    body = request.get_json(silent=True) or {}
    sid = str(body.get("id", ""))
    with lock:
        if sid not in sites:
            return jsonify(error="Website not found."), 404
        del sites[sid]
    return jsonify(ok=True)


if not scheduler_started:
    scheduler_started = True
    threading.Thread(target=scheduler, daemon=True).start()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        threaded=True
    )
