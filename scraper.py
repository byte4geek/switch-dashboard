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
        self.base_url = f"http://{self.ip}"
        self._cj = None
        self._opener = None

    def _login(self):
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

        r = self._opener.open(f"{self.base_url}/", timeout=10)
        r.read()

    def _fetch(self, path):
        if not self._opener:
            self._login()
        headers = {"Referer": f"{self.base_url}/"}
        req = urllib.request.Request(f"{self.base_url}{path}", headers=headers)
        try:
            r = self._opener.open(req, timeout=10)
            return r.read().decode("utf-8", errors="replace")
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
        try:
            self._login()
        except Exception as e:
            logger.error(f"Login failed for {self.ip}: {e}")
            return self._fallback()

        info_html = self._fetch("/info.cgi")
        stats_html = self._fetch("/port.cgi?page=stats")

        ports = []
        device_info = {}

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

            # Port status from second table
            if len(tables) >= 2:
                for row in tables[1].find_all("tr")[1:]:
                    cells = row.find_all("td")
                    if len(cells) >= 4:
                        port_name = cells[0].get_text(strip=True)
                        match = re.match(r"Port\s*(\d+)", port_name)
                        port_num = match.group(1) if match else port_name
                        ports.append({
                            "port": port_num,
                            "status": "up" if "Up" in cells[1].get_text(strip=True) else "down",
                            "link": cells[1].get_text(strip=True),
                            "speed": cells[3].get_text(strip=True),
                            "duplex": cells[2].get_text(strip=True),
                            "flow_control": cells[4].get_text(strip=True) if len(cells) > 4 else "",
                            "tx_packets": 0,
                            "rx_packets": 0,
                            "tx_bytes": 0,
                            "rx_bytes": 0,
                        })

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

        return {
            "name": self.name,
            "ip": self.ip,
            "model": device_info.get("model", "HC-SWTGW218AS"),
            "mac": device_info.get("mac", ""),
            "uptime": device_info.get("uptime", ""),
            "firmware": device_info.get("firmware", ""),
            "ports": ports or self._fallback_ports(),
            "timestamp": time.time(),
        }

    def scrape_mac_table(self):
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

