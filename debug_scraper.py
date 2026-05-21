#!/usr/bin/env python3
"""Test the switch scraper against real switches."""
import json
from scraper import scrape_switch

with open("config.json") as f:
    config = json.load(f)

for sw in config["switches"]:
    print(f"\n{'='*50}")
    print(f"Scraping: {sw['name']} ({sw['ip']})")
    print(f"{'='*50}")
    result = scrape_switch(sw)
    print(f"Timestamp: {result.get('timestamp')}")
    print(f"Error: {result.get('error', 'none')}")
    print(f"Ports ({len(result.get('ports', []))}):")
    for p in result.get("ports", []):
        print(f"  Port {p['port']:>2}: status={p['status']:<8} speed={p['speed']:<12} {p.get('raw', '')}")

