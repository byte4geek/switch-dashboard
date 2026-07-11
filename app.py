import json
import time
import threading
import os
import logging
from logging.handlers import RotatingFileHandler
from collections import deque
from flask import Flask, render_template, jsonify, request, redirect, url_for, Response, send_from_directory
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(__file__)
DATA_DIR = os.environ.get("DASHBOARD_DATA_DIR", BASE)
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
DEVICE_TYPES_YAML_PATH = os.path.join(DATA_DIR, "device_types.yaml")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
NOTES_PATH = os.path.join(DATA_DIR, "notes.json")
LOG_FILE_PATH = os.path.join(DATA_DIR, "logs", "dashboard.log")

import scanner_db

def setup_logging(level_name=None):
    if not level_name:
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH) as f:
                    cfg = json.load(f)
                    level_name = cfg.get("settings", {}).get("log_level", "INFO")
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

    # Copy device_types.yaml if not present in DATA_DIR
    dest_device_types = os.path.join(DATA_DIR, "device_types.yaml")
    if not os.path.exists(dest_device_types):
        src_device_types = os.path.join(BASE, "device_types.yaml")
        if os.path.exists(src_device_types):
            import shutil
            try:
                shutil.copy2(src_device_types, dest_device_types)
                logger.info(f"Initialized default device_types.yaml in {dest_device_types}")
            except Exception as e:
                logger.error(f"Failed to initialize default device_types.yaml: {e}")

COUNTERS_PATH = os.path.join(DATA_DIR, "counters.json")
HOURLY_PATH = os.path.join(DATA_DIR, "history_hourly.json")
DAILY_PATH = os.path.join(DATA_DIR, "history_daily.json")
BACKUP_DIR = os.path.join(DATA_DIR, "backup")
DEVICE_TEMPLATES_DIR = os.path.join(DATA_DIR, "device-templates")
os.makedirs(DEVICE_TEMPLATES_DIR, exist_ok=True)
VENDORS_TXT_PATH = os.path.join(DATA_DIR, "mac_vendors.txt")
OUI36_TXT_PATH = os.path.join(DATA_DIR, "oui36.txt")
OUI_TXT_PATH = os.path.join(DATA_DIR, "oui.txt")
VERSION = "2026.7.1"



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
config_lock = threading.RLock()
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
    with config_lock:
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH) as f:
                    config = json.load(f)
            except Exception as e:
                logger.error(f"Error loading config: {e}")
        switch_configs = config.get("switches", [])


