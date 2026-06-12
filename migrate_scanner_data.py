#!/usr/bin/env python3
import os
import sqlite3
from datetime import datetime

# Path to the old mainetwork-scanner .env file
old_env_path = "/opt/mainetwork-scanner/.env"
sqlite_db_path = "/opt/switch-dashboard/network_scanner.db"

def parse_env(path):
    env_vars = {}
    if not os.path.exists(path):
        print(f"Error: Old .env file not found at {path}")
        return None
    with open(path, "r") as f:
        for line in f:
            # Strip inline comments
            if "#" in line:
                # Simple comment stripping (works for standard env files without quoted #)
                line = line.split("#", 1)[0]
            line = line.strip()
            if not line:
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                val = val.strip().strip('"').strip("'")
                env_vars[key.strip()] = val
    return env_vars

def main():
    env = parse_env(old_env_path)
    if not env:
        return

    db_host = env.get("DB_HOST", "localhost")
    db_port = int(env.get("DB_PORT", 3306))
    db_user = env.get("DB_USER")
    db_pass = env.get("DB_PASSWORD")
    db_name = env.get("DB_NAME")

    if not all([db_user, db_pass, db_name]):
        print("Error: MariaDB credentials missing in the old .env file.")
        return

    print("Connecting to MariaDB...")
    try:
        import mariadb
        m_conn = mariadb.connect(
            host=db_host,
            port=db_port,
            user=db_user,
            password=db_pass,
            database=db_name
        )
        m_cursor = m_conn.cursor(dictionary=True)
    except Exception as e:
        print(f"Error connecting to MariaDB: {e}")
        print("Attempting to use mysql-connector-python as fallback...")
        try:
            import mysql.connector
            m_conn = mysql.connector.connect(
                host=db_host,
                port=db_port,
                user=db_user,
                password=db_pass,
                database=db_name
            )
            m_cursor = m_conn.cursor(dictionary=True)
        except Exception as ex:
            print(f"Error connecting to MariaDB via mysql-connector: {ex}")
            return

    print("Connecting to SQLite database...")
    try:
        s_conn = sqlite3.connect(sqlite_db_path)
        s_conn.row_factory = sqlite3.Row
        s_cursor = s_conn.cursor()
    except Exception as e:
        print(f"Error connecting to SQLite: {e}")
        m_conn.close()
        return

    # Create tables if they do not exist
    s_cursor.execute("""
        CREATE TABLE IF NOT EXISTS hosts (
            ip_address TEXT PRIMARY KEY,
            mac_address TEXT,
            vendor TEXT,
            hostname TEXT,
            ports TEXT,
            note TEXT,
            status TEXT,
            known_host INTEGER DEFAULT 0,
            first_seen TEXT,
            last_seen_online TEXT,
            last_updated TEXT
        )
    """)
    s_cursor.execute("""
        CREATE TABLE IF NOT EXISTS host_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip_address TEXT,
            status INTEGER,
            event_time TEXT
        )
    """)
    s_conn.commit()

    print("\n--- Migrating Hosts ---")
    try:
        m_cursor.execute("SELECT * FROM hosts")
        mariadb_hosts = m_cursor.fetchall()
        print(f"Found {len(mariadb_hosts)} hosts in MariaDB.")
    except Exception as e:
        print(f"Error fetching hosts from MariaDB: {e}")
        mariadb_hosts = []

    hosts_inserted = 0
    hosts_updated = 0

    for m_host in mariadb_hosts:
        ip = m_host.get("ip_address")
        mac = m_host.get("mac_address")
        vendor = m_host.get("vendor")
        hostname = m_host.get("hostname")
        ports = m_host.get("ports")
        note = m_host.get("note")
        status = m_host.get("status")
        known_host = m_host.get("known_host")
        first_seen = m_host.get("first_seen")
        last_seen_online = m_host.get("last_seen_online")

        # Convert datetime objects to string format
        if isinstance(first_seen, datetime):
            first_seen = first_seen.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(last_seen_online, datetime):
            last_seen_online = last_seen_online.strftime("%Y-%m-%d %H:%M:%S")

        # Normalize known_host to integer
        if known_host is not None:
            try:
                known_host = int(known_host)
            except ValueError:
                known_host = 0
        else:
            known_host = 0

        # Check if host exists in SQLite
        s_cursor.execute("SELECT * FROM hosts WHERE ip_address = ?", (ip,))
        s_host = s_cursor.fetchone()

        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        if not s_host:
            # Insert new host
            s_cursor.execute("""
                INSERT INTO hosts 
                (ip_address, mac_address, vendor, hostname, ports, note, status, known_host, first_seen, last_seen_online, last_updated) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (ip, mac, vendor, hostname, ports, note, status, known_host, first_seen or now_str, last_seen_online or now_str, now_str))
            hosts_inserted += 1
        else:
            # Update/Merge host data (Known host, hostname, and note)
            # If the SQLite version doesn't have a value or differs, we overwrite it with the MariaDB one.
            s_host = dict(s_host)
            
            # Prefer MariaDB hostname/note/known status if present
            merged_hostname = hostname if hostname else s_host.get("hostname")
            merged_note = note if note else s_host.get("note")
            merged_known = known_host if known_host is not None else s_host.get("known_host", 0)
            
            # Update SQLite host record
            s_cursor.execute("""
                UPDATE hosts 
                SET hostname = ?, note = ?, known_host = ?, mac_address = COALESCE(mac_address, ?), 
                    vendor = COALESCE(vendor, ?), ports = COALESCE(ports, ?), last_updated = ?
                WHERE ip_address = ?
            """, (merged_hostname, merged_note, merged_known, mac, vendor, ports, now_str, ip))
            hosts_updated += 1

    s_conn.commit()
    print(f"Hosts Migration Complete: {hosts_inserted} inserted, {hosts_updated} updated.")

    print("\n--- Migrating Host History ---")
    try:
        m_cursor.execute("SELECT ip_address, status, event_time FROM host_history")
        mariadb_history = m_cursor.fetchall()
        print(f"Found {len(mariadb_history)} history records in MariaDB.")
    except Exception as e:
        print(f"Error fetching history from MariaDB: {e}")
        mariadb_history = []

    # Get existing history in SQLite to avoid duplicates
    s_cursor.execute("SELECT ip_address, status, event_time FROM host_history")
    sqlite_history_rows = s_cursor.fetchall()
    
    # Store existing as a set of tuples: (ip, status, event_time)
    existing_history = set()
    for row in sqlite_history_rows:
        e_time = row["event_time"]
        if isinstance(e_time, str) and "T" in e_time:
            # Convert ISO to standard space format for key matching if needed
            e_time = e_time.replace("T", " ").replace("Z", "")
        existing_history.add((row["ip_address"], int(row["status"]), e_time))

    history_inserted = 0
    history_skipped = 0

    history_batch = []
    for m_hist in mariadb_history:
        ip = m_hist.get("ip_address")
        status = m_hist.get("status")
        event_time = m_hist.get("event_time")

        if isinstance(event_time, datetime):
            event_time_str = event_time.strftime("%Y-%m-%d %H:%M:%S")
        else:
            event_time_str = str(event_time) if event_time else ""

        # Normalize comparison key
        comp_time = event_time_str
        if "T" in comp_time:
            comp_time = comp_time.replace("T", " ").replace("Z", "")

        if (ip, int(status), comp_time) in existing_history:
            history_skipped += 1
            continue

        history_batch.append((ip, int(status), event_time_str))
        history_inserted += 1

    if history_batch:
        s_cursor.executemany("""
            INSERT INTO host_history (ip_address, status, event_time) 
            VALUES (?, ?, ?)
        """, history_batch)
        s_conn.commit()

    print(f"History Migration Complete: {history_inserted} inserted, {history_skipped} skipped (already existed).")

    # Close connections
    m_conn.close()
    s_conn.close()
    print("\nMigration script finished successfully.")

if __name__ == "__main__":
    main()
