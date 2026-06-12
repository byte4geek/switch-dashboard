import sqlite3
import os
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("scanner_db")

DB_DIR = os.environ.get("DASHBOARD_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(DB_DIR, "network_scanner.db")

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS host_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip_address TEXT,
            status INTEGER,
            event_time TEXT
        )
    """)
    conn.commit()
    conn.close()
    logger.info("SQLite Database initialized at " + DB_PATH)

def load_state_from_db():
    state = {}
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM hosts")
        for row in cursor.fetchall():
            ip = row['ip_address']
            d = dict(row)
            if d['known_host'] is not None:
                d['known_host'] = int(d['known_host'])
            else:
                d['known_host'] = 0
            state[ip] = d
    except Exception as e:
        logger.error(f"Error loading DB state: {e}")
    finally:
        conn.close()
    return state

def update_db_and_get_status(current_scan_results, last_db_state, ports_to_scan, perform_port_scan, port_scan_enabled, port_scan_timeout, port_scan_threads):
    from network_scanner import get_vendor, scan_ports_threaded
    from collections import OrderedDict
    
    final_report_state = OrderedDict()
    updated_count = 0
    inserted_count = 0
    offline_count = 0
    port_scan_count = 0
    ping_check_count = 0
    history_count = 0
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    now_ts_for_report = datetime.now()
    
    history_inserts = []
    
    online_ips = set(current_scan_results.keys())
    
    for ip in online_ips:
        data = current_scan_results[ip]
        mac = data['mac']
        vendor = get_vendor(mac)
        ports_result_str = None
        
        if port_scan_enabled and perform_port_scan and ports_to_scan:
            logger.info(f"Starting port scan for {ip} ({len(ports_to_scan)} ports)...")
            ports_result_str = scan_ports_threaded(ip, ports_to_scan, port_scan_timeout, port_scan_threads)
            if ports_result_str is not None:
                port_scan_count += 1
                logger.info(f"Port scan {ip} -> '{ports_result_str or 'None Open'}'")
        
        last_state = last_db_state.get(ip)
        current_ports = ports_result_str if ports_result_str is not None else (last_state.get('ports', '') if last_state else '')
        
        final_report_state[ip] = {
            'mac': mac, 
            'vendor': vendor, 
            'status': 'ONLINE', 
            'ports': current_ports or "", 
            'hostname': last_state.get('hostname', '') if last_state else '', 
            'note': last_state.get('note', '') if last_state else '', 
            'known_host': last_state.get('known_host', 0) if last_state else 0, 
            'timestamp': now_ts_for_report
        }
        
        if last_state: # UPDATE
            last_mac = last_state.get('mac_address', '') or last_state.get('mac', '') or ''
            last_vendor = last_state.get('vendor', '') or ''
            last_status = last_state.get('status', 'OFFLINE')
            last_ports = last_state.get('ports', '') or ''
            
            current_ports_compare = ports_result_str if ports_result_str is not None else last_ports
            port_scan_rel = port_scan_enabled and perform_port_scan and ports_to_scan and ports_result_str is not None
            ports_differ = (port_scan_rel and (current_ports_compare or "") != (last_ports or ""))
            status_changed = (last_status == 'OFFLINE')
            needs_update = (status_changed or last_mac != mac or last_vendor != vendor or ports_differ)
            
            if needs_update:
                set_clauses = ["mac_address = ?", "vendor = ?", "status = 'ONLINE'", "last_seen_online = ?", "last_updated = ?"]
                params = [mac, vendor, now_str, now_str]
                if port_scan_rel:
                    set_clauses.append("ports = ?")
                    params.append(ports_result_str if ports_result_str else None)
                if status_changed:
                    history_inserts.append((ip, 1, now_str))
                params.append(ip)
                
                update_query = f"UPDATE hosts SET {', '.join(set_clauses)} WHERE ip_address = ?"
                cursor.execute(update_query, tuple(params))
                updated_count += 1
            else:
                cursor.execute("UPDATE hosts SET last_updated = ? WHERE ip_address = ?", (now_str, ip))
        else: # INSERT
            current_ports_insert = ports_result_str if (port_scan_enabled and perform_port_scan and ports_result_str is not None) else None
            logger.info(f"DB INSERT: {ip} (MAC: {mac}, Ports: '{current_ports_insert or 'NULL'}')")
            
            insert_query = """
                INSERT INTO hosts 
                (ip_address, mac_address, vendor, ports, status, first_seen, last_seen_online, last_updated) 
                VALUES (?, ?, ?, ?, 'ONLINE', ?, ?, ?)
            """
            cursor.execute(insert_query, (ip, mac, vendor, current_ports_insert, now_str, now_str, now_str))
            inserted_count += 1
            history_inserts.append((ip, 1, now_str))
            
    # Process OFFLINE hosts
    from network_scanner import is_host_reachable_by_ping
    potentially_offline_ips = set(last_db_state.keys()) - online_ips
    if potentially_offline_ips:
        logger.info(f"{len(potentially_offline_ips)} hosts not in ARP. Pinging...")
        
    for ip in potentially_offline_ips:
        last_data = last_db_state[ip]
        if last_data.get('status') == 'ONLINE':
            ping_check_count += 1
            logger.debug(f"Pinging {ip}...")
            if is_host_reachable_by_ping(ip):
                logger.info(f"Ping success for {ip}. Kept as ONLINE.")
                final_report_state[ip] = {**last_data, 'status': 'ONLINE', 'timestamp': now_ts_for_report}
                cursor.execute("UPDATE hosts SET last_updated = ? WHERE ip_address = ?", (now_str, ip))
            else:
                logger.info(f"Ping failed for {ip}. Marking OFFLINE.")
                cursor.execute("UPDATE hosts SET status = 'OFFLINE', last_updated = ? WHERE ip_address = ?", (now_str, ip))
                offline_count += 1
                final_report_state[ip] = {**last_data, 'status': 'OFFLINE', 'timestamp': now_ts_for_report}
                history_inserts.append((ip, 0, now_str))
        else: # Already OFFLINE
            final_report_state[ip] = {**last_data, 'timestamp': now_ts_for_report}
            
    # Insert History Records
    if history_inserts:
        logger.info(f"Inserting {len(history_inserts)} history records...")
        history_query = "INSERT INTO host_history (ip_address, status, event_time) VALUES (?, ?, ?)"
        cursor.executemany(history_query, history_inserts)
        history_count = len(history_inserts)
        
    conn.commit()
    conn.close()
    
    logger.info(f"SQLite update complete: {inserted_count} IN, {updated_count} UP, {offline_count} OFF.")
    return final_report_state

def update_host_field(ip_address, field_name, new_value):
    conn = get_db_connection()
    cursor = conn.cursor()
    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    query = f"UPDATE hosts SET `{field_name}` = ?, last_updated = ? WHERE ip_address = ?"
    cursor.execute(query, (new_value, now_str, ip_address))
    rowcount = cursor.rowcount
    conn.commit()
    conn.close()
    return rowcount > 0

def update_known_host(ip_address, new_known_state):
    conn = get_db_connection()
    cursor = conn.cursor()
    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    query = "UPDATE hosts SET known_host = ?, last_updated = ? WHERE ip_address = ?"
    cursor.execute(query, (new_known_state, now_str, ip_address))
    rowcount = cursor.rowcount
    conn.commit()
    conn.close()
    return rowcount > 0

def delete_host(ip_address):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM hosts WHERE ip_address = ?", (ip_address,))
    rowcount = cursor.rowcount
    conn.commit()
    conn.close()
    return rowcount > 0

def get_history_data():
    from collections import defaultdict
    grouped_history = defaultdict(lambda: {"hostname": "", "events": []})
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT ip_address, hostname FROM hosts")
    for row in cursor.fetchall():
        grouped_history[row['ip_address']]["hostname"] = row['hostname'] or ''
        
    cursor.execute("SELECT ip_address, status, event_time FROM host_history ORDER BY ip_address, event_time ASC")
    for row in cursor.fetchall():
        ip = row['ip_address']
        ts = row['event_time']
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
        
        grouped_history[ip]["events"].append({
            "status": int(row['status']),
            "event_time": ts_str
        })
    conn.close()
    return dict(grouped_history)

def delete_host_history(ip_address):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM host_history WHERE ip_address = ?", (ip_address,))
    rowcount = cursor.rowcount
    conn.commit()
    conn.close()
    return rowcount

def delete_all_history():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM host_history")
    rowcount = cursor.rowcount
    conn.commit()
    conn.close()
    return rowcount

def purge_old_history(hours_to_keep):
    if hours_to_keep <= 0:
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    cutoff_date = (datetime.now(timezone.utc) - timedelta(hours=hours_to_keep)).strftime('%Y-%m-%d %H:%M:%S')
    purge_query = """
        DELETE FROM host_history
        WHERE event_time < ?
          AND id NOT IN (
              SELECT MAX(id)
              FROM host_history
              GROUP BY ip_address
          )
    """
    cursor.execute(purge_query, (cutoff_date,))
    rowcount = cursor.rowcount
    conn.commit()
    conn.close()
    logger.info(f"History purge complete. Deleted {rowcount} old records.")

def insert_host_history_batch(history_inserts):
    if not history_inserts:
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.executemany("INSERT INTO host_history (ip_address, status, event_time) VALUES (?, ?, ?)", history_inserts)
    conn.commit()
    conn.close()

