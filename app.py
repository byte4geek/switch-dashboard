import json
import time
import threading
import os
import logging
from logging.handlers import RotatingFileHandler
from collections import deque
from flask import Flask, render_template, jsonify, request, redirect, url_for, Response, send_from_directory
from datetime import datetime

BASE = os.path.dirname(__file__)
DATA_DIR = os.environ.get("DASHBOARD_DATA_DIR", BASE)
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
LOG_FILE_PATH = os.path.join(DATA_DIR, "logs", "dashboard.log")


def setup_logging(level_name=None):
    if not level_name:
        try:
            if os.path.exists(SETTINGS_PATH):
                with open(SETTINGS_PATH) as f:
                    settings = json.load(f)
                    level_name = settings.get("log_level", "INFO")
            else:
                level_name = "INFO"
        except Exception:
            level_name = "INFO"

    levels = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARN": logging.WARNING,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
        "NONE": 99
    }
    level = levels.get(level_name.upper(), logging.INFO)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    root.setLevel(level)
    
    # Silence or set level for child loggers explicitly to override propagations
    logging.getLogger('werkzeug').setLevel(level)
    logging.getLogger('scraper').setLevel(level)
    
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)
    root.addHandler(console_handler)

    try:
        os.makedirs(os.path.dirname(LOG_FILE_PATH), exist_ok=True)
        file_handler = RotatingFileHandler(LOG_FILE_PATH, maxBytes=1024*1024, backupCount=3, encoding='utf-8')
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        root.addHandler(file_handler)
    except Exception as e:
        print(f"Error setting up RotatingFileHandler: {e}")

setup_logging()
logger = logging.getLogger(__name__)

app = Flask(__name__,
            template_folder=os.path.join(BASE, "templates"),
            static_folder=os.path.join(BASE, "static"))

if DATA_DIR != BASE:
    os.makedirs(DATA_DIR, exist_ok=True)
    # Copy config.json if not present in DATA_DIR
    dest_config = os.path.join(DATA_DIR, "config.json")
    if not os.path.exists(dest_config):
        src_config = os.path.join(BASE, "config.json")
        if os.path.exists(src_config):
            import shutil
            try:
                shutil.copy2(src_config, dest_config)
                logger.info(f"Initialized default config.json in {dest_config}")
            except Exception as e:
                logger.error(f"Failed to initialize default config.json: {e}")

CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
COUNTERS_PATH = os.path.join(DATA_DIR, "counters.json")
NOTES_PATH = os.path.join(DATA_DIR, "notes.json")
HOURLY_PATH = os.path.join(DATA_DIR, "history_hourly.json")
DAILY_PATH = os.path.join(DATA_DIR, "history_daily.json")
BACKUP_DIR = os.path.join(DATA_DIR, "backup")
DEVICE_TEMPLATES_DIR = os.path.join(DATA_DIR, "device-templates")
os.makedirs(DEVICE_TEMPLATES_DIR, exist_ok=True)
VENDORS_TXT_PATH = os.path.join(DATA_DIR, "mac_vendors.txt")
OUI36_TXT_PATH = os.path.join(DATA_DIR, "oui36.txt")
OUI_TXT_PATH = os.path.join(DATA_DIR, "oui.txt")
VERSION = "2026.6.1"

ieee_vendors_cache = {}


def download_single_oui_file(url, dest_path):
    global ieee_vendors_cache
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    logger.info(f"Downloading OUI list from {url} to {dest_path}...")
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={'User-Agent': ua})
        with urllib.request.urlopen(req, timeout=15) as response:
            content = response.read()
        with open(dest_path, "wb") as f:
            f.write(content)
        ieee_vendors_cache = {}  # Clear cache to trigger reload
        logger.info(f"Successfully downloaded and saved {os.path.basename(dest_path)}.")
        return True
    except Exception as e:
        logger.warning(f"Failed to download {os.path.basename(dest_path)} using urllib: {e}. Trying fallback with curl...")
        try:
            import subprocess
            res = subprocess.run(['curl.exe', '-s', '-o', dest_path, '-A', ua, url], timeout=25)
            if res.returncode == 0 and os.path.exists(dest_path) and os.path.getsize(dest_path) > 1000:
                ieee_vendors_cache = {}  # Clear cache to trigger reload
                logger.info(f"Successfully downloaded and saved {os.path.basename(dest_path)} via curl.")
                return True
            else:
                logger.error(f"curl download failed or file is too small for {os.path.basename(dest_path)}. Return code: {res.returncode}")
        except Exception as ex:
            logger.error(f"Failed to download {os.path.basename(dest_path)} using curl: {ex}")
    return False


