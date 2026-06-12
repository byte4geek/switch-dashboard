import os
import sys
import socket
import logging
import time
import concurrent.futures
from ipaddress import ip_network, ip_address

# Silence Scapy logs
logging.getLogger("scapy.runtime").setLevel(logging.ERROR)
logging.getLogger("scapy.loading").setLevel(logging.ERROR)

try:
    from scapy.all import Ether, ARP, IP, ICMP, srp, sr1, conf
    conf.verb = 0
    scapy_available = True
except Exception as e:
    logging.warning(f"Scapy not fully loaded on local system: {e}. Active scanner will only operate on container.")
    scapy_available = False

logger = logging.getLogger("network_scanner")

def get_vendor(mac_address):
    """Retrieves vendor by combining custom overrides and IEEE OUI lists."""
    if not mac_address:
        return "Unknown"
    try:
        from app import load_mac_vendors, get_ieee_vendors, lookup_vendor
        custom_vendors = load_mac_vendors()
        ieee_vendors = get_ieee_vendors()
        vendor = lookup_vendor(mac_address, custom_vendors, ieee_vendors)
        return vendor if vendor else "Unknown"
    except Exception as e:
        logger.debug(f"OUI lookup failed: {e}")
        return "Unknown"

def parse_port_range(range_str):
    """Parses port ranges like '22,80,443,8000-8080' into a set of port integers."""
    ports = set()
    if not range_str:
        return ports
    try:
        for part in range_str.split(','):
            part = part.strip()
            if not part:
                continue
            if '-' in part:
                start, end = map(int, part.split('-', 1))
                if 1 <= start <= end <= 65535:
                    ports.update(range(start, end + 1))
                else:
                    logger.warning(f"Invalid port range ignored: {part}")
            else:
                port_num = int(part)
                if 1 <= port_num <= 65535:
                    ports.add(port_num)
                else:
                    logger.warning(f"Invalid port number ignored: {part}")
    except ValueError:
        logger.error(f"Invalid port range format: '{range_str}'")
        return set()
    return ports

def scan_port(ip, port, timeout):
    """Attempts to connect to a TCP port on a given IP. Returns True if open."""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        return sock.connect_ex((ip, port)) == 0
    except socket.error:
        return False
    finally:
        if sock:
            sock.close()

def scan_ports_threaded(ip, ports_to_scan, timeout, max_threads):
    """Scans multiple ports concurrently using a ThreadPoolExecutor."""
    open_ports = []
    if not ports_to_scan:
        return ""
    
    actual_threads = min(max_threads, len(ports_to_scan))
    if actual_threads <= 0:
        actual_threads = 1
        
    with concurrent.futures.ThreadPoolExecutor(max_workers=actual_threads) as executor:
        future_to_port = {executor.submit(scan_port, ip, port, timeout): port for port in ports_to_scan}
        for future in concurrent.futures.as_completed(future_to_port):
            port = future_to_port[future]
            try:
                if future.result():
                    open_ports.append(port)
            except Exception as exc:
                logger.warning(f"Exception scanning {ip}:{port} - {exc}")
                
    if open_ports:
        open_ports.sort()
        return ",".join(map(str, open_ports))
    return ""

def scan_network(network_cidr):
    """Runs a Scapy ARP broadcast scan over the target subnet CIDR."""
    active_hosts = {}
    if not scapy_available:
        logger.error("Scapy is not available. Skipping network scan.")
        return None
        
    logger.info(f"Starting ARP discovery scan on {network_cidr}...")
    try:
        network_obj = ip_network(network_cidr, strict=False)
        packet = Ether(dst="ff:ff:ff:ff:ff:ff") / ARP(pdst=str(network_cidr))
        answered, _ = srp(packet, timeout=2, retry=1, verbose=False)
        logger.info(f"ARP discovery completed. {len(answered)} hosts responded.")
        
        for _, received in answered:
            ip = received.psrc
            mac = received.hwsrc
            try:
                if ip_address(ip) in network_obj:
                    active_hosts[ip] = {'mac': mac}
            except ValueError:
                logger.debug(f"Ignoring invalid IP received: '{ip}'")
    except PermissionError:
        logger.critical("Root privileges required to run Scapy ARP scan.")
        return None
    except Exception as e:
        logger.error(f"Error during network ARP scan: {e}")
        return None
        
    return active_hosts

def is_host_reachable_by_ping(ip_address, timeout=1, retry=1):
    """Sends an ICMP Echo Request ping check using Scapy."""
    if not scapy_available:
        return False
    if not ip_address:
        return False
    try:
        response = sr1(IP(dst=ip_address) / ICMP(), timeout=timeout, retry=retry, verbose=False)
        return response is not None
    except Exception as e:
        logger.warning(f"Ping check error to {ip_address}: {e}")
        return False