def save_config():
    with config_lock:
        try:
            with open(CONFIG_PATH, "w") as f:
                json.dump(config, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving config: {e}")


def is_ignored_mac(mac):
    if not mac:
        return False
    mac_clean = mac.replace(":", "").replace("-", "").replace(" ", "").upper()
    settings = config.get("settings", {})
    ignored_patterns = settings.get("ignored_macs", [])
    if isinstance(ignored_patterns, str):
        ignored_patterns = [p.strip() for p in ignored_patterns.split(",") if p.strip()]
    for pattern in ignored_patterns:
        pattern_clean = pattern.strip().upper()
        if not pattern_clean:
            continue
        if pattern_clean.endswith("*"):
            prefix = pattern_clean[:-1].replace(":", "").replace("-", "")
            if mac_clean.startswith(prefix):
                return True
        else:
            full_pattern = pattern_clean.replace(":", "").replace("-", "")
            if mac_clean == full_pattern:
                return True
    return False



def load_notes():
    load_config()
    return config.get("notes", {})


def save_notes(n):
    load_config()
    config["notes"] = n
    save_config()


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
                            cleaned_v = []
                            for pt in v:
                                tx_val = pt.get("tx", 0)
                                rx_val = pt.get("rx", 0)
                                if tx_val > 12_500_000_000:
                                    tx_val = 0
                                if rx_val > 12_500_000_000:
                                    rx_val = 0
                                pt["tx"] = tx_val
                                pt["rx"] = rx_val
                                cleaned_v.append(pt)
                            history_hourly[(parts[0], parts[1])] = cleaned_v
                logger.info("Loaded hourly history successfully (scrubbed spikes).")
            except Exception as e:
                logger.error(f"Error loading hourly history: {e}")
                
        if os.path.exists(DAILY_PATH):
            try:
                with open(DAILY_PATH) as f:
                    raw = json.load(f)
                    for k, v in raw.items():
                        parts = k.split(":")
                        if len(parts) == 2:
                            cleaned_v = []
                            for pt in v:
                                tx_val = pt.get("tx", 0)
                                rx_val = pt.get("rx", 0)
                                if tx_val > 12_500_000_000:
                                    tx_val = 0
                                if rx_val > 12_500_000_000:
                                    rx_val = 0
                                pt["tx"] = tx_val
                                pt["rx"] = rx_val
                                cleaned_v.append(pt)
                            history_daily[(parts[0], parts[1])] = cleaned_v
                logger.info("Loaded daily history successfully (scrubbed spikes).")
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


def unify_speed(speed_val):
    if not speed_val:
        return ""
    s = str(speed_val).strip()
    s_lower = s.lower()
    if s_lower in ["auto", "disabled", "disable", "down", "unknown", ""]:
        return s
    import re
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([mMgGtT]?)[bB]?[pP]?[sS]?$", s)
    if m:
        val_str, unit = m.groups()
        val = float(val_str)
        unit = unit.upper()
        if unit == "M" or unit == "":
            if val >= 1000:
                val_g = val / 1000.0
                if val_g.is_integer():
                    return f"{int(val_g)}G"
                else:
                    return f"{val_g:.1f}".rstrip("0").rstrip(".") + "G"
            else:
                if val.is_integer():
                    return f"{int(val)}M"
                else:
                    return f"{val:.1f}".rstrip("0").rstrip(".") + "M"
        elif unit == "G":
            if val.is_integer():
                return f"{int(val)}G"
            else:
                return f"{val:.1f}".rstrip("0").rstrip(".") + "G"
    return s


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
        all_vm_mac_maps = {}
        should_save = False


        mac_refresh_multiplier = config.get("mac_refresh_multiplier", 5)

        # Purge disabled or deleted switches from cache
        enabled_ips = {sw["ip"] for sw in switch_configs if sw.get("enabled", True)}
        with cache_lock:
            for ip in list(cached_data.keys()):
                if ip not in enabled_ips:
                    del cached_data[ip]

        for sw in switch_configs:
            if not sw.get("enabled", True):
                continue
            ip = sw["ip"]
            
            model_lower = sw.get("model", "").lower()
            if model_lower == "internet":
                results[ip] = {
                    "name": sw["name"],
                    "ip": ip,
                    "ports": [],
                    "status": "online",
                    "mac_table": [],
                    "mac_timestamp": now,
                    "mac": "",
                    "model": "internet",
                    "timestamp": now
                }
                speeds[ip] = {}
                continue

            from scraper import HCSwitchScraper, OVSScraper, FritzBoxScraper
            if model_lower in ["openvswitch", "ovs"]:
                scraper_obj = OVSScraper(sw)
            elif model_lower == "fritzbox":
                scraper_obj = FritzBoxScraper(sw)
            else:
                scraper_obj = HCSwitchScraper(sw)
            scraper_obj.max_retries = config.get("max_request_retries", 5)
            
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
                if data and "ports" in data:
                    for p in data["ports"]:
                        if "speed" in p:
                            p["speed"] = unify_speed(p["speed"])
            except Exception as e:
                data = {"name": sw["name"], "ip": ip, "ports": [], "error": str(e)}

            data["mac_table"] = mac_tables.get(ip, [])
            data["mac_timestamp"] = last_mac_scrape_times.get(ip, 0)
            
            # Collect OVS vm_mac_map if available
            vm_mac_map = data.get("vm_mac_map", {})
            for m_mac, m_name in vm_mac_map.items():
                all_vm_mac_maps[m_mac.replace(":", "").upper()] = m_name


            # Accumulate cumulative counters
            for p in data.get("ports", []):
                port = p["port"]
                key = f"{ip}:{port}"
                last = counters.get(key, {"tx": 0, "rx": 0, "cum_tx": 0, "cum_rx": 0, "ts": None})
                cur_tx = p.get("tx_bytes", 0)
                cur_rx = p.get("rx_bytes", 0)

                # Determine baseline or calculation delta
                ts_exists = (last.get("ts") is not None)
                
                if not ts_exists:
                    # First poll of this dashboard instance: establish baseline
                    delta_tx = 0
                    delta_rx = 0
                else:
                    # Calculate delta for TX
                    if cur_tx >= last["tx"]:
                        delta_tx = cur_tx - last["tx"]
                    else:
                        # Counter wrapped or switch rebooted
                        if last["tx"] > 2_000_000_000:
                            # Highly likely 32-bit counter wrap
                            delta_tx = (4_294_967_296 - last["tx"]) + cur_tx
                        else:
                            # Likely switch reboot, counter reset to 0
                            delta_tx = cur_tx
                    
                    # Calculate delta for RX
                    if cur_rx >= last["rx"]:
                        delta_rx = cur_rx - last["rx"]
                    else:
                        # Counter wrapped or switch rebooted
                        if last["rx"] > 2_000_000_000:
                            # Highly likely 32-bit counter wrap
                            delta_rx = (4_294_967_296 - last["rx"]) + cur_rx
                        else:
                            # Likely switch reboot, counter reset to 0
                            delta_rx = cur_rx

                # Sanity check: prevent impossible speed spikes from counter rollovers/glitches
                last_ts = last.get("ts")
                if last_ts is not None:
                    # Parse link speed to determine a realistic maximum bandwidth limit
                    speed_str = p.get("speed", "")
                    link_speed_bps = None
                    if speed_str:
                        s_lower = speed_str.lower()
                        if "10g" in s_lower:
                            link_speed_bps = 10_000_000_000
                        elif "2.5g" in s_lower or "2500" in s_lower:
                            link_speed_bps = 2_500_000_000
                        elif "2g" in s_lower or "2000" in s_lower:
                            link_speed_bps = 2_000_000_000
                        elif "1g" in s_lower or "1000" in s_lower:
                            link_speed_bps = 1_000_000_000
                        elif "100m" in s_lower or "100" in s_lower:
                            link_speed_bps = 100_000_000
                        elif "10m" in s_lower or "10" in s_lower:
                            link_speed_bps = 10_000_000

                    if link_speed_bps is None:
                        # Fallback: 20G for trunk/lag ports, 10G for physical interfaces
                        if "trunk" in str(port).lower() or "lag" in str(port).lower():
                            link_speed_bps = 20_000_000_000
                        else:
                            link_speed_bps = 10_000_000_000

                    dt = now - last_ts
                    if dt <= 0:
                        dt = 15.0

                    # Max bytes physically possible (with 1.5x buffer for bursts/overhead)
                    max_bytes_limit = (link_speed_bps * dt * 1.5) / 8

                    if delta_tx > max_bytes_limit:
                        logger.warning(f"[SanityCheck] Impossible TX byte delta on {key} ({delta_tx} bytes in {dt:.1f}s, limit {max_bytes_limit:.0f} bytes). Discarding delta.")
                        delta_tx = 0
                    if delta_rx > max_bytes_limit:
                        logger.warning(f"[SanityCheck] Impossible RX byte delta on {key} ({delta_rx} bytes in {dt:.1f}s, limit {max_bytes_limit:.0f} bytes). Discarding delta.")
                        delta_rx = 0

                cum_tx = last["cum_tx"] + delta_tx
                cum_rx = last["cum_rx"] + delta_rx

                counters[key] = {"tx": cur_tx, "rx": cur_rx,
                                 "cum_tx": cum_tx, "cum_rx": cum_rx, "ts": now}
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

        # Collect successfully scraped switches and active client MACs
        scraped_switch_ips = set()
        active_clients = {} # mac -> {ip, port, vlan}
        
        # Build maps of Switch MACs and Infrastructure MACs to distinguish clients
        sw_macs = set()
        for sw_conf in switch_configs:
            sw_ip = sw_conf["ip"]
            sw_data = results.get(sw_ip, {})
            sw_mac = sw_data.get("mac", "").replace(":", "").replace("-", "").replace(" ", "").upper()
            if sw_mac:
                sw_macs.add(sw_mac)
        
        infra_macs = set()
        for dev in config.get("infrastructure_devices", []):
            infra_mac = dev.get("mac", "").replace(":", "").replace("-", "").replace(" ", "").upper()
            if infra_mac:
                infra_macs.add(infra_mac)

        for sw_conf in switch_configs:
            sw_ip = sw_conf["ip"]
            sw_data = results.get(sw_ip)
            if sw_data and "error" not in sw_data:
                scraped_switch_ips.add(sw_ip)

        # Map ports to learned MACs for all successfully scraped switches
        sw_port_learned_macs = {}
        for sw_ip in scraped_switch_ips:
            sw_data = results[sw_ip]
            sw_port_learned_macs[sw_ip] = {}
            for entry in sw_data.get("mac_table", []):
                port = str(entry.get("port", ""))
                mac = entry.get("mac", "").replace(":", "").replace("-", "").replace(" ", "").upper()
                if port and mac:
                    if port not in sw_port_learned_macs[sw_ip]:
                        sw_port_learned_macs[sw_ip][port] = []
                    sw_port_learned_macs[sw_ip][port].append((mac, entry.get("vlan", "")))

        # Build a map of switch IPs to their models
        switch_models = {sw["ip"]: sw.get("model", "") for sw in switch_configs}

        # Find active clients
        for sw_ip, ports in sw_port_learned_macs.items():
            for port, mac_vlan_list in ports.items():
                has_switch = any(mac in sw_macs for mac, vlan in mac_vlan_list)
                if not has_switch:
                    for mac, vlan in mac_vlan_list:
                        if mac not in sw_macs and mac not in infra_macs and not is_ignored_mac(mac):
                            formatted_mac = ":".join(mac[i:i+2] for i in range(0, len(mac), 2)).upper()
                            
                            is_current_fritz = (switch_models.get(sw_ip, "").lower() == "fritzbox")
                            if mac in active_clients:
                                prev_ip = active_clients[mac]["ip"]
                                is_prev_fritz = (switch_models.get(prev_ip, "").lower() == "fritzbox")
                                if is_prev_fritz and not is_current_fritz:
                                    active_clients[mac] = {
                                        "mac": formatted_mac,
                                        "ip": sw_ip,
                                        "port": port,
                                        "vlan": str(vlan)
                                    }
                            else:
                                active_clients[mac] = {
                                    "mac": formatted_mac,
                                    "ip": sw_ip,
                                    "port": port,
                                    "vlan": str(vlan)
                                }

        # Update client database in config
        try:
            load_config()
            db_clients = config.get("clients", {})
            
            # 1. Update/Add online clients
            for mac, info in active_clients.items():
                if mac in db_clients:
                    db_clients[mac].update({
                        "ip": info["ip"],
                        "port": info["port"],
                        "vlan": info["vlan"],
                        "status": "online",
                        "last_seen": now
                    })
                    if mac in all_vm_mac_maps and not db_clients[mac].get("host"):
                        db_clients[mac]["host"] = all_vm_mac_maps[mac]
                else:
                    db_clients[mac] = {
                        "mac": info["mac"],
                        "host": all_vm_mac_maps.get(mac, ""),
                        "ip": info["ip"],
                        "port": info["port"],
                        "vlan": info["vlan"],
                        "status": "online",
                        "last_seen": now
                    }

            
            # 2. Mark offline clients
            for mac, client_entry in db_clients.items():
                if mac not in active_clients:
                    last_ip = client_entry.get("ip")
                    if last_ip in scraped_switch_ips:
                        client_entry["status"] = "offline"
            
            config["clients"] = db_clients
            save_config()
        except Exception as ex:
            logger.error(f"Error updating clients in database: {ex}")

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


_scanner_thread = None
_stop_scanner_thread = False


def update_scanner_state_in_config(current_scan_results, ports_to_scan, perform_port_scan, port_scan_enabled, port_scan_timeout, port_scan_threads, host_threads=4):
    import network_scanner
    import concurrent.futures
    global config
    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    
    # 1. Do all port scans OUTSIDE the lock
    port_scan_results = {}
    online_ips = list(current_scan_results.keys())
    if port_scan_enabled and perform_port_scan and ports_to_scan:
        logger.info(f"Starting parallel port scan for {len(online_ips)} hosts using {host_threads} host threads and {port_scan_threads} port threads per host...")
        
        def scan_single_host(ip):
            logger.info(f"Starting port scan for {ip} ({len(ports_to_scan)} ports)...")
            res = network_scanner.scan_ports_threaded(ip, ports_to_scan, port_scan_timeout, port_scan_threads)
            return ip, res
            
        with concurrent.futures.ThreadPoolExecutor(max_workers=host_threads) as executor:
            futures = [executor.submit(scan_single_host, ip) for ip in online_ips]
            for future in concurrent.futures.as_completed(futures):
                try:
                    ip, ports_result_str = future.result()
                    port_scan_results[ip] = ports_result_str
                    if ports_result_str is not None:
                        logger.info(f"Port scan {ip} -> '{ports_result_str or 'None Open'}'")
                except Exception as e:
                    logger.error(f"Error scanning host in thread pool: {e}")

                
    # 2. Get list of potentially offline IPs briefly under the lock
    potentially_offline_macs_and_ips = []
    with config_lock:
        db_clients = config.get("clients", {})
        for mac_clean, c in db_clients.items():
            if c.get("scanner_detected") and c.get("scanner_status", "OFFLINE") == "ONLINE":
                # Check actual scanner_ip first, fallback to ip
                ip = c.get("scanner_ip") or c.get("ip")
                if ip and ip not in online_ips:
                    potentially_offline_macs_and_ips.append((mac_clean, ip))
                    
    # 3. Ping potentially offline hosts OUTSIDE the lock
    ping_results = {}
    if potentially_offline_macs_and_ips:
        logger.info(f"{len(potentially_offline_macs_and_ips)} hosts not in ARP. Pinging...")
        for mac_clean, ip in potentially_offline_macs_and_ips:
            logger.debug(f"Pinging {ip}...")
            is_online = network_scanner.is_host_reachable_by_ping(ip)
            ping_results[mac_clean] = (ip, is_online)
            
    # 4. Briefly acquire the lock to commit updates and save config
    history_inserts = []
    with config_lock:
        # Re-fetch database maps inside the lock to ensure freshness
        db_clients = config.setdefault("clients", {})

        # Sort online hosts numerically so that lower/primary IPs are processed first
        def ip_sort_key(ip_str):
            try:
                return [int(x) for x in ip_str.split('.')]
            except Exception:
                return [999, 999, 999, 999]
        sorted_online_ips = sorted(online_ips, key=ip_sort_key)

        # Update online hosts
        for ip in sorted_online_ips:
            data = current_scan_results[ip]
            mac = data['mac']
            mac_clean = mac.replace(":", "").replace("-", "").replace(" ", "").upper()
            vendor = network_scanner.get_vendor(mac)

            existing = db_clients.get(mac_clean)

            ports_result_str = port_scan_results.get(ip)

            if existing:
                last_status = existing.get("scanner_status", "OFFLINE")
                status_changed = (last_status == "OFFLINE")

                # If a port scan was performed, update ports; otherwise preserve existing
                current_ports = ports_result_str if ports_result_str is not None else existing.get("ports", "")

                # Avoid overwriting a valid active scanner_ip with an alias IP (like VPN clients)
                # If the host was deleted (scanner_detected is False), we treat it as a fresh scan and reset the IP.
                existing_ip = existing.get("scanner_ip")
                was_detected = existing.get("scanner_detected", False)
                if was_detected and existing_ip and existing_ip in online_ips:
                    target_ip = existing_ip
                else:
                    target_ip = ip

                existing.update({
                    "mac": mac,
                    "scanner_ip": target_ip, # Store the actual scanned IP separately!
                    "vendor": vendor,
                    "scanner_status": "ONLINE",
                    "ports": current_ports,
                    "scanner_detected": True,
                    "last_seen_online": now_str,
                    "last_updated": now_str
                })
                if "first_seen" not in existing:
                    existing["first_seen"] = now_str

                if status_changed:
                    history_inserts.append((target_ip, 1, now_str))
            else:
                db_clients[mac_clean] = {
                    "mac": mac,
                    "host": "",
                    "note": "",
                    "scanner_ip": ip, # Store the actual scanned IP separately!
                    "ip": "", # Initialize parent switch IP as empty
                    "vendor": vendor,
                    "ports": ports_result_str or "",
                    "scanner_status": "ONLINE",
                    "status": "offline",
                    "known_host": 0,
                    "first_seen": now_str,
                    "last_seen_online": now_str,
                    "last_updated": now_str,
                    "scanner_detected": True
                }
                history_inserts.append((ip, 1, now_str))
                    
        # Update offline hosts
        for mac_clean, (ip, is_online) in ping_results.items():
            c = db_clients.get(mac_clean)
            if not c:
                continue
            
            if is_online:
                logger.info(f"Ping success for {ip}. Kept as ONLINE.")
                c["scanner_status"] = "ONLINE"
                c["last_updated"] = now_str
            else:
                logger.info(f"Ping failed for {ip}. Marking OFFLINE.")
                c["scanner_status"] = "OFFLINE"
                c["last_updated"] = now_str
                history_inserts.append((ip, 0, now_str))
                
        config["clients"] = db_clients
        config.pop("scanner_history", None)
        
    save_config()

    if history_inserts:
        try:
            scanner_db.insert_host_history_batch(history_inserts)
            logger.info(f"Saved {len(history_inserts)} host history records to SQLite.")
        except Exception as e:
            logger.error(f"Error saving host history batch to SQLite: {e}")


def run_scanner_loop():
    global _stop_scanner_thread
    import network_scanner
    
    while not _stop_scanner_thread:
        load_config()
        
        scanner_enabled = config.get("scanner_enabled", False)
        
        if scanner_enabled:
            network_range = config.get("scanner_network_range", "192.168.1.0/24")
            port_scan_enabled = config.get("scanner_port_scan_enabled", True)
            port_scan_range_str = config.get("scanner_port_scan_range", "22,80,443,8080")
            scan_interval = config.get("scanner_interval", 60)
            port_scan_interval = config.get("scanner_port_scan_interval", 300)
            purge_history_hours = config.get("scanner_purge_history_hours", 72)
            
            scanner_db.purge_old_history(purge_history_hours)
            
            do_port_scan_this_run = False
            now_ts = time.time()
            
            state_file = os.path.join(DATA_DIR, "last_port_scan.ts")
            last_scan_ts = 0.0
            if os.path.exists(state_file):
                try:
                    with open(state_file, "r") as f:
                        last_scan_ts = float(f.read().strip())
                except Exception:
                    pass
                    
            if now_ts - last_scan_ts >= port_scan_interval:
                do_port_scan_this_run = True
                try:
                    with open(state_file, "w") as f:
                        f.write(str(now_ts))
                except Exception as e:
                    logger.error(f"Failed to update port scan state file: {e}")
                    
            ports_to_scan = network_scanner.parse_port_range(port_scan_range_str)
            
            logger.info(f"Running subnet scanner for range {network_range}...")
            current_scan = network_scanner.scan_network(network_range)
            
            if current_scan is not None:
                port_scan_threads = config.get("scanner_port_scan_threads", 20)
                host_threads = config.get("scanner_host_scan_threads", 4)
                port_scan_timeout_ms = config.get("scanner_port_scan_timeout_ms", 500)
                port_scan_timeout = port_scan_timeout_ms / 1000.0
                
                update_scanner_state_in_config(
                    current_scan, 
                    ports_to_scan, 
                    do_port_scan_this_run, 
                    port_scan_enabled, 
                    port_scan_timeout, 
                    port_scan_threads,
                    host_threads
                )

            else:
                logger.error("ARP Subnet Scan returned None. Check root permissions / Scapy installation.")
        
        interval = config.get("scanner_interval", 60)
        sleep_cycles = max(1, int(interval / 2))
        for _ in range(sleep_cycles):
            if _stop_scanner_thread:
                break
            time.sleep(2)

def start_scanner_thread():
    global _scanner_thread, _stop_scanner_thread
    _stop_scanner_thread = False
    _scanner_thread = threading.Thread(target=run_scanner_loop, daemon=True)
    _scanner_thread.start()


_telemetry_thread = None

def send_telemetry_packet(event_type):
    try:
        import urllib.request
        import urllib.error
        import json
        import uuid as uuid_mod
        
        # 1. Load config and check if telemetry is enabled
        load_config()
        telemetry_enabled = config.get("telemetry_enabled", False)
        if not telemetry_enabled:
            return
            
        uuid_str = config.get("uuid")
        if not uuid_str:
            uuid_str = str(uuid_mod.uuid4())
            with config_lock:
                config["uuid"] = uuid_str
                save_config()
    
        # 2. Collect switch data
        enabled_switches = [sw for sw in config.get("switches", []) if sw.get("enabled", True) and sw.get("model", "").lower() != "internet"]
        switches_count = len(enabled_switches)
        switch_models = [sw.get("model", "Unknown") for sw in enabled_switches]
        
        # Unmanaged switches count
        unmanaged_count = len(config.get("unmanaged_switches", []))
        
        # Hosts count (active scanner hosts or online clients)
        hosts_count = 0
        db_clients = config.get("clients", {})
        for c in db_clients.values():
            if c.get("scanner_status") == "ONLINE" or c.get("status") == "online":
                hosts_count += 1
        
        payload = {
            "uuid": uuid_str,
            "version": VERSION,
            "switches_count": switches_count,
            "switch_models": switch_models,
            "unmanaged_count": unmanaged_count,
            "hosts_count": hosts_count,
            "event_type": event_type
        }
               
        url = "https://switch-dashboard.byte4geek.workers.dev"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode('utf-8'),
            headers={"Content-Type": "application/json", "User-Agent": f"SwitchDashboard/{VERSION}"},
            method="POST"
        )
        
        with urllib.request.urlopen(req, timeout=10) as response:
            res_body = response.read().decode('utf-8')
            logger.info(f"Telemetry sent successfully ({event_type}): {res_body}")
            
    except Exception as e:
        logger.warning(f"Failed to send telemetry ({event_type}): {e}")

