import hashlib
import urllib.request
import urllib.parse
from http.cookiejar import CookieJar, Cookie
from bs4 import BeautifulSoup
import time
import logging
import re

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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

    def _login(self):
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

        r = self._opener.open(
            f"{self.base_url}/login.cgi", data=data, timeout=10
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

        r = self._opener.open(f"{self.base_url}/", timeout=10)
        r.read()

    def _fetch(self, path):
        if not self._opener:
            self._login()
        logger.debug(f"[_fetch] Fetching path: {path}")
        headers = {"Referer": f"{self.base_url}/"}
        req = urllib.request.Request(f"{self.base_url}{path}", headers=headers)
        try:
            r = self._opener.open(req, timeout=10)
            res = r.read().decode("utf-8", errors="replace")
            logger.debug(f"[_fetch] Path {path} successfully fetched (size: {len(res)} characters)")
            return res
        except Exception as e:
            logger.error(f"Failed to fetch {path}: {e}")
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
        logger.debug(f"Running full telemetry scrape for switch {self.name} ({self.ip})...")
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
                                            duplex_val = "Full"
                                    
                                    ports.append({
                                        "port": port_num,
                                        "status": status_val,
                                        "link": link_val,
                                        "speed": speed_val,
                                        "duplex": duplex_val,
                                        "flow_control": cells[4].get_text(strip=True) if len(cells) > 4 else "",
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
                for row in stats_table.find_all("tr")[1:]:
                    cells = row.find_all("td")
                    if len(cells) >= 7:
                        port_name = cells[0].get_text(strip=True)
                        match = re.match(r"Port\s*(\d+)", port_name)
                        port_num = match.group(1) if match else (
                            port_name if "trunk" in port_name.lower() else port_name
                        )
                        for p in ports:
                            if p["port"] == port_name.replace("Port ", ""):
                                p["tx_packets"] = self._parse_counter(cells[3].get_text(strip=True))
                                p["rx_packets"] = self._parse_counter(cells[4].get_text(strip=True))
                                p["tx_bytes"] = self._parse_counter(cells[5].get_text(strip=True))
                                p["rx_bytes"] = self._parse_counter(cells[6].get_text(strip=True))
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
            show_div = soup.find("div", class_="showdiv")
            if show_div:
                table = show_div.find("table")
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
            if enable_input and enable_input.has_attr("checked"):
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

    def download_backup(self):
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

    def scrape_mac_table(self):
        logger.debug(f"Scraping MAC address table for {self.ip}...")
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
            r = self._opener.open(req, timeout=10)
            return r.read().decode('utf-8', errors='replace')
        except Exception as e:
            logger.error(f"Failed to fetch MAC table page {page_num} on {self.ip}: {e}")
            return None

    def _parse_mac_table_rows(self, soup):
        rows_data = []
        tables = soup.find_all("table")
        for table in tables:
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            if "mac address" in headers or "mac" in headers:
                for row in table.find_all("tr")[1:]:
                    cells = row.find_all("td")
                    if len(cells) >= 4:
                        mac = cells[0].get_text(strip=True)
                        m_type = cells[1].get_text(strip=True)
                        port = cells[2].get_text(strip=True)
                        vlan = cells[3].get_text(strip=True)
                        
                        if ":" in mac and len(mac) >= 12:
                            rows_data.append({
                                "mac": mac,
                                "type": m_type,
                                "port": port,
                                "vlan": vlan
                            })
                break
        return rows_data

    def scrape_transceiver(self):
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

    def _fallback_ports(self):
        return [{"port": str(i), "status": "unknown", "speed": "",
                 "link": "Unknown", "duplex": "", "flow_control": "",
                 "tx_packets": 0, "rx_packets": 0,
                 "tx_bytes": 0, "rx_bytes": 0}
                for i in range(1, self.port_count + 1)]


def scrape_switch(config):
    scraper = HCSwitchScraper(config)
    return scraper.scrape()

