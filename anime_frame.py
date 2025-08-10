#!/usr/bin/env python3
"""
Anime Frame v2
- Kodi JSON-RPC playback control (resume timestamps)
- BH1750 light sensor (I2C)
- Flask web UI with posters, thumbnails, schedule, reorder, per-show progress
- SQLite DB for persistent state
"""

import os, json, time, threading, signal, sqlite3, subprocess, sys
from datetime import datetime, time as dt_time
from flask import Flask, render_template_string, request, jsonify, send_from_directory
import requests
from smbus2 import SMBus

# ---------------- CONFIG ----------------
KODI_URL = "http://localhost:8080/jsonrpc"
KODI_AUTH = ("", "")  # set if you used kodi username/password
ANIME_DIR = "/media/anime"
STATIC_DIR = "/home/pi/anime_static"
DB_FILE = "/home/pi/anime_frame.db"
BH1750_ADDR = 0x23
I2C_BUS = 1
LIGHT_THRESHOLD = 30   # lux
SENSOR_POLL = 3        # sec
FLASK_PORT = 5000

# ---------------- FLASK TEMPLATE ----------------
UI_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8">
<title>Anime Frame</title>
<style>
body{background:#071021;color:#e7f0f7;font-family:Inter,system-ui;padding:12px}
.header{display:flex;justify-content:space-between;align-items:center}
.controls button{margin-left:8px;padding:6px 10px;border-radius:8px;border:0;background:#13334a;color:#fff}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;margin-top:14px}
.card{background:#0b1b28;border-radius:10px;overflow:hidden}
.poster{height:220px;background:#09121a;background-size:cover;background-position:center}
.info{padding:10px}
.title{font-weight:700;margin:0 0 6px}
.meta{color:#8aa0b1;font-size:13px;margin-bottom:8px}
.btns{display:flex;gap:6px}
.btn{flex:1;padding:7px;border-radius:8px;border:0;background:#123b52;color:#fff;font-size:13px}
.settings{margin-top:18px;background:#071a29;padding:12px;border-radius:8px}
.small{font-size:12px;color:#8aa0b1}
</style></head><body>
<div class="header">
  <div>
    <h2 style="margin:0">Anime Frame</h2>
    <div class="small">Lux: <span id="lux">--</span> • Schedule: <span id="sched">--</span></div>
  </div>
  <div class="controls">
    <button onclick="fetch('/api/play').then(()=>load())">Play</button>
    <button onclick="fetch('/api/pause').then(()=>load())">Pause</button>
    <button onclick="fetch('/api/refresh').then(()=>load())">Refresh shows</button>
  </div>
</div>

<div style="display:flex;gap:14px;margin-top:12px">
  <form id="addForm" onsubmit="addShow(event)">
    <input name="show" placeholder="Add folder name" required style="padding:8px;border-radius:8px;border:0;width:260px">
    <button class="btn" style="margin-left:6px">Add</button>
  </form>

  <div class="settings">
    <div><strong>Playback settings</strong></div>
    <label class="small"><input id="use_light" type="checkbox"> Use light sensor</label><br>
    <label class="small"><input id="use_sched" type="checkbox"> Use schedule</label>
    <div style="margin-top:8px">
      <label class="small">Start: <input id="sched_start" type="time"></label>
      <label class="small" style="margin-left:10px">End: <input id="sched_end" type="time"></label>
      <button onclick="saveSched()" class="btn" style="margin-left:8px">Save</button>
    </div>
  </div>
</div>

<div id="grid" class="grid"></div>

<script>
async function load(){
  const r = await fetch('/api/state'); const st = await r.json();
  document.getElementById('lux').innerText = st.lux ?? '--';
  document.getElementById('sched').innerText = st.schedule_enabled ? `${st.schedule_start} → ${st.schedule_end}` : 'disabled';
  document.getElementById('use_light').checked = st.use_light;
  document.getElementById('use_sched').checked = st.schedule_enabled;
  document.getElementById('sched_start').value = st.schedule_start || '';
  document.getElementById('sched_end').value = st.schedule_end || '';
  render(st.playlist);
}

function el(tag,cls,html){ let e=document.createElement(tag); if(cls) e.className=cls; if(html!==undefined) e.innerHTML=html; return e; }

function render(list){
  const g = document.getElementById('grid'); g.innerHTML='';
  list.forEach(item=>{
    const c = el('div','card');
    const poster = el('div','poster'); poster.style.backgroundImage = `url("/poster/${encodeURIComponent(item.name)}?t=${Date.now()}")`;
    c.appendChild(poster);
    const info=el('div','info');
    info.appendChild(el('div','title',item.name));
    info.appendChild(el('div','meta',`Ep: ${item.current_ep_name || '?'} • progress: ${item.position || '0:00'}`));
    const btns=el('div','btns');
    const play=el('button','btn','Start'); play.onclick=()=>fetch(`/api/start/${encodeURIComponent(item.name)}`).then(()=>load());
    const restart=el('button','btn','Restart'); restart.onclick=()=>fetch(`/api/restart/${encodeURIComponent(item.name)}`).then(()=>load());
    const remove=el('button','btn','Remove'); remove.onclick=()=>{ if(confirm('Remove?')) fetch(`/api/remove/${encodeURIComponent(item.name)}`).then(()=>load()); };
    btns.appendChild(play); btns.appendChild(restart); btns.appendChild(remove);
    info.appendChild(btns); c.appendChild(info); g.appendChild(c);
  });
}

async function addShow(e){ e.preventDefault(); const fd=new FormData(e.target); await fetch('/api/add',{method:'POST',body:fd}); e.target.reset(); load(); }
async function saveSched(){
  const body = {use_light: document.getElementById('use_light').checked,
                schedule_enabled: document.getElementById('use_sched').checked,
                schedule_start: document.getElementById('sched_start').value,
                schedule_end: document.getElementById('sched_end').value};
  await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  load();
}
load();
setInterval(load,4000);
</script>
</body></html>"""

# ---------------- DB helpers ----------------
def init_db():
    need = not os.path.exists(DB_FILE)
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    cur = conn.cursor()
    if need:
        cur.execute("""CREATE TABLE shows (
            id INTEGER PRIMARY KEY,
            name TEXT UNIQUE,
            order_idx INTEGER,
            episode_index INTEGER DEFAULT 0,
            timestamp TEXT DEFAULT '00:00:00'
        )""")
        cur.execute("""CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )""")
        # default settings
        cur.execute("INSERT INTO settings(key,value) VALUES(?,?)", ("use_light","1"))
        cur.execute("INSERT INTO settings(key,value) VALUES(?,?)", ("schedule_enabled","0"))
        cur.execute("INSERT INTO settings(key,value) VALUES(?,?)", ("schedule_start","08:00"))
        cur.execute("INSERT INTO settings(key,value) VALUES(?,?)", ("schedule_end","23:00"))
        conn.commit()
    return conn

DB = init_db()
DB_LOCK = threading.Lock()

def db_all_shows():
    with DB_LOCK:
        cur = DB.cursor()
        cur.execute("SELECT name,order_idx,episode_index,timestamp FROM shows ORDER BY order_idx")
        return cur.fetchall()

def db_add_show(name):
    with DB_LOCK:
        cur = DB.cursor()
        # compute next order_idx
        cur.execute("SELECT COALESCE(MAX(order_idx),-1)+1 FROM shows")
        order_idx = cur.fetchone()[0]
        cur.execute("INSERT OR IGNORE INTO shows(name,order_idx) VALUES(?,?)", (name,order_idx))
        DB.commit()

def db_remove_show(name):
    with DB_LOCK:
        cur = DB.cursor()
        cur.execute("DELETE FROM shows WHERE name=?", (name,))
        DB.commit()

def db_get_setting(key):
    with DB_LOCK:
        cur = DB.cursor()
        cur.execute("SELECT value FROM settings WHERE key=?", (key,))
        r = cur.fetchone()
        return r[0] if r else None

def db_set_setting(key,val):
    with DB_LOCK:
        cur = DB.cursor()
        cur.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key,str(val)))
        DB.commit()

def db_update_progress(name, ep_index, timestamp_str):
    with DB_LOCK:
        cur = DB.cursor()
        cur.execute("UPDATE shows SET episode_index=?, timestamp=? WHERE name=?", (ep_index, timestamp_str, name))
        DB.commit()

def db_get_next_index(current_idx):
    with DB_LOCK:
        cur = DB.cursor()
        cur.execute("SELECT COUNT(*) FROM shows")
        c = cur.fetchone()[0]
        return (current_idx + 1) % c if c else 0

# ---------------- Utilities ----------------
def kodi_rpc(method, params=None):
    payload = {"jsonrpc":"2.0","id":1,"method":method}
    if params is not None: payload["params"] = params
    try:
        r = requests.post(KODI_URL, json=payload, auth=KODI_AUTH, timeout=4)
        return r.json()
    except Exception:
        return {}

def kodi_get_active_player():
    r = kodi_rpc("Player.GetActivePlayers")
    return r.get("result", [])

def kodi_get_time():
    players = kodi_get_active_player()
    if not players: return "00:00:00"
    pid = players[0]["playerid"]
    res = kodi_rpc("Player.GetProperties", {"playerid": pid, "properties":["time","percentage"]})
    time_info = res.get("result", {}).get("time", {"hours":0,"minutes":0,"seconds":0})
    h,m,s = time_info.get("hours",0), time_info.get("minutes",0), time_info.get("seconds",0)
    return f"{h:02d}:{m:02d}:{s:02d}"

def kodi_open_and_seek(path, timestamp_str):
    # open then seek
    kodi_rpc("Player.Open", {"item": {"file": path}})
    # simple sleep then seek (Kodi needs small time to open)
    time.sleep(1.2)
    players = kodi_get_active_player()
    if not players: return
    pid = players[0]["playerid"]
    # build value object
    h,m,s = map(int, timestamp_str.split(":"))
    kodi_rpc("Player.Seek", {"playerid": pid, "value": {"hours":h,"minutes":m,"seconds":s}})

def kodi_pause():
    players = kodi_get_active_player()
    if not players: return
    pid = players[0]["playerid"]
    kodi_rpc("Player.PlayPause", {"playerid": pid, "play": False})

# ---------------- Episode discovery & thumbnails ----------------
VIDEO_EXTS = (".mp4",".mkv",".m4v",".webm",".avi")
def list_shows_on_disk():
    return sorted([d for d in os.listdir(ANIME_DIR) if os.path.isdir(os.path.join(ANIME_DIR,d))])

def build_video_list(show):
    files=[]
    for root,_,fs in os.walk(os.path.join(ANIME_DIR,show)):
        for f in fs:
            if f.lower().endswith(VIDEO_EXTS):
                files.append(os.path.join(root,f))
    files.sort()
    return files

def ensure_thumbnail(show):
    # returns path accessible by poster endpoint: prefer poster.jpg/png, else generate thumb.png into STATIC_DIR/<show>.png
    sdir = os.path.join(ANIME_DIR, show)
    for name in ("poster.jpg","poster.png","cover.jpg","cover.png"):
        p = os.path.join(sdir, name)
        if os.path.exists(p): return p
    # generate if not exists
    target = os.path.join(STATIC_DIR, f"{show}.png")
    if os.path.exists(target): return target
    vids = build_video_list(show)
    if not vids:
        return os.path.join(STATIC_DIR,"fallback.png")
    first = vids[0]
    # run ffmpeg to extract frame at 00:00:05 (if available)
    try:
        os.makedirs(STATIC_DIR, exist_ok=True)
        cmd = ["ffmpeg","-y","-i", first, "-ss","00:00:05","-vframes","1","-vf","scale=400:-1", target]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=20)
        if os.path.exists(target): return target
    except Exception:
        pass
    return os.path.join(STATIC_DIR,"fallback.png")

# ---------------- Light sensor ----------------
def read_lux():
    try:
        with SMBus(I2C_BUS) as bus:
            data = bus.read_i2c_block_data(BH1750_ADDR, 0x10)
            lux = (data[0]<<8 | data[1]) / 1.2
            return lux
    except Exception:
        return None

# ---------------- Playback loop ----------------
running = True

def time_in_range(start_s, end_s, now=None):
    if not now: now = datetime.now().time()
    if start_s <= end_s:
        return start_s <= now <= end_s
    else:
        # crosses midnight
        return now >= start_s or now <= end_s

def playback_thread():
    # main loop: check DB shows in order, play when allowed
    while running:
        # read settings
        use_light = db_get_setting("use_light") == "1"
        schedule_enabled = db_get_setting("schedule_enabled") == "1"
        sched_start = db_get_setting("schedule_start") or "08:00"
        sched_end = db_get_setting("schedule_end") or "23:00"
        # determine permission to play
        allow_light = True
        if use_light:
            lux = read_lux()
            allow_light = (lux is not None and lux > LIGHT_THRESHOLD)
        else:
            lux = None
        allow_sched = True
        if schedule_enabled:
            s_h, s_m = map(int, sched_start.split(":"))
            e_h, e_m = map(int, sched_end.split(":"))
            allow_sched = time_in_range(dt_time(s_h,s_m), dt_time(e_h,e_m))
        # snapshot shows
        shows = db_all_shows()
        if not shows:
            # populate DB from disk if empty
            for s in list_shows_on_disk():
                db_add_show(s)
            time.sleep(SENSOR_POLL)
            continue
        # if both allowed (or respective disabled), play; implement: play only if (allow_light or not use_light) and (allow_sched or not schedule_enabled)
        if (allow_light or not use_light) and (allow_sched or not schedule_enabled):
            # get the first show by order that we should play (we pick the show with lowest order idx)
            with DB_LOCK:
                cur = DB.cursor()
                cur.execute("SELECT name,episode_index,timestamp FROM shows ORDER BY order_idx LIMIT 1")
                row = cur.fetchone()
            if not row:
                time.sleep(SENSOR_POLL); continue
            name, ep_idx, timestamp = row
            videos = build_video_list(name)
            if not videos:
                # nothing on disk for this show -> remove or skip. we skip and rotate
                with DB_LOCK:
                    DB.execute("UPDATE shows SET order_idx = (SELECT COALESCE(MAX(order_idx),0)+1 FROM shows) WHERE name=?", (name,))
                    DB.commit()
                time.sleep(1); continue
            # ensure index valid
            if ep_idx >= len(videos): ep_idx = 0
            file_to_play = videos[ep_idx]
            # start playback at timestamp
            kodi_open_and_seek(file_to_play, timestamp or "00:00:00")
            # while still allowed, update timestamp
            while True:
                time.sleep(SENSOR_POLL)
                if use_light:
                    lux_now = read_lux()
                    if not (lux_now is not None and lux_now > LIGHT_THRESHOLD):
                        # save and pause
                        cur_time = kodi_get_time()
                        db_update_progress(name, ep_idx, cur_time)
                        kodi_pause()
                        break
                if schedule_enabled:
                    s_h, s_m = map(int, sched_start.split(":"))
                    e_h, e_m = map(int, sched_end.split(":"))
                    if not time_in_range(dt_time(s_h,s_m), dt_time(e_h,e_m)):
                        cur_time = kodi_get_time()
                        db_update_progress(name, ep_idx, cur_time)
                        kodi_pause()
                        break
                # update timestamp periodically
                cur_time = kodi_get_time()
                db_update_progress(name, ep_idx, cur_time)
            # after pause, advance episode or restart series and rotate order
            videos = build_video_list(name)
            if ep_idx + 1 < len(videos):
                with DB_LOCK:
                    DB.execute("UPDATE shows SET episode_index=? WHERE name=?", (ep_idx+1, name))
                    DB.commit()
            else:
                # finished -> reset episode_index to 0 AND rotate show to end (so round-robin moves to next show)
                with DB_LOCK:
                    # set episode to 0 and bump order_idx to max+1 (rotate)
                    DB.execute("UPDATE shows SET episode_index=0 WHERE name=?", (name,))
                    DB.execute("UPDATE shows SET order_idx = (SELECT COALESCE(MAX(order_idx),0)+1 FROM shows)")
                    DB.commit()
            # small delay then continue loop
            time.sleep(0.5)
        else:
            # not allowed to play: update lux snapshot in DB settings for UI and sleep
            if lux is not None:
                db_set_setting("last_lux", str(lux))
            time.sleep(SENSOR_POLL)

# ---------------- Flask App ----------------
app = Flask(__name__)

@app.route("/")
def ui():
    return render_template_string(UI_TEMPLATE)

@app.route("/poster/<path:show>")
def poster(show):
    # prefer poster in folder; else generated thumbnail; else fallback
    p = ensure_thumbnail(show)
    if p.startswith(STATIC_DIR):
        # serve from static dir
        return send_from_directory(STATIC_DIR, os.path.basename(p))
    else:
        # serve from show dir
        showdir = os.path.join(ANIME_DIR, show)
        return send_from_directory(showdir, os.path.basename(p))

@app.route("/api/state")
def api_state():
    shows = []
    for name,order_idx,ep_idx,tstamp in db_all_shows():
        # compute current playing episode name text and position
        vids = build_video_list(name)
        cur_name = os.path.basename(vids[ep_idx]) if vids and ep_idx < len(vids) else None
        pos = tstamp or "00:00:00"
        shows.append({"name":name,"order":order_idx,"episode_index":ep_idx,"current_ep_name":cur_name,"position":pos})
    return jsonify({
        "playlist": shows,
        "use_light": db_get_setting("use_light") == "1",
        "schedule_enabled": db_get_setting("schedule_enabled") == "1",
        "schedule_start": db_get_setting("schedule_start"),
        "schedule_end": db_get_setting("schedule_end"),
        "lux": db_get_setting("last_lux")
    })

@app.route("/api/add", methods=["POST"])
def api_add():
    show = request.form.get("show","").strip()
    if show and os.path.isdir(os.path.join(ANIME_DIR,show)):
        db_add_show(show)
        return jsonify(success=True)
    return jsonify(success=False, msg="folder missing"), 400

@app.route("/api/remove/<path:show>", methods=["GET","DELETE"])
def api_remove(show):
    db_remove_show(show)
    return jsonify(success=True)

@app.route("/api/start/<path:show>")
def api_start(show):
    # move show to front (order_idx = min-1) so playback thread picks it next
    with DB_LOCK:
        cur = DB.cursor()
        cur.execute("SELECT COALESCE(MIN(order_idx),0)-1 FROM shows")
        new_idx = cur.fetchone()[0]
        cur.execute("UPDATE shows SET order_idx=? WHERE name=?", (new_idx, show))
        DB.commit()
    return jsonify(success=True)

@app.route("/api/restart/<path:show>")
def api_restart(show):
    with DB_LOCK:
        DB.execute("UPDATE shows SET episode_index=0, timestamp='00:00:00' WHERE name=?", (show,))
        # and move it to front
        cur = DB.cursor(); cur.execute("SELECT COALESCE(MIN(order_idx),0)-1 FROM shows"); new_idx = cur.fetchone()[0]
        DB.execute("UPDATE shows SET order_idx=? WHERE name=?", (new_idx, show))
        DB.commit()
    return jsonify(success=True)

@app.route("/api/pause")
def api_pause():
    # save timestamp for any active player
    players = kodi_get_active_player()
    if players:
        # save the current file name -> map it to DB show row
        # get currently playing item
        res = kodi_rpc("Player.GetItem", {"playerid": players[0]["playerid"], "properties":["file"]})
        path = res.get("result",{}).get("item",{}).get("file")
        if path:
            # find which show this path belongs to
            for show in list_shows_on_disk():
                if os.path.commonpath([os.path.abspath(path), os.path.abspath(os.path.join(ANIME_DIR,show))]) == os.path.abspath(os.path.join(ANIME_DIR,show)):
                    cur_time = kodi_get_time()
                    # find index of this file in show's videos
                    vids = build_video_list(show)
                    try:
                        idx = vids.index(path)
                    except ValueError:
                        idx = 0
                    db_update_progress(show, idx, cur_time)
                    break
    kodi_pause()
    return jsonify(success=True)

@app.route("/api/refresh")
def api_refresh():
    # sync disk shows into DB without removing existing
    disk = list_shows_on_disk()
    for s in disk:
        db_add_show(s)
    return jsonify(success=True)

@app.route("/api/settings", methods=["POST"])
def api_settings():
    data = request.get_json()
    db_set_setting("use_light", "1" if data.get("use_light") else "0")
    db_set_setting("schedule_enabled", "1" if data.get("schedule_enabled") else "0")
    db_set_setting("schedule_start", data.get("schedule_start") or "08:00")
    db_set_setting("schedule_end", data.get("schedule_end") or "23:00")
    return jsonify(success=True)

# ---------------- signal handling ----------------
def clean_exit(signum, frame):
    global running
    running = False
    # attempt a final timestamp save if playing
    try:
        players = kodi_get_active_player()
        if players:
            res = kodi_rpc("Player.GetItem", {"playerid": players[0]["playerid"], "properties":["file"]})
            path = res.get("result",{}).get("item",{}).get("file")
            cur_time = kodi_get_time()
            if path:
                for show in list_shows_on_disk():
                    if os.path.commonpath([os.path.abspath(path), os.path.abspath(os.path.join(ANIME_DIR,show))]) == os.path.abspath(os.path.join(ANIME_DIR,show)):
                        vids = build_video_list(show)
                        try:
                            idx = vids.index(path)
                        except ValueError:
                            idx = 0
                        db_update_progress(show, idx, cur_time)
                        break
    except Exception:
        pass
    sys.exit(0)

signal.signal(signal.SIGINT, clean_exit)
signal.signal(signal.SIGTERM, clean_exit)

# ---------------- start threads & app ----------------
if __name__ == "__main__":
    # ensure fallback exists
    os.makedirs(STATIC_DIR, exist_ok=True)
    if not os.path.exists(os.path.join(STATIC_DIR,"fallback.png")):
        # quick single-color fallback if user didn't provide
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGBA",(400,225),(20,30,40,255))
        img.save(os.path.join(STATIC_DIR,"fallback.png"))
    # seed DB from disk for any shows not present
    for s in list_shows_on_disk():
        db_add_show(s)
    t = threading.Thread(target=playback_thread, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=FLASK_PORT)