def run_telemetry_loop():
    time.sleep(10)
    send_telemetry_packet("boot")
    while True:
        time.sleep(43200) # 12 hours
        send_telemetry_packet("heartbeat")

def start_telemetry_thread():
    global _telemetry_thread
    if _telemetry_thread is not None and _telemetry_thread.is_alive():
        # Trigger an immediate config_update on reload/restart to report state changes
        threading.Thread(target=lambda: send_telemetry_packet("config_update"), daemon=True).start()
        return
    _telemetry_thread = threading.Thread(target=run_telemetry_loop, daemon=True)
    _telemetry_thread.start()


def migrate_old_files():
    migrated = False
    global config
    
    notes_path = NOTES_PATH
    settings_path = SETTINGS_PATH
    
    if os.path.exists(notes_path) or os.path.exists(settings_path):
        logger.info("Migrating legacy settings/notes to config.json...")
        load_config()
        
        if os.path.exists(notes_path):
            try:
                with open(notes_path, "r", encoding="utf-8") as f:
                    legacy_notes = json.load(f)
                if "notes" not in config:
                    config["notes"] = {}
                config["notes"].update(legacy_notes)
                os.remove(notes_path)
                logger.info("Migrated notes.json to config.json successfully and deleted it.")
                migrated = True
            except Exception as e:
                logger.error(f"Error migrating notes.json: {e}")
                
        if os.path.exists(settings_path):
            try:
                with open(settings_path, "r", encoding="utf-8") as f:
                    legacy_settings = json.load(f)
                if "settings" not in config:
                    config["settings"] = {}
                config["settings"].update(legacy_settings)
                os.remove(settings_path)
                logger.info("Migrated settings.json to config.json successfully and deleted it.")
                migrated = True
            except Exception as e:
                logger.error(f"Error migrating settings.json: {e}")
                
        if migrated:
            save_config()


