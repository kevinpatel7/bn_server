"""
BN Terminal Server — Upstox Edition (All-in-One)
==================================================
Terminal is served directly from this server.
No HTML file needed on your device.

After deploying to Railway:
1. Open https://your-railway-url.up.railway.app
2. Click Login with Upstox (one time)
3. Then open https://your-railway-url.up.railway.app/terminal
   on any device — phone, laptop, anywhere

SETUP:
  pip install flask flask-cors requests gunicorn
  python bn_server.py
"""

import threading, time, os, requests, json
from datetime import datetime, date, timedelta
from flask import Flask, jsonify, redirect, request, Response
from flask_cors import CORS
from urllib.parse import urlencode

app = Flask(__name__)
CORS(app)

API_KEY      = os.environ.get("UPSTOX_API_KEY",    "2ea6b52c-e87b-4ae0-b84c-6395a73790a2")
API_SECRET   = os.environ.get("UPSTOX_API_SECRET", "urcjw710uv")
REDIRECT_URI = os.environ.get("REDIRECT_URI",      "http://127.0.0.1:5000/callback")
TOKEN_FILE   = "upstox_token.json"
BN_KEY       = "NSE_INDEX|Nifty Bank"
VIX_KEY      = "NSE_INDEX|India VIX"

state = {"access_token": ""}
cache = {
    "spot": 0, "change": 0, "pct": 0,
    "high": 0, "low": 0, "open": 0, "vwap": 0,
    "vix": 0, "pcr": 0, "max_pain": 0,
    "tot_ce_oi": 0, "tot_pe_oi": 0,
    "sp500_chg": 0, "crude_chg": 0, "gold_chg": 0, "usdinr": 0,
    "option_chain": [],
    "last_updated": "", "source": "starting",
    "error": "", "authenticated": False,
    "market_open": False,
    "last_session": {},  # stores last known good market data
}
LAST_SESSION_FILE = "last_session.json"

def save_last_session():
    """Save last good data to disk so it survives server restarts."""
    try:
        session = {k: cache[k] for k in [
            "spot","change","pct","high","low","open","vwap",
            "vix","pcr","max_pain","tot_ce_oi","tot_pe_oi",
            "sp500_chg","crude_chg","gold_chg","usdinr","option_chain","last_updated"
        ]}
        session["saved_at"] = datetime.now().strftime("%d %b %Y %H:%M IST")
        with open(LAST_SESSION_FILE, "w") as f:
            json.dump(session, f)
        cache["last_session"] = session
        print(f"[SESSION] Saved last session data.")
    except Exception as e:
        print(f"[SESSION] Save error: {e}")

def load_last_session():
    """Load last session data from disk."""
    try:
        with open(LAST_SESSION_FILE) as f:
            session = json.load(f)
            cache["last_session"] = session
            print(f"[SESSION] Loaded last session from {session.get('saved_at','?')}")
    except:
        pass

def is_market_open():
    """Check if NSE market is currently open."""
    now = datetime.now()
    # Convert to IST (UTC+5:30)
    ist_hour = (now.hour + 5) % 24
    ist_min = (now.minute + 30) % 60
    if now.minute + 30 >= 60:
        ist_hour = (now.hour + 6) % 24
    # Market open 9:15 to 15:30 IST, Monday-Friday
    weekday = now.weekday()  # 0=Mon, 6=Sun
    if weekday >= 5:  # Weekend
        return False
    open_mins = ist_hour * 60 + ist_min
    market_start = 9 * 60 + 15   # 9:15
    market_end = 15 * 60 + 30    # 15:30
    return market_start <= open_mins <= market_end

TERMINAL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0"/>
<title>BN Terminal · Live</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700;800&family=Barlow+Condensed:wght@700;800;900&display=swap" rel="stylesheet"/>
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
:root{
  --bg:#060A10;--bg2:#0A1018;--bg3:#0E1620;
  --bdr:#192336;--bdr2:#1F2E42;
  --orange:#FF6D00;--teal:#00BFA5;--green:#00E676;
  --red:#FF1744;--yellow:#FFD600;
  --white:#D8E8F8;--muted:#4A6070;--dim:#253545;
  --mono:'JetBrains Mono',monospace;--cond:'Barlow Condensed',sans-serif;
}
html,body{width:100%;height:100%;background:var(--bg);color:var(--white);font-family:var(--mono);font-size:12px;overflow:hidden}
#app{display:flex;flex-direction:column;height:100dvh}

/* SETUP SCREEN */
#setup{position:fixed;top:0;left:0;right:0;bottom:0;background:var(--bg);z-index:300;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px;text-align:center}
#setup.hide{display:none}
.setup-box{background:var(--bg2);border:1px solid var(--bdr2);border-radius:8px;padding:24px 20px;max-width:380px;width:100%}
.setup-title{font-family:var(--cond);font-size:24px;font-weight:900;color:var(--orange);margin-bottom:6px;letter-spacing:0.08em}
.setup-sub{font-size:9px;color:var(--muted);margin-bottom:18px;line-height:1.6}
.setup-label{font-size:8px;color:var(--muted);letter-spacing:0.12em;text-transform:uppercase;text-align:left;margin-bottom:6px}
#server-url{width:100%;background:var(--bg3);border:2px solid var(--bdr2);color:var(--white);padding:10px 12px;font-family:var(--mono);font-size:11px;border-radius:4px;outline:none;margin-bottom:10px}
#server-url:focus{border-color:var(--orange)}
.setup-btn{width:100%;padding:12px;background:var(--orange);color:#000;border:none;font-family:var(--cond);font-size:16px;font-weight:900;cursor:pointer;border-radius:4px;letter-spacing:0.08em;margin-bottom:8px}
.setup-btn:hover{background:var(--yellow)}
.setup-hint{font-size:8px;color:var(--dim);line-height:1.7}
#test-result{font-size:9px;margin-top:8px;padding:6px 10px;border-radius:3px;display:none}

/* TOP BAR */
#topbar{display:flex;align-items:center;justify-content:space-between;padding:0 14px;height:52px;background:var(--bg2);border-bottom:2px solid var(--bdr2);flex-shrink:0}
.logo{display:flex;align-items:center;gap:8px}
.dot{width:10px;height:10px;border-radius:50%;background:var(--orange);box-shadow:0 0 10px var(--orange);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{box-shadow:0 0 6px var(--orange)}50%{box-shadow:0 0 20px var(--orange)}}
.logo-name{font-family:var(--cond);font-size:19px;font-weight:900;letter-spacing:0.1em}
.logo-sub{font-size:7px;color:var(--muted);letter-spacing:0.15em;text-transform:uppercase;margin-top:1px}
.top-right{display:flex;align-items:center;gap:8px}
#conn-pill{font-size:8px;font-weight:800;padding:3px 9px;border-radius:10px;border:1px solid var(--dim);color:var(--muted);display:flex;align-items:center;gap:4px;cursor:pointer}
#conn-pill .bd{width:6px;height:6px;border-radius:50%;animation:blink 1.4s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0.2}}
#clock{font-family:var(--cond);font-size:14px;font-weight:800;color:var(--teal)}

/* PRICE STRIP */
#price-strip{display:flex;align-items:center;height:52px;background:var(--bg3);border-bottom:1px solid var(--bdr2);flex-shrink:0;padding:0 14px;gap:16px}
#spot-big{font-family:var(--cond);font-size:32px;font-weight:900;color:var(--muted);flex-shrink:0}
#chg{font-size:11px;font-weight:700;color:var(--muted);flex-shrink:0}
.ohlc{display:flex;gap:14px}
.oi{display:flex;flex-direction:column;gap:1px}
.oi-l{font-size:7px;color:var(--muted);letter-spacing:0.1em;text-transform:uppercase}
.oi-v{font-size:10px;font-weight:700}
#upd-info{margin-left:auto;font-size:8px;color:var(--muted);text-align:right;flex-shrink:0}

/* GLOBALS */
#globals{display:flex;align-items:center;overflow-x:auto;height:30px;background:var(--bg2);border-bottom:1px solid var(--bdr2);flex-shrink:0}
#globals::-webkit-scrollbar{display:none}
.g{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:0 10px;border-right:1px solid var(--bdr);flex-shrink:0;height:100%}
.g-l{font-size:6px;color:var(--muted);letter-spacing:0.1em;text-transform:uppercase}
.g-v{font-size:10px;font-weight:700;margin-top:1px}

/* TABS */
#tabs{display:flex;height:42px;background:var(--bg2);border-bottom:2px solid var(--bdr2);flex-shrink:0}
.tab{flex:1;display:flex;align-items:center;justify-content:center;font-family:var(--cond);font-size:13px;font-weight:800;letter-spacing:0.08em;color:var(--muted);border-right:1px solid var(--bdr);cursor:pointer;transition:all 0.15s;text-transform:uppercase}
.tab:last-child{border-right:none}
.tab.on{color:var(--orange);border-bottom:2px solid var(--orange);background:rgba(255,109,0,0.06)}

/* PAGES */
#pages{flex:1;min-height:0;overflow:hidden;position:relative}
.page{position:absolute;top:0;left:0;right:0;bottom:0;overflow-y:auto;padding:10px;display:none;flex-direction:column;gap:8px}
.page.on{display:flex}
.page::-webkit-scrollbar{width:3px}
.page::-webkit-scrollbar-track{background:var(--bg)}
.page::-webkit-scrollbar-thumb{background:var(--bdr2);border-radius:2px}

/* CARDS */
.card{background:var(--bg2);border:1px solid var(--bdr);border-radius:5px;overflow:hidden}
.card-hd{padding:7px 12px;background:var(--bg3);border-bottom:1px solid var(--bdr);font-size:8px;font-weight:700;letter-spacing:0.16em;color:var(--muted);text-transform:uppercase;display:flex;align-items:center;justify-content:space-between}
.sec-title{font-size:8px;font-weight:700;letter-spacing:0.16em;color:var(--muted);text-transform:uppercase;margin-bottom:6px}

/* SIGNAL */
.sig-ac{padding:18px 14px 12px;text-align:center;border-bottom:1px solid var(--bdr)}
.sv{font-family:var(--cond);font-size:52px;font-weight:900;letter-spacing:0.06em;line-height:1}
.ss{font-size:13px;font-weight:800;letter-spacing:0.12em;margin-top:4px}
.sb{font-family:var(--cond);font-size:20px;font-weight:800;padding:6px 16px;border-radius:3px;display:inline-block;margin-top:8px}
.lg{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--bdr)}
.lc{background:var(--bg2);padding:12px 14px;border-left:3px solid transparent}
.lc-l{font-size:7px;color:var(--muted);letter-spacing:0.1em;text-transform:uppercase}
.lc-v{font-size:20px;font-weight:900;font-family:var(--cond);margin-top:3px}
.cr{display:flex;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid var(--bdr)}
.cb{flex:1;height:10px;background:var(--bdr);border-radius:5px;overflow:hidden}
.cf{height:100%;border-radius:5px;transition:width 0.5s}
.cds{padding:10px 14px;display:flex;flex-wrap:wrap;gap:4px;border-bottom:1px solid var(--bdr)}
.cd{font-size:8px;font-weight:700;padding:3px 7px;border-radius:2px}
.sig-foot{padding:8px 14px;font-size:8px;color:var(--muted)}
.sw{padding:28px 14px;text-align:center}
.sw-t{font-family:var(--cond);font-size:34px;font-weight:900;color:var(--muted);margin:8px 0}
.sw-m{font-size:10px;color:var(--muted);line-height:1.7}

