"""
BN Terminal Server — Upstox Edition
=====================================
Live Bank Nifty data from Upstox API.
Fetches prices, options chain, OI every 30 seconds.
Deploy to Railway for 24/7 cloud operation.

SETUP:
1. pip install flask flask-cors requests gunicorn
2. python bn_server.py
3. Open http://localhost:5000 in browser
4. Click "Login with Upstox" — approve once
5. Done — prices fetch automatically forever
"""

import threading
import time
import os
import requests
import json
from datetime import datetime, date
from flask import Flask, jsonify, redirect, request
from flask_cors import CORS
from urllib.parse import urlencode

app = Flask(__name__)
CORS(app)

# ── CONFIG ──
# Keys are loaded from environment variables for Railway deployment
# Fallback to hardcoded for local use
API_KEY      = os.environ.get("UPSTOX_API_KEY",    "2ea6b52c-e87b-4ae0-b84c-6395a73790a2")
API_SECRET   = os.environ.get("UPSTOX_API_SECRET", "urcjw710uv")
REDIRECT_URI = os.environ.get("REDIRECT_URI",      "http://127.0.0.1:5000/callback")
TOKEN_FILE   = "upstox_token.json"

BN_KEY  = "NSE_INDEX|Nifty Bank"
VIX_KEY = "NSE_INDEX|India VIX"

# ── STATE ──
state = {"access_token": "", "token_expiry": ""}
cache = {
    "spot": 0, "change": 0, "pct": 0,
    "high": 0, "low": 0, "open": 0, "vwap": 0,
    "vix": 0, "pcr": 0, "max_pain": 0,
    "tot_ce_oi": 0, "tot_pe_oi": 0,
    "sp500_chg": 0, "crude_chg": 0, "gold_chg": 0, "usdinr": 0,
    "option_chain": [],
    "last_updated": "", "source": "starting",
    "error": "", "authenticated": False,
}

# ── TOKEN ──
def save_token(data):
    state["access_token"] = data.get("access_token", "")
    state["token_expiry"] = data.get("token_expiry", "")
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f)
    cache["authenticated"] = True
    print(f"[AUTH] Token saved.")

def load_token():
    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)
            state["access_token"] = data.get("access_token", "")
            if state["access_token"]:
                cache["authenticated"] = True
                print("[AUTH] Token loaded.")
                return True
    except:
        pass
    return False

def headers():
    return {"Authorization": f"Bearer {state['access_token']}", "Accept": "application/json"}

# ── AUTH ROUTES ──
@app.route("/login")
def login():
    params = {
        "client_id": API_KEY,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "state": "bn_terminal"
    }
    return redirect("https://api.upstox.com/v2/login/authorization/dialog?" + urlencode(params))

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return "Error: No code received.", 400
    try:
        resp = requests.post(
            "https://api.upstox.com/v2/login/authorization/token",
            data={"code": code, "client_id": API_KEY, "client_secret": API_SECRET,
                  "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code"},
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        )
        data = resp.json()
        if "access_token" not in data:
            return f"Auth failed: {data}", 400
        save_token(data)
        fetch_prices()
        threading.Thread(target=fetch_loop, daemon=True).start()
        return """<html><body style='background:#060A10;color:#00E676;font-family:monospace;padding:40px;text-align:center'>
        <h1 style='color:#FF6D00;font-size:32px'>✓ CONNECTED</h1>
        <p style='font-size:16px;color:#D8E8F8'>Upstox authenticated successfully.<br><br>
        Close this tab and open <strong>bn_terminal.html</strong> in your browser.</p>
        <p style='color:#4A6070;font-size:11px;margin-top:20px'>Live BN prices are now fetching every 30 seconds.</p>
        </body></html>"""
    except Exception as e:
        return f"Error: {e}", 500

# ── FETCH ──
def fetch_quote(keys):
    try:
        url = f"https://api.upstox.com/v2/market-quote/quotes?instrument_key={requests.utils.quote(','.join(keys))}"
        r = requests.get(url, headers=headers(), timeout=10)
        r.raise_for_status()
        return r.json().get("data", {})
    except Exception as e:
        print(f"[QUOTE] {e}")
        return {}