scanner_db.init_db()
migrate_old_files()
load_config()
load_history()
start_cache_thread()
start_scanner_thread()
start_telemetry_thread()

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
    load_config()
    return render_template("index.html",
                           title=config.get("title", "Switch Dashboard"),
                           refresh=config.get("refresh_interval", 30),
                           enabled_columns=config.get("enabled_columns", ['port', 'status', 'speed', 'packets', 'bytes', 'info', 'notes']),
                           grid_columns=config.get("grid_columns", "auto"),
                           ports_wrap_threshold=config.get("ports_wrap_threshold", 0),
                           column_widths=config.get("column_widths", {}),
                           column_order=config.get("column_order", []),
                           version=VERSION)


@app.route("/map")
def network_map():
    load_config()
    return render_template("map.html",
                           title=config.get("title", "Switch Dashboard"),
                           map_positions=config.get("map_positions", {}),
                           version=VERSION)



@app.route("/api/switches")
def api_switches():
    notes = load_notes()
    vendors = load_mac_vendors()
    ieee_vendors = get_ieee_vendors()
    load_config()
    active_ips = []
    for sw in switch_configs:
        if sw.get("enabled", True) and sw.get("model", "").lower() != "internet":
            ip = sw["ip"]
            if ip not in active_ips:
                active_ips.append(ip)
    with cache_lock:
        data = [cached_data[ip] for ip in active_ips if ip in cached_data]
    db_clients = config.get("clients", {})
    
    for sw in data:
        sw["mac_table"] = [entry for entry in sw.get("mac_table", []) if not is_ignored_mac(entry.get("mac"))]
        for entry in sw.get("mac_table", []):
            entry["vendor"] = lookup_vendor(entry.get("mac"), vendors, ieee_vendors)
            norm_mac = entry.get("mac", "").replace(":", "").replace("-", "").replace(" ", "").upper()
            if norm_mac in db_clients:
                entry["host"] = db_clients[norm_mac].get("host", "")
            else:
                entry["host"] = ""
        for p in sw.get("ports", []):
            custom_note = notes.get(f"{sw['ip']}:{p['port']}", "")
            if custom_note:
                p["note"] = custom_note
            else:
                p["note"] = p.get("vm_name") or ""
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
        
    if sw.get("model", "").lower() == "internet":
        return jsonify({"status": "ok", "count": 0, "mac_table": []})
        
    try:
        from scraper import HCSwitchScraper, OVSScraper, FritzBoxScraper
        model_lower = sw.get("model", "").lower()
        if model_lower in ["openvswitch", "ovs"]:
            scraper_obj = OVSScraper(sw)
        elif model_lower == "fritzbox":
            scraper_obj = FritzBoxScraper(sw)
        else:
            scraper_obj = HCSwitchScraper(sw)
        mac_table = scraper_obj.scrape_mac_table()
        mac_table = [entry for entry in mac_table if not is_ignored_mac(entry.get("mac"))]

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
        
    if sw.get("model", "").lower() == "internet":
        return jsonify({"error": "No transceiver data available for virtual Internet node"}), 404
        
    try:
        from scraper import HCSwitchScraper, OVSScraper, FritzBoxScraper
        model_lower = sw.get("model", "").lower()
        if model_lower in ["openvswitch", "ovs"]:
            scraper_obj = OVSScraper(sw)
        elif model_lower == "fritzbox":
            scraper_obj = FritzBoxScraper(sw)
        else:
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


