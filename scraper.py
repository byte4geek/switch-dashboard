import collections
import collections.abc
collections.MutableMapping = collections.abc.MutableMapping
collections.Mapping = collections.abc.Mapping
collections.Sequence = collections.abc.Sequence
collections.Iterable = collections.abc.Iterable
collections.Container = collections.abc.Container
collections.Callable = collections.abc.Callable

import hashlib
import urllib.request
import urllib.parse
import urllib.error
from http.cookiejar import CookieJar, Cookie
from bs4 import BeautifulSoup
import time
import logging
import re
import os
import yaml
import threading

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_locks_lock = threading.Lock()
_switch_locks = {}


def get_switch_lock(ip):
    with _locks_lock:
        if ip not in _switch_locks:
            _switch_locks[ip] = threading.Lock()
        return _switch_locks[ip]


class HCSwitchScraper:
    def __init__(self, config):
        self.name = config["name"]
        self.ip = config["ip"]
        self.username = config.get("username", "admin")
        self.password = config.get("password", "admin")
        self.port_count = config.get("port_count", 9)
        self.model = config.get("model", "Generic Model")
        self.base_url = f"http://{self.ip}"
        self._cj = None
        self._opener = None

    def _open_request_with_retry(self, req, timeout=45, max_retries=5, retry_delay=3):
        # Enforce spacing between sequential uIP HTTP requests
        time.sleep(0.5)
        
        # Use instance-specific max_retries if set
        configured_retries = getattr(self, "max_retries", max_retries)
        attempts = max(1, configured_retries)
        
        for attempt in range(1, attempts + 1):
            try:
                if self._opener:
                    logger.debug(f"[_open_request_with_retry] Attempt {attempt}/{attempts} with opener. Timeout={timeout}")
                    return self._opener.open(req, timeout=timeout)
                else:
                    logger.debug(f"[_open_request_with_retry] Attempt {attempt}/{attempts} with urlopen. Timeout={timeout}")
                    return urllib.request.urlopen(req, timeout=timeout)
            except Exception as e:
                url_str = req if isinstance(req, str) else (req.full_url if hasattr(req, 'full_url') else str(req))
                
                # If it's a 404 Not Found error, do not retry
                if isinstance(e, urllib.error.HTTPError) and e.code == 404:
                    logger.debug(f"[_open_request_with_retry] HTTP 404 received for {url_str}. Skipping retries.")
                    raise e
                    
                logger.warning(f"[_open_request_with_retry] Attempt {attempt}/{attempts} failed for {url_str}: {e}")
                
                # Check if this is a POST request and we want to fall back to raw socket
                if isinstance(req, urllib.request.Request) and req.data is not None:
                    try:
                        logger.warning(f"[_open_request_with_retry] urllib failed for POST to {url_str}. Attempting raw socket fallback...")
                        return self._raw_socket_fallback(req, timeout=15)
                    except Exception as ex:
                        logger.error(f"[_open_request_with_retry] Raw socket fallback also failed: {ex}")
                
                if attempt < attempts:
                    time.sleep(retry_delay)
                else:
                    raise e

    def _raw_socket_fallback(self, req, timeout=15):
        import socket
        import urllib.parse
        
        url_parsed = urllib.parse.urlparse(req.full_url)
        path = url_parsed.path
        if url_parsed.query:
            path += '?' + url_parsed.query
            
        method = req.get_method()
        
        # Ensure cookies are added to headers if cj is present
        if self._cj:
            self._cj.add_cookie_header(req)
            
        headers_dict = {}
        for name, val in req.header_items():
            headers_dict[name.title()] = val
        for name, val in req.unredirected_hdrs.items():
            headers_dict[name.title()] = val
            
        if req.data and 'Content-Length' not in headers_dict:
            headers_dict['Content-Length'] = str(len(req.data))
            
        if 'User-Agent' not in headers_dict:
            headers_dict['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            
        headers_dict['Connection'] = 'close'
        
        req_lines = [f'{method} {path} HTTP/1.1']
        if 'Host' not in headers_dict:
            headers_dict['Host'] = url_parsed.netloc
            
        for name, val in headers_dict.items():
            req_lines.append(f'{name}: {val}')
            
        req_bytes = '\r\n'.join(req_lines).encode('utf-8') + b'\r\n\r\n'
        if req.data:
            req_bytes += req.data
            
        host_port = url_parsed.netloc.split(':')
        host = host_port[0]
        port = int(host_port[1]) if len(host_port) > 1 else 80
        
        logger.info(f"[_raw_socket_fallback] Sending raw socket request to {host}:{port} ({method} {path})...")
        
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        s.sendall(req_bytes)
        
        response_parts = []
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            response_parts.append(chunk)
        s.close()
        
        resp_bytes = b''.join(response_parts)
        
        # Split headers and body
        parts = resp_bytes.split(b'\r\n\r\n', 1)
        header_part = parts[0]
        body_part = parts[1] if len(parts) > 1 else b''
        
        # Parse status line and redirect headers
        header_lines = header_part.decode('latin-1').splitlines()
        status_line = header_lines[0] if header_lines else ""
        
        logger.info(f"[_raw_socket_fallback] Raw socket request completed. Status: {status_line}")
        
        resp_headers = {}
        for line in header_lines[1:]:
            line = line.strip()
            if not line:
                continue
            h_parts = line.split(':', 1)
            if len(h_parts) == 2:
                resp_headers[h_parts[0].strip().lower()] = h_parts[1].strip()
                
        # Handle Cookies if Set-Cookie is in response headers
        if self._cj:
            for line in header_lines[1:]:
                if line.lower().startswith('set-cookie:'):
                    cookie_content = line[11:].strip()
                    c_parts = cookie_content.split(';')
                    if c_parts:
                        name_val = c_parts[0].split('=', 1)
                        if len(name_val) == 2:
                            c_name = name_val[0].strip()
                            c_val = name_val[1].strip()
                            new_cookie = Cookie(
                                version=0, name=c_name, value=c_val,
                                port=None, port_specified=False,
                                domain=host, domain_specified=True,
                                domain_initial_dot=False,
                                path="/", path_specified=True,
                                secure=False, expires=None, discard=True,
                                comment=None, comment_url=None, rest={}
                            )
                            self._cj.set_cookie(new_cookie)
                            logger.info(f"[_raw_socket_fallback] Extracted and stored cookie: {c_name}={c_val}")
                            
        # Redirect URL logic (if redirect found, e.g. Location header)
        final_url = req.full_url
        if 'location' in resp_headers:
            loc = resp_headers['location']
            if loc.startswith('http://') or loc.startswith('https://'):
                final_url = loc
            else:
                final_url = f"http://{host}:{port}{loc}"
                
        class RawSocketResponse:
            def __init__(self, body, url, headers):
                self.body = body
                self.url = url
                self.headers = headers
            def read(self):
                return self.body
            def geturl(self):
                return self.url
                
        return RawSocketResponse(body_part, final_url, resp_headers)

    def _load_template(self):
        # Look for templates in DASHBOARD_DATA_DIR or local directory
        data_dir = os.environ.get("DASHBOARD_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
        templates_dir = os.path.join(data_dir, "device-templates")
        
        template_name = f"{self.model}.yaml"
        template_path = os.path.join(templates_dir, template_name)
        
        if not os.path.exists(template_path):
            # Fallback to local directory if not found in data_dir
            local_templates_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "device-templates")
            template_path = os.path.join(local_templates_dir, template_name)
            
        if os.path.exists(template_path):
            try:
                with open(template_path, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f)
            except Exception as e:
                logger.error(f"Error loading YAML template at {template_path}: {e}")
        return None

    def _login(self):
        template = self._load_template()
        login_cfg = template.get("login") if template else None
        login_required = True
        if login_cfg is False:
            login_required = False
        elif isinstance(login_cfg, dict):
            login_required = login_cfg.get("required", True)

        if not login_required:
            logger.debug(f"[_login] Login not required by template for {self.ip}. Initializing basic opener.")
            self._cj = CookieJar()
            self._opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(self._cj)
            )
            return

        if login_required and isinstance(login_cfg, dict) and "url" in login_cfg:
            logger.debug(f"[_login] Custom template-driven login for {self.ip}...")
            self._cj = CookieJar()
            self._opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(self._cj)
            )
            
            login_url = login_cfg.get("url")
            method = login_cfg.get("method", "POST")
            post_data_raw = login_cfg.get("post_data", {})
            post_data = {}
            
            auth_str = self.username + self.password
            md5hash = hashlib.md5(auth_str.encode()).hexdigest()
            
            for k, v in post_data_raw.items():
                if isinstance(v, str):
                    post_data[k] = v.replace("{{username}}", self.username)\
                                    .replace("{{password}}", self.password)\
                                    .replace("{{md5hash}}", md5hash)
                else:
                    post_data[k] = v
                    
            referer_path = login_cfg.get("referer_path", "/login.html")
            headers = {"Referer": f"{self.base_url}{referer_path}"}
            
            data = None
            if method.upper() == "POST":
                data = urllib.parse.urlencode(post_data).encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                
            req = urllib.request.Request(f"{self.base_url}{login_url}", data=data, headers=headers, method=method)
            try:
                r = self._open_request_with_retry(req, timeout=45, max_retries=5)
                r.read()
                if "login.html" in r.geturl():
                    logger.warning(f"[_login] Custom login redirect returned login.html on {self.ip}. Login failed!")
                    raise Exception("Login redirected to login.html")
                logger.debug(f"[_login] Custom template-driven login response read successfully")
                return
            except Exception as e:
                logger.warning(f"Custom template-driven login urllib request failed for {self.ip}: {e}. Trying raw socket login fallback...")
                try:
                    import socket
                    host_port = self.ip.split(":")
                    host = host_port[0]
                    port = int(host_port[1]) if len(host_port) > 1 else 80
                    
                    body = urllib.parse.urlencode(post_data)
                    req_lines = [
                        f"{method} {login_url} HTTP/1.0",
                        f"Host: {self.ip}",
                        f"Referer: {self.base_url}{referer_path}",
                        "Content-Type: application/x-www-form-urlencoded",
                        f"Content-Length: {len(body)}",
                        "Connection: close",
                        "",
                        body
                    ]
                    req_bytes = "\r\n".join(req_lines).encode("utf-8")
                    
                    logger.debug(f"Sending raw socket login request to {host}:{port}...")
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(5)
                    s.connect((host, port))
                    s.sendall(req_bytes)
                    
                    response_parts = []
                    while True:
                        chunk = s.recv(4096)
                        if not chunk:
                            break
                        response_parts.append(chunk)
                    s.close()
                    
                    response_text = b"".join(response_parts).decode("latin-1")
                    logger.debug(f"Raw socket response: {response_text}")
                    
                    cookie_found = False
                    for line in response_text.splitlines():
                        if line.lower().startswith("set-cookie:"):
                            cookie_content = line[11:].strip()
                            parts = cookie_content.split(";")
                            if parts:
                                name_val = parts[0].split("=", 1)
                                if len(name_val) == 2:
                                    c_name = name_val[0].strip()
                                    c_val = name_val[1].strip()
                                    
                                    new_cookie = Cookie(
                                        version=0, name=c_name, value=c_val,
                                        port=None, port_specified=False,
                                        domain=host, domain_specified=True,
                                        domain_initial_dot=False,
                                        path="/", path_specified=True,
                                        secure=False, expires=None, discard=True,
                                        comment=None, comment_url=None, rest={}
                                    )
                                    self._cj.set_cookie(new_cookie)
                                    logger.info(f"Successfully extracted and set cookie via raw socket fallback: {c_name}={c_val}")
                                    cookie_found = True
                    if cookie_found:
                        return
                    else:
                        logger.error("No Set-Cookie headers found in raw socket response")
                        raise Exception("No cookies in raw socket response")
                except Exception as ex:
                    logger.error(f"Raw socket login fallback failed for {self.ip}: {ex}")
                    raise e

        logger.debug(f"[_login] Generating credentials MD5 hash for {self.username} on {self.ip}...")
        auth_str = self.username + self.password
        md5hash = hashlib.md5(auth_str.encode()).hexdigest()

        self._cj = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cj)
        )

        data = urllib.parse.urlencode({
            "username": self.username,
            "password": self.password,
            "Response": md5hash,
            "language": "EN"
        }).encode()

        req_login = urllib.request.Request(f"{self.base_url}/login.cgi", data=data)
        r = self._open_request_with_retry(
            req_login, timeout=45, max_retries=5
        )
        r.read()

        admin_c = Cookie(
            version=0, name="admin", value=md5hash,
            port=None, port_specified=False,
            domain=self.ip, domain_specified=True,
            domain_initial_dot=False,
            path="/", path_specified=True,
            secure=False, expires=None, discard=True,
            comment=None, comment_url=None, rest={}
        )
        self._cj.set_cookie(admin_c)
        logger.debug(f"[_login] Authenticated cookie jar initialized. Admin cookie set to {md5hash}")

        req_base = urllib.request.Request(f"{self.base_url}/")
        r = self._open_request_with_retry(req_base, timeout=45, max_retries=5)
        r.read()

    def _fetch(self, path):
        if not self._opener:
            self._login()
        logger.debug(f"[_fetch] Fetching path: {path}")
        # Enforce spacing between sequential uIP HTTP requests
        time.sleep(0.5)
        headers = {"Referer": f"{self.base_url}/"}
        req = urllib.request.Request(f"{self.base_url}{path}", headers=headers)
        try:
            r = self._open_request_with_retry(req, timeout=45, max_retries=5)
            res = r.read().decode("utf-8", errors="replace")
            logger.debug(f"[_fetch] Path {path} successfully fetched (size: {len(res)} characters)")
            return res
        except Exception as e:
            logger.error(f"Failed to fetch {path}: {e}")
            if isinstance(e, urllib.error.HTTPError) and e.code in [401, 403]:
                logger.warning(f"Received HTTP {e.code} for {path} on {self.ip}. Resetting session opener to force re-login.")
                self._opener = None
                self._cj = None
            return None

    def _fmt_uptime(self, raw):
        m = re.match(r"(?:(\d+)Day)?(?:(\d+)Hour)?(?:(\d+)Minute)?(?:(\d+)Second)?", raw)
        if m:
            parts = []
            for v, s in zip(m.groups(), ["d", "h", "m", "s"]):
                if v:
                    parts.append(f"{v}{s}")
            return " ".join(parts) if parts else raw
        return raw

    def _parse_counter(self, val):
        val = val.strip()
        if val.lower().startswith("0x"):
            try:
                return int(val, 16)
            except ValueError:
                return 0
        parts = val.split("-")
        if len(parts) == 2:
            try:
                return int(parts[0]) * 4294967296 + int(parts[1])
            except ValueError:
                return 0
        try:
            return int(val)
        except ValueError:
            return 0

    def scrape(self):
        with get_switch_lock(self.ip):
            logger.debug(f"Running full telemetry scrape for switch {self.name} ({self.ip})...")
            template = self._load_template()
            if template:
                try:
                    logger.info(f"Using template-driven scraping for model {self.model} on {self.ip}")
                    return self._scrape_with_template(template)
                except Exception as e:
                    logger.error(f"Template-driven scraping failed for {self.ip}: {e}. Falling back to built-in scraper.")
                    try:
                        return self._scrape_builtin()
                    except Exception as ex:
                        logger.error(f"Built-in scraping also failed for {self.ip}: {ex}")
                        return self._fallback()

            try:
                return self._scrape_builtin()
            except Exception as e:
                logger.error(f"Built-in scraping failed for {self.ip}: {e}")
                return self._fallback()

    def _scrape_builtin(self):
        try:
            self._login()
        except Exception as e:
            logger.error(f"Login failed for {self.ip}: {e}")
            return self._fallback()

        info_html = self._fetch("/info.cgi")
        stats_html = self._fetch("/port.cgi?page=stats")
        port_cfg_html = self._fetch("/port.cgi")

        ports = []
        device_info = {}
        port_states = {}

        # Try to parse port state configuration from /port.cgi
        if port_cfg_html:
            try:
                port_soup = BeautifulSoup(port_cfg_html, "html.parser")
                port_list_h3 = port_soup.find(lambda tag: tag.name == "h3" and "Port List" in tag.text)
                port_table = None
                if port_list_h3:
                    port_table = port_list_h3.find_next("table")
                
                if not port_table:
                    # Fallback: search for table with port & state headers and no select inputs
                    for t in port_soup.find_all("table"):
                        headers = [th.get_text(strip=True).lower() for th in t.find_all("th")]
                        if "port" in headers and "state" in headers:
                            first_tr = t.find("tr")
                            if first_tr:
                                next_tr = first_tr.find_next("tr")
                                if next_tr and not next_tr.find("select"):
                                    port_table = t
                                    break
                
                if port_table:
                    for row in port_table.find_all("tr"):
                        cells = row.find_all("td")
                        if len(cells) >= 2:
                            port_name = cells[0].get_text(strip=True)
                            state = cells[1].get_text(strip=True).strip().lower()
                            match = re.search(r"\d+", port_name)
                            if match:
                                port_num = match.group(0)
                                port_states[port_num] = state
                logger.debug(f"Parsed port config states (admin status) from /port.cgi: {port_states}")
            except Exception as e:
                logger.error(f"Error parsing port config states for {self.ip}: {e}")

        if info_html:
            soup = BeautifulSoup(info_html, "html.parser")
            tables = soup.find_all("table")

            # System info from first table (4-cell rows: th, td, th, td)
            if tables:
                for row in tables[0].find_all("tr"):
                    cells = row.find_all(["td", "th"])
                    for i in range(0, len(cells), 2):
                        if i + 1 >= len(cells):
                            break
                        label = cells[i].get_text(strip=True).rstrip(":")
                        value = cells[i + 1].get_text(strip=True)
                        if "Sys Uptime" in label:
                            device_info["uptime"] = self._fmt_uptime(value)
                        elif "MAC Address" in label:
                            device_info["mac"] = value
                        elif "IP Address" in label:
                            device_info["ip"] = value
                        elif "Firmware Version" in label:
                            device_info["firmware"] = value
                        elif "Device Model" in label or "Device Name" in label:
                            if "Model" in label or "model" in label:
                                device_info["model"] = value
                logger.debug(f"Parsed global device info from /info.cgi: {device_info}")

            # Port status from second table
            if len(tables) >= 2:
                for row in tables[1].find_all("tr")[1:]:
                    cells = row.find_all("td")
                    if len(cells) >= 4:
                        port_name = cells[0].get_text(strip=True)
                        match = re.match(r"Port\s*(\d+)", port_name)
                        port_num = match.group(1) if match else port_name
                        
                        admin_state = port_states.get(port_num, "enable")
                        if admin_state in ["disable", "disabled"]:
                            status_val = "disable"
                            link_val = "Disabled"
                        else:
                            status_val = "up" if "Up" in cells[1].get_text(strip=True) else "down"
                            link_val = cells[1].get_text(strip=True)

                        ports.append({
                            "port": port_num,
                            "status": status_val,
                            "link": link_val,
                            "speed": cells[3].get_text(strip=True),
                            "duplex": cells[2].get_text(strip=True),
                            "flow_control": cells[4].get_text(strip=True) if len(cells) > 4 else "",
                            "tx_packets": 0,
                            "rx_packets": 0,
                            "tx_bytes": 0,
                            "rx_bytes": 0,
                        })
                logger.debug(f"Parsed active port link/speed configurations: {ports}")

            # Fallback: check if port status table is inside port_cfg_html (/port.cgi) Table 2 (e.g. KeepLink switch model)
            if len(ports) == 0 and port_cfg_html:
                try:
                    port_soup = BeautifulSoup(port_cfg_html, "html.parser")
                    tables_port = port_soup.find_all("table")
                    status_table = None
                    for t in tables_port:
                        rows = t.find_all("tr")
                        if len(rows) >= 3:
                            text_content = t.get_text()
                            if "Actual" in text_content and "Config" in text_content and "Port 1" in text_content:
                                status_table = t
                                break
                    
                    if status_table:
                        logger.debug("Found port status table inside /port.cgi instead of /info.cgi! Parsing KeepLink style...")
                        rows = status_table.find_all("tr")
                        start_idx = 0
                        for idx, r in enumerate(rows):
                            cells = r.find_all(["td", "th"])
                            cells_text = [c.get_text(strip=True) for c in cells]
                            if len(cells_text) > 0 and "Port 1" in cells_text[0]:
                                start_idx = idx
                                break
                        
                        if start_idx > 0:
                            for row in rows[start_idx:]:
                                cells = row.find_all(["td", "th"])
                                if len(cells) >= 4:
                                    port_name = cells[0].get_text(strip=True)
                                    match = re.match(r"Port\s*(\d+)", port_name)
                                    port_num = match.group(1) if match else port_name
                                    
                                    admin_state = cells[1].get_text(strip=True).strip().lower()
                                    actual_link = cells[3].get_text(strip=True).strip()
                                    
                                    if admin_state in ["disable", "disabled"]:
                                        status_val = "disable"
                                        link_val = "Disabled"
                                        speed_val = "Disabled"
                                        duplex_val = "Disabled"
                                    else:
                                        status_val = "down" if "down" in actual_link.lower() else "up"
                                        link_val = "Link Up" if status_val == "up" else "Link Down"
                                        
                                        speed_val = "Auto"
                                        duplex_val = "Full"
                                        if status_val == "up":
                                            speed_match = re.match(r"(\d+G?)(Full|Half)?", actual_link, re.IGNORECASE)
                                            if speed_match:
                                                speed_num = speed_match.group(1)
                                                if "G" in speed_num.upper():
                                                    speed_val = speed_num.upper()
                                                else:
                                                    speed_val = f"{speed_num}M"
                                                
                                                duplex_val = speed_match.group(2) or "Full"
                                                if duplex_val.lower() == "full":
                                                    duplex_val = "Full"
                                                elif duplex_val.lower() == "half":
                                                    duplex_val = "Half"
                                            else:
                                                speed_val = actual_link
                                        else:
                                            speed_val = "Auto"
                                            duplex_val = ""
                                    
                                    ports.append({
                                        "port": port_num,
                                        "status": status_val,
                                        "link": link_val,
                                        "speed": speed_val,
                                        "duplex": duplex_val,
                                        "flow_control": cells[5].get_text(strip=True) if len(cells) > 5 else "",
                                        "tx_packets": 0,
                                        "rx_packets": 0,
                                        "tx_bytes": 0,
                                        "rx_bytes": 0,
                                    })
                            logger.debug(f"Parsed active port link/speed from /port.cgi status_table: {ports}")
                except Exception as e:
                    logger.error(f"Error parsing KeepLink port status table from /port.cgi: {e}")

        if stats_html:
            soup = BeautifulSoup(stats_html, "html.parser")
            stats_table = soup.find("table")
            if stats_table:
                rows = stats_table.find_all("tr")
                if len(rows) > 0:
                    first_row = rows[0]
                    headers = [c.get_text(strip=True).strip().lower() for c in first_row.find_all(["td", "th"])]
                    logger.debug(f"Parsing stats table. Found headers: {headers}")
                    
                    tx_pkt_idx = -1
                    rx_pkt_idx = -1
                    tx_bytes_idx = -1
                    rx_bytes_idx = -1
                    
                    for idx, h in enumerate(headers):
                        is_bytes = any(term in h for term in ["byte", "octet"])
                        if not is_bytes and (any(term in h for term in ["txgoodpkt", "txpackets", "tx packet", "txok", "tx_pkt", "tx pkt"]) or h == "tx"):
                            tx_pkt_idx = idx
                        elif not is_bytes and (any(term in h for term in ["rxgoodpkt", "rxpackets", "rx packet", "rxok", "rx_pkt", "rx pkt"]) or h == "rx"):
                            rx_pkt_idx = idx
                        elif any(term in h for term in ["txbytes", "tx byte", "tx_octet", "tx octet", "txgoodbytes", "tx_bytes", "txbytes", "tx good bytes"]):
                            tx_bytes_idx = idx
                        elif any(term in h for term in ["rxbytes", "rx byte", "rx_octet", "rx octet", "rxgoodbytes", "rx_bytes", "rxbytes", "rx good bytes"]):
                            rx_bytes_idx = idx
                    
                    # Fallback to defaults if not resolved
                    if tx_pkt_idx == -1: tx_pkt_idx = 3
                    if rx_pkt_idx == -1: rx_pkt_idx = 5 if "rxgoodpkt" in headers else 4
                    
                    logger.debug(f"Dynamic stats column mapping: tx_pkt_idx={tx_pkt_idx}, rx_pkt_idx={rx_pkt_idx}, tx_bytes_idx={tx_bytes_idx}, rx_bytes_idx={rx_bytes_idx}")
                    
                    for row in rows[1:]:
                        cells = row.find_all("td")
                        if len(cells) > max(tx_pkt_idx, rx_pkt_idx):
                            port_name = cells[0].get_text(strip=True)
                            match = re.match(r"Port\s*(\d+)", port_name)
                            port_num = match.group(1) if match else (
                                port_name if "trunk" in port_name.lower() else port_name
                            )
                            for p in ports:
                                if p["port"] == port_name.replace("Port ", ""):
                                    tx_pkts = self._parse_counter(cells[tx_pkt_idx].get_text(strip=True))
                                    rx_pkts = self._parse_counter(cells[rx_pkt_idx].get_text(strip=True))
                                    
                                    p["tx_packets"] = tx_pkts
                                    p["rx_packets"] = rx_pkts
                                    
                                    if tx_bytes_idx != -1 and len(cells) > tx_bytes_idx:
                                        p["tx_bytes"] = self._parse_counter(cells[tx_bytes_idx].get_text(strip=True))
                                    else:
                                        p["tx_bytes"] = tx_pkts * 800
                                        
                                    if rx_bytes_idx != -1 and len(cells) > rx_bytes_idx:
                                        p["rx_bytes"] = self._parse_counter(cells[rx_bytes_idx].get_text(strip=True))
                                    else:
                                        p["rx_bytes"] = rx_pkts * 800
                                    break
                logger.debug(f"Parsed port statistical counter bytes: {ports}")

        # Scrape DHCP Snooping
        dhcp_snooping = self.scrape_dhcp_snooping()

        # Scrape IGMP snooping
        igmp = self.scrape_igmp()

        # Scrape Jumbo Frame
        jumbo_frame = self.scrape_jumbo_frame()

        return {
            "name": self.name,
            "ip": self.ip,
            "model": device_info.get("model", self.model),
            "mac": device_info.get("mac", ""),
            "uptime": device_info.get("uptime", ""),
            "firmware": device_info.get("firmware", ""),
            "ports": ports or self._fallback_ports(),
            "dhcp_snooping": dhcp_snooping,
            "igmp": igmp,
            "jumbo_frame": jumbo_frame,
            "timestamp": time.time(),
        }

    def scrape_dhcp_snooping(self):
        logger.debug(f"Scraping DHCP Snooping configurations for {self.ip}...")
        try:
            html = self._fetch("/dhcp_snooping.cgi?page=dump")
            if not html:
                return {"enabled": False, "ports": {}}
                
            soup = BeautifulSoup(html, "html.parser")
            
            # 1. Parse whether DHCP Snooping is enabled
            enabled = False
            enable_input = soup.find("input", {"name": "enable_dhcpsnp"})
            if enable_input and enable_input.has_attr("checked"):
                enabled = True
                
            # 2. Parse Trusted vs Untrusted ports
            ports_trust = {}
            static_form = soup.find("form", action=re.compile(r"page=static"))
            if static_form:
                inputs = static_form.find_all("input", class_="chkp")
                for inp in inputs:
                    inp_id = inp.get("id")
                    if inp_id:
                        label = static_form.find("label", {"for": inp_id})
                        if label:
                            label_text = label.get_text(strip=True)
                            port_name = label_text
                            if label_text.startswith("Port "):
                                port_name = label_text[5:]
                            
                            is_trusted = inp.has_attr("checked")
                            ports_trust[port_name] = "Trusted" if is_trusted else "Untrusted"
            
            res = {
                "enabled": enabled,
                "ports": ports_trust
            }
            logger.debug(f"DHCP Snooping scrape results: {res}")
            return res
        except Exception as e:
            logger.error(f"Failed to scrape DHCP Snooping for {self.ip}: {e}")
            return {"enabled": False, "ports": {}}

    def scrape_igmp(self):
        logger.debug(f"Scraping IGMP Snooping Multicast groups for {self.ip}...")
        try:
            html = self._fetch("/igmp.cgi?page=dump")
            if not html:
                return {"enabled": False, "entries": []}
                
            soup = BeautifulSoup(html, "html.parser")
            
            # 1. Parse whether IGMP is globally enabled
            enabled = False
            enable_input = soup.find("input", {"name": "enable_igmp"})
            if enable_input and enable_input.has_attr("checked"):
                enabled = True
                
            # 2. Parse Dump IGMP entry table
            entries = []
            table = None
            for t in soup.find_all("table"):
                text_content = t.get_text()
                if "IP Address" in text_content and "Port" in text_content and "VLAN ID" in text_content:
                    table = t
                    break
            
            if table:
                rows = table.find_all("tr")[1:] # skip header row
                for row in rows:
                    cells = row.find_all("td")
                    if len(cells) >= 3:
                        ip_addr = cells[0].get_text(strip=True)
                        ports = cells[1].get_text(strip=True)
                        vlan = cells[2].get_text(strip=True)
                        entries.append({
                            "vlan": vlan,
                            "ip": ip_addr,
                            "ports": ports
                        })
            res = {
                "enabled": enabled,
                "entries": entries
            }
            logger.debug(f"IGMP Snooping scrape results: {res}")
            return res
        except Exception as e:
            logger.error(f"Failed to scrape IGMP for {self.ip}: {e}")
            return {"enabled": False, "entries": []}

    def scrape_jumbo_frame(self):
        logger.debug(f"Scraping Jumbo Frame parameters for {self.ip}...")
        try:
            html = self._fetch("/fwd.cgi?page=jumboframe")
            if not html:
                return {"enabled": False, "size": "Disabled"}
                
            soup = BeautifulSoup(html, "html.parser")
            
            # 1. Parse whether Jumbo Frame is globally enabled
            enabled = False
            enable_input = soup.find("input", {"name": "enable_jumbo"})
            if enable_input:
                if enable_input.has_attr("checked"):
                    enabled = True
            else:
                # If there's no enable checkbox, it's always active at the selected size
                enabled = True
                
            # 2. Parse size value
            size_val = "Unknown"
            select = soup.find("select", {"name": "jumboframe"})
            if select:
                selected_option = select.find("option", selected=True)
                if not selected_option:
                    for opt in select.find_all("option"):
                        if opt.has_attr("selected"):
                            selected_option = opt
                            break
                if selected_option:
                    text_node = selected_option.find(string=True, recursive=False)
                    size_val = text_node.strip() if text_node else selected_option.get_text(strip=True)
                else:
                    options = select.find_all("option")
                    if options:
                        text_node = options[0].find(string=True, recursive=False)
                        size_val = text_node.strip() if text_node else options[0].get_text(strip=True)
                        
            res = {
                "enabled": enabled,
                "size": size_val
            }
            logger.debug(f"Jumbo Frame scrape results: {res}")
            return res
        except Exception as e:
            logger.error(f"Failed to scrape Jumbo Frame for {self.ip}: {e}")
            return {"enabled": False, "size": "Disabled"}

    def _fallback(self):
        return {
            "name": self.name,
            "ip": self.ip,
            "model": self.model,
            "mac": "",
            "uptime": "",
            "firmware": "",
            "ports": self._fallback_ports(),
            "dhcp_snooping": {"enabled": False, "ports": {}},
            "igmp": {"enabled": False, "entries": []},
            "jumbo_frame": {"enabled": False, "size": "Disabled"},
            "timestamp": time.time(),
        }

    def _download_backup_with_template(self, backup_cfg):
        if not self._opener:
            self._login()
            
        url_path = backup_cfg.get("url", "/config_back.cgi?cmd=conf_backup")
        referer_path = backup_cfg.get("referer_path", "/config_back.cgi")
        method = backup_cfg.get("method", "GET")
        
        headers = {"Referer": f"{self.base_url}{referer_path}"}
        
        post_data = None
        if method.upper() == "POST" and "post_data" in backup_cfg:
            post_data = urllib.parse.urlencode(backup_cfg["post_data"]).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            
        req = urllib.request.Request(f"{self.base_url}{url_path}", data=post_data, headers=headers)
        try:
            r = self._open_request_with_retry(req, timeout=45, max_retries=5)
            return r.read()
        except Exception as e:
            logger.error(f"Failed to download config backup via template on {self.ip}: {e}")
            raise e

    def _reboot_switch_with_template(self, reboot_cfg):
        if not self._opener:
            self._login()
            
        url_path = reboot_cfg.get("url", "/reboot.cgi")
        referer_path = reboot_cfg.get("referer_path", "/reboot.cgi")
        method = reboot_cfg.get("method", "POST")
        
        headers = {"Referer": f"{self.base_url}{referer_path}"}
        
        post_data = None
        if method.upper() == "POST" and "post_data" in reboot_cfg:
            post_data = urllib.parse.urlencode(reboot_cfg["post_data"]).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            
        req = urllib.request.Request(f"{self.base_url}{url_path}", data=post_data, headers=headers)
        try:
            r = self._open_request_with_retry(req, timeout=45, max_retries=5)
            return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            logger.error(f"Failed to reboot switch via template on {self.ip}: {e}")
            raise e

    def download_backup(self):
        with get_switch_lock(self.ip):
            template = self._load_template()
            if template:
                backup_cfg = template.get("backup", {})
                if backup_cfg:
                    try:
                        return self._download_backup_with_template(backup_cfg)
                    except Exception as e:
                        logger.error(f"Failed to download config backup via template for {self.ip}: {e}. Trying built-in backup...")
                    
            if not self._opener:
                self._login()
            headers = {"Referer": f"{self.base_url}/config_back.cgi"}
            req = urllib.request.Request(f"{self.base_url}/config_back.cgi?cmd=conf_backup", headers=headers)
            try:
                r = self._opener.open(req, timeout=15)
                return r.read()
            except Exception as e:
                logger.error(f"Failed to download config backup for {self.ip}: {e}")
                raise e

    def reboot_switch(self):
        with get_switch_lock(self.ip):
            template = self._load_template()
            if template:
                reboot_cfg = template.get("reboot", {})
                if reboot_cfg:
                    try:
                        return self._reboot_switch_with_template(reboot_cfg)
                    except Exception as e:
                        logger.error(f"Failed to reboot switch via template for {self.ip}: {e}. Trying built-in reboot...")
                    
            if not self._opener:
                self._login()
            headers = {
                "Referer": f"{self.base_url}/reboot.cgi",
                "Content-Type": "application/x-www-form-urlencoded"
            }
            data = urllib.parse.urlencode({
                "cmd": "reboot"
            }).encode()
            req = urllib.request.Request(f"{self.base_url}/reboot.cgi", data=data, headers=headers)
            try:
                r = self._opener.open(req, timeout=10)
                return r.read().decode("utf-8", errors="replace")
            except Exception as e:
                logger.error(f"Failed to trigger reboot for {self.ip}: {e}")
                raise e

    def _parse_mac_json_entries(self, data, mac_cfg):
        entries = []
        mapping = mac_cfg.get("mapping", {})
        mac_key = mapping.get("mac", "mac")
        port_key = mapping.get("port", "port")
        type_key = mapping.get("type", "type")
        vlan_key = mapping.get("vlan", "vlan")
        
        for item in data:
            mac = str(item.get(mac_key, ""))
            port = str(item.get(port_key, ""))
            m_type = str(item.get(type_key, "dynamic"))
            vlan = str(item.get(vlan_key, "1"))
            
            if mac:
                entries.append({
                    "mac": mac.upper(),
                    "type": "static" if "static" in m_type.lower() or m_type.lower() == "s" else "dynamic",
                    "port": f"Port {port}" if not port.lower().startswith("port") else port,
                    "vlan": vlan
                })
        return entries

    def _scrape_mac_table_with_template(self, mac_cfg):
        logger.debug(f"[_scrape_mac_table_with_template] Executing template-driven MAC scrape...")
        try:
            self._login()
        except Exception as e:
            logger.error(f"Login failed for MAC scrape on {self.ip}: {e}")
            return []

        url = mac_cfg.get("url", "/mac.cgi?page=fwd_tbl")
        format_type = mac_cfg.get("format", "html")
        entries = []
        try:
            # For JSON pagination, if page_parameter is set, append ?<param>=0 to satisfy strict uIP URL matching
            page_param = mac_cfg.get("page_parameter")
            fetch_url = url
            if format_type == "json" and page_param and "?" not in url:
                fetch_url = f"{url}?{page_param}=0"
                
            html = self._fetch(fetch_url)
            if not html:
                return []
                
            if format_type == "json":
                try:
                    import json
                    data = json.loads(html)
                    entries.extend(self._parse_mac_json_entries(data, mac_cfg))
                    
                    page_param = mac_cfg.get("page_parameter")
                    if page_param and len(data) > 0:
                        last_idx = data[-1].get(page_param)
                        perpage_val = int(mac_cfg.get("perpage_value", 3))
                        seen_indices = set()
                        if last_idx is not None:
                            seen_indices.add(str(last_idx))
                        while last_idx and len(data) >= perpage_val:
                            # Safely parse last_idx as an integer (hex or decimal)
                            try:
                                if isinstance(last_idx, str):
                                    if last_idx.startswith("0x"):
                                        val = int(last_idx, 16)
                                    elif any(c in 'abcdefABCDEF' for c in last_idx) or len(last_idx) == 4:
                                        val = int(last_idx, 16)
                                    else:
                                        val = int(last_idx, 10)
                                else:
                                    val = int(last_idx)
                                
                                # Increment by 1 as done in l2.js (l2CurrentEntry = s[s.length-1].idx + 1)
                                # and query in decimal representation since uIP atoi expects base 10
                                query_idx = str(val + 1)
                            except Exception:
                                query_idx = str(last_idx)

                            if query_idx in seen_indices:
                                logger.warning(f"Detected potential MAC table pagination loop at idx={query_idx}. Breaking.")
                                break
                            seen_indices.add(query_idx)

                            logger.info(f"Scraping MAC table next page with idx={query_idx} (raw: {last_idx}) for {self.ip}")
                            next_url = f"{url}?{page_param}={query_idx}"
                            next_html = self._fetch(next_url)
                            if not next_html:
                                break
                            data = json.loads(next_html)
                            if not data:
                                break
                            page_entries = self._parse_mac_json_entries(data, mac_cfg)
                            if not page_entries:
                                break
                            entries.extend(page_entries)
                            last_idx = data[-1].get(page_param)
                except Exception as e:
                    logger.error(f"Error scraping JSON MAC table via template on {self.ip}: {e}")
            else:
                soup = BeautifulSoup(html, "html.parser")
                entries.extend(self._parse_mac_table_rows(soup))
                
                page_param = mac_cfg.get("page_parameter")
                if page_param:
                    total_pages = 1
                    totalpage_label = soup.find(id="totalpage")
                    if totalpage_label:
                        try:
                            total_pages = int(totalpage_label.get_text(strip=True))
                        except ValueError:
                            total_pages = 1
                            
                    for page in range(2, total_pages + 1):
                        logger.info(f"Scraping MAC table page {page}/{total_pages} for {self.ip}")
                        page_html = self._fetch_mac_page_with_template(mac_cfg, page)
                        if page_html:
                            page_soup = BeautifulSoup(page_html, "html.parser")
                            entries.extend(self._parse_mac_table_rows(page_soup))
        except Exception as e:
            logger.error(f"Error scraping MAC table via template on {self.ip}: {e}")
            
        # De-duplicate entries by MAC address to prevent wrap-around pagination duplicates
        unique_entries = []
        seen_macs = set()
        for entry in entries:
            mac_upper = entry["mac"].upper()
            if mac_upper not in seen_macs:
                seen_macs.add(mac_upper)
                unique_entries.append(entry)
        return unique_entries

    def _fetch_mac_page_with_template(self, mac_cfg, page_num):
        if not self._opener:
            self._login()
            
        url = mac_cfg.get("url", "/mac.cgi?page=fwd_tbl")
        
        post_data = {}
        cmd_param = mac_cfg.get("cmd_parameter")
        if cmd_param:
            post_data[cmd_param] = mac_cfg.get("cmd_value", "goto")
            
        page_param = mac_cfg.get("page_parameter")
        if page_param:
            post_data[page_param] = str(page_num)
            
        perpage_param = mac_cfg.get("perpage_parameter")
        if perpage_param:
            post_data[perpage_param] = str(mac_cfg.get("perpage_value", "3"))
            
        data = urllib.parse.urlencode(post_data).encode()
        
        headers = {
            'Referer': f'{self.base_url}/',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        req = urllib.request.Request(f'{self.base_url}{url}', data=data, headers=headers)
        try:
            r = self._open_request_with_retry(req, timeout=45, max_retries=5)
            return r.read().decode('utf-8', errors='replace')
        except Exception as e:
            logger.error(f"Failed to fetch MAC table page {page_num} via template on {self.ip}: {e}")
            return None

    def scrape_mac_table(self):
        with get_switch_lock(self.ip):
            logger.debug(f"Scraping MAC address table for {self.ip}...")
            template = self._load_template()
            if template:
                mac_cfg = template.get("mac_table", {})
                if mac_cfg:
                    try:
                        res = self._scrape_mac_table_with_template(mac_cfg)
                        if res:
                            return res
                        logger.warning(f"Template MAC scrape returned no entries for {self.ip}. Trying built-in MAC scraper...")
                    except Exception as e:
                        logger.error(f"Error scraping MAC table via template on {self.ip}: {e}. Falling back to built-in scraper.")
            try:
                self._login()
            except Exception as e:
                logger.error(f"Login failed for MAC scrape on {self.ip}: {e}")
                return []

            entries = []
            try:
                # Page 1
                html = self._fetch("/mac.cgi?page=fwd_tbl")
                if not html:
                    return []
                
                # Parse page 1 and extract total pages
                total_pages = 1
                soup = BeautifulSoup(html, "html.parser")
                
                # Find totalpage label
                totalpage_label = soup.find(id="totalpage")
                if totalpage_label:
                    try:
                        total_pages = int(totalpage_label.get_text(strip=True))
                    except ValueError:
                        total_pages = 1
                
                # Parse table rows on page 1
                entries.extend(self._parse_mac_table_rows(soup))
                
                # If total_pages > 1, fetch additional pages
                for page in range(2, total_pages + 1):
                    logger.info(f"Scraping MAC table page {page}/{total_pages} for {self.ip}")
                    page_html = self._fetch_mac_page(page)
                    if page_html:
                        page_soup = BeautifulSoup(page_html, "html.parser")
                        entries.extend(self._parse_mac_table_rows(page_soup))
                        
                logger.debug(f"Found {len(entries)} MAC table entries on {self.ip}")
            except Exception as e:
                logger.error(f"Error scraping MAC table on {self.ip}: {e}")
                
            return entries

    def _fetch_mac_page(self, page_num):
        if not self._opener:
            self._login()
        
        data = urllib.parse.urlencode({
            'cmd': 'goto',
            'pageidx': str(page_num),
            'perpage': '3' # corresponds to 30 items per page
        }).encode()
        
        headers = {
            'Referer': f'{self.base_url}/',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        req = urllib.request.Request(f'{self.base_url}/mac.cgi?page=fwd_tbl', data=data, headers=headers)
        try:
            r = self._open_request_with_retry(req, timeout=45, max_retries=5)
            return r.read().decode('utf-8', errors='replace')
        except Exception as e:
            logger.error(f"Failed to fetch MAC table page {page_num} on {self.ip}: {e}")
            return None

    def _parse_mac_table_rows(self, soup):
        rows_data = []
        tables = soup.find_all("table")
        for table in tables:
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            if not headers:
                first_tr = table.find("tr")
                if first_tr:
                    headers = [c.get_text(strip=True).lower() for c in first_tr.find_all(["td", "th"])]
            if "mac address" in headers or "mac" in headers:
                # Dynamically locate columns by header names
                mac_idx = -1
                type_idx = -1
                port_idx = -1
                vlan_idx = -1
                
                for idx, h in enumerate(headers):
                    if "mac address" in h or h == "mac":
                        mac_idx = idx
                    elif "type" in h:
                        type_idx = idx
                    elif "port" in h:
                        port_idx = idx
                    elif "vlan" in h or h == "vlan id":
                        vlan_idx = idx
                
                if mac_idx != -1 and port_idx != -1:
                    for row in table.find_all("tr")[1:]:
                        cells = row.find_all("td")
                        if len(cells) > max(mac_idx, port_idx, type_idx, vlan_idx):
                            mac = cells[mac_idx].get_text(strip=True)
                            port = cells[port_idx].get_text(strip=True)
                            m_type = cells[type_idx].get_text(strip=True) if type_idx != -1 else "dynamic"
                            vlan = cells[vlan_idx].get_text(strip=True) if vlan_idx != -1 else "1"
                            
                            if ":" in mac and len(mac) >= 12:
                                rows_data.append({
                                    "mac": mac.upper(),
                                    "type": m_type,
                                    "port": port,
                                    "vlan": vlan
                                })
                break
        return rows_data

    def scrape_transceiver(self):
        with get_switch_lock(self.ip):
            logger.debug(f"Scraping SFP+ Transceiver diagnostics for {self.ip}...")
            try:
                self._login()
            except Exception as e:
                logger.error(f"Login failed for Transceiver scrape on {self.ip}: {e}")
                return None

            try:
                html = self._fetch("/transceiver.cgi")
                if not html:
                    return None
                
                # Clean the malformed <th>...</td> tags by replacing '<th' with '<td'
                cleaned_html = html.replace("<th", "<td").replace("</th>", "</td>")
                soup = BeautifulSoup(cleaned_html, "html.parser")
                info = {}
                
                table = soup.find("table", class_="infotbl")
                if not table:
                    table = soup.find("table")
                if not table:
                    return None
                    
                for row in table.find_all("tr"):
                    cells = row.find_all(["td", "th"])
                    if len(cells) >= 2:
                        key = cells[0].get_text(strip=True).rstrip(":")
                        val = cells[1].get_text(strip=True)
                        
                        if cells[1].has_attr("id"):
                            id_name = cells[1]["id"]
                            info[id_name + "_raw"] = val
                        else:
                            norm_key = key.replace(" ", "_").replace("(", "").replace(")", "").replace("/", "_").lower()
                            info[norm_key] = val

                import math
                def decode_ddmi(raw_str, multiplier, is_power=False):
                    try:
                        parts = raw_str.split('-')
                        if len(parts) == 2:
                            val = int(parts[0]) * 256 + int(parts[1])
                            decoded = val * multiplier
                            if is_power:
                                if decoded <= 0:
                                    return "-inf dBm"
                                dbm = 10 * math.log10(decoded)
                                return f"{dbm:.2f} dBm"
                            return decoded
                    except Exception:
                        pass
                    return raw_str

                if "temp_raw" in info:
                    raw = info["temp_raw"]
                    val = decode_ddmi(raw, 0.00391)
                    info["temperature"] = f"{val:.2f} °C" if isinstance(val, (int, float)) else raw
                if "voltage_raw" in info:
                    raw = info["voltage_raw"]
                    val = decode_ddmi(raw, 0.0001)
                    info["voltage"] = f"{val:.2f} V" if isinstance(val, (int, float)) else raw
                if "current_raw" in info:
                    raw = info["current_raw"]
                    val = decode_ddmi(raw, 0.002)
                    info["current"] = f"{val:.2f} mA" if isinstance(val, (int, float)) else raw
                if "txpower_raw" in info:
                    raw = info["txpower_raw"]
                    info["tx_power"] = decode_ddmi(raw, 0.0001, is_power=True)
                if "rxpower_raw" in info:
                    raw = info["rxpower_raw"]
                    info["rx_power"] = decode_ddmi(raw, 0.0001, is_power=True)
                    
                logger.debug(f"SFP+ Transceiver diagnostics scrape results on {self.ip}: {info}")
                return info
            except Exception as e:
                logger.error(f"Error scraping Transceiver on {self.ip}: {e}")
                return None

    def _scrape_with_template(self, template):
        logger.debug(f"[_scrape_with_template] Executing template-driven scrape...")
        self._login()

        ports = []
        device_info = {}
        port_states = {}

        # 1. Scraping global device info
        info_cfg = template.get("device_info", {})
        if info_cfg:
            url = info_cfg.get("url", "/info.cgi")
            info_html = self._fetch(url)
            if info_html:
                if info_cfg.get("format") == "json" or template.get("format") == "json":
                    try:
                        import json
                        data = json.loads(info_html)
                        mappings = info_cfg.get("mappings", {})
                        for key, json_key in mappings.items():
                            device_info[key] = str(data.get(json_key, ""))
                    except Exception as e:
                        logger.error(f"Error parsing JSON device_info: {e}")
                else:
                    soup = BeautifulSoup(info_html, "html.parser")
                    tables = soup.find_all("table")
                    if tables and info_cfg.get("method") == "key_value_grid":
                        for row in tables[0].find_all("tr"):
                            cells = row.find_all(["td", "th"])
                            for i in range(0, len(cells), 2):
                                if i + 1 >= len(cells):
                                    break
                                label = cells[i].get_text(strip=True).rstrip(":")
                                value = cells[i + 1].get_text(strip=True)
                                
                                mappings = info_cfg.get("mappings", {})
                                for key, label_term in mappings.items():
                                    if label_term.lower() in label.lower():
                                        if key == "uptime":
                                            device_info["uptime"] = self._fmt_uptime(value)
                                        else:
                                            device_info[key] = value

        # 2. Scraping administrative port states or standard link statuses
        ports_cfg = template.get("ports", {})
        if ports_cfg:
            url = ports_cfg.get("url", "/info.cgi")
            source = ports_cfg.get("source", "info_table")
            
            html = self._fetch(url)
            if html:
                if ports_cfg.get("format") == "json" or template.get("format") == "json":
                    try:
                        import json
                        port_list = json.loads(html)
                        list_key = ports_cfg.get("list_key")
                        if list_key and isinstance(port_list, dict):
                            port_list = port_list.get(list_key, [])
                            
                        mapping = ports_cfg.get("mapping", {})
                        for port_obj in port_list:
                            port_num_key = mapping.get("port", "portNum")
                            port_num = str(port_obj.get(port_num_key, ""))
                            
                            admin_key = mapping.get("admin_state", "enabled")
                            admin_enabled = port_obj.get(admin_key, 1)
                            
                            if admin_enabled == 0 or admin_enabled is False:
                                status_val = "disable"
                                link_val = "Disabled"
                                speed_val = "Disabled"
                                duplex_val = "Disabled"
                            else:
                                link_key = mapping.get("link", "link")
                                link_code = port_obj.get(link_key, 0)
                                if isinstance(link_code, (int, float)):
                                    link_code = int(link_code)
                                    if link_code == 0:
                                        status_val = "down"
                                        link_val = "Link Down"
                                        speed_val = "Auto"
                                        duplex_val = ""
                                    else:
                                        status_val = "up"
                                        link_map = {
                                            1: ("100M", "Half"),
                                            2: ("100M", "Full"),
                                            3: ("1000M", "Half"),
                                            4: ("1000M", "Full"),
                                            5: ("2.5G", "Full"),
                                            6: ("5G", "Full"),
                                            7: ("10G", "Full")
                                        }
                                        speed_val, duplex_val = link_map.get(link_code, ("Auto", "Full"))
                                        link_val = f"Link Up"
                                else:
                                    link_str = str(link_code)
                                    status_val = "up" if "up" in link_str.lower() else "down"
                                    link_val = link_str
                                    speed_val = str(port_obj.get(mapping.get("speed", "speed"), "Auto"))
                                    duplex_val = str(port_obj.get(mapping.get("duplex", "duplex"), "Full"))
                                    
                            flow_key = mapping.get("flow_control", "flow")
                            flow_val = str(port_obj.get(flow_key, ""))
                            
                            tx_pkts = self._parse_counter(str(port_obj.get(mapping.get("tx_packets", "txG"), "0")))
                            rx_pkts = self._parse_counter(str(port_obj.get(mapping.get("rx_packets", "rxG"), "0")))
                            tx_bytes = self._parse_counter(str(port_obj.get(mapping.get("tx_bytes", "txB"), "0")))
                            rx_bytes = self._parse_counter(str(port_obj.get(mapping.get("rx_bytes", "rxB"), "0")))
                            
                            ports.append({
                                "port": port_num,
                                "status": status_val,
                                "link": link_val,
                                "speed": speed_val,
                                "duplex": duplex_val,
                                "flow_control": flow_val,
                                "tx_packets": tx_pkts,
                                "rx_packets": rx_pkts,
                                "tx_bytes": tx_bytes,
                                "rx_bytes": rx_bytes
                            })
                    except Exception as e:
                        logger.error(f"Error parsing JSON ports: {e}")
                else:
                    soup = BeautifulSoup(html, "html.parser")
                    columns = ports_cfg.get("columns", {})
                    
                    if source == "info_table":
                        # Horaco style: Ports status from second table in info page
                        tables = soup.find_all("table")
                        if len(tables) >= 2:
                            for row in tables[1].find_all("tr")[1:]:
                                cells = row.find_all("td")
                                port_idx = columns.get("port", 0)
                                link_idx = columns.get("link", 1)
                                duplex_idx = columns.get("duplex", 2)
                                speed_idx = columns.get("speed", 3)
                                flow_idx = columns.get("flow_control", 4)
                                
                                if len(cells) > max(port_idx, link_idx, duplex_idx, speed_idx):
                                    port_name = cells[port_idx].get_text(strip=True)
                                    match = re.match(r"Port\s*(\d+)", port_name)
                                    port_num = match.group(1) if match else port_name
                                    
                                    status_val = "up" if "Up" in cells[link_idx].get_text(strip=True) else "down"
                                    link_val = cells[link_idx].get_text(strip=True)
                                    
                                    ports.append({
                                        "port": port_num,
                                        "status": status_val,
                                        "link": link_val,
                                        "speed": cells[speed_idx].get_text(strip=True),
                                        "duplex": cells[duplex_idx].get_text(strip=True),
                                        "flow_control": cells[flow_idx].get_text(strip=True) if len(cells) > flow_idx else "",
                                        "tx_packets": 0, "rx_packets": 0, "tx_bytes": 0, "rx_bytes": 0
                                    })
                    
                    elif source == "port_table":
                        # KeepLink style: ports link from /port.cgi table
                        tables = soup.find_all("table")
                        status_table = None
                        match_keyword = ports_cfg.get("port_table_match", "Actual")
                        for t in tables:
                            rows = t.find_all("tr")
                            if len(rows) >= 3:
                                text_content = t.get_text()
                                if match_keyword in text_content and "Port 1" in text_content:
                                    status_table = t
                                    break
                        
                        if status_table:
                            rows = status_table.find_all("tr")
                            start_idx = 0
                            for idx, r in enumerate(rows):
                                cells = r.find_all(["td", "th"])
                                cells_text = [c.get_text(strip=True) for c in cells]
                                if len(cells_text) > 0 and "Port 1" in cells_text[0]:
                                    start_idx = idx
                                    break
                                    
                            if start_idx > 0:
                                for row in rows[start_idx:]:
                                    cells = row.find_all(["td", "th"])
                                    port_idx = columns.get("port", 0)
                                    admin_idx = columns.get("admin_state", 1)
                                    link_idx = columns.get("actual_link", 3)
                                    flow_idx = columns.get("flow_control", 5)
                                    
                                    if len(cells) > max(port_idx, admin_idx, link_idx):
                                        port_name = cells[port_idx].get_text(strip=True)
                                        match = re.match(r"Port\s*(\d+)", port_name)
                                        port_num = match.group(1) if match else port_name
                                        
                                        admin_state = cells[admin_idx].get_text(strip=True).strip().lower()
                                        actual_link = cells[link_idx].get_text(strip=True).strip()
                                        
                                        if admin_state in ["disable", "disabled"]:
                                            status_val = "disable"
                                            link_val = "Disabled"
                                            speed_val = "Disabled"
                                            duplex_val = "Disabled"
                                        else:
                                            status_val = "down" if "down" in actual_link.lower() else "up"
                                            link_val = "Link Up" if status_val == "up" else "Link Down"
                                            speed_val = "Auto"
                                            duplex_val = "Full"
                                            
                                            if status_val == "up":
                                                speed_match = re.match(r"(\d+G?)(Full|Half)?", actual_link, re.IGNORECASE)
                                                if speed_match:
                                                    speed_num = speed_match.group(1)
                                                    if "G" in speed_num.upper():
                                                        speed_val = speed_num.upper()
                                                    else:
                                                        speed_val = f"{speed_num}M"
                                                    duplex_val = speed_match.group(2) or "Full"
                                                    if duplex_val.lower() == "full":
                                                        duplex_val = "Full"
                                                    elif duplex_val.lower() == "half":
                                                        duplex_val = "Half"
                                                else:
                                                    speed_val = actual_link
                                            else:
                                                speed_val = "Auto"
                                                duplex_val = ""
                                                
                                        ports.append({
                                            "port": port_num,
                                            "status": status_val,
                                            "link": link_val,
                                            "speed": speed_val,
                                            "duplex": duplex_val,
                                            "flow_control": cells[flow_idx].get_text(strip=True) if len(cells) > flow_idx else "",
                                            "tx_packets": 0, "rx_packets": 0, "tx_bytes": 0, "rx_bytes": 0
                                        })

        # 3. Scraping transmission statistics
        stats_cfg = template.get("statistics", {})
        has_stats = len(ports) > 0 and any(p.get("tx_packets", 0) > 0 for p in ports)
        if stats_cfg and not has_stats:
            url = stats_cfg.get("url", "/port.cgi?page=stats")
            stats_html = self._fetch(url)
            if stats_html:
                soup = BeautifulSoup(stats_html, "html.parser")
                stats_table = soup.find("table")
                if stats_table:
                    rows = stats_table.find_all("tr")
                    if len(rows) > 0:
                        first_row = rows[0]
                        headers = [c.get_text(strip=True).strip().lower() for c in first_row.find_all(["td", "th"])]
                        
                        tx_pkt_idx = -1
                        rx_pkt_idx = -1
                        tx_bytes_idx = -1
                        rx_bytes_idx = -1
                        
                        # Match lists
                        terms = stats_cfg.get("terms", {})
                        tx_pkt_terms = terms.get("tx_packets", ["txgoodpkt", "txpackets", "tx packet", "txok", "tx_pkt", "tx pkt"])
                        rx_pkt_terms = terms.get("rx_packets", ["rxgoodpkt", "rxpackets", "rx packet", "rxok", "rx_pkt", "rx pkt"])
                        tx_bytes_terms = terms.get("tx_bytes", ["txbytes", "tx byte", "tx_octet", "tx octet", "txgoodbytes", "tx_bytes"])
                        rx_bytes_terms = terms.get("rx_bytes", ["rxbytes", "rx byte", "rx_octet", "rx octet", "rxgoodbytes", "rx_bytes"])
                        
                        for idx, h in enumerate(headers):
                            is_bytes = any(term in h for term in ["byte", "octet"])
                            if not is_bytes and (any(term in h for term in tx_pkt_terms) or h == "tx"):
                                tx_pkt_idx = idx
                            elif not is_bytes and (any(term in h for term in rx_pkt_terms) or h == "rx"):
                                rx_pkt_idx = idx
                            elif any(term in h for term in tx_bytes_terms):
                                tx_bytes_idx = idx
                            elif any(term in h for term in rx_bytes_terms):
                                rx_bytes_idx = idx
                                
                        if tx_pkt_idx == -1: tx_pkt_idx = 3
                        if rx_pkt_idx == -1: rx_pkt_idx = 5 if "rxgoodpkt" in headers else 4
                        
                        for row in rows[1:]:
                            cells = row.find_all("td")
                            if len(cells) > max(tx_pkt_idx, rx_pkt_idx):
                                port_name = cells[0].get_text(strip=True)
                                for p in ports:
                                    if p["port"] == port_name.replace("Port ", ""):
                                        tx_pkts = self._parse_counter(cells[tx_pkt_idx].get_text(strip=True))
                                        rx_pkts = self._parse_counter(cells[rx_pkt_idx].get_text(strip=True))
                                        p["tx_packets"] = tx_pkts
                                        p["rx_packets"] = rx_pkts
                                        
                                        if tx_bytes_idx != -1 and len(cells) > tx_bytes_idx:
                                            p["tx_bytes"] = self._parse_counter(cells[tx_bytes_idx].get_text(strip=True))
                                        else:
                                            p["tx_bytes"] = tx_pkts * 800
                                            
                                        if rx_bytes_idx != -1 and len(cells) > rx_bytes_idx:
                                            p["rx_bytes"] = self._parse_counter(cells[rx_bytes_idx].get_text(strip=True))
                                        else:
                                            p["rx_bytes"] = rx_pkts * 800
                                        break

        # 4. Scraping DHCP Snooping
        dhcp_snooping = {"enabled": False, "ports": {}}
        dhcp_cfg = template.get("dhcp_snooping", {})
        if dhcp_cfg:
            url = dhcp_cfg.get("url", "/dhcp_snooping.cgi?page=dump")
            html = self._fetch(url)
            if html:
                soup = BeautifulSoup(html, "html.parser")
                
                enable_input_name = dhcp_cfg.get("enable_input_name", "enable_dhcpsnp")
                enable_input = soup.find("input", {"name": enable_input_name})
                if enable_input and enable_input.has_attr("checked"):
                    dhcp_snooping["enabled"] = True
                    
                ports_trust = {}
                form_action = dhcp_cfg.get("ports_form_action", "page=static")
                static_form = soup.find("form", action=re.compile(form_action))
                if static_form:
                    chk_class = dhcp_cfg.get("trust_checkbox_class", "chkp")
                    inputs = static_form.find_all("input", class_=chk_class)
                    for inp in inputs:
                        inp_id = inp.get("id")
                        if inp_id:
                            label = static_form.find("label", {"for": inp_id})
                            if label:
                                label_text = label.get_text(strip=True)
                                port_name = label_text[5:] if label_text.startswith("Port ") else label_text
                                is_trusted = inp.has_attr("checked")
                                ports_trust[port_name] = "Trusted" if is_trusted else "Untrusted"
                dhcp_snooping["ports"] = ports_trust

        # 5. Scraping IGMP Snooping
        igmp = {"enabled": False, "entries": []}
        igmp_cfg = template.get("igmp", {})
        if igmp_cfg:
            url = igmp_cfg.get("url", "/igmp.cgi?page=dump")
            html = self._fetch(url)
            if html:
                soup = BeautifulSoup(html, "html.parser")
                
                enable_input_name = igmp_cfg.get("enable_input_name", "enable_igmp")
                enable_input = soup.find("input", {"name": enable_input_name})
                if enable_input and enable_input.has_attr("checked"):
                    igmp["enabled"] = True
                    
                entries = []
                table = None
                keywords = igmp_cfg.get("table_header_keywords", ["IP Address", "Port", "VLAN ID"])
                for t in soup.find_all("table"):
                    text_content = t.get_text()
                    if all(k in text_content for k in keywords):
                        table = t
                        break
                        
                if table:
                    rows = table.find_all("tr")[1:]
                    for row in rows:
                        cells = row.find_all("td")
                        if len(cells) >= 3:
                            ip_addr = cells[0].get_text(strip=True)
                            ports_text = cells[1].get_text(strip=True)
                            vlan = cells[2].get_text(strip=True)
                            entries.append({
                                "vlan": vlan,
                                "ip": ip_addr,
                                "ports": ports_text
                            })
                igmp["entries"] = entries

        # 6. Scraping Jumbo Frame
        jumbo_frame = {"enabled": False, "size": "Disabled"}
        jumbo_cfg = template.get("jumbo_frame", {})
        if jumbo_cfg:
            url = jumbo_cfg.get("url", "/fwd.cgi?page=jumboframe")
            html = self._fetch(url)
            if html:
                if jumbo_cfg.get("format") == "json" or template.get("format") == "json":
                    try:
                        import json
                        data = json.loads(html)
                        max_mtu = 1500
                        enabled = False
                        for port_obj in data:
                            mtu_val = port_obj.get("mtu", "1500")
                            try:
                                if isinstance(mtu_val, str):
                                    if mtu_val.lower().startswith("0x"):
                                        val = int(mtu_val, 16)
                                    else:
                                        val = int(mtu_val)
                                else:
                                    val = int(mtu_val)
                                if val > max_mtu:
                                    max_mtu = val
                            except Exception:
                                pass
                        if max_mtu > 1522:
                            enabled = True
                            size_val = f"{max_mtu}Bytes"
                        else:
                            enabled = False
                            size_val = "Disabled"
                        jumbo_frame = {"enabled": enabled, "size": size_val}
                    except Exception as e:
                        logger.error(f"Error parsing JSON jumbo_frame: {e}")
                else:
                    soup = BeautifulSoup(html, "html.parser")
                    
                    enabled = False
                    enable_input_name = jumbo_cfg.get("enable_input_name", "enable_jumbo")
                    enable_input = soup.find("input", {"name": enable_input_name})
                    if enable_input:
                        if enable_input.has_attr("checked"):
                            enabled = True
                    else:
                        enabled = True
                        
                    size_val = "Unknown"
                    select_name = jumbo_cfg.get("select_name", "jumboframe")
                    select = soup.find("select", {"name": select_name})
                    if select:
                        selected_option = select.find("option", selected=True)
                        if not selected_option:
                            for opt in select.find_all("option"):
                                if opt.has_attr("selected"):
                                    selected_option = opt
                                    break
                        if selected_option:
                            text_node = selected_option.find(string=True, recursive=False)
                            size_val = text_node.strip() if text_node else selected_option.get_text(strip=True)
                        else:
                            options = select.find_all("option")
                            if options:
                                text_node = options[0].find(string=True, recursive=False)
                                size_val = text_node.strip() if text_node else options[0].get_text(strip=True)
                    jumbo_frame = {"enabled": enabled, "size": size_val}

        return {
            "name": self.name,
            "ip": self.ip,
            "model": device_info.get("model", self.model),
            "mac": device_info.get("mac", ""),
            "uptime": device_info.get("uptime", ""),
            "firmware": device_info.get("firmware", ""),
            "ports": ports or self._fallback_ports(),
            "dhcp_snooping": dhcp_snooping,
            "igmp": igmp,
            "jumbo_frame": jumbo_frame,
            "timestamp": time.time(),
        }

    def _fallback_ports(self):
        return [{"port": str(i), "status": "unknown", "speed": "",
                 "link": "Unknown", "duplex": "", "flow_control": "",
                 "tx_packets": 0, "rx_packets": 0,
                 "tx_bytes": 0, "rx_bytes": 0}
                for i in range(1, self.port_count + 1)]


class OVSScraper:
    def __init__(self, config):
        self.name = config["name"]
        self.ip = config["ip"]
        self.username = config.get("username", "ovs-monitor")
        self.password = config.get("password", "")
        self.bridge = config.get("bridge", "vmbr0")
        self.port_count = config.get("port_count", 24)
        self.model = config.get("model", "openvswitch")
        self._cached_data = None
        self._cache_time = 0

    def _get_data(self):
        import time
        if self._cached_data and (time.time() - self._cache_time < 5.0):
            return self._cached_data
        data = self._scrape_ovs()
        self._cached_data = data
        self._cache_time = time.time()
        return data

    def scrape(self):
        return self._get_data()

    def scrape_mac_table(self):
        data = self._get_data()
        return data.get("mac_table", [])

    def scrape_dhcp_snooping(self):
        return {"enabled": False, "ports": {}}

    def scrape_igmp(self):
        return {"enabled": False, "entries": []}

    def scrape_jumbo_frame(self):
        return {"enabled": False, "size": "Disabled"}

    def scrape_transceiver(self):
        return None

    def download_backup(self):
        return b""

    def reboot_switch(self):
        return "Not supported for virtual switches."

    def _scrape_ovs(self):
        import paramiko
        import json
        import time

        remote_script = """
import subprocess
import json
import re
import os

def run_cmd(cmd):
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return res.stdout, res.stderr
    except Exception as e:
        return "", str(e)

data = {}

# 1. OVS VSCTL show
vsctl_out, _ = run_cmd("sudo ovs-vsctl show")
ovs_ver = re.search(r"ovs_version:\\s*\\\"([^\\\"]+)\\\"", vsctl_out)
data["ovs_version"] = ovs_ver.group(1) if ovs_ver else "3.5.0"

# 2. Find bridges
bridges = re.findall(r"Bridge\\s+(\\S+)", vsctl_out)
data["bridges"] = bridges
bridge = "{bridge}"

# 3. OVS OFCTL show <bridge>
ofctl_out, _ = run_cmd("sudo ovs-ofctl show " + bridge)
data["ofctl"] = ofctl_out

# 4. OVS APPCTL fdb/show <bridge>
fdb_out, _ = run_cmd("sudo ovs-appctl fdb/show " + bridge)
data["fdb"] = fdb_out

# 5. VMID to name mappings
vm_names = {}
pct_out, _ = run_cmd("sudo pct list")
for line in pct_out.splitlines():
    line = line.strip()
    if not line or line.startswith("VMID"):
        continue
    parts = line.split()
    if len(parts) >= 3:
        vm_names[parts[0]] = "LXC " + parts[0] + " (" + parts[-1] + ")"

qm_out, _ = run_cmd("sudo qm list")
for line in qm_out.splitlines():
    line = line.strip()
    if not line or "VMID" in line:
        continue
    parts = line.split()
    if len(parts) >= 3:
        vm_names[parts[0]] = "VM " + parts[0] + " (" + parts[1] + ")"

data["vm_names"] = vm_names

# Get uptime
uptime_str = "unknown"
try:
    with open("/proc/uptime") as f:
        uptime_seconds = float(f.read().split()[0])
    days = int(uptime_seconds // 86400)
    hours = int((uptime_seconds % 86400) // 3600)
    minutes = int((uptime_seconds % 3600) // 60)
    if days > 0:
        uptime_str = str(days) + "d " + str(hours) + "h " + str(minutes) + "m"
    elif hours > 0:
        uptime_str = str(hours) + "h " + str(minutes) + "m"
    else:
        uptime_str = str(minutes) + "m"
except:
    pass
data["uptime"] = uptime_str

# bridge mac
bridge_mac = ""
try:
    with open("/sys/class/net/" + bridge + "/address") as f:
        bridge_mac = f.read().strip().upper()
except:
    pass
data["mac"] = bridge_mac

# 6. Read interface stats
interfaces = {}
for dev in os.listdir("/sys/class/net/"):
    stat_path = "/sys/class/net/" + dev + "/statistics"
    if os.path.exists(stat_path):
        stats = {}
        for sfile in ["tx_bytes", "rx_bytes", "tx_packets", "rx_packets"]:
            try:
                with open(stat_path + "/" + sfile) as f:
                    stats[sfile] = int(f.read().strip())
            except:
                stats[sfile] = 0
                
        speed = 0
        try:
            with open("/sys/class/net/" + dev + "/speed") as f:
                speed = int(f.read().strip())
        except:
            pass
            
        operstate = "unknown"
        try:
            with open("/sys/class/net/" + dev + "/operstate") as f:
                operstate = f.read().strip()
        except:
            pass
            
        interfaces[dev] = {
            "speed": speed,
            "operstate": operstate,
            "stats": stats
        }
data["interfaces"] = interfaces

print(json.dumps(data))
""".replace("{bridge}", self.bridge)

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(self.ip, username=self.username, password=self.password, timeout=15)
            stdin, stdout, stderr = client.exec_command("python3")
            stdin.write(remote_script)
            stdin.close()

            out = stdout.read().decode('utf-8')
            err = stderr.read().decode('utf-8')
            client.close()

            if err.strip() and not out.strip():
                logger.error(f"[OVSScraper] Remote python error on {self.ip}: {err}")
                raise Exception(err)

            res_data = json.loads(out.strip())
            return self._parse_scraped_data(res_data)
        except Exception as e:
            logger.error(f"[OVSScraper] Failed to scrape OVS switch {self.name} ({self.ip}): {e}")
            return self._fallback()

    def _parse_scraped_data(self, data):
        import re
        import time

        vm_names = data.get("vm_names", {})
        ofctl = data.get("ofctl", "")
        fdb = data.get("fdb", "")
        interfaces = data.get("interfaces", {})

        port_to_iface = {}
        for line in ofctl.splitlines():
            line = line.strip()
            m = re.match(r"(\d+|LOCAL)\((\S+)\):", line)
            if m:
                port_num = m.group(1)
                iface_name = m.group(2)
                port_to_iface[port_num] = iface_name

        ports = []
        port_to_key = {}
        vm_mac_map = {}

        for port_num, iface in port_to_iface.items():
            m = re.search(r"(?:veth|tap|fwln)(\d+)", iface)
            is_vm = False
            vm_name = None
            friendly_name = iface

            if m:
                vmid = m.group(1)
                is_vm = True
                if vmid in vm_names:
                    full_name = vm_names[vmid]
                    short_m = re.search(r"\(([^)]+)\)", full_name)
                    short_name = short_m.group(1) if short_m else vmid
                    friendly_name = f"LXC {vmid} ({short_name})" if "LXC" in full_name else f"VM {vmid} ({short_name})"
                    vm_name = short_name
                else:
                    friendly_name = f"VM/LXC {vmid}"
                    vm_name = f"VM {vmid}"
            else:
                vm_name = None

            port_to_key[port_num] = (port_num, friendly_name if is_vm else None)

            istat = interfaces.get(iface, {})
            speed_val = istat.get("speed", 0)
            operstate = istat.get("operstate", "up")

            if speed_val >= 10000:
                speed_str = "10G"
            elif speed_val >= 1000:
                if speed_val % 1000 == 0:
                    speed_str = f"{int(speed_val/1000)}G"
                else:
                    speed_str = f"{speed_val/1000:.1f}".rstrip("0").rstrip(".") + "G"
            elif speed_val > 0:
                speed_str = f"{speed_val}M"
            else:
                if iface.startswith("veth") or iface.startswith("tap") or iface.startswith("fwln"):
                    speed_str = "10G"
                else:
                    speed_str = "1G"

            status = "up" if operstate.lower() in ["up", "unknown"] else "down"
            stats = istat.get("stats", {})

            ports.append({
                "port": port_num,
                "status": status,
                "link": "Link Up" if status == "up" else "Link Down",
                "speed": speed_str,
                "duplex": "Full" if status == "up" else "",
                "flow_control": "",
                "tx_packets": stats.get("tx_packets", 0),
                "rx_packets": stats.get("rx_packets", 0),
                "tx_bytes": stats.get("tx_bytes", 0),
                "rx_bytes": stats.get("rx_bytes", 0),
                "vm_name": vm_name,
                "interface": iface
            })

        # Parse MAC table
        mac_table = []
        for line in fdb.splitlines():
            line = line.strip()
            if not line or "VLAN" in line:
                continue
            parts = line.split()
            if len(parts) >= 3:
                port_num = parts[0]
                vlan = parts[1]
                mac = parts[2].upper()

                p_key, vm_label = port_to_key.get(port_num, (port_num, None))

                mac_table.append({
                    "mac": mac,
                    "type": "dynamic" if port_num != "LOCAL" else "static",
                    "port": p_key,
                    "vlan": vlan
                })

                if vm_label:
                    clean_mac = mac.replace(":", "").upper()
                    vm_mac_map[clean_mac] = vm_label

        return {
            "name": self.name,
            "ip": self.ip,
            "model": "Open vSwitch",
            "mac": data.get("mac", ""),
            "uptime": data.get("uptime", ""),
            "firmware": data.get("ovs_version", ""),
            "ports": ports,
            "mac_table": mac_table,
            "vm_mac_map": vm_mac_map,
            "dhcp_snooping": {"enabled": False, "ports": {}},
            "igmp": {"enabled": False, "entries": []},
            "jumbo_frame": {"enabled": False, "size": "Disabled"},
            "timestamp": time.time(),
        }

    def _fallback(self):
        import time
        return {
            "name": self.name,
            "ip": self.ip,
            "model": "Open vSwitch",
            "mac": "",
            "uptime": "",
            "firmware": "",
            "ports": [{"port": str(i), "status": "unknown", "speed": "",
                       "link": "Unknown", "duplex": "", "flow_control": "",
                       "tx_packets": 0, "rx_packets": 0,
                       "tx_bytes": 0, "rx_bytes": 0}
                      for i in range(1, self.port_count + 1)],
            "mac_table": [],
            "vm_mac_map": {},
            "dhcp_snooping": {"enabled": False, "ports": {}},
            "igmp": {"enabled": False, "entries": []},
            "jumbo_frame": {"enabled": False, "size": "Disabled"},
            "timestamp": time.time(),
        }


class FritzBoxScraper:
    def __init__(self, config):
        self.name = config["name"]
        self.ip = config["ip"]
        self.username = config.get("username", "admin")
        self.password = config.get("password", "")
        self.model = config.get("model", "fritzbox")
        self._cached_data = None
        self._cache_time = 0

    def _get_data(self):
        import time
        if self._cached_data and (time.time() - self._cache_time < 5.0):
            return self._cached_data
        data = self._scrape_fritzbox()
        self._cached_data = data
        self._cache_time = time.time()
        return data

    def scrape(self):
        return self._get_data()

    def scrape_mac_table(self):
        data = self._get_data()
        return data.get("mac_table", [])

    def scrape_dhcp_snooping(self):
        return {"enabled": False, "ports": {}}

    def scrape_igmp(self):
        return {"enabled": False, "entries": []}

    def scrape_jumbo_frame(self):
        return {"enabled": False, "size": "Disabled"}

    def scrape_transceiver(self):
        return None

    def download_backup(self):
        return b""

    def reboot_switch(self):
        return "Not supported."

    def _soap_request(self, service, action, body_content=""):
        import requests
        from requests.auth import HTTPDigestAuth

        service_part = service.split(":")[-2].lower()
        if "wancommoninterfaceconfig" in service_part:
            control_path = "wancommonifconfig1"
        elif "lanethernetinterfaceconfig" in service_part:
            control_path = "lanethernetifcfg"
        else:
            control_path = service_part

        ip = self.ip
        if ":" not in ip:
            ip = f"{ip}:49000"

        url = f"http://{ip}/upnp/control/{control_path}"
        
        headers = {
            "SoapAction": f"{service}#{action}",
            "Content-Type": 'text/xml; charset="utf-8"'
        }
        
        envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:{action} xmlns:u="{service}">
      {body_content}
    </u:{action}>
  </s:Body>
</s:Envelope>"""
        
        auth = HTTPDigestAuth(self.username, self.password) if self.username else None
        r = requests.post(url, data=envelope, headers=headers, auth=auth, timeout=5)
        r.raise_for_status()
        return r.text

    def _scrape_fritzbox(self):
        import xml.etree.ElementTree as ET
        import time
        import requests
        from requests.auth import HTTPDigestAuth

        try:
            def strip_ns(el):
                for elem in el.iter():
                    if '}' in elem.tag:
                        elem.tag = elem.tag.split('}', 1)[1]
                return el

            # 1. Device Info
            uptime = ""
            model = "FRITZ!Box"
            firmware = ""
            mac = ""
            try:
                res_info = self._soap_request("urn:dslforum-org:service:DeviceInfo:1", "GetInfo")
                root_info = ET.fromstring(res_info)
                strip_ns(root_info)
                
                el_uptime = root_info.find(".//NewUpTime")
                if el_uptime is not None:
                    uptime_sec = int(el_uptime.text)
                    days = uptime_sec // 86400
                    hours = (uptime_sec % 86400) // 3600
                    minutes = (uptime_sec % 3600) // 60
                    if days > 0:
                        uptime = f"{days}d {hours}h {minutes}m"
                    elif hours > 0:
                        uptime = f"{hours}h {minutes}m"
                    else:
                        uptime = f"{minutes}m"
                        
                el_model = root_info.find(".//NewModelName")
                if el_model is not None:
                    model = el_model.text
                    
                el_fw = root_info.find(".//NewSoftwareVersion")
                if el_fw is not None:
                    firmware = el_fw.text

                el_sn = root_info.find(".//NewSerialNumber")
                if el_sn is not None:
                    mac = el_sn.text
                    if len(mac) == 12 and all(c in '0123456789ABCDEFabcdef' for c in mac):
                        mac = ":".join(mac[i:i+2] for i in range(0, 12, 2)).upper()
            except Exception as e:
                logger.error(f"FritzBoxScraper: Error fetching DeviceInfo: {e}")

            # 2. WAN Status & Statistics
            wan_status = "down"
            wan_link = "Link Down"
            wan_speed = "Auto"
            wan_tx_bytes = 0
            wan_rx_bytes = 0
            wan_tx_packets = 0
            wan_rx_packets = 0

            try:
                res_wan = self._soap_request("urn:dslforum-org:service:WANCommonInterfaceConfig:1", "GetCommonLinkProperties")
                root_wan = ET.fromstring(res_wan)
                strip_ns(root_wan)
                
                el_link = root_wan.find(".//NewPhysicalLinkStatus")
                if el_link is not None:
                    wan_status = "up" if el_link.text.lower() == "up" else "down"
                    wan_link = "Link Up" if wan_status == "up" else "Link Down"
                    
                el_ds_rate = root_wan.find(".//NewLayer1DownstreamMaxBitRate")
                el_us_rate = root_wan.find(".//NewLayer1UpstreamMaxBitRate")
                if el_ds_rate is not None and el_us_rate is not None:
                    ds_bps = int(el_ds_rate.text) * 1000
                    us_bps = int(el_us_rate.text) * 1000
                    
                    def fmt_bps_short(bps):
                        if bps >= 1_000_000_000:
                            return f"{int(bps/1_000_000_000)}G"
                        if bps >= 1_000_000:
                            return f"{int(bps/1_000_000)}M"
                        return f"{int(bps/1_000)}K"
                    
                    wan_speed = f"{fmt_bps_short(ds_bps)}/{fmt_bps_short(us_bps)}"

                res_tx_b = self._soap_request("urn:dslforum-org:service:WANCommonInterfaceConfig:1", "GetTotalBytesSent")
                root_tx_b = ET.fromstring(res_tx_b)
                strip_ns(root_tx_b)
                el_tx_b = root_tx_b.find(".//NewTotalBytesSent")
                if el_tx_b is not None:
                    wan_tx_bytes = int(el_tx_b.text)

                res_rx_b = self._soap_request("urn:dslforum-org:service:WANCommonInterfaceConfig:1", "GetTotalBytesReceived")
                root_rx_b = ET.fromstring(res_rx_b)
                strip_ns(root_rx_b)
                el_rx_b = root_rx_b.find(".//NewTotalBytesReceived")
                if el_rx_b is not None:
                    wan_rx_bytes = int(el_rx_b.text)

                res_tx_p = self._soap_request("urn:dslforum-org:service:WANCommonInterfaceConfig:1", "GetTotalPacketsSent")
                root_tx_p = ET.fromstring(res_tx_p)
                strip_ns(root_tx_p)
                el_tx_p = root_tx_p.find(".//NewTotalPacketsSent")
                if el_tx_p is not None:
                    wan_tx_packets = int(el_tx_p.text)

                res_rx_p = self._soap_request("urn:dslforum-org:service:WANCommonInterfaceConfig:1", "GetTotalPacketsReceived")
                root_rx_p = ET.fromstring(res_rx_p)
                strip_ns(root_rx_p)
                el_rx_p = root_rx_p.find(".//NewTotalPacketsReceived")
                if el_rx_p is not None:
                    wan_rx_packets = int(el_rx_p.text)

            except Exception as e:
                logger.error(f"FritzBoxScraper: Error fetching WAN stats: {e}")

            # 3. Hosts List & Virtual WLAN/LAN Active status
            mac_table = []
            active_wlan_hosts = 0
            max_wlan_speed = 0
            lan_ports_active = {1: False, 2: False, 3: False, 4: False}
            lan_ports_max_speed = {1: 0, 2: 0, 3: 0, 4: 0}

            try:
                res_path = self._soap_request("urn:dslforum-org:service:Hosts:1", "X_AVM-DE_GetHostListPath")
                root_path = ET.fromstring(res_path)
                strip_ns(root_path)
                el_path = root_path.find(".//NewX_AVM-DE_HostListPath")
                if el_path is not None and el_path.text:
                    path = el_path.text
                    ip_part = self.ip
                    if ":" not in ip_part:
                        ip_part = f"{ip_part}:49000"
                    if path.startswith("http"):
                        download_url = path
                    else:
                        download_url = f"http://{ip_part}{path}"
                        
                    auth = HTTPDigestAuth(self.username, self.password) if self.username else None
                    r_list = requests.get(download_url, auth=auth, timeout=5)
                    r_list.raise_for_status()
                    
                    root_list = ET.fromstring(r_list.content)
                    items = root_list.findall("Item")
                    
                    for item in items:
                        el_act = item.find("Active")
                        if el_act is not None and el_act.text == "1":
                            el_mac = item.find("MACAddress")
                            el_type = item.find("InterfaceType")
                            el_port = item.find("X_AVM-DE_Port")
                            el_speed = item.find("X_AVM-DE_Speed")
                            
                            mac_addr = el_mac.text.upper() if el_mac is not None and el_mac.text else ""
                            iftype = el_type.text if el_type is not None and el_type.text else ""
                            port_str = el_port.text if el_port is not None and el_port.text else "0"
                            speed_str = el_speed.text if el_speed is not None and el_speed.text else "0"
                            
                            try:
                                speed_val = int(speed_str)
                            except ValueError:
                                speed_val = 0
                                
                            if not mac_addr:
                                continue
                                
                            v_port = ""
                            if iftype == "802.11":
                                v_port = "WLAN"
                                active_wlan_hosts += 1
                                if speed_val > max_wlan_speed:
                                    max_wlan_speed = speed_val
                            elif iftype == "Ethernet":
                                try:
                                    port_num = int(port_str)
                                except ValueError:
                                    port_num = 0
                                    
                                if port_num in [1, 2, 3, 4]:
                                    v_port = f"LAN {port_num}"
                                    lan_ports_active[port_num] = True
                                    if speed_val > lan_ports_max_speed[port_num]:
                                        lan_ports_max_speed[port_num] = speed_val
                                else:
                                    v_port = "LAN 1"
                                    lan_ports_active[1] = True
                                    if speed_val > lan_ports_max_speed[1]:
                                        lan_ports_max_speed[1] = speed_val
                                        
                            if v_port:
                                mac_table.append({
                                    "mac": mac_addr,
                                    "type": "dynamic",
                                    "port": v_port,
                                    "vlan": "1"
                                })
            except Exception as e:
                logger.error(f"FritzBoxScraper: Error parsing hosts list: {e}")

            # 4. Construct Virtual Ports
            ports = []
            ports.append({
                "port": "WAN",
                "status": wan_status,
                "link": wan_link,
                "speed": wan_speed,
                "duplex": "Full" if wan_status == "up" else "",
                "flow_control": "",
                "tx_packets": wan_tx_packets,
                "rx_packets": wan_rx_packets,
                "tx_bytes": wan_tx_bytes,
                "rx_bytes": wan_rx_bytes
            })
            
            for i in range(1, 5):
                active = lan_ports_active[i]
                speed_val = lan_ports_max_speed[i]
                speed_str = "Auto"
                if active:
                    if speed_val >= 1000:
                        speed_str = "1G"
                    elif speed_val > 0:
                        speed_str = f"{speed_val}M"
                    else:
                        speed_str = "1G"
                ports.append({
                    "port": f"LAN {i}",
                    "status": "up" if active else "down",
                    "link": "Link Up" if active else "Link Down",
                    "speed": speed_str if active else "",
                    "duplex": "Full" if active else "",
                    "flow_control": "",
                    "tx_packets": 0, "rx_packets": 0, "tx_bytes": 0, "rx_bytes": 0
                })
                
            wlan_active = active_wlan_hosts > 0
            wlan_speed_str = ""
            if wlan_active:
                if max_wlan_speed >= 1000:
                    wlan_speed_str = f"{max_wlan_speed/1000:.1f}G".replace(".0", "")
                elif max_wlan_speed > 0:
                    wlan_speed_str = f"{max_wlan_speed}M"
                else:
                    wlan_speed_str = "Auto"
            ports.append({
                "port": "WLAN",
                "status": "up" if wlan_active else "down",
                "link": "Link Up" if wlan_active else "Link Down",
                "speed": wlan_speed_str if wlan_active else "",
                "duplex": "Full" if wlan_active else "",
                "flow_control": "",
                "tx_packets": 0, "rx_packets": 0, "tx_bytes": 0, "rx_bytes": 0
            })

            return {
                "name": self.name,
                "ip": self.ip,
                "model": model,
                "mac": mac,
                "uptime": uptime,
                "firmware": firmware,
                "ports": ports,
                "mac_table": mac_table,
                "dhcp_snooping": {"enabled": False, "ports": {}},
                "igmp": {"enabled": False, "entries": []},
                "jumbo_frame": {"enabled": False, "size": "Disabled"},
                "timestamp": time.time(),
            }
        except Exception as e:
            logger.error(f"FritzBoxScraper: Scraping failed for {self.ip}: {e}")
            return self._fallback()

    def _fallback(self):
        import time
        return {
            "name": self.name,
            "ip": self.ip,
            "model": "FRITZ!Box",
            "mac": "",
            "uptime": "",
            "firmware": "",
            "ports": [
                {"port": "WAN", "status": "down", "speed": "", "link": "Link Down", "duplex": "", "flow_control": "", "tx_packets": 0, "rx_packets": 0, "tx_bytes": 0, "rx_bytes": 0},
                {"port": "LAN 1", "status": "down", "speed": "", "link": "Link Down", "duplex": "", "flow_control": "", "tx_packets": 0, "rx_packets": 0, "tx_bytes": 0, "rx_bytes": 0},
                {"port": "LAN 2", "status": "down", "speed": "", "link": "Link Down", "duplex": "", "flow_control": "", "tx_packets": 0, "rx_packets": 0, "tx_bytes": 0, "rx_bytes": 0},
                {"port": "LAN 3", "status": "down", "speed": "", "link": "Link Down", "duplex": "", "flow_control": "", "tx_packets": 0, "rx_packets": 0, "tx_bytes": 0, "rx_bytes": 0},
                {"port": "LAN 4", "status": "down", "speed": "", "link": "Link Down", "duplex": "", "flow_control": "", "tx_packets": 0, "rx_packets": 0, "tx_bytes": 0, "rx_bytes": 0},
                {"port": "WLAN", "status": "down", "speed": "", "link": "Link Down", "duplex": "", "flow_control": "", "tx_packets": 0, "rx_packets": 0, "tx_bytes": 0, "rx_bytes": 0}
            ],
            "mac_table": [],
            "dhcp_snooping": {"enabled": False, "ports": {}},
            "igmp": {"enabled": False, "entries": []},
            "jumbo_frame": {"enabled": False, "size": "Disabled"},
            "timestamp": time.time(),
        }


def scrape_switch(config):
    model = config.get("model", "").lower()
    if model in ["openvswitch", "ovs"]:
        scraper = OVSScraper(config)
    elif model == "fritzbox":
        scraper = FritzBoxScraper(config)
    else:
        scraper = HCSwitchScraper(config)
    return scraper.scrape()