def fetch_option_chain(spot):
    try:
        # Get next Thursday expiry
        from datetime import timedelta
        d = date.today()
        days_ahead = (3 - d.weekday()) % 7  # 3 = Thursday
        if days_ahead == 0: days_ahead = 7
        expiry = (d + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        url = f"https://api.upstox.com/v2/option/chain?instrument_key={requests.utils.quote(BN_KEY)}&expiry_date={expiry}"
        r = requests.get(url, headers=headers(), timeout=10)
        if r.status_code != 200:
            return None
        data = r.json().get("data", [])
        if not data:
            return None
        chain = []
        tot_ce = 0
        tot_pe = 0
        pain = {}
        for item in data:
            s = item.get("strike_price", 0)
            ce = item.get("call_options", {}).get("market_data", {})
            pe = item.get("put_options",  {}).get("market_data", {})
            co = ce.get("oi", 0) or 0
            po = pe.get("oi", 0) or 0
            tot_ce += co
            tot_pe += po
            chain.append({
                "strike":    s,
                "ce_ltp":    round(ce.get("ltp", 0) or 0, 1),
                "ce_oi":     co,
                "ce_oi_chg": round(ce.get("oi_change_perc", 0) or 0, 1),
                "ce_iv":     round(ce.get("iv", 0) or 0, 1),
                "pe_ltp":    round(pe.get("ltp", 0) or 0, 1),
                "pe_oi":     po,
                "pe_oi_chg": round(pe.get("oi_change_perc", 0) or 0, 1),
                "pe_iv":     round(pe.get("iv", 0) or 0, 1),
                "is_atm":    abs(s - spot) < 50,
            })
            # Max pain
            loss = sum(max(0, ss.get("strike_price",0)-s)*(ss.get("call_options",{}).get("market_data",{}).get("oi",0) or 0)
                     + max(0, s-ss.get("strike_price",0))*(ss.get("put_options",{}).get("market_data",{}).get("oi",0) or 0)
                     for ss in data)
            pain[s] = loss
        max_pain = min(pain, key=pain.get) if pain else round(spot/100)*100
        pcr = round(tot_pe / tot_ce, 2) if tot_ce > 0 else 1.0
        # Keep 13 strikes around ATM
        chain.sort(key=lambda x: x["strike"])
        idx = min(range(len(chain)), key=lambda i: abs(chain[i]["strike"]-spot))
        chain = chain[max(0,idx-6):idx+7]
        return {"chain": chain, "pcr": pcr, "max_pain": max_pain, "tot_ce_oi": tot_ce, "tot_pe_oi": tot_pe}
    except Exception as e:
        print(f"[OC] {e}")
        return None

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

def fetch_prices():
    if not state["access_token"]:
        cache["error"] = "Not authenticated — open http://localhost:5000 and login"
        cache["source"] = "unauthenticated"
        return False
    try:
        data = fetch_quote([BN_KEY, VIX_KEY])
        bn  = data.get("NSE_INDEX:Nifty Bank", {})
        vix = data.get("NSE_INDEX:India VIX", {})
        if not bn:
            raise ValueError("No BN data")
        spot = bn.get("last_price", 0)
        if not spot or spot < 30000 or spot > 110000:
            raise ValueError(f"Bad price: {spot}")
        ohlc = bn.get("ohlc", {})
        cache.update({
            "spot":   round(spot, 2),
            "change": round(bn.get("net_change", 0), 2),
            "pct":    round(bn.get("change_percentage", 0), 2),
            "high":   round(ohlc.get("high", spot), 2),
            "low":    round(ohlc.get("low",  spot), 2),
            "open":   round(ohlc.get("open", spot), 2),
            "vwap":   round(bn.get("average_price", spot), 2),
            "vix":    round(vix.get("last_price", 0), 2),
            "last_updated": datetime.now().strftime("%H:%M:%S"),
            "source": "Upstox Live ✓",
            "error":  "", "authenticated": True,
        })
        oc = fetch_option_chain(spot)
        if oc:
            cache.update({
                "option_chain": oc["chain"],
                "pcr":          oc["pcr"],
                "max_pain":     oc["max_pain"],
                "tot_ce_oi":    oc["tot_ce_oi"],
                "tot_pe_oi":    oc["tot_pe_oi"],
            })
        fetch_globals()
        print(f"[{cache['last_updated']}] BN ₹{spot:,.0f} | VIX {cache['vix']} | PCR {cache['pcr']}")
        return True
    except Exception as e:
        cache["error"] = str(e)
        cache["source"] = "error"
        print(f"[FETCH] Error: {e}")
        return False

def fetch_loop():
    while True:
        fetch_prices()
        time.sleep(30)

# ── ROUTES ──
@app.route("/")
def index():
    if not cache["authenticated"]:
        return """<html><body style='background:#060A10;color:#D8E8F8;font-family:monospace;padding:40px;text-align:center'>
        <h1 style='color:#FF6D00;font-size:28px;letter-spacing:0.1em'>BN TERMINAL SERVER</h1>
        <p style='color:#4A6070;margin:10px 0 30px'>One-time Upstox login required</p>
        <a href='/login' style='padding:14px 32px;background:#FF6D00;color:#000;font-weight:900;font-size:16px;border-radius:4px;text-decoration:none;letter-spacing:0.08em'>LOGIN WITH UPSTOX →</a>
        <p style='color:#253545;font-size:10px;margin-top:30px'>After login, open bn_terminal.html in your browser</p>
        </body></html>"""
    return f"""<html><body style='background:#060A10;color:#00E676;font-family:monospace;padding:40px;text-align:center'>
    <h1 style='color:#FF6D00'>✓ BN SERVER RUNNING</h1>
    <p>BN ₹{cache['spot']:,} | VIX {cache['vix']} | PCR {cache['pcr']}</p>
    <p style='color:#4A6070'>Last updated: {cache['last_updated']} | Source: {cache['source']}</p>
    <p style='margin-top:20px'>Open <strong>bn_terminal.html</strong> in your browser or phone</p>
    </body></html>"""

@app.route("/api/price")
def price_api():
    return jsonify(cache)

@app.route("/api/status")
def status():
    return jsonify({"running": True, "authenticated": cache["authenticated"],
                    "spot": cache["spot"], "last_updated": cache["last_updated"],
                    "source": cache["source"]})

@app.route("/ping")
def ping():
    return "pong"

# ── BOOT ──
if __name__ == "__main__":
    print("=" * 50)
    print("  BN TERMINAL — UPSTOX SERVER")
    print("=" * 50)
    load_token()
    if cache["authenticated"]:
        print("Fetching live prices...")
        fetch_prices()
        threading.Thread(target=fetch_loop, daemon=True).start()
    else:
        print("Open http://localhost:5000 → click Login with Upstox")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