@app.route("/api/topology")
def api_topology():
    def normalize_mac(mac):
        if not mac:
            return ""
        return mac.replace(":", "").replace("-", "").replace(" ", "").upper()

    notes = load_notes()
    vendors = load_mac_vendors()
    ieee_vendors = get_ieee_vendors()
    
    load_config()
    unmanaged_switches = config.get("unmanaged_switches", [])
    unmanaged_by_port = {}
    for us in unmanaged_switches:
        p_ip = us.get("parent_ip", "")
        p_port = str(us.get("parent_port", ""))
        if p_ip and p_port:
            unmanaged_by_port[(p_ip, p_port)] = {
                "id": f"unmanaged_{p_ip}_{p_port}",
                "name": us.get("name", "Unmanaged Switch"),
                "parent_ip": p_ip,
                "parent_port": p_port
            }
    
    # 1. Get all switches and their MACs
    with cache_lock:
        sw_data_copy = {ip: dict(data) for ip, data in cached_data.items()}
        
    switches_by_ip = {}
    mac_to_switch_ip = {}
    
    for sw in switch_configs:
        if not sw.get("enabled", True):
            continue
        ip = sw["ip"]
        sw_data = sw_data_copy.get(ip, {})
        sw_mac = normalize_mac(sw_data.get("mac", ""))
        
        switches_by_ip[ip] = {
            "ip": ip,
            "name": sw["name"],
            "model": sw.get("model", ""),
            "mac": sw_mac,
            "ports": sw_data.get("ports", []),
            "mac_table": sw_data.get("mac_table", []),
            "status": "online" if "error" not in sw_data and sw_data else "offline"
        }
        if sw_mac:
            mac_to_switch_ip[sw_mac] = ip
            
    # 2. Get infrastructure devices from config
    infra_devices = config.get("infrastructure_devices", [])
    infra_by_mac = {}
    router_mac = None
    
    # First, let's see if we have a monitored switch that is a router/fritzbox
    for ip, sw in switches_by_ip.items():
        if sw.get("model", "").lower() == "fritzbox" and sw.get("mac"):
            router_mac = sw["mac"]
            break

    for dev in infra_devices:
        norm_mac = normalize_mac(dev["mac"])
        if norm_mac:
            # Skip if this infrastructure device is already a monitored switch/router node
            if norm_mac in mac_to_switch_ip:
                continue
            dev_type = dev.get("type", "other")
            infra_by_mac[norm_mac] = {
                "mac": norm_mac,
                "name": dev["name"],
                "type": dev_type,
                "vendor": lookup_vendor(norm_mac, vendors, ieee_vendors)
            }
            if dev_type == "router" and not router_mac:
                router_mac = norm_mac
            
    # Helper to check if a MAC is a switch
    def is_switch_mac(mac):
        return mac in mac_to_switch_ip
        
    # Helper to check if a MAC is infra
    def is_infra_mac(mac):
        return mac in infra_by_mac
        
    # 3. For each switch, parse the MAC table to see what is on each port
    # sw_port_macs[ip][port] = list of normalized MACs
    sw_port_macs = {}
    for ip, sw in switches_by_ip.items():
        sw_port_macs[ip] = {}
        for entry in sw["mac_table"]:
            port = str(entry.get("port", ""))
            mac = normalize_mac(entry.get("mac", ""))
            if port and mac:
                if is_ignored_mac(mac):
                    continue
                if port not in sw_port_macs[ip]:
                    sw_port_macs[ip][port] = []
                if mac not in sw_port_macs[ip][port]:
                    sw_port_macs[ip][port].append(mac)
                    
    # 4. Find switch-to-switch direct links
    # For each switch S and port P, find which switches are learned on it.
    # Then filter out those that are "behind" others.
    direct_switch_links = [] # list of tuples: (src_ip, port, dst_ip)
    
    for src_ip, ports in sw_port_macs.items():
        src_mac = switches_by_ip[src_ip]["mac"]
        for port, macs in ports.items():
            # Find all switches learned on this port
            switches_on_port = []
            for mac in macs:
                if is_switch_mac(mac):
                    switches_on_port.append(mac_to_switch_ip[mac])
            
            if not switches_on_port:
                continue
                
            direct_neighbors = []
            for t_ip in switches_on_port:
                t_mac = switches_by_ip[t_ip]["mac"]
                is_behind_any = False
                for u_ip in switches_on_port:
                    if u_ip == t_ip:
                        continue
                    # Check if t is behind u relative to src_ip
                    u_ports = sw_port_macs.get(u_ip, {})
                    
                    port_u_t = None
                    port_u_s = None
                    
                    for u_p, u_macs in u_ports.items():
                        if t_mac in u_macs:
                            port_u_t = u_p
                        if src_mac in u_macs:
                            port_u_s = u_p
                            
                    # Fallback for port_u_s if src_mac is not in U's MAC table
                    if not port_u_s and router_mac:
                        for u_p, u_macs in u_ports.items():
                            if router_mac in u_macs:
                                port_u_s = u_p
                                break
                                
                    if port_u_t and port_u_s and port_u_t != port_u_s:
                        is_behind_any = True
                        break
                if not is_behind_any:
                    direct_neighbors.append(t_ip)
            
            for dst_ip in direct_neighbors:
                direct_switch_links.append((src_ip, port, dst_ip))

    # Load static switch uplinks from config
    static_uplinks = {} # child_ip -> { parent_ip, parent_port, uplink_port }
    for sw in switch_configs:
        if not sw.get("enabled", True):
            continue
        ip = sw["ip"]
        p_ip = sw.get("parent_ip", "")
        p_port = sw.get("parent_port", "")
        up_port = sw.get("uplink_port", "")
        if p_ip and p_port:
            static_uplinks[ip] = {
                "parent_ip": p_ip,
                "parent_port": str(p_port),
                "uplink_port": str(up_port) if up_port else ""
            }

    # Bidirectional links
    processed_links = set()
    links = []
    
    # Build static switch-to-switch links
    for child_ip, info in static_uplinks.items():
        p_ip = info["parent_ip"]
        p_port = info["parent_port"]
        up_port = info["uplink_port"]
        
        if child_ip not in switches_by_ip:
            continue
            
        speed = "Unknown"
        tx_bps = 0
        rx_bps = 0
        
        if p_ip in switches_by_ip:
            parent_ports_info = switches_by_ip[p_ip]["ports"]
            for p_info in parent_ports_info:
                if str(p_info.get("port")).lower() == p_port.lower() or str(p_info.get("port")).lower() == f"port {p_port}".lower():
                    speed = p_info.get("speed", "Unknown")
                    tx_bps = p_info.get("speed_tx_bps", 0)
                    rx_bps = p_info.get("speed_rx_bps", 0)
                    break
        elif child_ip in switches_by_ip:
            child_ports_info = switches_by_ip[child_ip]["ports"]
            for p_info in child_ports_info:
                if up_port and (str(p_info.get("port")).lower() == up_port.lower() or str(p_info.get("port")).lower() == f"port {up_port}".lower()):
                    speed = p_info.get("speed", "Unknown")
                    tx_bps = p_info.get("speed_tx_bps", 0)
                    rx_bps = p_info.get("speed_rx_bps", 0)
                    break
                    
        source_port_label = p_port if ("lan" in p_port.lower() or "wan" in p_port.lower() or "port" in p_port.lower()) else f"Port {p_port}"
        target_port_label = up_port if ("port" in up_port.lower() or "wan" in up_port.lower() or "lan" in up_port.lower() or not up_port) else f"Port {up_port}"
        if not target_port_label:
            target_port_label = "unknown"
            
        links.append({
            "source": p_ip,
            "target": child_ip,
            "source_port": source_port_label,
            "target_port": target_port_label,
            "speed": speed,
            "tx_bps": tx_bps,
            "rx_bps": rx_bps,
            "type": "uplink"
        })
        
        link_key = tuple(sorted([p_ip, child_ip]))
        processed_links.add(link_key)

    switch_link_ports = {}
    for src_ip, port, dst_ip in direct_switch_links:
        switch_link_ports[(src_ip, dst_ip)] = port
        
    for src_ip, port, dst_ip in direct_switch_links:
        link_key = tuple(sorted([src_ip, dst_ip]))
        if link_key in processed_links:
            continue
            
        # Reject auto-discovered links where a child has a configured static parent,
        # unless that parent is the configured parent.
        if dst_ip in static_uplinks and static_uplinks[dst_ip]["parent_ip"] != src_ip:
            continue
            
        processed_links.add(link_key)
        dst_port = switch_link_ports.get((dst_ip, src_ip), "unknown")
        
        speed = "Unknown"
        tx_bps = 0
        rx_bps = 0
        src_ports_info = switches_by_ip[src_ip]["ports"]
        for p_info in src_ports_info:
            if str(p_info.get("port")) == str(port):
                speed = p_info.get("speed", "Unknown")
                tx_bps = p_info.get("speed_tx_bps", 0)
                rx_bps = p_info.get("speed_rx_bps", 0)
                break
                
        links.append({
            "source": src_ip,
            "target": dst_ip,
            "source_port": f"Port {port}",
            "target_port": f"Port {dst_port}" if dst_port != "unknown" else "unknown",
            "speed": speed,
            "tx_bps": tx_bps,
            "rx_bps": rx_bps,
            "type": "uplink"
        })

    # 5. Direct connections for infra devices
    infra_connections = {} # mac -> (switch_ip, port)
    for mac, dev in infra_by_mac.items():
        candidates = []
        for ip, ports in sw_port_macs.items():
            for port, macs in ports.items():
                if mac in macs:
                    has_switch = any(is_switch_mac(m) for m in macs)
                    if not has_switch:
                        candidates.append((ip, port))
        if candidates:
            infra_connections[mac] = candidates[0]

    # Add infra links
    for mac, conn in infra_connections.items():
        ip, port = conn
        speed = "Unknown"
        src_ports_info = switches_by_ip[ip]["ports"]
        for p_info in src_ports_info:
            if str(p_info.get("port")) == str(port):
                speed = p_info.get("speed", "Unknown")
                break
                
        if (ip, str(port)) in unmanaged_by_port:
            source_id = unmanaged_by_port[(ip, str(port))]["id"]
            source_port = ""
        else:
            source_id = ip
            source_port = f"Port {port}"
            
        links.append({
            "source": source_id,
            "target": mac,
            "source_port": source_port,
            "target_port": "",
            "speed": speed,
            "type": "infra"
        })

    # 6. Direct connections for Client devices
    clients = {}
    client_links = []
    
    # Process all clients in config["clients"] + any newly active client MACs
    load_config()
    db_clients = config.get("clients", {})
    all_known_client_macs = set(db_clients.keys())
    
    # Add active client MACs
    active_macs_to_port = {} # normalized_mac -> (switch_ip, port)
    for ip, ports in sw_port_macs.items():
        for port, macs in ports.items():
            for mac in macs:
                if not is_switch_mac(mac) and not is_infra_mac(mac):
                    has_switch = any(is_switch_mac(m) for m in macs)
                    if not has_switch:
                        # Prioritize physical switches over fritzbox
                        is_current_fritz = (switches_by_ip.get(ip, {}).get("model", "").lower() == "fritzbox")
                        if mac in active_macs_to_port:
                            prev_ip, prev_port = active_macs_to_port[mac]
                            is_prev_fritz = (switches_by_ip.get(prev_ip, {}).get("model", "").lower() == "fritzbox")
                            if is_prev_fritz and not is_current_fritz:
                                active_macs_to_port[mac] = (ip, port)
                        else:
                            active_macs_to_port[mac] = (ip, port)
                            all_known_client_macs.add(mac)
                        
    for mac in all_known_client_macs:
        if is_ignored_mac(mac):
            continue
        if is_switch_mac(mac) or is_infra_mac(mac):
            continue
        is_active = mac in active_macs_to_port
        client_entry = db_clients.get(mac, {})
        host_name = client_entry.get("host", "")
        
        if is_active:
            ip, port = active_macs_to_port[mac]
            status = "online"
        else:
            ip = client_entry.get("ip", "")
            port = client_entry.get("port", "")
            status = "offline"
            
        if not ip or not port:
            continue
            
        # Skip clients whose parent switch is currently disabled or deleted
        all_switch_ips = {sw["ip"] for sw in switch_configs}
        if ip in all_switch_ips and ip not in switches_by_ip:
            continue
            
        attached_infra = None
        for infra_mac, conn in infra_connections.items():
            if conn == (ip, port):
                attached_infra = infra_mac
                break
                
        vendor = lookup_vendor(mac, vendors, ieee_vendors)
        
        formatted_mac = client_entry.get("mac", "")
        if not formatted_mac:
            formatted_mac = ":".join(mac[i:i+2] for i in range(0, len(mac), 2)).upper()
            
        display_name = host_name if host_name else (vendor if vendor else f"Client {formatted_mac[-8:]}")
        
        clients[mac] = {
            "id": mac,
            "name": display_name,
            "mac": formatted_mac,
            "host": host_name,
            "type": "client",
            "device_type": client_entry.get("device_type", "laptop"),
            "vendor": vendor,
            "status": status,
            "last_seen_ip": ip,
            "last_seen_port": port,
            "last_seen_time": client_entry.get("last_seen", 0)
        }
        
        target_node = mac
        if attached_infra:
            source_node = attached_infra
            source_port = ""
        elif (ip, str(port)) in unmanaged_by_port:
            source_node = unmanaged_by_port[(ip, str(port))]["id"]
            source_port = ""
        else:
            source_node = ip
            source_port = f"Port {port}"
            
        speed = ""
        if not attached_infra and is_active:
            src_ports_info = switches_by_ip[ip]["ports"]
            for p_info in src_ports_info:
                if str(p_info.get("port")) == str(port):
                    speed = p_info.get("speed", "Unknown")
                    break
        elif not attached_infra and not is_active:
            speed = "offline"
            
        client_links.append({
            "source": source_node,
            "target": target_node,
            "source_port": source_port,
            "target_port": "",
            "speed": speed,
            "type": "client"
        })

    # Assemble nodes
    nodes = []
    
    # Check if there is an explicit internet node configured
    explicit_internet = None
    for ip, sw in switches_by_ip.items():
        if sw.get("model", "").lower() == "internet":
            explicit_internet = sw
            break

    if explicit_internet:
        nodes.append({
            "id": "internet",
            "name": explicit_internet["name"],
            "type": "internet",
            "status": "online"
        })
        # Check if a link to the internet node already exists in links (e.g. via static parent config)
        has_link_to_internet = False
        for link in links:
            if link["source"] == "internet" or link["target"] == "internet":
                has_link_to_internet = True
                break
        if not has_link_to_internet:
            # Fallback auto-link: find fritzbox and link it to WAN
            fritzbox_ip = None
            fritzbox_wan_speed = "1G/300M"
            for ip, sw in switches_by_ip.items():
                if sw.get("model", "").lower() == "fritzbox":
                    fritzbox_ip = ip
                    for p_info in sw.get("ports", []):
                        if str(p_info.get("port")).lower() == "wan":
                            fritzbox_wan_speed = p_info.get("speed", "1G/300M")
                            break
                    break
            if fritzbox_ip:
                links.append({
                    "source": "internet",
                    "target": fritzbox_ip,
                    "source_port": "",
                    "target_port": "WAN",
                    "speed": fritzbox_wan_speed,
                    "type": "internet"
                })
    else:
        # Add Internet (ONT) virtual node if Fritz!Box is configured
        fritzbox_ip = None
        fritzbox_online = False
        fritzbox_wan_speed = "1G/300M"
        for ip, sw in switches_by_ip.items():
            if sw.get("model", "").lower() == "fritzbox":
                fritzbox_ip = ip
                fritzbox_online = (sw.get("status") == "online")
                for p_info in sw.get("ports", []):
                    if str(p_info.get("port")).lower() == "wan":
                        fritzbox_wan_speed = p_info.get("speed", "1G/300M")
                        break
                break

        if fritzbox_ip:
            nodes.append({
                "id": "internet",
                "name": "Internet (ONT)",
                "type": "internet",
                "status": "online" if fritzbox_online else "offline"
            })
            links.append({
                "source": "internet",
                "target": fritzbox_ip,
                "source_port": "",
                "target_port": "WAN",
                "speed": fritzbox_wan_speed,
                "type": "internet"
            })

    # Add Switches
    for ip, sw in switches_by_ip.items():
        if sw.get("model", "").lower() == "internet":
            continue
        nodes.append({
            "id": ip,
            "name": sw["name"],
            "type": "switch",
            "ip": ip,
            "mac": sw["mac"],
            "model": sw["model"],
            "status": sw["status"]
        })
        
    # Add Infra devices
    for mac, dev in infra_by_mac.items():
        status = "online" if mac in infra_connections or dev["type"] == "router" else "offline"
        nodes.append({
            "id": mac,
            "name": dev["name"],
            "type": dev["type"],
            "mac": mac,
            "vendor": dev["vendor"],
            "status": status
        })
        
    # Add Unmanaged Switches and links
    for us_info in unmanaged_by_port.values():
        parent_sw_status = "online"
        parent_sw = switches_by_ip.get(us_info["parent_ip"])
        if parent_sw:
            parent_sw_status = parent_sw["status"]
            
        nodes.append({
            "id": us_info["id"],
            "name": us_info["name"],
            "type": "unmanaged_switch",
            "status": parent_sw_status,
            "parent_ip": us_info["parent_ip"],
            "parent_port": us_info["parent_port"]
        })
        
        p_ip = us_info["parent_ip"]
        p_port = us_info["parent_port"]
        speed = "Unknown"
        if p_ip in switches_by_ip:
            for p_info in switches_by_ip[p_ip]["ports"]:
                if str(p_info.get("port")) == str(p_port):
                    speed = p_info.get("speed", "Unknown")
                    break
                    
        links.append({
            "source": p_ip,
            "target": us_info["id"],
            "source_port": f"Port {p_port}",
            "target_port": "",
            "speed": speed,
            "type": "infra"
        })

    # Add Clients
    for mac, dev in clients.items():
        nodes.append({
            "id": mac,
            "name": dev["name"],
            "type": "client",
            "device_type": dev.get("device_type", "laptop"),
            "mac": dev["mac"],
            "vendor": dev["vendor"],
            "status": dev["status"],
            "host": dev["host"],
            "last_seen_ip": dev["last_seen_ip"],
            "last_seen_port": dev["last_seen_port"],
            "last_seen_time": dev["last_seen_time"]
        })

    if router_mac and router_mac not in infra_connections and router_mac not in mac_to_switch_ip and switch_configs:
        first_ip = switch_configs[0]["ip"]
        links.append({
            "source": first_ip,
            "target": router_mac,
            "source_port": "Port 1",
            "target_port": "",
            "speed": "1G",
            "type": "infra"
        })

    return jsonify({
        "nodes": nodes,
        "links": links + client_links
    })


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
        enabled_vals = request.form.getlist("switch_enabled[]")

        parent_ips = request.form.getlist("parent_ip[]")
        parent_ports = request.form.getlist("parent_port[]")
        uplink_ports = request.form.getlist("uplink_port[]")

        for i in range(len(ips)):
            if i >= len(keep) or keep[i] != "1":
                continue
            is_enabled = True
            if i < len(enabled_vals):
                is_enabled = (enabled_vals[i] == "1")
                
            p_ip = parent_ips[i].strip() if i < len(parent_ips) else ""
            p_port = parent_ports[i].strip() if i < len(parent_ports) else ""
            up_port = uplink_ports[i].strip() if i < len(uplink_ports) else ""
            
            sw_dict = {
                "name": names[i] if i < len(names) else f"Switch {i+1}",
                "ip": ips[i].strip(),
                "username": usernames[i].strip() if i < len(usernames) else "admin",
                "password": passwords[i].strip() if i < len(passwords) else "admin",
                "model": models[i].strip() if i < len(models) else "",
                "port_count": int(port_counts[i]) if i < len(port_counts) else 9,
                "enabled": is_enabled
            }
            if p_ip:
                sw_dict["parent_ip"] = p_ip
            if p_port:
                sw_dict["parent_port"] = p_port
            if up_port:
                sw_dict["uplink_port"] = up_port
                
            new_switches.append(sw_dict)

        # Parse infrastructure devices
        infra_names = request.form.getlist("infra_name[]")
        infra_macs = request.form.getlist("infra_mac[]")
        infra_types = request.form.getlist("infra_type[]")
        infra_keeps = request.form.getlist("infra_keep[]")

        new_infra = []
        for i in range(len(infra_macs)):
            if i >= len(infra_keeps) or infra_keeps[i] != "1":
                continue
            mac = infra_macs[i].strip().replace("-", ":").upper()
            if mac:
                new_infra.append({
                    "name": infra_names[i].strip() if i < len(infra_names) else f"Device {i+1}",
                    "mac": mac,
                    "type": infra_types[i].strip() if i < len(infra_types) else "other"
                })

        # Parse unmanaged switches
        unmanaged_names = request.form.getlist("unmanaged_name[]")
        unmanaged_parent_ips = request.form.getlist("unmanaged_parent_ip[]")
        unmanaged_parent_ports = request.form.getlist("unmanaged_parent_port[]")
        unmanaged_keeps = request.form.getlist("unmanaged_keep[]")

        new_unmanaged = []
        for i in range(len(unmanaged_names)):
            if i >= len(unmanaged_keeps) or unmanaged_keeps[i] != "1":
                continue
            name = unmanaged_names[i].strip()
            parent_ip = unmanaged_parent_ips[i].strip() if i < len(unmanaged_parent_ips) else ""
            parent_port = unmanaged_parent_ports[i].strip() if i < len(unmanaged_parent_ports) else ""
            if name and parent_ip and parent_port:
                new_unmanaged.append({
                    "name": name,
                    "parent_ip": parent_ip,
                    "parent_port": parent_port
                })

        new_title = request.form.get("title", config.get("title", ""))
        new_refresh = int(request.form.get("refresh_interval", 30))
        new_mac_multiplier = int(request.form.get("mac_refresh_multiplier", 5))
        new_ports_wrap_threshold = int(request.form.get("ports_wrap_threshold", 0))
        new_max_retries = int(request.form.get("max_request_retries", 5))
        new_columns = request.form.getlist("columns[]")
        if not new_columns:
            new_columns = ['port', 'status', 'speed', 'packets', 'bytes', 'info', 'notes']
        new_grid_columns = request.form.get("grid_columns", "auto")

        ignored_macs_raw = request.form.get("ignored_macs", "")
        ignored_macs_list = [p.strip() for p in ignored_macs_raw.split(",") if p.strip()]

        load_config()
        config["title"] = new_title
        config["refresh_interval"] = new_refresh
        config["mac_refresh_multiplier"] = new_mac_multiplier
        config["ports_wrap_threshold"] = new_ports_wrap_threshold
        config["max_request_retries"] = new_max_retries
        config["enabled_columns"] = new_columns
        config["grid_columns"] = new_grid_columns
        config["switches"] = new_switches
        config["infrastructure_devices"] = new_infra
        config["unmanaged_switches"] = new_unmanaged
        
        config["scanner_enabled"] = request.form.get("scanner_enabled") == "true"
        config["scanner_network_range"] = request.form.get("scanner_network_range", "192.168.1.0/24").strip()
        config["scanner_port_scan_enabled"] = request.form.get("scanner_port_scan_enabled") == "true"
        config["scanner_port_scan_range"] = request.form.get("scanner_port_scan_range", "22,80,443,8080").strip()
        config["scanner_interval"] = int(request.form.get("scanner_interval", 60))
        config["scanner_port_scan_interval"] = int(request.form.get("scanner_port_scan_interval", 300))
        config["scanner_purge_history_hours"] = int(request.form.get("scanner_purge_history_hours", 72))
        config["scanner_port_scan_threads"] = int(request.form.get("scanner_port_scan_threads", 20))
        config["scanner_host_scan_threads"] = int(request.form.get("scanner_host_scan_threads", 4))
        config["scanner_port_scan_timeout_ms"] = int(request.form.get("scanner_port_scan_timeout_ms", 500))
        config["telemetry_enabled"] = request.form.get("telemetry_enabled") == "true"

        
        if "settings" not in config:
            config["settings"] = {}
        config["settings"]["ignored_macs"] = ignored_macs_list

        save_config()

        global _stop_thread
        _stop_thread = True
        
        global _stop_scanner_thread
        _stop_scanner_thread = True
        
        time.sleep(0.5)
        load_config()
        start_cache_thread()
        start_scanner_thread()
        start_telemetry_thread()

        return redirect(url_for("dashboard"))

    load_config()
    settings = config.get("settings", {})
    ignored_macs_list = settings.get("ignored_macs", [])
    ignored_macs_str = ", ".join(ignored_macs_list)

    return render_template("config.html", title=config.get("title", "Switch Dashboard"),
                           switches=switch_configs,
                           infrastructure_devices=config.get("infrastructure_devices", []),
                           unmanaged_switches=config.get("unmanaged_switches", []),
                           refresh=config.get("refresh_interval", 30),
                           mac_multiplier=config.get("mac_refresh_multiplier", 5),
                           ports_wrap_threshold=config.get("ports_wrap_threshold", 0),
                           max_request_retries=config.get("max_request_retries", 5),
                           enabled_columns=config.get("enabled_columns", ['port', 'status', 'speed', 'packets', 'bytes', 'info', 'notes']),
                           grid_columns=config.get("grid_columns", "auto"),
                           ignored_macs=ignored_macs_str,
                           scanner_enabled=config.get("scanner_enabled", False),
                           scanner_network_range=config.get("scanner_network_range", "192.168.1.0/24"),
                           scanner_port_scan_enabled=config.get("scanner_port_scan_enabled", True),
                           scanner_port_scan_range=config.get("scanner_port_scan_range", "22,80,443,8080"),
                           scanner_interval=config.get("scanner_interval", 60),
                           scanner_port_scan_interval=config.get("scanner_port_scan_interval", 300),
                           scanner_purge_history_hours=config.get("scanner_purge_history_hours", 72),
                           scanner_port_scan_threads=config.get("scanner_port_scan_threads", 20),
                           scanner_host_scan_threads=config.get("scanner_host_scan_threads", 4),
                           scanner_port_scan_timeout_ms=config.get("scanner_port_scan_timeout_ms", 500),
                           version=VERSION,
                           telemetry_enabled=config.get("telemetry_enabled", False))


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    load_config()
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        if "settings" not in config:
            config["settings"] = {}
        for k, v in data.items():
            if k.startswith("scanner_"):
                config[k] = v
            else:
                config["settings"][k] = v
        save_config()
        return jsonify({"status": "ok"})
    
    # GET method
    res = dict(config.get("settings", {}))
    for k in ["scanner_enabled", "scanner_network_range", "scanner_port_scan_enabled", 
              "scanner_port_scan_range", "scanner_interval", "scanner_port_scan_interval", 
              "scanner_purge_history_hours", "scanner_port_scan_threads", 
              "scanner_host_scan_threads", "scanner_port_scan_timeout_ms"]:
        if k in config:
            res[k] = config[k]

    return jsonify(res)