/* TREND */
.tlabel{font-family:var(--cond);font-size:26px;font-weight:900;letter-spacing:0.1em}
.tbar{height:8px;background:var(--bdr);border-radius:4px;overflow:hidden;margin:8px 0 6px}
.tbf{height:100%;border-radius:4px;transition:width 0.5s}
.tstrat{font-size:9px;font-weight:700;padding:5px 10px;border-radius:3px;display:inline-block;margin-top:4px;letter-spacing:0.05em}

/* INDS */
.ig{display:grid;grid-template-columns:repeat(3,1fr);gap:5px}
.ind{background:var(--bg2);border:1px solid var(--bdr);border-radius:4px;padding:8px 10px}
.ind-l{font-size:7px;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase}
.ind-v{font-size:14px;font-weight:800;margin-top:3px;font-family:var(--cond)}
.ind-b{height:3px;background:var(--bdr);border-radius:2px;margin-top:4px;overflow:hidden}
.ind-bf{height:100%;border-radius:2px}

/* MINIS */
.mg{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.mi{background:var(--bg2);border:1px solid var(--bdr);border-radius:5px;padding:10px 12px}
.mi-l{font-size:7px;color:var(--muted);letter-spacing:0.1em;text-transform:uppercase;margin-bottom:4px}
.mi-v{font-size:20px;font-weight:900;font-family:var(--cond)}
.mi-s{font-size:8px;color:var(--muted);margin-top:2px}

/* SR */
.sr-list{display:flex;flex-direction:column;gap:3px}
.sr-row{display:flex;align-items:center;justify-content:space-between;padding:9px 12px;background:var(--bg2);border:1px solid var(--bdr);border-radius:4px}
.sr-n{font-size:9px;font-weight:700;letter-spacing:0.1em;color:var(--muted);width:36px}
.sr-v{font-size:15px;font-weight:800;font-family:var(--cond)}
.sr-d{font-size:9px;width:52px;text-align:right}
.sr-spot{background:var(--bg3);border-color:var(--bdr2)}

/* OC */
.oc-scroll{overflow-x:auto}
#oct{width:100%;border-collapse:collapse;min-width:320px}
#oct th{padding:6px 8px;font-size:7px;color:var(--muted);font-weight:700;letter-spacing:0.1em;text-align:right;border-bottom:1px solid var(--bdr);background:var(--bg3)}
#oct th.c{text-align:center;color:var(--yellow)}
#oct td{padding:4px 8px;font-size:10px;font-weight:600;text-align:right;border-bottom:1px solid var(--bdr)}
#oct td.c{text-align:center}
#oct tr.atm td{background:rgba(255,214,0,0.06)}
.oib{display:inline-block;height:6px;border-radius:1px;vertical-align:middle;margin-left:2px}

/* RISK */
.ri{display:flex;align-items:center;gap:10px;padding:4px 12px}
.ri label{font-size:9px;color:var(--muted);width:90px;flex-shrink:0}
.ri input{flex:1;background:var(--bg3);border:1px solid var(--bdr2);color:var(--white);padding:8px 10px;font-family:var(--mono);font-size:12px;border-radius:3px;outline:none;min-width:0}
.ri input:focus{border-color:var(--orange)}
.rres{display:flex;justify-content:space-between;align-items:center;padding:10px 12px;background:var(--bg3);border-top:1px solid var(--bdr)}
.rres-l{font-size:9px;color:var(--muted)}
.rres-v{font-size:18px;font-weight:900;font-family:var(--cond)}

/* CHART */
.chart-wrap{background:var(--bg2);border:1px solid var(--bdr);border-radius:5px;overflow:hidden;height:360px;display:flex;flex-direction:column}
.chart-hd{display:flex;align-items:center;justify-content:space-between;padding:7px 10px;border-bottom:1px solid var(--bdr);background:var(--bg3);flex-shrink:0}
.chart-hd-t{font-size:8px;font-weight:700;letter-spacing:0.16em;color:var(--muted);text-transform:uppercase}
.chart-body{flex:1}
.tf-row{display:flex;gap:4px}
.tf{padding:4px 10px;border-radius:2px;font-family:var(--mono);font-size:9px;font-weight:700;cursor:pointer;border:1px solid var(--bdr);background:transparent;color:var(--muted)}
.tf.on{background:var(--orange);color:#000;border-color:var(--orange)}

/* AI */
#ai-msgs{height:300px;overflow-y:auto;display:flex;flex-direction:column;gap:6px;padding:10px 12px;background:var(--bg2);border:1px solid var(--bdr);border-radius:5px}
.msg{padding:9px 12px;border-radius:5px;font-size:10px;line-height:1.6}
.msg.bot{background:var(--bg3);border:1px solid var(--bdr);border-left:3px solid var(--teal)}
.msg.user{background:rgba(255,109,0,0.08);border:1px solid rgba(255,109,0,0.2);border-left:3px solid var(--orange)}
.typing{padding:9px 12px;display:flex;gap:4px;align-items:center;background:var(--bg3);border:1px solid var(--bdr);border-left:3px solid var(--teal);border-radius:5px}
.typing span{width:5px;height:5px;border-radius:50%;background:var(--teal);animation:bo 0.9s ease-in-out infinite}
.typing span:nth-child(2){animation-delay:0.15s}.typing span:nth-child(3){animation-delay:0.3s}
@keyframes bo{0%,100%{transform:translateY(0)}50%{transform:translateY(-5px)}}
.ai-bar{display:flex;gap:6px;margin-top:6px}
#ai-in{flex:1;background:var(--bg2);border:1px solid var(--bdr2);color:var(--white);padding:10px 12px;font-family:var(--mono);font-size:10px;border-radius:4px;outline:none}
#ai-in:focus{border-color:var(--teal)}
#ai-btn{padding:0 14px;background:var(--teal);color:#000;border:none;font-family:var(--cond);font-size:13px;font-weight:800;cursor:pointer;border-radius:4px}
#ai-btn:disabled{opacity:0.4}

/* NEWS */
.ni{padding:8px 12px;border-bottom:1px solid var(--bdr)}
.ni-top{display:flex;align-items:center;gap:5px;margin-bottom:3px}
.ni-sev{font-size:7px;font-weight:800;padding:2px 6px;border-radius:2px;letter-spacing:0.1em}
.ni-src{font-size:8px;color:var(--muted)}
.ni-body{font-size:9px;line-height:1.4}

/* LOG */
#logbar{height:24px;background:var(--bg3);border-top:1px solid var(--bdr);display:flex;align-items:center;padding:0 12px;font-size:8px;flex-shrink:0;gap:8px}
#logtxt{color:var(--muted);flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* TOAST */
#toast{position:fixed;top:60px;left:50%;transform:translateX(-50%) translateY(-8px);background:var(--bg3);border:1px solid var(--orange);border-radius:5px;padding:10px 18px;font-family:var(--cond);font-size:15px;font-weight:800;color:var(--orange);opacity:0;pointer-events:none;transition:all 0.3s;z-index:999;box-shadow:0 0 24px rgba(255,109,0,0.3)}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.up{color:var(--green)}.dn{color:var(--red)}.neu{color:var(--muted)}
</style>
</head>
<body>
<div id="app">

<!-- SETUP SCREEN — shown until URL is set -->
<div id="setup" class="hide">
  <div class="setup-box">
    <div class="setup-title">⚙ CONNECT SERVER</div>
    <div class="setup-sub">
      Paste your Railway URL below.<br>
      This is shown after you deploy bn_server.py to Railway.<br>
      It looks like: <span style="color:var(--teal)">https://your-app.up.railway.app</span>
    </div>
    <div class="setup-label">Railway Server URL</div>
    <input id="server-url" type="url" placeholder="https://your-app.up.railway.app" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false"/>
    <button class="setup-btn" onclick="saveAndConnect()">CONNECT & SAVE</button>
    <div id="test-result"></div>
    <div class="setup-hint">
      Your URL is saved in this browser — you only enter it once.<br>
      Tap the green LIVE pill anytime to change it.
    </div>
  </div>
</div>

<!-- TOP BAR -->
<div id="topbar">
  <div class="logo">
    <div class="dot"></div>
    <div><div class="logo-name">BN TERMINAL</div><div class="logo-sub">Upstox · Live</div></div>
  </div>
  <div class="top-right">
    <div id="conn-pill" onclick="showSetup()"><div class="bd" style="background:var(--muted)"></div><span id="conn-txt">CONNECTING</span></div>
    <div id="clock">—:—:—</div>
  </div>
</div>

<!-- PRICE STRIP -->
<div id="price-strip">
  <div>
    <div id="spot-big" style="font-family:var(--cond);font-size:32px;font-weight:900;color:var(--muted)">—</div>
    <div id="chg" style="font-size:11px;font-weight:700;color:var(--muted)">Auto-live prices</div>
  </div>
  <div class="ohlc">
    <div class="oi"><div class="oi-l">OPEN</div><div id="d-o" class="oi-v">—</div></div>
    <div class="oi"><div class="oi-l">HIGH</div><div id="d-h" class="oi-v up">—</div></div>
    <div class="oi"><div class="oi-l">LOW</div> <div id="d-l" class="oi-v dn">—</div></div>
    <div class="oi"><div class="oi-l">VWAP</div><div id="d-vw" class="oi-v">—</div></div>
  </div>
  <div id="upd-info" style="margin-left:auto;font-size:8px;color:var(--muted);text-align:right;flex-shrink:0">
    AUTO-LIVE<br><span id="upd-ts" style="color:var(--dim)">—</span>
  </div>
</div>

<!-- GLOBALS -->
<div id="globals">
  <div class="g"><div class="g-l">VIX</div>  <div id="g-vix" class="g-v">—</div></div>
  <div class="g"><div class="g-l">PCR</div>   <div id="g-pcr" class="g-v">—</div></div>
  <div class="g"><div class="g-l">S&amp;P</div><div id="g-sp"  class="g-v">—</div></div>
  <div class="g"><div class="g-l">CRUDE</div> <div id="g-cr"  class="g-v">—</div></div>
  <div class="g"><div class="g-l">GOLD</div>  <div id="g-gd"  class="g-v">—</div></div>
  <div class="g"><div class="g-l">₹/USD</div><div id="g-usd" class="g-v">—</div></div>
</div>

<!-- TABS -->
<div id="tabs">
  <div class="tab on" onclick="goTab(0)">📊 SIGNAL</div>
  <div class="tab"    onclick="goTab(1)">📈 CHART</div>
  <div class="tab"    onclick="goTab(2)">⛓ LEVELS</div>
  <div class="tab"    onclick="goTab(3)">🤖 ARIA</div>
</div>

<!-- PAGES -->
<div id="pages">

  <!-- PAGE 0: SIGNAL -->
  <div class="page on" id="page-0">
    <div class="card">
      <div class="card-hd">SIGNAL ENGINE<span id="sig-ts" style="font-weight:400">—</span></div>
      <div id="sig-body">
        <div class="sw"><div style="font-size:34px;margin-bottom:8px">⏳</div><div class="sw-t">CONNECTING</div><div class="sw-m">Connecting to cloud server...<br>Prices and signals load automatically.</div></div>
      </div>
    </div>

    <div class="card">
      <div class="card-hd">Day Bias</div>
      <div style="padding:10px 12px">
        <div id="tl" class="tlabel" style="color:var(--muted)">Loading...</div>
        <div class="tbar"><div id="tf2" class="tbf" style="width:50%;background:var(--muted)"></div></div>
        <div id="t-fac" style="font-size:8px;color:var(--muted)">—</div>
        <div id="t-str" class="tstrat" style="background:rgba(74,96,112,0.2);color:var(--muted)">—</div>
      </div>
    </div>

    <div>
      <div class="sec-title">Live Indicators</div>
      <div class="ig">
        <div class="ind"><div class="ind-l">EMA 9</div><div id="i-e9" class="ind-v" style="color:var(--muted)">—</div><div class="ind-b"><div id="i-e9b" class="ind-bf" style="width:50%;background:var(--green)"></div></div></div>
        <div class="ind"><div class="ind-l">EMA 21</div><div id="i-e21" class="ind-v" style="color:var(--muted)">—</div></div>
        <div class="ind"><div class="ind-l">RSI 14</div><div id="i-rsi" class="ind-v" style="color:var(--muted)">—</div><div class="ind-b"><div id="i-rsib" class="ind-bf" style="width:50%;background:var(--yellow)"></div></div></div>
        <div class="ind"><div class="ind-l">VWAP</div><div id="i-vw" class="ind-v" style="color:var(--muted)">—</div></div>
        <div class="ind"><div class="ind-l">S.TREND</div><div id="i-st" class="ind-v" style="color:var(--muted)">—</div></div>
        <div class="ind"><div class="ind-l">PCR</div><div id="i-pcr" class="ind-v" style="color:var(--muted)">—</div></div>
      </div>
    </div>

    <div class="mg">
      <div class="mi"><div class="mi-l">PCR (OI)</div><div id="m-pcr" class="mi-v" style="color:var(--muted)">—</div><div id="m-bias" class="mi-s">—</div></div>
      <div class="mi"><div class="mi-l">Max Pain</div><div id="m-mp" class="mi-v" style="color:var(--yellow)">—</div><div class="mi-s">₹ strike</div></div>
      <div class="mi"><div class="mi-l">VIX</div><div id="m-vix" class="mi-v" style="color:var(--muted)">—</div><div id="m-vixs" class="mi-s">—</div></div>
      <div class="mi"><div class="mi-l">Total OI</div><div id="m-oi" class="mi-v" style="color:var(--muted)">—</div><div class="mi-s">CE+PE Lakh</div></div>
    </div>

    <div class="card">
      <div class="card-hd">Risk Calculator</div>
      <div style="padding:6px 0">
        <div class="ri"><label>Capital ₹</label><input id="rc-c" type="number" value="50000" inputmode="numeric"/></div>
        <div class="ri"><label>Entry ₹</label><input id="rc-e" type="number" placeholder="option premium" inputmode="decimal"/></div>
        <div class="ri"><label>Stop Loss ₹</label><input id="rc-s" type="number" placeholder="sl premium" inputmode="decimal"/></div>
      </div>
      <div class="rres"><div><div class="rres-l">LOTS (1% risk · 15 qty)</div></div><div id="rc-lots" class="rres-v" style="color:var(--teal)">—</div></div>
      <div class="rres" style="border-top:1px solid var(--bdr)"><div class="rres-l">Max Risk ₹</div><div id="rc-mr" class="rres-v" style="color:var(--red)">—</div></div>
    </div>
  </div>

  <!-- PAGE 1: CHART -->
  <div class="page" id="page-1">
    <div class="chart-wrap">
      <div class="chart-hd">
        <span class="chart-hd-t">BANKNIFTY · UPSTOX LIVE</span>
        <div class="tf-row">
          <button class="tf" onclick="loadChart(1)">1m</button>
          <button class="tf on" onclick="loadChart(5)">5m</button>
          <button class="tf" onclick="loadChart(15)">15m</button>
          <button class="tf" onclick="loadChart(60)">1h</button>
        </div>
      </div>
      <div class="chart-body" style="position:relative">
        <div id="lw_chart" style="width:100%;height:100%"></div>
        <div id="chart-loading" style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-family:var(--cond);font-size:13px;color:var(--muted);font-weight:700;letter-spacing:0.1em;text-align:center">LOADING...</div>
        <div id="chart-legend" style="position:absolute;top:8px;left:8px;font-size:9px;color:var(--white);background:rgba(6,10,16,0.85);padding:4px 8px;border-radius:3px;pointer-events:none;z-index:10;letter-spacing:0.05em"></div>
      </div>
    </div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;padding:4px 2px">
      <span style="font-size:8px;color:var(--muted);display:flex;align-items:center;gap:4px"><span style="width:14px;height:2px;background:#00BFA5;display:inline-block;border-radius:1px"></span>EMA 9</span>
      <span style="font-size:8px;color:var(--muted);display:flex;align-items:center;gap:4px"><span style="width:14px;height:2px;background:#FF6D00;display:inline-block;border-radius:1px"></span>EMA 21</span>
      <span style="font-size:8px;color:var(--muted);display:flex;align-items:center;gap:4px"><span style="width:14px;height:2px;background:#FFD600;display:inline-block;border-radius:1px;opacity:0.7"></span>VWAP</span>
    </div>
  </div>

  <!-- PAGE 2: LEVELS -->
  <div class="page" id="page-2">
    <div>
      <div class="sec-title">Support &amp; Resistance · Daily Pivot</div>
      <div class="sr-list" id="sr-list"><div style="color:var(--muted);text-align:center;padding:20px;font-size:10px">Loading...</div></div>
    </div>
    <div class="card">
      <div class="card-hd">Option Chain (Synthetic)<span id="oc-b" style="font-size:7px;font-weight:700;padding:2px 6px;border-radius:2px;background:rgba(255,214,0,0.1);color:var(--yellow)">WEEKLY</span></div>
      <div class="oc-scroll">
        <table id="oct">
          <thead><tr>
            <th style="text-align:left;color:var(--green)">CE LTP</th><th style="color:var(--teal)">CE OI</th>
            <th class="c">STRIKE</th>
            <th style="color:var(--orange)">PE OI</th><th style="text-align:right;color:var(--red)">PE LTP</th>
          </tr></thead>
          <tbody id="oc-body"><tr><td colspan="5" style="text-align:center;color:var(--muted);padding:20px">Loading...</td></tr></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- PAGE 3: ARIA -->
  <div class="page" id="page-3">
    <div id="ai-msgs">
      <div class="msg bot">👋 I'm <strong>ARIA</strong>.<br><br>Your server is running in the cloud 24/7 — prices update automatically every 30 seconds.<br><br>I'll auto-explain every signal. Ask me anything.<br><br><span style="color:var(--teal)">"Why BUY?" · "What is PCR?" · "Is this a good setup?"</span></div>
    </div>
    <div class="ai-bar">
      <input id="ai-in" placeholder="Ask ARIA..." onkeydown="if(event.key==='Enter'){event.preventDefault();sendAI()}"/>
      <button id="ai-btn" onclick="sendAI()">ASK</button>
    </div>
    <div class="card" style="margin-top:0">
      <div class="card-hd">Market Alerts</div>
      <div id="news-list"></div>
    </div>
  </div>

</div>
</div>

<div id="logbar"><span id="logtxt">Connecting to cloud server...</span></div>
<div id="toast"><span id="toast-msg"></span></div>

<script>
// ═══════════════════ BN CLOUD TERMINAL ═══════════════════
const STORAGE_KEY = 'bn_server_url';
let SERVER = localStorage.getItem(STORAGE_KEY) || '';
const S = {spot:0,open:0,high:0,low:0,vwap:0,change:0,pct:0,vix:0,sp500chg:0,crudechg:0,goldchg:0,usdinr:0,pcr:0.9,maxPain:0,totCE:0,totPE:0,candles:[],signal:null};
let ariaKey='',ariaHist=[],prevSig=null;

// Clock
setInterval(()=>{const d=new Date(Date.now()+5.5*3600000);document.getElementById('clock').textContent=`${String(d.getUTCHours()).padStart(2,'0')}:${String(d.getUTCMinutes()).padStart(2,'0')}:${String(d.getUTCSeconds()).padStart(2,'0')} IST`;},1000);
function log(m,ok=null){document.getElementById('logtxt').textContent=m;document.getElementById('logtxt').style.color=ok===true?'var(--green)':ok===false?'var(--red)':'var(--muted)';}
function goTab(i){document.querySelectorAll('.tab').forEach((t,j)=>t.classList.toggle('on',i===j));document.querySelectorAll('.page').forEach((p,j)=>p.classList.toggle('on',i===j));}

// Setup
function showSetup(){document.getElementById('setup').classList.remove('hide');if(SERVER)document.getElementById('server-url').value=SERVER;}
function fetchT(url,ms){return Promise.race([fetch(url,{cache:'no-store'}),new Promise((_,r)=>setTimeout(()=>r(new Error('Timeout')),ms))]);}
async function saveAndConnect(){
  const url=document.getElementById('server-url').value.trim().replace(/\\/+$/,'');
  if(!url.startsWith('http')){showResult('Enter a valid URL starting with https://','var(--red)');return;}
  showResult('Testing connection...','var(--yellow)');
  try{
    const res=await fetchT('/api/status',6000);
    if(!res.ok)throw new Error('Server responded with '+res.status);
    const d=await res.json();
    showResult('✓ Connected! BN ₹'+(d.spot||'loading...'),'var(--green)');
    SERVER=url;
    localStorage.setItem(STORAGE_KEY,SERVER);
    setTimeout(()=>{document.getElementById('setup').classList.add('hide');fetchFromServer();},1200);
  }catch(e){showResult('❌ Cannot connect: '+e.message,'var(--red)');}
}
function showResult(m,c){const el=document.getElementById('test-result');el.style.display='block';el.style.background=c+'22';el.style.color=c;el.style.border='1px solid '+c+'44';el.style.borderRadius='3px';el.style.padding='8px 10px';el.style.fontSize='9px';el.style.lineHeight='1.6';el.textContent=m;}
function setConn(ok,marketOpen,lastSessionTime){
  const p=document.getElementById('conn-pill');const d=p.querySelector('.bd');
  let label,c;
  if(ok&&marketOpen){label='LIVE';c='var(--green)';}
  else if(ok&&!marketOpen){label='CLOSED';c='var(--yellow)';}
  else{label='OFFLINE';c='var(--red)';}
  document.getElementById('conn-txt').textContent=label;
  p.style.color=c;p.style.borderColor=c;d.style.background=c;
  // Show/hide last session banner
  let banner=document.getElementById('session-banner');
  if(!banner){banner=document.createElement('div');banner.id='session-banner';
    banner.style.cssText='background:rgba(255,214,0,0.08);border-bottom:1px solid rgba(255,214,0,0.2);padding:4px 14px;font-size:8px;color:var(--yellow);text-align:center;flex-shrink:0;letter-spacing:0.08em;font-weight:700;';
    const tabs=document.getElementById('tabs');tabs.parentNode.insertBefore(banner,tabs);}
  if(!marketOpen&&lastSessionTime){banner.textContent='MARKET CLOSED · Showing last session: '+lastSessionTime;banner.style.display='block';}
  else{banner.style.display='none';}
}

// Fetch
async function fetchFromServer(){
  if(!SERVER){showSetup();return;}
  try{
    const res=await fetchT('/api/price',8000);
    if(!res.ok)throw new Error('HTTP '+res.status);
    const d=await res.json();
    if(!d.spot||d.spot<30000)throw new Error('No valid price');
    const prev=S.spot;
    S.spot=d.spot;S.change=d.change;S.pct=d.pct;
    S.high=d.high;S.low=d.low;S.open=d.open;
    S.vwap=(d.high+d.low+d.spot)/3;
    S.vix=d.vix;S.sp500chg=d.sp500_chg;S.crudechg=d.crude_chg;
    S.goldchg=d.gold_chg;S.usdinr=d.usdinr;
    const now=new Date(Date.now()+5.5*3600000);
    const t=`${String(now.getUTCHours()).padStart(2,'0')}:${String(now.getUTCMinutes()).padStart(2,'0')}`;
    const last=S.candles[S.candles.length-1];
    if(last&&last.t===t){last.c=d.spot;last.h=Math.max(last.h,d.high||d.spot);last.l=Math.min(last.l,d.low||d.spot);}
    else{S.candles.push({t,o:prev||d.spot,h:d.high||d.spot,l:d.low||d.spot,c:d.spot,v:1});}
    if(S.candles.length>200)S.candles=S.candles.slice(-200);
    document.getElementById('upd-ts').textContent=d.last_updated||'—';
    setConn(true,d.market_open!==false,d.last_session_time||'');
    log((d.using_last_session?'Last session · ':'Live · ')+'BN ₹'+d.spot.toLocaleString('en-IN')+' · VIX '+d.vix+' · '+(d.last_session_time||d.last_updated),d.using_last_session?null:true);
    renderAll();
  }catch(e){
    setConn(false,false,'');
    log('Server error: '+e.message,false);
  }
}

// All indicator / render functions
function ema(arr,p){const k=2/(p+1);let e=arr[0];for(let i=1;i<arr.length;i++)e=arr[i]*k+e*(1-k);return e;}
function rsi(arr,p=14){if(arr.length<p+1)return 50;let g=0,l=0;for(let i=arr.length-p;i<arr.length;i++){const d=arr[i]-arr[i-1];if(d>0)g+=d;else l-=d;}if(l===0)return 100;return+(100-100/(1+(g/p)/(l/p))).toFixed(1);}
function vwapCalc(cs){return cs.reduce((a,c)=>a+(c.h+c.l+c.c)/3,0)/cs.length;}
function supertrend(cs,aP=10,f=3){if(cs.length<aP+1)return{bull:S.change>=0};const atrs=[];for(let i=1;i<cs.length;i++)atrs.push(Math.max(cs[i].h-cs[i].l,Math.abs(cs[i].h-cs[i-1].c),Math.abs(cs[i].l-cs[i-1].c)));const atr=atrs.slice(-aP).reduce((a,b)=>a+b,0)/aP;const last=cs[cs.length-1];const mid=(last.h+last.l)/2;return{bull:last.c>mid-f*atr};}
function calcPivots(cs){const H=cs.length?Math.max(...cs.map(c=>c.h)):S.high||S.spot;const L=cs.length?Math.min(...cs.map(c=>c.l)):S.low||S.spot;const C=cs.length?cs[cs.length-1].c:S.spot;const p=(H+L+C)/3;return{P:+p.toFixed(0),R1:+(2*p-L).toFixed(0),R2:+(p+H-L).toFixed(0),R3:+(H+2*(p-L)).toFixed(0),S1:+(2*p-H).toFixed(0),S2:+(p-(H-L)).toFixed(0),S3:+(L-2*(H-p)).toFixed(0)};}
function buildOC(spot){const atm=Math.round(spot/100)*100;const strikes=[];for(let i=-6;i<=6;i++)strikes.push(atm+i*100);const ce={},pe={};let totCE=0,totPE=0;const vol=(S.vix||14)/100*Math.sqrt(7/365);for(const s of strikes){const absd=Math.abs(s-spot),isATM=absd<50;const tP=spot*vol*Math.exp(-absd/(spot*0.015+1))*100;ce[s]={ltp:+(Math.max(0.5,Math.max(0,spot-s)+tP).toFixed(1)),oi:Math.round((isATM?500:Math.max(15,500-absd*0.8))*1000),oiChg:+(Math.random()*6-1.5).toFixed(1)};pe[s]={ltp:+(Math.max(0.5,Math.max(0,s-spot)+tP).toFixed(1)),oi:Math.round((isATM?480:Math.max(15,480-absd*0.75))*1000),oiChg:+(Math.random()*6-1.5).toFixed(1)};totCE+=ce[s].oi;totPE+=pe[s].oi;}let maxPain=atm,minLoss=Infinity;for(const s of strikes){let loss=0;for(const ss of strikes){loss+=Math.max(0,ss-s)*(ce[ss]?.oi||0)+Math.max(0,s-ss)*(pe[ss]?.oi||0);}if(loss<minLoss){minLoss=loss;maxPain=s;}}const pcr=+(totPE/totCE).toFixed(2);S.pcr=pcr;S.maxPain=maxPain;S.totCE=totCE;S.totPE=totPE;return{strikes,ce,pe,pcr,maxPain,totCE,totPE};}
function computeTrend(cs,spot){if(cs.length<3)return null;const cl=cs.map(c=>c.c);const e9=ema(cl,9),e21=ema(cl,21),rs=rsi(cl,14),vw=vwapCalc(cs),st=supertrend(cs);const slope=(cl[cl.length-1]-cl[Math.max(0,cl.length-6)])/6;const bf=[['Above VWAP',spot>vw],['EMA9>EMA21',e9>e21],['Supertrend Bull',st.bull],['RSI>52',rs>52],['Trending Up',slope>5],['S&P+',S.sp500chg>0],['VIX<16',S.vix<16||S.vix===0],['Above Open',S.open>0&&spot>S.open]];const brf=[['Below VWAP',spot<vw],['EMA9<EMA21',e9<e21],['Supertrend Bear',!st.bull],['RSI<48',rs<48],['Trending Down',slope<-5],['S&P−',S.sp500chg<0],['VIX>18',S.vix>18],['Below Open',S.open>0&&spot<S.open]];const bs=bf.filter(f=>f[1]).length,br=brf.filter(f=>f[1]).length,net=bs-br;let label,col,strat;if(net>=5){label='STRONGLY BULLISH';col='var(--green)';strat='Buy dips — target R2/R3';}else if(net>=3){label='BULLISH';col='#66BB6A';strat='Buy pullbacks — SL below EMA9';}else if(net>=1){label='MILDLY BULLISH';col='var(--teal)';strat='Cautious CE only';}else if(net>=-1){label='SIDEWAYS';col='var(--yellow)';strat='Avoid — wait for breakout';}else if(net>=-3){label='MILDLY BEARISH';col='#FFA040';strat='Cautious PE only';}else if(net>=-5){label='BEARISH';col='#FF7043';strat='Sell rallies to VWAP';}else{label='STRONGLY BEARISH';col='var(--red)';strat='Sell all rallies — no long trades';}return{label,col,pct:Math.round(bs/8*100),strat,bs,br,factors:bf.filter(f=>f[1]).map(f=>f[0]).join(' · ')||'—',meta:{e9:+e9.toFixed(0),e21:+e21.toFixed(0),rsi:rs,vwap:+vw.toFixed(0),st}};}
function computeSignal(cs,spot){if(cs.length<4){const piv=calcPivots([]);const atm=Math.round(spot/100)*100;return{signal:'WAIT',conf:0,gate:'Collecting candle data...',strike:atm,otype:'-',entry:null,sl:null,t1:piv.R1,t2:piv.R2,t3:piv.R3,pivots:piv,cl:{},bs:0,br:0,meta:{e9:0,e21:0,rsi:50,vwap:S.vwap,st:{bull:S.change>=0}}};}const cl=cs.map(c=>c.c);const e9=ema(cl,9),e21=ema(cl,21),rs=rsi(cl,14),vw=vwapCalc(cs),st=supertrend(cs),piv=calcPivots(cs);const slope=(cl[cl.length-1]-cl[Math.max(0,cl.length-6)])/6;const ist=new Date(Date.now()+5.5*3600000);const hh=ist.getUTCHours(),mm=ist.getUTCMinutes();const bull={'Price>VWAP':spot>vw,'EMA9>EMA21':e9>e21,'Supertrend Bull':st.bull,'RSI 45-68':rs>=45&&rs<=68,'Momentum Up':slope>3,'VIX Safe':S.vix<20||S.vix===0,'PCR>0.9':S.pcr>0.9,'RSI>50':rs>50};const bear={'Price<VWAP':spot<vw,'EMA9<EMA21':e9<e21,'Supertrend Bear':!st.bull,'RSI 32-55':rs>=32&&rs<=55,'Momentum Down':slope<-3,'VIX Safe':S.vix<20||S.vix===0,'PCR<1.0':S.pcr<1.0,'RSI<50':rs<50};const bs=Object.values(bull).filter(Boolean).length,br=Object.values(bear).filter(Boolean).length;let raw,conds;if(bs>=6){raw='BUY';conds=bull;}else if(br>=6){raw='SELL';conds=bear;}else if(bs>=5){raw='BUY';conds=bull;}else if(br>=5){raw='SELL';conds=bear;}else{raw='WAIT';conds=bs>=br?bull:bear;}const score=raw==='BUY'?bs:raw==='SELL'?br:Math.max(bs,br);const conf=raw==='WAIT'?Math.round(score/8*100):Math.min(95,60+score*5);const tooEarly=(hh<9)||(hh===9&&mm<30),tooLate=(hh>15)||(hh===15&&mm>0);let gate=null;if(tooEarly)gate='9:30 AM not cleared';else if(tooLate)gate='Market closed';else if(S.vix>22)gate='VIX too high';else if(conf<60&&raw!=='WAIT')gate='Confidence too low ('+conf+'%)';const sig=gate?'WAIT':raw;const atm=Math.round(spot/100)*100;return{signal:sig,raw,conf,score,gate,strike:atm,otype:sig==='BUY'?'CE':sig==='SELL'?'PE':'-',entry:sig==='BUY'?piv.R1:sig==='SELL'?piv.S1:null,sl:sig==='BUY'?piv.S1:sig==='SELL'?piv.R1:null,t1:sig==='BUY'?piv.R1:sig==='SELL'?piv.S1:null,t2:sig==='BUY'?piv.R2:sig==='SELL'?piv.S2:null,t3:sig==='BUY'?piv.R3:sig==='SELL'?piv.S3:null,pivots:piv,cl:conds,bs,br,meta:{e9:+e9.toFixed(0),e21:+e21.toFixed(0),rsi:rs,vwap:+vw.toFixed(0),st}};}

function renderAll(){if(!S.spot)return;const f=n=>Math.round(n).toLocaleString('en-IN');const cc=n=>n>=0?'up':'dn';const fp=n=>(n>=0?'+':'')+n.toFixed(2)+'%';const el=document.getElementById('spot-big');el.textContent='₹'+f(S.spot);el.className=cc(S.change);el.style.fontFamily='var(--cond)';el.style.fontSize='32px';el.style.fontWeight='900';document.getElementById('chg').textContent=(S.change>=0?'▲':'▼')+Math.abs(S.change).toFixed(2)+' ('+Math.abs(S.pct).toFixed(2)+'%)';document.getElementById('chg').className=cc(S.change);document.getElementById('d-o').textContent=f(S.open);document.getElementById('d-h').textContent=f(S.high);document.getElementById('d-l').textContent=f(S.low);document.getElementById('d-vw').textContent=f(S.vwap);document.getElementById('g-vix').textContent=S.vix?S.vix.toFixed(1):'—';document.getElementById('g-vix').className='g-v '+(S.vix>20?'dn':S.vix>15?'neu':'up');document.getElementById('g-pcr').textContent=S.pcr.toFixed(2);document.getElementById('g-pcr').className='g-v '+(S.pcr>=1.2?'up':S.pcr>=0.9?'neu':'dn');['g-sp','g-cr','g-gd'].forEach((id,i)=>{const v=[S.sp500chg,S.crudechg,S.goldchg][i];document.getElementById(id).textContent=v?fp(v):'—';document.getElementById(id).className='g-v '+cc(v);});document.getElementById('g-usd').textContent=S.usdinr?S.usdinr.toFixed(1):'—';const oc=buildOC(S.spot);renderOC(oc);renderMinis(oc);const t=computeTrend(S.candles,S.spot);renderTrend(t);const sig=computeSignal(S.candles,S.spot);S.signal=sig;renderSignal(sig);renderInds(sig?.meta||t?.meta);renderSR(sig?.pivots,S.spot);if(prevSig&&prevSig!=='WAIT'&&sig?.signal!==prevSig&&sig?.signal!=='WAIT')toast('Signal: '+prevSig+' → '+sig.signal);if((!prevSig||prevSig==='WAIT')&&sig?.signal!=='WAIT'){ariaExplain(sig);goTab(0);}prevSig=sig?.signal;}

function renderTrend(t){if(!t){document.getElementById('tl').textContent=S.candles.length<3?'Collecting...':'—';return;}document.getElementById('tl').textContent=t.label;document.getElementById('tl').style.color=t.col;document.getElementById('tf2').style.width=t.pct+'%';document.getElementById('tf2').style.background=t.col;document.getElementById('t-fac').textContent='Bull '+t.bs+'/8 · Bear '+t.br+'/8 · '+t.factors;document.getElementById('t-str').textContent=t.strat;document.getElementById('t-str').style.background=t.col+'22';document.getElementById('t-str').style.color=t.col;}
function renderSignal(sig){const body=document.getElementById('sig-body');const ist=new Date(Date.now()+5.5*3600000);document.getElementById('sig-ts').textContent=`${String(ist.getUTCHours()).padStart(2,'0')}:${String(ist.getUTCMinutes()).padStart(2,'0')} IST`;if(!sig||sig.signal==='WAIT'){const m=sig?.gate||(sig?`Bull ${sig.bs}/8 · Bear ${sig.br}/8`:'Waiting...');body.innerHTML=`<div class="sw"><div style="font-size:32px;margin-bottom:6px">⏸</div><div class="sw-t">WAIT</div><div class="sw-m">${m}${sig&&sig.bs+sig.br>0?`<br><br><span style="color:var(--dim)">Bull: ${sig.bs}/8 · Bear: ${sig.br}/8 · Conf: ${sig.conf}%</span>`:''}</div></div>`;return;}const isBuy=sig.signal==='BUY';const col=isBuy?'var(--green)':'var(--red)';const bg=isBuy?'rgba(0,230,118,0.04)':'rgba(255,23,68,0.04)';const f=n=>n?Math.round(n).toLocaleString('en-IN'):'—';const conds=Object.entries(sig.cl);const met=conds.filter(([,v])=>v).length;body.innerHTML=`<div class="sig-ac" style="background:${bg}"><div class="sv" style="color:${col}">${isBuy?'BUY CALL':'BUY PUT'}</div><div class="ss" style="color:${col}">${isBuy?'BULLISH':'BEARISH'} SETUP</div><div class="sb" style="background:${col}18;color:${col};border:1px solid ${col}44">${sig.strike} ${sig.otype} · WEEKLY</div></div><div class="lg"><div class="lc" style="border-color:var(--teal)"><div class="lc-l">ENTRY</div><div class="lc-v" style="color:var(--teal)">₹${f(sig.entry)}</div></div><div class="lc" style="border-color:var(--red)"><div class="lc-l">STOP LOSS</div><div class="lc-v" style="color:var(--red)">₹${f(sig.sl)}</div></div><div class="lc" style="border-color:var(--green)"><div class="lc-l">TARGET 1</div><div class="lc-v" style="color:var(--green)">₹${f(sig.t1)}</div></div><div class="lc" style="border-color:var(--yellow)"><div class="lc-l">TARGET 2</div><div class="lc-v" style="color:var(--yellow)">₹${f(sig.t2)}</div></div></div><div class="cr"><span style="font-size:9px;color:var(--muted);flex-shrink:0">CONFIDENCE</span><div class="cb"><div class="cf" style="width:${sig.conf}%;background:${sig.conf>=75?'var(--green)':sig.conf>=60?'var(--yellow)':'var(--red)'}"></div></div><span style="font-size:15px;font-weight:900;font-family:var(--cond);color:${sig.conf>=75?'var(--green)':sig.conf>=60?'var(--yellow)':'var(--red)'};flex-shrink:0">${sig.conf}%</span><span style="font-size:9px;color:var(--muted);flex-shrink:0">${met}/${conds.length}</span></div><div class="cds">${conds.map(([k,v])=>`<span class="cd" style="background:${v?'rgba(0,230,118,0.1)':'rgba(74,96,112,0.15)'};color:${v?'var(--green)':'var(--muted)'}">${v?'✓':'✗'} ${k}</span>`).join('')}</div><div class="sig-foot">⚠ Exit if BN ${isBuy?'closes below':'closes above'} ₹${f(sig.sl)} · T3: ₹${f(sig.t3)}</div>`;}
function renderInds(meta){if(!meta)return;document.getElementById('i-e9').textContent=meta.e9?.toLocaleString('en-IN')||'—';document.getElementById('i-e9').style.color=meta.e9>meta.e21?'var(--green)':'var(--red)';document.getElementById('i-e21').textContent=meta.e21?.toLocaleString('en-IN')||'—';document.getElementById('i-rsi').textContent=meta.rsi||'—';document.getElementById('i-rsi').style.color=meta.rsi>65?'var(--red)':meta.rsi<35?'var(--green)':meta.rsi>50?'var(--green)':'var(--orange)';document.getElementById('i-rsib').style.width=(meta.rsi||50)+'%';document.getElementById('i-rsib').style.background=meta.rsi>70?'var(--red)':meta.rsi<30?'var(--green)':'var(--yellow)';document.getElementById('i-vw').textContent=meta.vwap?.toLocaleString('en-IN')||'—';document.getElementById('i-vw').style.color=S.spot>meta.vwap?'var(--green)':'var(--red)';document.getElementById('i-st').textContent=meta.st?.bull?'BULL ▲':'BEAR ▼';document.getElementById('i-st').style.color=meta.st?.bull?'var(--green)':'var(--red)';document.getElementById('i-pcr').textContent=S.pcr.toFixed(2);document.getElementById('i-pcr').style.color=S.pcr>=1.2?'var(--green)':S.pcr>=0.9?'var(--teal)':'var(--red)';}
function renderMinis(oc){if(!oc)return;const pcr=oc.pcr;const pEl=document.getElementById('m-pcr');pEl.textContent=pcr.toFixed(2);pEl.style.color=pcr>=1.3?'var(--green)':pcr>=1.0?'var(--teal)':pcr>=0.7?'var(--yellow)':'var(--red)';document.getElementById('m-bias').textContent=pcr>=1.2?'Bullish ↑':pcr>=0.9?'Neutral':'Bearish ↓';document.getElementById('m-mp').textContent='₹'+oc.maxPain.toLocaleString('en-IN');document.getElementById('m-oi').textContent=((oc.totCE+oc.totPE)/100000).toFixed(1)+'L';const vix=S.vix;document.getElementById('m-vix').textContent=vix?vix.toFixed(1):'—';document.getElementById('m-vix').style.color=vix<14?'var(--green)':vix<18?'var(--teal)':vix<22?'var(--yellow)':'var(--red)';document.getElementById('m-vixs').textContent=vix?vix<14?'CALM':vix<18?'NORMAL':vix<22?'ELEVATED':'DANGER':'—';}
function renderOC(oc){if(!oc)return;const spot=S.spot,atm=Math.round(spot/100)*100;const maxOI=Math.max(...oc.strikes.map(s=>Math.max(oc.ce[s]?.oi||0,oc.pe[s]?.oi||0)),1);let html='';for(const s of oc.strikes){const c=oc.ce[s]||{ltp:0,oi:0},p=oc.pe[s]||{ltp:0,oi:0};const isATM=Math.abs(s-spot)<50;const cw=Math.round((c.oi/maxOI)*28),pw=Math.round((p.oi/maxOI)*28);const oiK=n=>n>=100000?(n/100000).toFixed(1)+'L':(n/1000).toFixed(0)+'K';html+=`<tr${isATM?' class="atm"':''}><td style="text-align:left;color:var(--green)">₹${c.ltp}<span class="oib" style="width:${cw}px;background:var(--teal)"></span></td><td style="color:var(--teal)">${oiK(c.oi)}</td><td class="c" style="font-weight:900${isATM?';color:var(--yellow)':''}">${s.toLocaleString('en-IN')}${isATM?'<br><span style="font-size:6px;color:var(--yellow)">ATM</span>':''}</td><td style="color:var(--orange)">${oiK(p.oi)}</td><td style="text-align:right;color:var(--red)">₹${p.ltp}<span class="oib" style="width:${pw}px;background:var(--orange)"></span></td></tr>`;}document.getElementById('oc-body').innerHTML=html;}
function renderSR(piv,spot){if(!piv||!spot)return;const f=n=>Math.round(n).toLocaleString('en-IN');const di=n=>{const d=Math.round(n-spot);return(d>=0?'+':'')+d;};const c=n=>n>spot?'var(--red)':n<spot?'var(--green)':'var(--yellow)';const lvls=[{n:'R3',v:piv.R3},{n:'R2',v:piv.R2},{n:'R1',v:piv.R1},null,{n:'S1',v:piv.S1},{n:'S2',v:piv.S2},{n:'S3',v:piv.S3},{n:'PVT',v:piv.P,sp:true}];document.getElementById('sr-list').innerHTML=lvls.map(l=>l?`<div class="sr-row${l.sp?' sr-spot':''}"><span class="sr-n" style="color:${l.sp?'var(--yellow)':'var(--muted)'}">${l.n}</span><span class="sr-v" style="color:${c(l.v)}">₹${f(l.v)}</span><span class="sr-d" style="color:${c(l.v)}">${di(l.v)}</span></div>`:`<div class="sr-row sr-spot"><span class="sr-n" style="color:var(--white);font-weight:700">SPOT</span><span class="sr-v" style="color:var(--white)">₹${f(spot)}</span><span class="sr-d" style="color:var(--muted)">current</span></div>`).join('');}

const NEWS=[{sev:'HIGH',src:'NSE',body:'BN weekly expiry — max pain tracking ATM'},{sev:'MED',src:'RBI',body:'RBI holds at 6.5% — neutral stance'},{sev:'HIGH',src:'ET Mkts',body:'FIIs sold ₹2,840 Cr in index futures'},{sev:'MED',src:'Reuters',body:'Crude near $68.50 ahead of OPEC+'},{sev:'HIGH',src:'Bloomberg',body:'Fed signals higher-for-longer — DXY rising'},];
(function(){const sC={HIGH:'var(--red)',MED:'var(--yellow)',LOW:'var(--teal)'};document.getElementById('news-list').innerHTML=NEWS.map(n=>`<div class="ni"><div class="ni-top"><span class="ni-sev" style="background:${sC[n.sev]}22;color:${sC[n.sev]}">${n.sev}</span><span class="ni-src">${n.src}</span></div><div class="ni-body">${n.body}</div></div>`).join('');})();

['rc-c','rc-e','rc-s'].forEach(id=>document.getElementById(id).addEventListener('input',()=>{const cap=parseFloat(document.getElementById('rc-c').value)||50000;const ent=parseFloat(document.getElementById('rc-e').value);const sl=parseFloat(document.getElementById('rc-s').value);if(!ent||!sl||ent<=sl){document.getElementById('rc-lots').textContent='—';document.getElementById('rc-mr').textContent='—';return;}const lots=Math.max(1,Math.min(10,Math.floor(cap*0.01/((ent-sl)*15))));document.getElementById('rc-lots').textContent=lots+(lots===1?' lot':' lots');document.getElementById('rc-mr').textContent='₹'+(lots*(ent-sl)*15).toLocaleString('en-IN');}));

function toast(m){document.getElementById('toast-msg').textContent=m;const t=document.getElementById('toast');t.classList.add('show');setTimeout(()=>t.classList.remove('show'),4500);}

const ARIA_SYS=`You are ARIA, Bank Nifty options trading assistant. Be brief (2-4 sentences), direct. No markdown.`;
async function callAria(msg){if(!ariaKey){ariaKey=prompt('Anthropic API key for ARIA\\n(console.anthropic.com)\\nLeave blank to skip:');if(!ariaKey)return'ARIA not connected.';}const ctx=`BN ₹${S.spot||'—'} | PCR ${S.pcr.toFixed(2)} | Signal: ${S.signal?.signal||'WAIT'} ${S.signal?.conf||0}% | RSI ${S.signal?.meta?.rsi||'—'}`;ariaHist.push({role:'user',content:`[${ctx}]\\n${msg}`});if(ariaHist.length>10)ariaHist=ariaHist.slice(-10);const res=await fetch('https://api.anthropic.com/v1/messages',{method:'POST',headers:{'Content-Type':'application/json','x-api-key':ariaKey,'anthropic-version':'2023-06-01','anthropic-dangerous-direct-browser-access':'true'},body:JSON.stringify({model:'claude-sonnet-4-6',max_tokens:200,system:ARIA_SYS,messages:ariaHist})});const d=await res.json();if(d.error)throw new Error(d.error.message);const r=d.content?.[0]?.text||'Sorry.';ariaHist.push({role:'assistant',content:r});return r;}
function addMsg(html,type){const box=document.getElementById('ai-msgs');const el=document.createElement('div');el.className='msg '+type;el.innerHTML=html;box.appendChild(el);box.scrollTop=box.scrollHeight;return el;}
async function sendAI(){const inp=document.getElementById('ai-in');const msg=inp.value.trim();if(!msg)return;inp.value='';document.getElementById('ai-btn').disabled=true;addMsg(msg,'user');const td=addMsg('<div class="typing"><span></span><span></span><span></span></div>','bot');try{const r=await callAria(msg);td.className='msg bot';td.innerHTML=r;}catch(e){td.className='msg bot';td.innerHTML='Error: '+e.message;}document.getElementById('ai-btn').disabled=false;}
function ariaExplain(sig){if(!sig||sig.signal==='WAIT')return;const isBuy=sig.signal==='BUY';addMsg(`${isBuy?'🟢':'🔴'} <strong>${isBuy?'BUY CALL':'BUY PUT'}</strong> — ${sig.strike} ${sig.otype} · ${sig.conf}% conf<br>Entry: ₹${sig.entry?.toLocaleString('en-IN')||'—'} · SL: ₹${sig.sl?.toLocaleString('en-IN')||'—'}`,'bot');}

// CHART ENGINE
let chartInst=null;
function loadLWC(cb){if(window.LightweightCharts){cb();return;}const s=document.createElement('script');s.src='https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js';s.onload=cb;document.head.appendChild(s);}
function cEMA(d,p){const k=2/(p+1);let e=d[0].close;return d.map((c,i)=>{e=i===0?c.close:c.close*k+e*(1-k);return{time:c.time,value:+e.toFixed(2)};});}
function cVWAP(d){let tv=0,v=0;return d.map(c=>{const tp=(c.high+c.low+c.close)/3;tv+=tp*c.volume;v+=c.volume;return{time:c.time,value:v>0?+(tv/v).toFixed(2):c.close};});}
function cRSI(d,p=14){const out=[];let ag=0,al=0;for(let i=1;i<d.length;i++){const dv=d[i].close-d[i-1].close;if(i<=p){if(dv>0)ag+=dv;else al-=dv;if(i===p){ag/=p;al/=p;out.push({time:d[i].time,value:al===0?100:+(100-100/(1+ag/al)).toFixed(1)});}}else{ag=(ag*(p-1)+(dv>0?dv:0))/p;al=(al*(p-1)+(dv<0?-dv:0))/p;out.push({time:d[i].time,value:al===0?100:+(100-100/(1+ag/al)).toFixed(1)});}}return out;}
function buildChart(candles){
  const loadEl=document.getElementById('chart-loading');
  if(!candles||!candles.length){loadEl.style.display='block';loadEl.textContent='No candle data available.';return;}
  loadEl.style.display='none';
  const c=document.getElementById('chart-container'),rc=document.getElementById('rsi-container');
  c.innerHTML='';rc.innerHTML='';
  const ch=LightweightCharts.createChart(c,{width:c.clientWidth,height:c.clientHeight||280,layout:{background:{color:'#060A10'},textColor:'#4A6070'},grid:{vertLines:{color:'#192336'},horzLines:{color:'#192336'}},crosshair:{mode:1},rightPriceScale:{borderColor:'#192336'},timeScale:{borderColor:'#192336',timeVisible:true,secondsVisible:false}});
  const cs=ch.addCandlestickSeries({upColor:'#00E676',downColor:'#FF1744',borderUpColor:'#00E676',borderDownColor:'#FF1744',wickUpColor:'#00E676',wickDownColor:'#FF1744'});
  cs.setData(candles);
  const vs=ch.addHistogramSeries({priceScaleId:'vol',scaleMargins:{top:0.85,bottom:0}});
  vs.setData(candles.map(c=>({time:c.time,value:c.volume,color:c.close>=c.open?'rgba(0,230,118,0.2)':'rgba(255,23,68,0.2)'})));
  const e9=ch.addLineSeries({color:'#FF6D00',lineWidth:1,priceLineVisible:false,lastValueVisible:false});e9.setData(cEMA(candles,9));
  const e21=ch.addLineSeries({color:'#00BFA5',lineWidth:1,priceLineVisible:false,lastValueVisible:false});e21.setData(cEMA(candles,21));
  const vw=ch.addLineSeries({color:'#FFD600',lineWidth:1,lineStyle:2,priceLineVisible:false,lastValueVisible:false});vw.setData(cVWAP(candles));
  ch.timeScale().fitContent();chartInst=ch;
  const rc2=LightweightCharts.createChart(rc,{width:rc.clientWidth,height:76,layout:{background:{color:'#0A1018'},textColor:'#4A6070'},grid:{vertLines:{color:'#192336'},horzLines:{color:'#0E1620'}},rightPriceScale:{borderColor:'#192336'},timeScale:{visible:false},crosshair:{mode:1}});
  const rs=rc2.addLineSeries({color:'#9C27B0',lineWidth:1,lastValueVisible:true,priceLineVisible:false});
  const rd=cRSI(candles,14);rs.setData(rd);
  if(rd.length){const t0=rd[0].time,t1=rd[rd.length-1].time;rc2.addLineSeries({color:'rgba(255,23,68,0.35)',lineWidth:1,lineStyle:2,priceLineVisible:false,lastValueVisible:false}).setData([{time:t0,value:70},{time:t1,value:70}]);rc2.addLineSeries({color:'rgba(0,230,118,0.35)',lineWidth:1,lineStyle:2,priceLineVisible:false,lastValueVisible:false}).setData([{time:t0,value:30},{time:t1,value:30}]);}
  const ea=cEMA(candles,9),eb=cEMA(candles,21),vc=cVWAP(candles),rf=cRSI(candles,14);
  const fi=v=>Math.round(v).toLocaleString('en-IN');
  document.getElementById('chart-legend').innerHTML=`<span style='color:#FF6D00'>EMA9 ${fi(ea[ea.length-1]?.value||0)}</span> <span style='color:#00BFA5'>EMA21 ${fi(eb[eb.length-1]?.value||0)}</span> <span style='color:#FFD600'>VWAP ${fi(vc[vc.length-1]?.value||0)}</span> <span style='color:#9C27B0'>RSI ${rf[rf.length-1]?.value||'-'}</span>`;
  new ResizeObserver(()=>{if(ch)ch.resize(c.clientWidth,c.clientHeight||280);if(rc2)rc2.resize(rc.clientWidth,76);}).observe(c);
}
async function loadChart(iv){
  document.querySelectorAll('.tf').forEach(b=>{const m={'1':'1m','5':'5m','15':'15m','60':'1h'};b.classList.toggle('on',b.textContent===m[iv]);});
  const el=document.getElementById('chart-loading');el.style.display='block';el.textContent='Fetching '+iv+'m candles from Upstox...';
  try{const res=await fetch('/api/candles?interval='+iv);const d=await res.json();loadLWC(()=>buildChart(d.candles||[]));}
  catch(e){el.textContent='Error loading chart: '+e.message;}
}
document.querySelectorAll('.tab').forEach((t,i)=>{t.addEventListener('click',()=>{if(i===1&&!chartInst)setTimeout(()=>loadChart('5'),200);});});
// Boot
fetchFromServer();setInterval(fetchFromServer,30000);

// ═══════ UPSTOX CHART ENGINE ═══════
var lwC=null,cSeries=null,e9S=null,e21S=null,vwS=null,curIv=5;

function initChart(){
  if(typeof LightweightCharts==='undefined'){setTimeout(initChart,400);return;}
  var el=document.getElementById('lw_chart');
  if(!el||lwC)return;
  lwC=LightweightCharts.createChart(el,{
    width:el.clientWidth,height:el.clientHeight||320,
    layout:{background:{color:'#060A10'},textColor:'#4A6070'},
    grid:{vertLines:{color:'#192336'},horzLines:{color:'#192336'}},
    rightPriceScale:{borderColor:'#192336'},
    timeScale:{borderColor:'#192336',timeVisible:true,secondsVisible:false},
  });
  cSeries=lwC.addCandlestickSeries({upColor:'#00E676',downColor:'#FF1744',borderUpColor:'#00E676',borderDownColor:'#FF1744',wickUpColor:'#00E676',wickDownColor:'#FF1744'});
  e9S=lwC.addLineSeries({color:'#00BFA5',lineWidth:1,priceLineVisible:false,lastValueVisible:false});
  e21S=lwC.addLineSeries({color:'#FF6D00',lineWidth:1,priceLineVisible:false,lastValueVisible:false});
  vwS=lwC.addLineSeries({color:'#FFD600',lineWidth:1,lineStyle:1,priceLineVisible:false,lastValueVisible:false});
  lwC.subscribeCrosshairMove(function(p){
    if(!p.time)return;
    var d=p.seriesData&&p.seriesData.get(cSeries);
    if(!d)return;
    var leg=document.getElementById('chart-legend');
    if(leg)leg.textContent='O:'+Math.round(d.open).toLocaleString('en-IN')+'  H:'+Math.round(d.high).toLocaleString('en-IN')+'  L:'+Math.round(d.low).toLocaleString('en-IN')+'  C:'+Math.round(d.close).toLocaleString('en-IN');
  });
  window.addEventListener('resize',function(){if(lwC&&el)lwC.resize(el.clientWidth,el.clientHeight);});
  loadChart(5);
}

function calcEMA(data,p){var k=2/(p+1),e=data[0].close;return data.map(function(d,i){if(i>0)e=d.close*k+e*(1-k);return{time:d.time,value:+e.toFixed(2)};});}
function calcVWAP(data){var pv=0,v=0;return data.map(function(d){var tp=(d.high+d.low+d.close)/3;pv+=tp*(d.volume||1);v+=(d.volume||1);return{time:d.time,value:+(pv/v).toFixed(2)};});}

async function loadChart(iv){
  curIv=iv;
  document.querySelectorAll('.tf').forEach(function(b){var m={1:'1m',5:'5m',15:'15m',60:'1h'};b.classList.toggle('on',b.textContent===m[iv]);});
  var ld=document.getElementById('chart-loading');
  if(ld){ld.style.display='block';ld.textContent='LOADING CANDLES...';}
  try{
    var res=await fetchT('/api/candles?interval='+iv,12000);
    var data=await res.json();
    var c=data.candles||[];
    if(!c.length){if(ld)ld.textContent='Market closed — no candles yet';return;}
    if(ld)ld.style.display='none';
    if(!lwC)initChart();
    setTimeout(function(){
      if(!cSeries)return;
      cSeries.setData(c);
      e9S.setData(calcEMA(c,9));
      e21S.setData(calcEMA(c,21));
      vwS.setData(calcVWAP(c));
      lwC.timeScale().fitContent();
    },100);
  }catch(e){if(ld)ld.textContent='Error: '+e.message;}
}

// Load Lightweight Charts library
(function(){
  var s=document.createElement('script');
  s.src='https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js';
  s.onload=function(){setTimeout(initChart,200);};
  document.head.appendChild(s);
})();

// Auto-refresh chart every 30s when chart tab open
setInterval(function(){if(document.getElementById('page-1').classList.contains('on'))loadChart(curIv);},30000);

</script>
</body>
</html>
"""

def save_token(data):
    state["access_token"] = data.get("access_token", "")
    with open(TOKEN_FILE, "w") as f: json.dump(data, f)
    cache["authenticated"] = True
    print("[AUTH] Token saved.")

def load_token():
    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)
            state["access_token"] = data.get("access_token", "")
            if state["access_token"]:
                cache["authenticated"] = True
                print("[AUTH] Token loaded.")
                return True
    except: pass
    return False

def hdr():
    return {"Authorization": f"Bearer {state['access_token']}", "Accept": "application/json"}

@app.route("/login")
def login():
    params = {"client_id": API_KEY, "redirect_uri": REDIRECT_URI,
              "response_type": "code", "state": "bn_terminal"}
    return redirect("https://api.upstox.com/v2/login/authorization/dialog?" + urlencode(params))

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code: return "Error: No code.", 400
    try:
        resp = requests.post(
            "https://api.upstox.com/v2/login/authorization/token",
            data={"code": code, "client_id": API_KEY, "client_secret": API_SECRET,
                  "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code"},
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        )
        data = resp.json()
        if "access_token" not in data: return f"Auth failed: {data}", 400
        save_token(data)
        fetch_prices()
        threading.Thread(target=fetch_loop, daemon=True).start()
        return """<html><body style='background:#060A10;color:#00E676;font-family:monospace;padding:40px;text-align:center'>
        <h1 style='color:#FF6D00;font-size:32px'>✓ CONNECTED</h1>
        <p style='font-size:16px;color:#D8E8F8'>Upstox authenticated!<br><br>
        <a href='/terminal' style='padding:14px 28px;background:#FF6D00;color:#000;font-weight:900;font-size:16px;border-radius:4px;text-decoration:none'>OPEN TERMINAL →</a></p>
        <p style='color:#4A6070;font-size:11px;margin-top:20px'>Bookmark /terminal — open it anytime on any device.</p>
        </body></html>"""
    except Exception as e:
        return f"Error: {e}", 500

@app.route("/terminal")
def terminal():
    return Response(TERMINAL_HTML, mimetype="text/html")

def fetch_quote(keys):
    try:
        url = f"https://api.upstox.com/v2/market-quote/quotes?instrument_key={requests.utils.quote(','.join(keys))}"
        r = requests.get(url, headers=hdr(), timeout=10)
        r.raise_for_status()
        return r.json().get("data", {})
    except Exception as e:
        print(f"[QUOTE] {e}"); return {}

def fetch_option_chain(spot):
    try:
        d = date.today()
        days = (3 - d.weekday()) % 7
        if days == 0: days = 7
        expiry = (d + timedelta(days=days)).strftime("%Y-%m-%d")
        url = f"https://api.upstox.com/v2/option/chain?instrument_key={requests.utils.quote(BN_KEY)}&expiry_date={expiry}"
        r = requests.get(url, headers=hdr(), timeout=10)
        if r.status_code != 200: return None
        data = r.json().get("data", [])
        if not data: return None
        chain, tot_ce, tot_pe, pain = [], 0, 0, {}
        for item in data:
            s = item.get("strike_price", 0)
            ce = item.get("call_options", {}).get("market_data", {})
            pe = item.get("put_options",  {}).get("market_data", {})
            co, po = ce.get("oi", 0) or 0, pe.get("oi", 0) or 0
            tot_ce += co; tot_pe += po
            chain.append({"strike": s, "ce_ltp": round(ce.get("ltp",0) or 0,1),
                "ce_oi": co, "ce_iv": round(ce.get("iv",0) or 0,1),
                "pe_ltp": round(pe.get("ltp",0) or 0,1),
                "pe_oi": po, "pe_iv": round(pe.get("iv",0) or 0,1),
                "is_atm": abs(s-spot)<50})
            pain[s] = sum(max(0,ss.get("strike_price",0)-s)*(ss.get("call_options",{}).get("market_data",{}).get("oi",0) or 0)
                        + max(0,s-ss.get("strike_price",0))*(ss.get("put_options",{}).get("market_data",{}).get("oi",0) or 0)
                        for ss in data)
        max_pain = min(pain, key=pain.get) if pain else round(spot/100)*100
        pcr = round(tot_pe/tot_ce, 2) if tot_ce > 0 else 1.0
        chain.sort(key=lambda x: x["strike"])
        idx = min(range(len(chain)), key=lambda i: abs(chain[i]["strike"]-spot))
        chain = chain[max(0,idx-6):idx+7]
        return {"chain": chain, "pcr": pcr, "max_pain": max_pain, "tot_ce_oi": tot_ce, "tot_pe_oi": tot_pe}
    except Exception as e:
        print(f"[OC] {e}"); return None

def fetch_globals():
    try:
        url = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=%5EGSPC,CL%3DF,GC%3DF,USDINR%3DX"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        q = {x["symbol"]: x for x in r.json().get("quoteResponse", {}).get("result", [])}
        cache["sp500_chg"] = round(q.get("^GSPC",   {}).get("regularMarketChangePercent", 0), 2)
        cache["crude_chg"] = round(q.get("CL=F",    {}).get("regularMarketChangePercent", 0), 2)
        cache["gold_chg"]  = round(q.get("GC=F",    {}).get("regularMarketChangePercent", 0), 2)
        cache["usdinr"]    = round(q.get("USDINR=X",{}).get("regularMarketPrice", 0), 2)
    except Exception as e:
        print(f"[GLOBALS] {e}")


def fetch_candles(interval='5minute', days=2):
    """Fetch OHLCV candle data from Upstox for the chart."""
    try:
        from datetime import timedelta
        end = date.today()
        start = end - timedelta(days=days)
        # Map interval to Upstox format
        iv_map = {'1':'1minute','5':'5minute','15':'15minute','60':'60minute',
                  '1minute':'1minute','5minute':'5minute','15minute':'15minute','60minute':'60minute'}
        iv = iv_map.get(str(interval), '5minute')
        url = (f"https://api.upstox.com/v2/historical-candle/"
               f"{requests.utils.quote(BN_KEY)}/{iv}/"
               f"{end.strftime('%Y-%m-%d')}/{start.strftime('%Y-%m-%d')}")
        r = requests.get(url, headers=hdr(), timeout=15)
        if r.status_code != 200:
            print(f"[CANDLES] HTTP {r.status_code}: {r.text[:200]}")
            return []
        data = r.json().get("data", {}).get("candles", [])
        # Upstox format: [timestamp, open, high, low, close, volume, oi]
        candles = []
        for c in data:
            try:
                ts = int(datetime.fromisoformat(c[0].replace('Z','+00:00')).timestamp())
                candles.append({"time": ts, "open": c[1], "high": c[2],
                                "low": c[3], "close": c[4], "volume": c[5]})
            except: pass
        candles.sort(key=lambda x: x["time"])
        print(f"[CANDLES] Fetched {len(candles)} candles ({iv})")
        return candles
    except Exception as e:
        print(f"[CANDLES] Error: {e}")
        return []

# Candle cache
candle_cache = {"1": [], "5": [], "15": [], "60": [], "last_fetch": {}}

def refresh_candles(interval='5'):
    """Refresh candle cache for given interval."""
    iv_map = {"1":"1minute","5":"5minute","15":"15minute","60":"60minute"}
    data = fetch_candles(iv_map.get(interval,'5minute'))
    if data:
        candle_cache[interval] = data
        candle_cache["last_fetch"][interval] = datetime.now().strftime("%H:%M:%S")
    return data

# Candle cache
candle_cache = {"1": [], "5": [], "15": [], "60": []}

def fetch_candles(interval="5"):
    """Fetch historical candles from Upstox for given interval in minutes."""
    if not state["access_token"]:
        return []
    try:
        from datetime import timedelta
        interval_map = {"1":"1minute","5":"5minute","15":"15minute","60":"60minute"}
        upstox_interval = interval_map.get(str(interval), "5minute")
        today = date.today()
        # For intraday use today; if weekend/after hours use last trading day
        weekday = today.weekday()
        if weekday == 5: today = today - timedelta(days=1)
        elif weekday == 6: today = today - timedelta(days=2)
        from_date = today.strftime("%Y-%m-%d")
        to_date   = today.strftime("%Y-%m-%d")
        url = (f"https://api.upstox.com/v2/historical-candle/intraday/"
               f"{requests.utils.quote(BN_KEY)}/{upstox_interval}")
        r = requests.get(url, headers=hdr(), timeout=10)
        if r.status_code != 200:
            print(f"[CANDLES] HTTP {r.status_code}: {r.text[:200]}")
            return []
        data = r.json().get("data", {}).get("candles", [])
        # Upstox format: [timestamp, open, high, low, close, volume, oi]
        candles = []
        for c in data:
            try:
                ts = int(datetime.fromisoformat(c[0].replace("Z","+00:00")).timestamp())
                candles.append({
                    "time": ts,
                    "open":   round(float(c[1]), 2),
                    "high":   round(float(c[2]), 2),
                    "low":    round(float(c[3]), 2),
                    "close":  round(float(c[4]), 2),
                    "volume": int(c[5]),
                })
            except: pass
        candles.sort(key=lambda x: x["time"])
        candle_cache[str(interval)] = candles
        print(f"[CANDLES] {interval}m: {len(candles)} candles fetched")
        return candles
    except Exception as e:
        print(f"[CANDLES] Error: {e}")
        return []

def fetch_prices():
    if not state["access_token"]:
        cache["error"] = "Not authenticated"; cache["source"] = "unauthenticated"; return False
    try:
        data = fetch_quote([BN_KEY, VIX_KEY])
        bn  = data.get("NSE_INDEX:Nifty Bank", {})
        vix = data.get("NSE_INDEX:India VIX", {})
        if not bn: raise ValueError("No BN data")
        spot = bn.get("last_price", 0)
        if not spot or spot < 30000 or spot > 110000: raise ValueError(f"Bad price: {spot}")
        ohlc = bn.get("ohlc", {})
        cache.update({
            "spot": round(spot,2), "change": round(bn.get("net_change",0),2),
            "pct":  round(bn.get("change_percentage",0),2),
            "high": round(ohlc.get("high",spot),2), "low": round(ohlc.get("low",spot),2),
            "open": round(ohlc.get("open",spot),2),
            "vwap": round(bn.get("average_price",spot),2),
            "vix":  round(vix.get("last_price",0),2),
            "last_updated": datetime.now().strftime("%H:%M:%S"),
            "source": "Upstox Live ✓", "error": "", "authenticated": True,
        })
        oc = fetch_option_chain(spot)
        if oc:
            cache.update({"option_chain": oc["chain"], "pcr": oc["pcr"],
                "max_pain": oc["max_pain"], "tot_ce_oi": oc["tot_ce_oi"], "tot_pe_oi": oc["tot_pe_oi"]})
        fetch_globals()
        cache["market_open"] = True
        save_last_session()
        # Refresh 5min candles every fetch cycle
        refresh_candles("5")
        print(f"[{cache['last_updated']}] BN ₹{spot:,.0f} | VIX {cache['vix']} | PCR {cache['pcr']}")
        return True
    except Exception as e:
        cache["error"] = str(e)
        cache["market_open"] = False
        # If market is closed, use last session data as display fallback
        if cache["last_session"]:
            cache["source"] = f"Last session: {cache['last_session'].get('saved_at','?')}"
        else:
            cache["source"] = "error"
        print(f"[FETCH] {e}"); return False

def fetch_loop():
    was_open = False
    while True:
        now_open = is_market_open()
        cache["market_open"] = now_open
        if now_open:
            was_open = True
            fetch_prices()
            time.sleep(30)
        else:
            if was_open:
                # Market just closed — save final session
                print("[SESSION] Market closed. Saving final session data.")
                save_last_session()
                was_open = False
            # Outside market hours — check every 5 min
            time.sleep(300)

@app.route("/")
def index():
    if not cache["authenticated"]:
        return """<html><body style='background:#060A10;color:#D8E8F8;font-family:monospace;padding:40px;text-align:center'>
        <h1 style='color:#FF6D00;font-size:28px;letter-spacing:0.1em'>BN TERMINAL SERVER</h1>
        <p style='color:#4A6070;margin:10px 0 30px'>One-time Upstox login required</p>
        <a href='/login' style='padding:14px 32px;background:#FF6D00;color:#000;font-weight:900;font-size:16px;border-radius:4px;text-decoration:none;letter-spacing:0.08em'>LOGIN WITH UPSTOX →</a>
        </body></html>"""
    return redirect("/terminal")

@app.route("/api/price")
def price_api():
    # When market closed, merge last session into response for display
    response = dict(cache)
    if not cache["market_open"] and cache["last_session"]:
        ls = cache["last_session"]
        for key in ["spot","change","pct","high","low","open","vwap",
                    "vix","pcr","max_pain","tot_ce_oi","tot_pe_oi",
                    "sp500_chg","crude_chg","gold_chg","usdinr","option_chain"]:
            if response.get(key, 0) == 0 and ls.get(key):
                response[key] = ls[key]
        response["using_last_session"] = True
        response["last_session_time"] = ls.get("saved_at", "")
    return jsonify(response)

@app.route("/api/status")
def status(): return jsonify({"running": True, "authenticated": cache["authenticated"],
    "spot": cache["spot"], "last_updated": cache["last_updated"], "source": cache["source"]})

@app.route("/api/candles")
def candles_api():
    interval = request.args.get("interval", "5")
    if interval not in candle_cache or not candle_cache[interval]:
        # Fetch on demand if not cached
        data = refresh_candles(interval)
    else:
        data = candle_cache[interval]
    return jsonify({"candles": data, "interval": interval,
                    "count": len(data), "last_fetch": candle_cache["last_fetch"].get(interval,"")})

@app.route("/api/candles")
def candles_api():
    interval = request.args.get("interval", "5")
    cached = candle_cache.get(str(interval), [])
    if not cached:
        # Fetch on demand if not cached
        cached = fetch_candles(interval)
    return jsonify({"candles": cached, "interval": interval, "count": len(cached)})

@app.route("/ping")
def ping(): return "pong"

if __name__ == "__main__":
    print("=" * 50)
    print("  BN TERMINAL — UPSTOX ALL-IN-ONE SERVER")
    print("=" * 50)
    load_token()
    load_last_session()
    if cache["authenticated"]:
        print("Fetching prices...")
        fetch_prices()
        threading.Thread(target=fetch_loop, daemon=True).start()
    else:
        print("Open http://localhost:5000 → Login with Upstox")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