def download_oui_files():
    if not os.path.exists(OUI36_TXT_PATH):
        download_single_oui_file("https://standards-oui.ieee.org/oui36/oui36.txt", OUI36_TXT_PATH)
    if not os.path.exists(OUI_TXT_PATH):
        download_single_oui_file("https://standards-oui.ieee.org/oui/oui.txt", OUI_TXT_PATH)


def load_ieee_oui():
    oui_db = {}
    
    # 1. Parse oui36.txt if exists
    if os.path.exists(OUI36_TXT_PATH):
        try:
            with open(OUI36_TXT_PATH, "r", encoding="utf-8", errors="ignore") as f:
                current_oui = None
                for line in f:
                    if "(hex)" in line:
                        parts = line.split("(hex)")
                        prefix = parts[0].strip().replace("-", "").replace(":", "").replace(" ", "").upper()
                        current_oui = prefix
                    elif "(base 16)" in line and current_oui:
                        parts = line.split("(base 16)")
                        range_str = parts[0].strip()
                        name = parts[1].strip()
                        if "-" in range_str:
                            range_prefix = range_str.split("-")[0].strip()[:3]
                            full_prefix = current_oui + range_prefix
                            oui_db[full_prefix] = name
                        else:
                            oui_db[current_oui] = name
        except Exception as e:
            logger.error(f"Error parsing IEEE OUI36 file: {e}")

    # 2. Parse oui.txt if exists
    if os.path.exists(OUI_TXT_PATH):
        try:
            with open(OUI_TXT_PATH, "r", encoding="utf-8", errors="ignore") as f:
                current_oui = None
                for line in f:
                    if "(hex)" in line:
                        parts = line.split("(hex)")
                        prefix = parts[0].strip().replace("-", "").replace(":", "").replace(" ", "").upper()
                        current_oui = prefix
                    elif "(base 16)" in line and current_oui:
                        parts = line.split("(base 16)")
                        range_str = parts[0].strip()
                        name = parts[1].strip()
                        if "-" in range_str:
                            range_prefix = range_str.split("-")[0].strip()[:3]
                            full_prefix = current_oui + range_prefix
                            oui_db[full_prefix] = name
                        else:
                            oui_db[current_oui] = name
        except Exception as e:
            logger.error(f"Error parsing IEEE OUI file: {e}")
            
    return oui_db


def get_ieee_vendors():
    global ieee_vendors_cache
    if not ieee_vendors_cache:
        ieee_vendors_cache = load_ieee_oui()
    return ieee_vendors_cache