@app.route("/api/config/settings", methods=["GET", "POST"])
def api_config_settings():
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        load_config()
        for key in ["column_widths", "column_order", "map_positions"]:
            if key in data:
                config[key] = data[key]
        save_config()
        return jsonify({"status": "ok"})
    
    # GET method
    load_config()
    return jsonify({
        "column_widths": config.get("column_widths", {}),
        "column_order": config.get("column_order", []),
        "map_positions": config.get("map_positions", {})
    })


@app.route("/api/clients/update_host", methods=["POST"])
def api_clients_update_host():
    data = request.get_json(force=True, silent=True) or {}
    mac = data.get("mac", "").replace(":", "").replace("-", "").replace(" ", "").upper()
    host = data.get("host", "").strip()
    if not mac:
        return jsonify({"error": "Missing mac"}), 400
        
    load_config()
    db_clients = config.get("clients", {})
    
    if mac in db_clients:
        db_clients[mac]["host"] = host
    else:
        # Create a new entry if not exists
        formatted_mac = ":".join(mac[i:i+2] for i in range(0, len(mac), 2)).upper()
        db_clients[mac] = {
            "mac": formatted_mac,
            "host": host,
            "ip": "",
            "port": "",
            "vlan": "",
            "status": "offline",
            "last_seen": 0
        }
        
    config["clients"] = db_clients
    save_config()
    return jsonify({"status": "ok"})


