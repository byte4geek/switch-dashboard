# 🌐 Switch Dashboard (HORACO & OEM Managed Switches)

A premium, high-performance, **real-time monitoring dashboard** designed for HORACO (`HC-SWTGW218AS`) and similar 8-Port + 2-Port SFP+ (10G) managed switches. The dashboard queries the switch’s native HTTP CGI interface, meaning **no SNMP setup or configuration is required**. 

Built with a gorgeous, high-tech glassmorphic dark-mode interface, it features automatic rolling bandwidth history, persistent byte-delta tracking, MAC address table searches, on-demand optical transceiver telemetry (DDMI), and a built-in interactive API explorer.

![Dashboard](https://raw.githubusercontent.com/byte4geek/switch-dashboard/refs/heads/main/images/dashboard.png)

---

## 🌟 Premium Features

### 1. High-Tech Glassmorphic Interface
* **Stunning Dark Mode**: Custom typography (Outfit/Inter/JetBrains Mono), harmonized color palettes, and frosted-glass components (`backdrop-filter: blur(12px)`) that look amazing on operations consoles.
* **Micro-Animations**: Dynamic state transitions, pulse loading indicators, and rotating refresh controls.
* **Adaptive Grid Layout**: Tailored viewports supporting smooth font scaling (S / M / L / XL) persisted directly to server settings.

### 2. SFP+ Transceiver Diagnostics & Telemetry (DDMI)
* **Real-time Optical Sensors**: Click the speed label of an SFP+ port (e.g. Port 9) to display physical module metadata (Vendor, Model, Serial, Compliances, Wavelength) and real-time DDMI diagnostics.
* **Visual Telemetry Bars**: Progress bars dynamically colored based on safe thresholds for:
  * **Temperature** (°C)
  * **Supply Voltage** (V)
  * **Bias Current** (mA)
  * **TX/RX Optical Power** (dBm)
* **Dynamic Refresh Loop**: Dedicated refresh button inside the modal to re-scrape physical hardware registers on-demand.
* **Bs4 Malformed Parsing Fix**: Custom pre-parsing sanitization engine handles malformed switch micro-controller HTML tables (like unclosed `<th>` tags closed by `</td>`), ensuring perfect parsing.

![SFP_DETAILS](https://raw.githubusercontent.com/byte4geek/switch-dashboard/refs/heads/main/images/sfp_details.png)

### 3. Interactive Bandwidth History Charts
* **Multi-Scale Sparklines**: Click any standard port speed to launch real-time bandwidth charts powered by **Chart.js**.
* **Three-Tier Historical Ranges**:
  * `Live` (High-resolution, real-time delta speed in bits per second)
  * `1-Hour` (Rolling 1-hour average in Bytes per second)
  * `24-Hour` (Rolling 24-hour average in 15-minute windows)

### 4. Advanced MAC Forwarding Table
* **Flexible Search & Filtering**: Multi-column text filtering makes searching by VLAN, Port, MAC, or status trivial.
* **Interactive Height Controls**: Dynamically drag to resize table heights (saves user preference to `settings.json` on disk).
* **Manual Re-indexing**: On-demand POST request scraper forces active switches to dump fresh bridge routing entries.

![MAC_TABLE](https://raw.githubusercontent.com/byte4geek/switch-dashboard/refs/heads/main/images/mac_table.png)

### 5. Configurator & Persistent Store
* **Multi-Switch Panel**: Dynamically add, modify, or delete switches through a beautifully organized `/config` route.
* **Persistent Accumulation**: Standard bytes counters are persistent across server reboots, complete with a delta tracking script to auto-detect and compensate for switch physical reboots without corrupting traffic metrics.
* **Annotation Layer**: Add custom, persistent labels/notes (e.g., "Uplink to NAS", "SmartTV") to any port.

### 6. Interactive Developer API Reference
* **Built-in Docs**: Hosted locally at `/api-docs` with a beautiful, fast sidebar navigation.
* **Developer Resources**: Contains interactive code cards, response payload previews, parameter grids, and copy-to-clipboard buttons for instant cURL examples.

### 7. DHCP Snooping & IGMP Snooping Telemetry
* **Global Configuration Tracking**: Scrapes global enable state for DHCP Snooping and IGMP Snooping, rendering status badges directly on the dashboard.
* **Port-Level Trust Badges**: Real-time port trust classifications are fetched from `/port.cgi` to show customized untrusted and trusted labels.
* **IGMP Multicast Tables**: Fully parsed multicast group database extracted from `/igmp.cgi?page=dump` displayed in an expandable glassmorphic table.

### 8. Jumbo Frame Configuration Status & Size
* **Frame Size Scraper**: Inspects `/fwd.cgi?page=jumboframe` to retrieve Jumbo Frame configuration status and parse the exact selected frame size (e.g. `9216Bytes`).

### 9. Server-Side Configuration Backups Manager
* **Config Backups**: Safely triggers configuration archive downloads via `/config_back.cgi?cmd=conf_backup`, storing them directly on the server's disk space.
* **Interactive Manager**: Beautiful `/backups` interface listing saved configs with quick downloads and single-click deletions protected by glassmorphic modal dialogues.

### 10. Secure Device Reboot Modal
* **Remote Power Cycle**: Triggers a remote switch reboot via `/reboot.cgi` securely.
* **Safety Confirmation**: Uses an immersive custom warning card modal using frosted glass backdrop effects, with real-time feedback loops during reboot execution.

---

## 🛠️ System Architecture

```mermaid
graph TD
    A[User Browser] -- HTML5 / JS / Chart.js --> B[Flask Server: Port 8080]
    B -- Config / API Route --> C[Local Cache: config.json / counters.json]
    B -- Background Poller / Thread --> D[scraper.py]
    D -- HTTP POST Login / CGI Scrape --> E[Managed Switch: 192.168.1.100]
    E -- /info.cgi, /port.cgi, /transceiver.cgi --> D
    B -- Interactive Explorer --> F[API Reference: /api-docs]
```

* **Backend**: Flask (Python 3.9+), single-worker polling thread to prevent session thrashing.
* **Frontend**: Vanilla ES6 JS, Custom CSS (Aesthetic glassmorphism), Chart.js (multi-tier canvas).
* **Scraper**: Custom HTTP CookieJar scraper communicating with switch `/login.cgi`, `/info.cgi`, `/port.cgi?page=stats`, and `/transceiver.cgi`.

---

## 📦 Requirements

* **Python 3.9+**
* **Operating System**: Linux with `systemd` (Debian, Ubuntu, CentOS, Arch) or standalone Windows/macOS.
* **Network Access**: Port 80 access to target managed switches (e.g. HORACO `HC-SWTGW218AS` or OEM equivalents).

---

## 🚀 Installation & Deployment (Linux)

Our automated script handles the entire installation seamlessly, creating a dedicated Python virtual environment to avoid interfering with system packages.

### Automated Install

```bash
# Clone the repository
git clone https://github.com/yourusername/switch-dashboard.git
cd switch-dashboard

# Run the installer as root
sudo bash install.sh
```

The script will:
1. Copy all project files to `/opt/switch-dashboard/`.
2. Check for system-level dependencies (`python3-pip`, `python3-venv`).
3. Set up a secure virtual environment inside `/opt/switch-dashboard/venv`.
4. Install all requirements (`Flask`, `BeautifulSoup4`).
5. Generate and register a `switch-dashboard.service` with `systemd`.
6. Start and enable the service on boot.

Once completed, the dashboard is live at:
* **Dashboard**: `http://<your-ip>:8080`
* **API Documentation**: `http://<your-ip>:8080/api-docs`

---

## 🔧 Manual Setup & Debugging

If you prefer to configure the dashboard manually or want to run it on non-systemd machines (Windows / macOS):

### 1. Install Dependencies
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Monitored Switches
Create or edit `/opt/switch-dashboard/config.json`:
```json
{
  "title": "Network Command Center",
  "refresh_interval": 10,
  "mac_refresh_multiplier": 5,
  "switches": [
    {
      "name": "HORACO Core",
      "ip": "192.168.1.100",
      "username": "admin",
      "password": "admin",
      "model": "HC-SWTGW218AS",
      "port_count": 9
    }
  ]
}
```

### 3. Run Standalone
```bash
python3 app.py
```

---

## 📋 REST API Reference

The dashboard includes a set of REST endpoints. For complete details, response payloads, and copyable cURL examples, navigate to the `/api-docs` route.

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/api/switches` | Returns live status of all switches and ports, including custom notes. |
| `GET` | `/api/switches/<ip>/transceiver` | Returns parsed SFP module diagnostics and DDMI telemetry. |
| `POST`| `/api/switches/<ip>/refresh_mac` | Forces an immediate scrape and returns the active MAC forwarding table. |
| `GET` | `/api/speeds` | Returns real-time delta speed states (Bytes/s, bps) for active interfaces. |
| `GET` | `/api/history` | Returns high-res historical statistics for Chart.js (`live`, `1h`, `24h`). |
| `POST`| `/api/notes` | Attaches a persistent label/description to a port (saves to `notes.json`). |
| `GET` | `/api/settings` | Retrieves global UI preferences (e.g., base font scale). |
| `POST`| `/api/settings` | Updates global UI settings on the disk. |
| `POST`| `/api/reset` | Purges cumulative logs and bandwidth cache, resetting delta Baselines. |
| `POST`| `/api/switches/<ip>/backup` | Triggers a configuration backup and saves the binary archive to the server's disk. |
| `POST`| `/api/switches/<ip>/reboot` | Safely sends a reboot trigger to the physical switch hardware. |
| `GET` | `/api/backups` | Lists all configuration backups stored on the server. |
| `GET` | `/api/backups/<filename>/download` | Downloads a specific saved switch configuration file. |
| `DELETE`| `/api/backups/<filename>` | Deletes a specific switch configuration file from the server. |
| `GET` | `/backups` | Returns the HTML template for the backups manager dashboard. |

---

## 📂 Project Structure

```
/opt/switch-dashboard/
├── app.py              # Flask core service, poller thread, and REST API routes
├── scraper.py          # Switch scraping engine, custom login cookie session, and tags cleaner
├── config.json         # User-defined switches, polling intervals, and general configuration
├── counters.json       # Persistent cumulative traffic database
├── notes.json          # Persistent custom port descriptions
├── settings.json       # Persisted user UI configurations (e.g. font sizes, table dimensions)
├── install.sh          # Self-healing, multi-distro Linux automated systemd installer
├── requirements.txt    # Python package dependencies
├── templates/
│   ├── index.html      # Main high-tech glassmorphic dashboard
│   ├── config.html     # Switch configurator interface
│   └── api_docs.html   # Premium built-in developer documentation manual
└── static/
    ├── style.css       # Premium core CSS tokens, custom dark scrollbars, and layouts
    ├── app.js          # Core front-end logic, rendering engine, Chart.js mapping
    └── logo.png        # Horaco brand assets
```

---

## 🛡️ License

This project is licensed under the **MIT License**. Feel free to modify, distribute, or incorporate it into your network setup.

---

## 📅 Release Notes & Changelog

### 🚀 Release 2026.5.1 (Current)
* **🎨 Administrative Port Contrast & Legend Integration**:
  - Outlined administratively disabled ports in the graphical representation using a unique, premium dark-grey (`#353c45`) to distinguish them from standard offline ports (`#57606a`).
  - Integrated the new **Disabled** status label and color badge directly into the central `PORT STATUS` legend.
* **🔒 Administrative Port Status Scraping**:
  - Upgraded scraping engine to fetch `/port.cgi` and parse active configuration states, accurately reporting `DISABLE` status instead of misleading physical link states.
* **🔄 Safe Device Reboot Modal**:
  - Implemented secure switch reboots via `/reboot.cgi` with an immersive glassmorphic warning modal to protect users from accidental network disruption.
* **💾 Configuration Backup Manager**:
  - Added on-demand server-side configuration backups (`.bin` files) with an interactive dashboard to download, view, and safely delete configurations.
* **🟢 Snooping & Multicast Telemetry Integration**:
  - Integrated global and port-level status for **DHCP Snooping**, **IGMP Snooping** (with collapsible multicast table), and **Jumbo Frame** size properties.
* **🎨 Visual Color Harmonization**:
  - Aligned table port speed texts (`colonna speed`) with matching port status indicators in the graphics bar.
* **📋 Interactive API Reference Expansion**:
  - Documented reboot and backups endpoints in the interactive local `/api-docs` interface.

---

# Donation
Buy me a coffee

[![Donate](https://img.shields.io/badge/Donate-PayPal-green.svg)](https://www.paypal.com/cgi-bin/webscr?cmd=_s-xclick&hosted_button_id=VK4CSX9NVQAZU)