def load_mac_vendors():
    vendors = {}
    if os.path.exists(VENDORS_TXT_PATH):
        try:
            with open(VENDORS_TXT_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split(None, 1)
                    if len(parts) == 2:
                        prefix, name = parts
                        clean_prefix = prefix.replace(":", "").replace("-", "").replace(" ", "").upper()
                        if len(clean_prefix) in [6, 9]:
                            vendors[clean_prefix] = name.strip()
        except Exception as e:
            logger.error(f"Error loading custom MAC vendors: {e}")
    return vendors


def lookup_vendor(mac_str, custom_vendors, ieee_vendors):
    if not mac_str:
        return ""
    clean_mac = mac_str.replace(":", "").replace("-", "").replace(" ", "").upper()
    prefix_9 = clean_mac[:9]
    
    # 1. Match downloaded IEEE OUI-36 (9-hex prefix)
    if len(prefix_9) == 9 and prefix_9 in ieee_vendors:
        return ieee_vendors[prefix_9]
        
    # 2. Match downloaded IEEE OUI-24 (6-hex prefix)
    prefix_6 = clean_mac[:6]
    if len(prefix_6) == 6 and prefix_6 in ieee_vendors:
        return ieee_vendors[prefix_6]
        
    # 3. Match custom OUI (9-hex prefix or 6-hex prefix)
    if len(prefix_9) == 9 and prefix_9 in custom_vendors:
        return custom_vendors[prefix_9]
    if len(prefix_6) == 6 and prefix_6 in custom_vendors:
        return custom_vendors[prefix_6]
        
    return ""


config = {}
switch_configs = []
cached_data = {}
cached_speeds = {}
cache_lock = threading.Lock()
counters_lock = threading.Lock()
history_lock = threading.Lock()
_cache_thread = None
_stop_thread = False

# Three-tier rolling history system
history_live = {}   # {(ip, port): deque of {ts, tx, rx}, maxlen=120}
history_hourly = {} # {(ip, port): list of {ts, tx, rx}, maxlen=3600/refresh_interval}
history_daily = {}  # {(ip, port): list of {ts, tx, rx}, maxlen=96}


def load_config():
    global config, switch_configs
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    switch_configs = config.get("switches", [])


def load_notes():
    if os.path.exists(NOTES_PATH):
        with open(NOTES_PATH) as f:
            return json.load(f)
    return {}


def save_notes(n):
    with open(NOTES_PATH, "w") as f:
        json.dump(n, f, indent=2)


def load_counters():
    if os.path.exists(COUNTERS_PATH):
        with open(COUNTERS_PATH) as f:
            return json.load(f)
    return {}


def save_counters(c):
    with open(COUNTERS_PATH, "w") as f:
        json.dump(c, f, indent=2)


def load_history():
    global history_hourly, history_daily
    with history_lock:
        history_hourly = {}
        history_daily = {}
        
        if os.path.exists(HOURLY_PATH):
            try:
                with open(HOURLY_PATH) as f:
                    raw = json.load(f)
                    for k, v in raw.items():
                        parts = k.split(":")
                        if len(parts) == 2:
                            history_hourly[(parts[0], parts[1])] = v
                logger.info("Loaded hourly history successfully.")
            except Exception as e:
                logger.error(f"Error loading hourly history: {e}")
                
        if os.path.exists(DAILY_PATH):
            try:
                with open(DAILY_PATH) as f:
                    raw = json.load(f)
                    for k, v in raw.items():
                        parts = k.split(":")
                        if len(parts) == 2:
                            history_daily[(parts[0], parts[1])] = v
                logger.info("Loaded daily history successfully.")
            except Exception as e:
                logger.error(f"Error loading daily history: {e}")


def save_history():
    with history_lock:
        try:
            raw_hourly = {f"{k[0]}:{k[1]}": list(v) for k, v in history_hourly.items()}
            with open(HOURLY_PATH, "w") as f:
                json.dump(raw_hourly, f, indent=2)
                
            raw_daily = {f"{k[0]}:{k[1]}": list(v) for k, v in history_daily.items()}
            with open(DAILY_PATH, "w") as f:
                json.dump(raw_daily, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving history: {e}")


def get_avg_speed(ip, port, duration):
    key = (ip, port)
    hl = list(history_live.get(key, []))
    if len(hl) < 2:
        return 0, 0
    
    target_ts = time.time() - duration
    best_idx = 0
    min_diff = abs(hl[0]["ts"] - target_ts)
    for idx, s in enumerate(hl):
        diff = abs(s["ts"] - target_ts)
        if diff < min_diff:
            min_diff = diff
            best_idx = idx
            
    s_start = hl[best_idx]
    s_end = hl[-1]
    dt = s_end["ts"] - s_start["ts"]
    if dt <= 0:
        return 0, 0
        
    tx_diff = s_end["tx"] - s_start["tx"]
    rx_diff = s_end["rx"] - s_start["rx"]
    
    if tx_diff < 0: tx_diff = s_end["tx"]
    if rx_diff < 0: rx_diff = s_end["rx"]
    
    tx_speed = tx_diff * 8 / dt
    rx_speed = rx_diff * 8 / dt
    return int(tx_speed), int(rx_speed)


def update_cache():
    global cached_data, cached_speeds, _stop_thread, history_live, history_hourly, history_daily
    from scraper import scrape_switch

    mac_tables = {}
    last_mac_scrape_times = {}

    while True:
        if _stop_thread:
            break

        counters = load_counters()
        now = time.time()
        results = {}
        speeds = {}
        should_save = False

        mac_refresh_multiplier = config.get("mac_refresh_multiplier", 5)

        for sw in switch_configs:
            ip = sw["ip"]
            
            from scraper import HCSwitchScraper
            scraper_obj = HCSwitchScraper(sw)
            
            # Scrape MAC table if due
            last_scrape = last_mac_scrape_times.get(ip, 0)
            r_interval = config.get("refresh_interval", 30)
            if now - last_scrape >= (r_interval * mac_refresh_multiplier) or ip not in mac_tables:
                logger.info(f"Scheduled scraping of MAC table for {ip}...")
                try:
                    mac_table = scraper_obj.scrape_mac_table()
                    mac_tables[ip] = mac_table
                    last_mac_scrape_times[ip] = now
                except Exception as e:
                    logger.error(f"Error scraping MAC table inside thread for {ip}: {e}")
                    if ip not in mac_tables:
                        mac_tables[ip] = []

            try:
                data = scraper_obj.scrape()
            except Exception as e:
                data = {"name": sw["name"], "ip": ip, "ports": [], "error": str(e)}

            data["mac_table"] = mac_tables.get(ip, [])
            data["mac_timestamp"] = last_mac_scrape_times.get(ip, 0)

            # Accumulate cumulative counters
            for p in data.get("ports", []):
                port = p["port"]
                key = f"{ip}:{port}"
                last = counters.get(key, {"tx": 0, "rx": 0, "cum_tx": 0, "cum_rx": 0})
                cur_tx = p.get("tx_bytes", 0)
                cur_rx = p.get("rx_bytes", 0)

                if cur_tx >= last["tx"]:
                    delta_tx = cur_tx - last["tx"]
                else:
                    delta_tx = cur_tx
                if cur_rx >= last["rx"]:
                    delta_rx = cur_rx - last["rx"]
                else:
                    delta_rx = cur_rx

                cum_tx = last["cum_tx"] + delta_tx
                cum_rx = last["cum_rx"] + delta_rx

                counters[key] = {"tx": cur_tx, "rx": cur_rx,
                                 "cum_tx": cum_tx, "cum_rx": cum_rx}
                p["cum_tx"] = cum_tx
                p["cum_rx"] = cum_rx

                # Speed history (live)
                hist_key = (ip, port)
                if hist_key not in history_live:
                    history_live[hist_key] = deque(maxlen=120)
                history_live[hist_key].append({
                    "ts": now, "tx": cum_tx, "rx": cum_rx
                })

                # Calculate speed from last 2 samples
                h = history_live[hist_key]
                if len(h) >= 2:
                    p1 = h[-2]
                    p2 = h[-1]
                    dt = p2["ts"] - p1["ts"]
                    if dt > 0:
                        speed_tx = (p2["tx"] - p1["tx"]) * 8 / dt
                        speed_rx = (p2["rx"] - p1["rx"]) * 8 / dt
                        if speed_tx < 0:
                            speed_tx = p2["tx"] * 8 / dt
                        if speed_rx < 0:
                            speed_rx = p2["rx"] * 8 / dt
                        p["speed_tx_bps"] = int(speed_tx)
                        p["speed_rx_bps"] = int(speed_rx)

                # Hourly history (high-res: recorded at each refresh cycle)
                with history_lock:
                    if hist_key not in history_hourly:
                        history_hourly[hist_key] = []
                    points_h = history_hourly[hist_key]
                    
                    r_interval = config.get("refresh_interval", 30)
                    max_points = max(1, int(3600 / r_interval))
                    
                    speed_tx = p.get("speed_tx_bps", 0)
                    speed_rx = p.get("speed_rx_bps", 0)
                    
                    points_h.append({"ts": now, "tx": speed_tx, "rx": speed_rx})
                    while len(points_h) > max_points:
                        points_h.pop(0)
                    should_save = True

                    # Daily history (15-minute averages)
                    if hist_key not in history_daily:
                        history_daily[hist_key] = []
                    points_d = history_daily[hist_key]
                    if len(points_d) == 0 or now - points_d[-1]["ts"] >= 900:
                        avg_tx, avg_rx = get_avg_speed(ip, port, 900)
                        points_d.append({"ts": now, "tx": avg_tx, "rx": avg_rx})
                        if len(points_d) > 96:
                            points_d.pop(0)
                        should_save = True

            results[ip] = data

            # Per-switch speed overview (TX max per port)
            sw_speeds = {}
            for p in data.get("ports", []):
                sw_speeds[p["port"]] = {
                    "speed_tx": p.get("speed_tx_bps", 0),
                    "speed_rx": p.get("speed_rx_bps", 0),
                }
            speeds[ip] = sw_speeds

        save_counters(counters)
        if should_save:
            save_history()

        with cache_lock:
            cached_data = results
            cached_speeds = speeds

        time.sleep(config.get("refresh_interval", 30))


def start_cache_thread():
    global _cache_thread, _stop_thread
    _stop_thread = False
    _cache_thread = threading.Thread(target=update_cache, daemon=True)
    _cache_thread.start()


load_config()
load_history()
start_cache_thread()

if not os.path.exists(OUI_TXT_PATH) or not os.path.exists(OUI36_TXT_PATH):
    threading.Thread(target=download_oui_files, daemon=True).start()


def _format_bps(bps):
    if bps >= 1_000_000_000:
        return f"{bps/1_000_000_000:.1f} Gbps"
    if bps >= 1_000_000:
        return f"{bps/1_000_000:.1f} Mbps"
    if bps >= 1_000:
        return f"{bps/1_000:.1f} Kbps"
    return f"{bps} bps"


@app.route("/")
def dashboard():
    return render_template("index.html",
                           title=config.get("title", "Switch Dashboard"),
                           refresh=config.get("refresh_interval", 30),
                           enabled_columns=config.get("enabled_columns", ['port', 'status', 'speed', 'packets', 'bytes', 'raw_bytes', 'info', 'notes']),
                           grid_columns=config.get("grid_columns", "auto"),
                           version=VERSION)



@app.route("/api/switches")
def api_switches():
    notes = load_notes()
    vendors = load_mac_vendors()
    ieee_vendors = get_ieee_vendors()
    with cache_lock:
        data = list(cached_data.values())
    for sw in data:
        for entry in sw.get("mac_table", []):
            entry["vendor"] = lookup_vendor(entry.get("mac"), vendors, ieee_vendors)
        for p in sw.get("ports", []):
            p["note"] = notes.get(f"{sw['ip']}:{p['port']}", "")
    return jsonify(data)


@app.route("/api/switches/<ip>/refresh_mac", methods=["POST"])
def refresh_mac(ip):
    sw = None
    for s in switch_configs:
        if s["ip"] == ip:
            sw = s
            break
    if not sw:
        return jsonify({"error": "Switch not found"}), 404
        
    try:
        from scraper import HCSwitchScraper
        scraper_obj = HCSwitchScraper(sw)
        mac_table = scraper_obj.scrape_mac_table()
        
        vendors = load_mac_vendors()
        ieee_vendors = get_ieee_vendors()
        for entry in mac_table:
            entry["vendor"] = lookup_vendor(entry.get("mac"), vendors, ieee_vendors)
            
        with cache_lock:
            if ip in cached_data:
                cached_data[ip]["mac_table"] = mac_table
                cached_data[ip]["mac_timestamp"] = time.time()
                
        return jsonify({"status": "ok", "count": len(mac_table), "mac_table": mac_table})
    except Exception as e:
        logger.error(f"Manual MAC scrape failed for {ip}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/switches/<ip>/transceiver")
def get_transceiver(ip):
    sw = None
    for s in switch_configs:
        if s["ip"] == ip:
            sw = s
            break
    if not sw:
        return jsonify({"error": "Switch not found"}), 404
        
    try:
        from scraper import HCSwitchScraper
        scraper_obj = HCSwitchScraper(sw)
        transceiver_info = scraper_obj.scrape_transceiver()
        if not transceiver_info:
            return jsonify({"error": "No transceiver data available or SFP module not inserted"}), 404
        return jsonify(transceiver_info)
    except Exception as e:
        logger.error(f"Transceiver scrape failed for {ip}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/switches/<ip>/image")
def get_switch_image(ip):
    sw = None
    for s in switch_configs:
        if s["ip"] == ip:
            sw = s
            break
    if sw:
        model = sw.get("model", "")
        if model:
            # Look for model-specific image files (png, jpg, jpeg) in device-templates
            for ext in ["png", "jpg", "jpeg"]:
                img_name = f"{model}.{ext}"
                img_path = os.path.join(DEVICE_TEMPLATES_DIR, img_name)
                if os.path.exists(img_path):
                    return send_from_directory(DEVICE_TEMPLATES_DIR, img_name)
    # Default fallback switch icon
    return send_from_directory(os.path.join(BASE, "static"), "switch_icon.png")


@app.route("/api/speeds")
def api_speeds():
    with cache_lock:
        return jsonify(cached_speeds)


@app.route("/api/history")
def api_history():
    ip = request.args.get("ip")
    port = request.args.get("port")
    range_type = request.args.get("range", "live")  # live, 1h, 24h
    
    if not ip or not port:
        return jsonify({"error": "Missing ip or port"}), 400
        
    key = (ip, port)
    tx = []
    rx = []
    timestamps = []
    
    with history_lock:
        if range_type == "live":
            hl = list(history_live.get(key, []))
            for i in range(1, len(hl)):
                dt = hl[i]["ts"] - hl[i-1]["ts"]
                if dt > 0:
                    tx_diff = hl[i]["tx"] - hl[i-1]["tx"]
                    rx_diff = hl[i]["rx"] - hl[i-1]["rx"]
                    if tx_diff < 0: tx_diff = hl[i]["tx"]
                    if rx_diff < 0: rx_diff = hl[i]["rx"]
                    tx.append(int(tx_diff * 8 / dt))
                    rx.append(int(rx_diff * 8 / dt))
                    timestamps.append(hl[i]["ts"])
        elif range_type == "1h":
            points = history_hourly.get(key, [])
            for p in points:
                tx.append(p["tx"])
                rx.append(p["rx"])
                timestamps.append(p["ts"])
        elif range_type == "24h":
            points = history_daily.get(key, [])
            for p in points:
                tx.append(p["tx"])
                rx.append(p["rx"])
                timestamps.append(p["ts"])
                
    return jsonify({
        "tx": tx,
        "rx": rx,
        "timestamps": timestamps
    })


@app.route("/api/reset", methods=["POST"])
def api_reset():
    with counters_lock:
        if os.path.exists(COUNTERS_PATH):
            os.remove(COUNTERS_PATH)
    with history_lock:
        if os.path.exists(HOURLY_PATH):
            os.remove(HOURLY_PATH)
        if os.path.exists(DAILY_PATH):
            os.remove(DAILY_PATH)
        global history_live, history_hourly, history_daily
        history_live = {}
        history_hourly = {}
        history_daily = {}
    return jsonify({"status": "ok"})


NOTES_LOCK = threading.Lock()


@app.route("/api/notes", methods=["POST"])
def api_notes():
    data = request.get_json(force=True, silent=True) or {}
    key = data.get("key")
    note = data.get("note", "")
    if not key:
        return jsonify({"error": "missing key"}), 400
    with NOTES_LOCK:
        notes = load_notes()
        if note.strip():
            notes[key] = note.strip()
        else:
            notes.pop(key, None)
        save_notes(notes)
    return jsonify({"status": "ok"})


@app.route("/api/templates")
def list_templates():
    try:
        os.makedirs(DEVICE_TEMPLATES_DIR, exist_ok=True)
        files = [f for f in os.listdir(DEVICE_TEMPLATES_DIR) if f.endswith(".yaml")]
        return jsonify({"templates": files})
    except Exception as e:
        logger.error(f"Failed to list YAML templates: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/templates/<filename>", methods=["GET", "POST"])
def manage_template(filename):
    if ".." in filename or "/" in filename or "\\" in filename or not filename.endswith(".yaml"):
        return jsonify({"error": "Invalid filename"}), 400

    filepath = os.path.join(DEVICE_TEMPLATES_DIR, filename)

    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        content = data.get("content", "")
        if not content.strip():
            return jsonify({"error": "Content cannot be empty"}), 400
        try:
            import yaml
            yaml.safe_load(content)
        except Exception as ye:
            return jsonify({"error": f"Invalid YAML Syntax: {ye}"}), 400

        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info(f"Updated YAML template: {filename}")
            return jsonify({"status": "ok"})
        except Exception as e:
            logger.error(f"Failed to save template {filename}: {e}")
            return jsonify({"error": str(e)}), 500

    # GET method
    if not os.path.exists(filepath):
        return jsonify({"error": "Template not found"}), 404
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        return jsonify({"content": content})
    except Exception as e:
        logger.error(f"Failed to read template {filename}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/config", methods=["GET", "POST"])
def config_page():
    if request.method == "POST":
        new_switches = []
        names = request.form.getlist("name[]")
        ips = request.form.getlist("ip[]")
        usernames = request.form.getlist("username[]")
        passwords = request.form.getlist("password[]")
        models = request.form.getlist("model[]")
        port_counts = request.form.getlist("port_count[]")
        keep = request.form.getlist("keep[]")

        for i in range(len(ips)):
            if i >= len(keep) or keep[i] != "1":
                continue
            new_switches.append({
                "name": names[i] if i < len(names) else f"Switch {i+1}",
                "ip": ips[i].strip(),
                "username": usernames[i].strip() if i < len(usernames) else "admin",
                "password": passwords[i].strip() if i < len(passwords) else "admin",
                "model": models[i].strip() if i < len(models) else "",
                "port_count": int(port_counts[i]) if i < len(port_counts) else 9,
            })

        new_title = request.form.get("title", config.get("title", ""))
        new_refresh = int(request.form.get("refresh_interval", 30))
        new_mac_multiplier = int(request.form.get("mac_refresh_multiplier", 5))
        new_columns = request.form.getlist("columns[]")
        if not new_columns:
            new_columns = ['port', 'status', 'speed', 'packets', 'bytes', 'raw_bytes', 'info', 'notes']
        new_grid_columns = request.form.get("grid_columns", "auto")

        new_config = {
            "title": new_title,
            "refresh_interval": new_refresh,
            "mac_refresh_multiplier": new_mac_multiplier,
            "enabled_columns": new_columns,
            "grid_columns": new_grid_columns,
            "switches": new_switches,
        }

        with open(CONFIG_PATH, "w") as f:
            json.dump(new_config, f, indent=2)

        global _stop_thread
        _stop_thread = True
        time.sleep(0.5)
        load_config()
        start_cache_thread()

        return redirect(url_for("dashboard"))

    return render_template("config.html", title=config.get("title", "Switch Dashboard"),
                           switches=switch_configs,
                           refresh=config.get("refresh_interval", 30),
                           mac_multiplier=config.get("mac_refresh_multiplier", 5),
                           enabled_columns=config.get("enabled_columns", ['port', 'status', 'speed', 'packets', 'bytes', 'raw_bytes', 'info', 'notes']),
                           grid_columns=config.get("grid_columns", "auto"),
                           version=VERSION)


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        existing = {}
        if os.path.exists(SETTINGS_PATH):
            try:
                with open(SETTINGS_PATH) as f:
                    existing = json.load(f)
            except Exception:
                pass
        existing.update(data)
        with open(SETTINGS_PATH, "w") as f:
            json.dump(existing, f, indent=2)
        return jsonify({"status": "ok"})
    if os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH) as f:
            return jsonify(json.load(f))
    return jsonify({"font_size": "md"})


@app.route("/api/vendors", methods=["GET", "POST"])
def api_vendors():
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        content = data.get("content", "")
        try:
            with open(VENDORS_TXT_PATH, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info("Custom MAC vendors file updated successfully.")
            return jsonify({"status": "ok"})
        except Exception as e:
            logger.error(f"Failed to save custom MAC vendors file: {e}")
            return jsonify({"error": str(e)}), 500

    # GET method
    content = ""
    if os.path.exists(VENDORS_TXT_PATH):
        try:
            with open(VENDORS_TXT_PATH, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            logger.error(f"Failed to read custom MAC vendors file: {e}")
            return jsonify({"error": str(e)}), 500
    else:
        content = (
            "# Custom MAC Vendor mappings (one per line)\n"
            "# Format: AA:BB:CC Vendor Name\n"
            "# Example:\n"
            "AA:BB:CC Custom Local Device\n"
            "00:11:22 Custom Router\n"
        )
        try:
            with open(VENDORS_TXT_PATH, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            logger.error(f"Failed to write default custom MAC vendors file: {e}")
    return jsonify({"content": content})


@app.route("/api/vendors/update_oui", methods=["POST"])
def update_oui_api():
    try:
        success_36 = download_single_oui_file("https://standards-oui.ieee.org/oui36/oui36.txt", OUI36_TXT_PATH)
        success_24 = download_single_oui_file("https://standards-oui.ieee.org/oui/oui.txt", OUI_TXT_PATH)
        if success_36 and success_24:
            return jsonify({"status": "ok", "message": "IEEE OUI databases successfully updated."})
        else:
            return jsonify({"status": "error", "message": "Failed to download one or both OUI files. Check server logs."}), 500
    except Exception as e:
        logger.error(f"Manual OUI update failed: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api-docs")
def api_docs_page():
    return render_template("api_docs.html", title=config.get("title", "Switch Dashboard"),
                           version=VERSION)


@app.route("/api/switches/<ip>/backup", methods=["POST"])
def backup_switch_config(ip):
    sw = None
    for s in switch_configs:
        if s["ip"] == ip:
            sw = s
            break
    if not sw:
        return jsonify({"error": "Switch not found"}), 404

    try:
        from scraper import HCSwitchScraper
        scraper_obj = HCSwitchScraper(sw)
        binary_data = scraper_obj.download_backup()
        if not binary_data:
            return jsonify({"error": "No backup data retrieved from switch"}), 500

        backup_dir = BACKUP_DIR
        os.makedirs(backup_dir, exist_ok=True)

        ip_dashed = ip.replace(".", "-")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M")
        filename = f"switch_cfg_{ip_dashed}_{timestamp}.bin"
        filepath = os.path.join(backup_dir, filename)

        with open(filepath, "wb") as f:
            f.write(binary_data)

        logger.info(f"Successfully saved backup: {filename}")
        return jsonify({
            "status": "ok",
            "filename": filename,
            "size": len(binary_data),
            "timestamp": timestamp
        })
    except Exception as e:
        logger.error(f"Backup failed for {ip}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/switches/<ip>/reboot", methods=["POST"])
def reboot_switch_api(ip):
    sw = None
    for s in switch_configs:
        if s["ip"] == ip:
            sw = s
            break
    if not sw:
        return jsonify({"error": "Switch not found"}), 404

    try:
        from scraper import HCSwitchScraper
        scraper_obj = HCSwitchScraper(sw)
        scraper_obj.reboot_switch()
        return jsonify({"status": "ok", "message": "Reboot command sent successfully"})
    except Exception as e:
        logger.error(f"Reboot failed for {ip}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/backups")
def get_backups():
    backup_dir = BACKUP_DIR
    if not os.path.exists(backup_dir):
        return jsonify([])

    backups = []
    try:
        for filename in os.listdir(backup_dir):
            if filename.startswith("switch_cfg_") and filename.endswith(".bin"):
                filepath = os.path.join(backup_dir, filename)
                if os.path.isfile(filepath):
                    stat = os.stat(filepath)
                    size = stat.st_size
                    if size >= 1048576:
                        size_str = f"{size / 1048576:.1f} MB"
                    elif size >= 1024:
                        size_str = f"{size / 1024:.1f} KB"
                    else:
                        size_str = f"{size} B"

                    parts = filename[:-4].split("_")
                    ip = ""
                    dt_str = ""
                    if len(parts) >= 5:
                        ip = parts[2].replace("-", ".")
                        date_part = parts[3]
                        time_part = parts[4]
                        if len(date_part) == 8 and len(time_part) == 4:
                            dt_str = f"{date_part[0:4]}-{date_part[4:6]}-{date_part[6:8]} {time_part[0:2]}:{time_part[2:4]}"
                    else:
                        ip = "Unknown"
                        dt_str = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")

                    backups.append({
                        "filename": filename,
                        "ip": ip,
                        "size": size,
                        "size_str": size_str,
                        "datetime": dt_str,
                        "mtime": stat.st_mtime
                    })
        backups.sort(key=lambda x: x["mtime"], reverse=True)
    except Exception as e:
        logger.error(f"Failed to list backups: {e}")
        return jsonify({"error": str(e)}), 500

    return jsonify(backups)


@app.route("/api/backups/<filename>/download")
def download_backup_file(filename):
    if ".." in filename or "/" in filename or "\\" in filename:
        return jsonify({"error": "Invalid filename"}), 400

    backup_dir = BACKUP_DIR
    filepath = os.path.join(backup_dir, filename)
    if not os.path.exists(filepath) or not os.path.isfile(filepath):
        return jsonify({"error": "File not found"}), 404

    return send_from_directory(backup_dir, filename, as_attachment=True)


@app.route("/api/backups/<filename>", methods=["DELETE"])
def delete_backup_file(filename):
    if ".." in filename or "/" in filename or "\\" in filename:
        return jsonify({"error": "Invalid filename"}), 400

    backup_dir = BACKUP_DIR
    filepath = os.path.join(backup_dir, filename)
    if not os.path.exists(filepath) or not os.path.isfile(filepath):
        return jsonify({"error": "File not found"}), 404

    try:
        os.remove(filepath)
        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error(f"Failed to delete backup file {filename}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/logs")
def logs_page():
    log_level = "INFO"
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH) as f:
                settings = json.load(f)
                log_level = settings.get("log_level", "INFO")
        except Exception:
            pass
    from flask import make_response
    rendered = render_template("logs.html", 
                               title=config.get("title", "Switch Dashboard"),
                               version=VERSION,
                               current_log_level=log_level)
    response = make_response(rendered)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/api/logs")
def api_logs():
    lines_limit = 150
    if not os.path.exists(LOG_FILE_PATH):
        return jsonify([])
    try:
        with open(LOG_FILE_PATH, "r", encoding="utf-8", errors="replace") as f:
            log_lines = deque(f, maxlen=lines_limit)
        response = jsonify([line.rstrip() for line in log_lines])
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    except Exception as e:
        logger.error(f"Failed to read log file: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/logs/level", methods=["POST"])
def api_logs_level():
    data = request.get_json(force=True, silent=True) or {}
    level_name = data.get("level", "INFO").upper()
    valid_levels = ["DEBUG", "INFO", "WARN", "WARNING", "ERROR", "CRITICAL", "NONE"]
    if level_name not in valid_levels:
        return jsonify({"error": "Invalid log level"}), 400

    try:
        existing = {}
        if os.path.exists(SETTINGS_PATH):
            try:
                with open(SETTINGS_PATH) as f:
                    existing = json.load(f)
            except Exception:
                pass
        existing["log_level"] = level_name
        with open(SETTINGS_PATH, "w") as f:
            json.dump(existing, f, indent=2)

        setup_logging(level_name)
        logger.warning(f"Log level dynamically changed to {level_name} by user request.")
        return jsonify({"status": "ok", "level": level_name})
    except Exception as e:
        logger.error(f"Failed to update log level: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/logs/clear", methods=["POST"])
def api_logs_clear():
    try:
        with open(LOG_FILE_PATH, "w", encoding="utf-8") as f:
            f.truncate(0)
        logger.warning("Log history was cleared by the administrator.")
        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error(f"Failed to clear log file: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/logs/download")
def api_logs_download():
    if not os.path.exists(LOG_FILE_PATH):
        try:
            with open(LOG_FILE_PATH, "w", encoding="utf-8") as f:
                pass
        except Exception:
            return jsonify({"error": "Log file not found"}), 404
            
    dir_name = os.path.dirname(LOG_FILE_PATH)
    file_name = os.path.basename(LOG_FILE_PATH)
    return send_from_directory(dir_name, file_name, as_attachment=True)


@app.route("/backups")
def backups_page():
    return render_template("backups.html", title=config.get("title", "Switch Dashboard"),
                           version=VERSION)


@app.route("/static/<path:path>")
def static_files(path):
    from flask import send_from_directory
    return send_from_directory("static", path)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