@app.route("/api/clients/update_type", methods=["POST"])
def api_clients_update_type():
    data = request.get_json(force=True, silent=True) or {}
    mac = data.get("mac", "").replace(":", "").replace("-", "").replace(" ", "").upper()
    device_type = data.get("type", "").strip()
    if not mac:
        return jsonify({"error": "Missing mac"}), 400
        
    load_config()
    db_clients = config.get("clients", {})
    
    if mac in db_clients:
        db_clients[mac]["device_type"] = device_type
    else:
        # Create a new entry if not exists
        formatted_mac = ":".join(mac[i:i+2] for i in range(0, len(mac), 2)).upper()
        db_clients[mac] = {
            "mac": formatted_mac,
            "host": "",
            "device_type": device_type,
            "ip": "",
            "port": "",
            "vlan": "",
            "status": "offline",
            "last_seen": 0
        }
        
    config["clients"] = db_clients
    save_config()
    return jsonify({"status": "ok"})


@app.route("/api/clients/delete", methods=["POST"])
def api_clients_delete():
    data = request.get_json(force=True, silent=True) or {}
    mac = data.get("mac", "").replace(":", "").replace("-", "").replace(" ", "").upper()
    if not mac:
        return jsonify({"error": "Missing mac"}), 400
        
    load_config()
    db_clients = config.get("clients", {})
    map_positions = config.get("map_positions", {})
    
    deleted = False
    if mac in db_clients:
        del db_clients[mac]
        deleted = True
    if mac in map_positions:
        del map_positions[mac]
        deleted = True
        
    if deleted:
        config["clients"] = db_clients
        config["map_positions"] = map_positions
        save_config()
        
    return jsonify({"status": "ok"})


@app.route("/api/clients/import_csv", methods=["POST"])
def api_clients_import_csv():
    if "file" not in request.files:
        return jsonify({"error": "No file part in the request"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No file selected for uploading"}), 400
    if not file.filename.endswith(".csv"):
        return jsonify({"error": "Only CSV files are allowed"}), 400

    try:
        content = file.read().decode("utf-8", errors="ignore")
        import csv
        import io
        
        reader = csv.reader(io.StringIO(content))
        imported_count = 0
        
        load_config()
        db_clients = config.get("clients", {})
        
        for row in reader:
            if not row:
                continue
            # Trim all spaces
            row = [cell.strip() for cell in row]
            
            # Skip header row if matches common terms
            if len(row) >= 2:
                c0_lower = row[0].lower()
                c1_lower = row[1].lower()
                if "host" in c0_lower or "name" in c0_lower or "mac" in c1_lower:
                    continue
                    
                host_name = row[0]
                mac_raw = row[1]
                
                # Normalize MAC
                mac = mac_raw.replace(":", "").replace("-", "").replace(" ", "").upper()
                if len(mac) == 12 and all(c in "0123456789ABCDEF" for c in mac):
                    formatted_mac = ":".join(mac[i:i+2] for i in range(0, len(mac), 2)).upper()
                    
                    if mac in db_clients:
                        db_clients[mac]["host"] = host_name
                    else:
                        db_clients[mac] = {
                            "mac": formatted_mac,
                            "host": host_name,
                            "ip": "",
                            "port": "",
                            "vlan": "",
                            "status": "offline",
                            "last_seen": 0
                        }
                    imported_count += 1
                    
        if imported_count > 0:
            config["clients"] = db_clients
            save_config()
            
        return jsonify({"status": "ok", "imported": imported_count})
    except Exception as e:
        logger.error(f"Failed to import client CSV: {e}")
        return jsonify({"error": str(e)}), 500



@app.route("/api/device_types/raw", methods=["GET", "POST"])
def api_device_types_raw():
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        content = data.get("content", "")
        if not content.strip():
            return jsonify({"error": "Content cannot be empty"}), 400
        try:
            import yaml
            parsed = yaml.safe_load(content)
            if not isinstance(parsed, dict):
                return jsonify({"error": "YAML must be a key-value dictionary mapping type keys to labels and icons/paths."}), 400
            for k, v in parsed.items():
                if not isinstance(v, dict) or "label" not in v:
                    return jsonify({"error": f"Entry '{k}' must contain a 'label' key."}), 400
                if "icon" not in v and "path" not in v:
                    return jsonify({"error": f"Entry '{k}' must contain either 'icon' or 'path' key."}), 400
                if not isinstance(v.get("label"), str):
                    return jsonify({"error": f"Field 'label' for entry '{k}' must be a string."}), 400
                if "icon" in v and not isinstance(v.get("icon"), str):
                    return jsonify({"error": f"Field 'icon' for entry '{k}' must be a string."}), 400
                if "path" in v and not isinstance(v.get("path"), str):
                    return jsonify({"error": f"Field 'path' for entry '{k}' must be a string."}), 400
        except Exception as ye:
            return jsonify({"error": f"Invalid YAML Syntax: {ye}"}), 400

        try:
            with open(DEVICE_TYPES_YAML_PATH, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info("Updated custom device types configuration.")
            return jsonify({"status": "ok"})
        except Exception as e:
            logger.error(f"Failed to save custom device types: {e}")
            return jsonify({"error": str(e)}), 500

    # GET method
    content = ""
    if os.path.exists(DEVICE_TYPES_YAML_PATH):
        try:
            with open(DEVICE_TYPES_YAML_PATH, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            logger.error(f"Failed to read custom device types file: {e}")
            return jsonify({"error": str(e)}), 500
    return jsonify({"content": content})


@app.route("/api/device_types", methods=["GET"])
def api_device_types_json():
    try:
        import yaml
        if os.path.exists(DEVICE_TYPES_YAML_PATH):
            with open(DEVICE_TYPES_YAML_PATH, "r", encoding="utf-8") as f:
                parsed = yaml.safe_load(f) or {}
                return jsonify(parsed)
    except Exception as e:
        logger.error(f"Failed to parse custom device types: {e}")
    return jsonify({})


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

    if sw.get("model", "").lower() == "internet":
        return jsonify({"error": "Virtual Internet node cannot be backed up"}), 400

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

    if sw.get("model", "").lower() == "internet":
        return jsonify({"error": "Virtual Internet node cannot be rebooted"}), 400

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
        load_config()
        if "settings" not in config:
            config["settings"] = {}
        config["settings"]["log_level"] = level_name
        save_config()

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


@app.route("/scanner")
def scanner_dashboard():
    load_config()
    settings = config.get("settings", {})
    col_widths = config.get("scanner_column_widths", settings.get("scanner_column_widths", {
        "del": 45,
        "ip_address": 120,
        "mac_address": 140,
        "vendor": 140,
        "hostname": 140,
        "known_host": 75,
        "status": 100,
        "ports": 120,
        "note": 180,
        "first_seen": 145,
        "last_seen_online": 145,
        "last_updated": 145
    }))
    col_order = config.get("scanner_column_order", settings.get("scanner_column_order", [
        "del", "ip_address", "mac_address", "vendor", "hostname",
        "known_host", "status", "ports", "note", "first_seen",
        "last_seen_online", "last_updated"
    ]))
    col_visibility = config.get("scanner_column_visibility", settings.get("scanner_column_visibility", {
        "del": True,
        "ip_address": True,
        "mac_address": True,
        "vendor": True,
        "hostname": True,
        "known_host": True,
        "status": True,
        "ports": True,
        "note": True,
        "first_seen": True,
        "last_seen_online": True,
        "last_updated": True
    }))
    return render_template("scanner.html", 
                           title=config.get("title", "Network Switch Dashboard"),
                           version=VERSION,
                           column_widths=col_widths,
                           column_order=col_order,
                           column_visibility=col_visibility)


@app.route("/scanner/history")
def scanner_history():
    load_config()
    return render_template("scanner_history.html", 
                           title=config.get("title", "Network Switch Dashboard"),
                           version=VERSION)


def find_client_by_ip(ip_address):
    db_clients = config.get("clients", {})
    # 1. Check if the passed ip_address is actually a MAC address
    mac_clean = ip_address.replace(":", "").replace("-", "").replace(" ", "").upper()
    if len(mac_clean) == 12 and all(ch in "0123456789ABCDEF" for ch in mac_clean):
        if mac_clean in db_clients:
            return mac_clean, db_clients[mac_clean]

    # 2. Check for exact match on scanner_ip
    for mac_clean, c in db_clients.items():
        if c.get("scanner_ip") == ip_address:
            return mac_clean, c

    # 3. Fallback to matching c.get("ip")
    for mac_clean, c in db_clients.items():
        if c.get("ip") == ip_address:
            return mac_clean, c

    return None, None


@app.route("/api/scanner/hosts")
def api_scanner_hosts():
    load_config()
    db_clients = config.get("clients", {})
    hosts_list = []
    for mac_clean, c in db_clients.items():
        if not c.get("scanner_detected"):
            continue
        hosts_list.append({
            "ip_address": c.get("scanner_ip", ""),
            "mac_address": c.get("mac", ""),
            "vendor": c.get("vendor", ""),
            "hostname": c.get("host", ""),
            "ports": c.get("ports", ""),
            "note": c.get("note", ""),
            "status": c.get("scanner_status", "ONLINE" if c.get("status") == "online" else "OFFLINE"),
            "known_host": int(c.get("known_host", 0)),
            "first_seen": c.get("first_seen", ""),
            "last_seen_online": c.get("last_seen_online", ""),
            "last_updated": c.get("last_updated", "")
        })
    return jsonify(hosts_list)


@app.route("/api/scanner/hosts/<ip_address>/known", methods=["POST"])
def api_scanner_known(ip_address):
    load_config()
    data = request.get_json(force=True, silent=True) or {}
    new_state = int(data.get("known", 0))
    
    with config_lock:
        mac_clean, client = find_client_by_ip(ip_address)
        if client:
            client["known_host"] = new_state
            config["clients"][mac_clean] = client
            save_config()
            return jsonify({"success": True})
    return jsonify({"error": "Host not found"}), 404


@app.route("/api/scanner/hosts/<ip_address>/update", methods=["POST"])
def api_scanner_update_host(ip_address):
    load_config()
    data = request.get_json(force=True, silent=True) or {}
    field = data.get("field")
    value = data.get("value", "")
    if field not in ["hostname", "note"]:
        return jsonify({"error": "Field not updatable"}), 400
        
    with config_lock:
        mac_clean, client = find_client_by_ip(ip_address)
        if client:
            if field == "hostname":
                client["host"] = value
            elif field == "note":
                client["note"] = value
            client["last_updated"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            config["clients"][mac_clean] = client
            save_config()
            return jsonify({"success": True})
    return jsonify({"error": "Host not found"}), 404


@app.route("/api/scanner/hosts/<ip_address>", methods=["DELETE"])
def api_scanner_delete_host(ip_address):
    load_config()
    with config_lock:
        mac_clean, client = find_client_by_ip(ip_address)
        if client:
            if "port" in client:
                client["scanner_detected"] = False
                config["clients"][mac_clean] = client
            else:
                config["clients"].pop(mac_clean, None)
            save_config()
            return jsonify({"success": True})
    return jsonify({"error": "Host not found"}), 404


@app.route("/api/scanner/history")
def api_scanner_history():
    load_config()
    db_clients = config.get("clients", {})
    
    # Load history from SQLite database
    db_history = scanner_db.get_history_data()

    grouped_history = {}
    for mac_clean, c in db_clients.items():
        if not c.get("scanner_detected"):
            continue
        ip = c.get("scanner_ip") or c.get("ip", "")
        if not ip:
            continue
            
        db_entry = db_history.get(ip, {"events": []})
        events = db_entry.get("events", [])
        
        formatted_events = []
        for e in events:
            ts = e.get("event_time", "")
            if ts:
                try:
                    if 'T' not in ts:
                        dt = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
                        ts_str = dt.isoformat() + 'Z'
                    else:
                        ts_str = ts
                except Exception:
                    ts_str = ts
            else:
                ts_str = ""
            formatted_events.append({
                "status": int(e.get("status", 0)),
                "event_time": ts_str
            })
        grouped_history[ip] = {
            "hostname": c.get("host", ""),
            "events": formatted_events
        }
    return jsonify(grouped_history)


@app.route("/api/scanner/history/<ip_address>", methods=["DELETE"])
def api_scanner_delete_host_history(ip_address):
    load_config()
    # If it is a MAC address, look up its IP
    mac_clean = ip_address.replace(":", "").replace("-", "").replace(" ", "").upper()
    if len(mac_clean) == 12 and all(ch in "0123456789ABCDEF" for ch in mac_clean):
        with config_lock:
            client = config.get("clients", {}).get(mac_clean)
            if client:
                target_ip = client.get("scanner_ip") or client.get("ip", "")
            else:
                target_ip = ""
    else:
        target_ip = ip_address

    if target_ip:
        scanner_db.delete_host_history(target_ip)
    return jsonify({"success": True})


@app.route("/api/scanner/history/all", methods=["DELETE"])
def api_scanner_clear_all_history():
    load_config()
    scanner_db.delete_all_history()
    return jsonify({"success": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
