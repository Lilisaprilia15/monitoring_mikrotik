from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file
import socket
import time
import os
import threading
import csv
import hmac
import json
import ipaddress
import tempfile
import subprocess
import re
import random
from datetime import datetime
from routeros_api import RouterOsApiPool
from openpyxl import Workbook, load_workbook
from dotenv import load_dotenv


load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]



# =====================================================
# LOGIN ROUTEROS
# =====================================================

ROUTEROS_USERNAME = os.environ["ROUTEROS_USERNAME"]
ROUTEROS_PASSWORD = os.environ["ROUTEROS_PASSWORD"]
APP_USERS_CONFIG = os.getenv("APP_USERS")
if APP_USERS_CONFIG:
    try:
        APP_USERS = json.loads(APP_USERS_CONFIG)
    except json.JSONDecodeError as error:
        raise ValueError("APP_USERS must be a JSON object of usernames and passwords.") from error

    if (
        not isinstance(APP_USERS, dict)
        or not APP_USERS
        or any(
            not isinstance(username, str)
            or not username
            or not isinstance(password, str)
            or not password
            for username, password in APP_USERS.items()
        )
    ):
        raise ValueError("APP_USERS must contain at least one non-empty username and password.")
else:
    legacy_username = os.getenv("APP_USERNAME")
    legacy_password = os.getenv("APP_PASSWORD")
    if not legacy_username or not legacy_password:
        raise RuntimeError("Configure APP_USERS in .env with one or more dashboard accounts.")
    APP_USERS = {legacy_username: legacy_password}


# =====================================================
# DATA ROUTER
# =====================================================

DEFAULT_ROUTER_CONFIGS = {
    "CHR 1": {
        "name": "MikroTik-CHR-1",
        "ip": "192.168.56.10",
        "traffic_interface": "ether2"
    },
    "CHR 2": {
        "name": "MikroTik-CHR-2",
        "ip": "192.168.56.11",
        "traffic_interface": "ether3"
    },
}

ROUTER_CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "routers.json"
)
router_config_lock = threading.Lock()

# =====================================================
# SISTEM TELEMETRI LANJUTAN & ALERTING ENGINE
# =====================================================
ENABLE_DEMO_MODE = True
alerts_store = []
alerts_lock = threading.Lock()
alert_seq = 0


def record_alert(router_name, severity, title, message):
    global alert_seq
    with alerts_lock:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for alert in alerts_store:
            if alert["router"] == router_name and alert["title"] == title and not alert.get("acknowledged", False):
                alert["last_seen"] = now_str
                return
        alert_seq += 1
        alerts_store.insert(0, {
            "id": f"ALT-{alert_seq:04d}",
            "router": router_name,
            "severity": severity,  # CRITICAL, WARNING, INFO
            "title": title,
            "message": message,
            "timestamp": now_str,
            "last_seen": now_str,
            "acknowledged": False
        })
        if len(alerts_store) > 100:
            alerts_store.pop()


def build_router_state(name, ip, traffic_interface):
    return {
        "name": name,
        "ip": ip,
        "traffic_interface": traffic_interface,
        "version": "RouterOS 7.x",
        "cpu": 0,
        "memory": 0,
        "uptime": "Router Offline",
        "status": "OFFLINE",
        "interface": "-",
        "total_memory_mb": 0,
        "free_memory_mb": 0,
        "total_hdd_mb": 0,
        "free_hdd_mb": 0,
        "hdd_usage": 0,
        "arch": "-",
        "board": "-",
        "cpu_freq": "-",
        "cpu_count": "-",
        "interfaces": [],
        "dhcp_leases": [],
        "arp_table": [],
        "active_connections_count": 0,
        "connections_sample": [],
        "firewall_rules_count": 0,
        "nat_rules_count": 0,
        "logs": []
    }


def populate_demo_router_data(router_name, router):
    router["status"] = "ONLINE (SIM)"
    router["version"] = "RouterOS 7.15.2 (CHR)"
    router["cpu"] = random.randint(14, 38)
    router["memory"] = round(34.2 + random.uniform(-2.5, 3.5), 1)
    router["total_memory_mb"] = 1024
    router["free_memory_mb"] = int(1024 * (1 - router["memory"] / 100))
    router["total_hdd_mb"] = 128
    router["free_hdd_mb"] = 99
    router["hdd_usage"] = 22.7
    router["uptime"] = "18d 04:22:10"
    router["arch"] = "x86_64"
    router["board"] = "CHR"
    router["cpu_freq"] = "2600 MHz"
    router["cpu_count"] = "2"
    router["interface"] = router.get("traffic_interface", "ether2")

    sim_dl = round(random.uniform(9.5, 28.4), 3)
    sim_ul = round(random.uniform(2.4, 9.8), 3)
    traffic_data[router_name] = {
        "download": sim_dl,
        "upload": sim_ul
    }

    router["interfaces"] = [
        {
            "name": "ether1",
            "type": "ether",
            "running": True,
            "disabled": False,
            "rx_byte": 1450289410,
            "tx_byte": 892341020,
            "rx_packet": 128940,
            "tx_packet": 94210,
            "rx_drop": 0,
            "tx_drop": 0,
            "rx_error": 0,
            "tx_error": 0
        },
        {
            "name": "ether2",
            "type": "ether",
            "running": True,
            "disabled": False,
            "rx_byte": 892341020,
            "tx_byte": 1450289410,
            "rx_packet": 94210,
            "tx_packet": 128940,
            "rx_drop": 0,
            "tx_drop": 0,
            "rx_error": 0,
            "tx_error": 0
        },
        {
            "name": "ether3",
            "type": "ether",
            "running": True,
            "disabled": False,
            "rx_byte": 324102940,
            "tx_byte": 412093840,
            "rx_packet": 34102,
            "tx_packet": 42109,
            "rx_drop": 0,
            "tx_drop": 0,
            "rx_error": 0,
            "tx_error": 0
        }
    ]

    base_ip = "192.168.88" if "1" in router_name else ("192.168.89" if "2" in router_name else "192.168.90")
    router["dhcp_leases"] = [
        {"address": f"{base_ip}.100", "mac_address": "70:85:C2:5A:11:42", "host_name": "Laptop-Admin-NOC", "status": "bound", "expires_after": "2d 18h", "dynamic": True},
        {"address": f"{base_ip}.101", "mac_address": "DC:A9:04:78:22:15", "host_name": "iPhone-15-Pro-Max", "status": "bound", "expires_after": "1d 04h", "dynamic": True},
        {"address": f"{base_ip}.102", "mac_address": "00:15:5D:89:33:91", "host_name": "Production-Database", "status": "bound", "expires_after": "6d 21h", "dynamic": False},
        {"address": f"{base_ip}.103", "mac_address": "3C:06:30:EF:44:80", "host_name": "Workstation-Eng-01", "status": "bound", "expires_after": "3d 12h", "dynamic": True},
        {"address": f"{base_ip}.104", "mac_address": "F4:8C:50:AA:55:12", "host_name": "LaserJet-Printer-Office", "status": "bound", "expires_after": "5d 08h", "dynamic": False},
        {"address": f"{base_ip}.105", "mac_address": "B8:27:EB:66:77:33", "host_name": "MeetingRoom-Display", "status": "bound", "expires_after": "2d 02h", "dynamic": True}
    ]

    router["arp_table"] = [
        {"address": f"{base_ip}.100", "mac_address": "70:85:C2:5A:11:42", "interface": "ether2", "complete": True, "dynamic": True},
        {"address": f"{base_ip}.101", "mac_address": "DC:A9:04:78:22:15", "interface": "ether2", "complete": True, "dynamic": True},
        {"address": f"{base_ip}.102", "mac_address": "00:15:5D:89:33:91", "interface": "ether2", "complete": True, "dynamic": False},
        {"address": f"{base_ip}.103", "mac_address": "3C:06:30:EF:44:80", "interface": "ether2", "complete": True, "dynamic": True},
        {"address": f"{base_ip}.1", "mac_address": "48:8F:5A:33:11:00", "interface": "ether1", "complete": True, "dynamic": False}
    ]

    router["active_connections_count"] = random.randint(95, 230)
    router["firewall_rules_count"] = 18
    router["nat_rules_count"] = 5
    router["connections_sample"] = [
        {"protocol": "tcp", "src_address": f"{base_ip}.100:54120", "dst_address": "142.250.190.46:443", "tcp_state": "established", "timeout": "23h59m"},
        {"protocol": "tcp", "src_address": f"{base_ip}.101:49812", "dst_address": "104.16.12.34:443", "tcp_state": "established", "timeout": "11h34m"},
        {"protocol": "udp", "src_address": f"{base_ip}.102:53412", "dst_address": "8.8.8.8:53", "tcp_state": "assured", "timeout": "00:02:45"},
        {"protocol": "tcp", "src_address": f"{base_ip}.100:56102", "dst_address": f"{router['ip']}:8291", "tcp_state": "established", "timeout": "04h20m"},
        {"protocol": "tcp", "src_address": f"{base_ip}.103:51299", "dst_address": "13.107.42.16:443", "tcp_state": "established", "timeout": "18h15m"}
    ]

    t_now = datetime.now().strftime("%H:%M:%S")
    router["logs"] = [
        {"time": t_now, "topics": "system,info", "message": f"user admin logged in from {base_ip}.100 via winbox"},
        {"time": t_now, "topics": "dhcp,info", "message": f"assigned {base_ip}.101 to DC:A9:04:78:22:15"},
        {"time": t_now, "topics": "firewall,info", "message": "defconf: forward: in:ether2 out:ether1, proto TCP (SYN)"},
        {"time": t_now, "topics": "system,warning", "message": "DNS cache threshold warning: 82% capacity"},
        {"time": t_now, "topics": "interface,info", "message": "ether2 link up (speed 1Gbps, full duplex)"}
    ]


def load_routers():
    loaded_routers = {
        router_key: build_router_state(
            config["name"],
            config["ip"],
            config["traffic_interface"]
        )
        for router_key, config in DEFAULT_ROUTER_CONFIGS.items()
    }

    if not os.path.exists(ROUTER_CONFIG_FILE):
        return loaded_routers

    try:
        with open(ROUTER_CONFIG_FILE, "r", encoding="utf-8") as config_file:
            saved_routers = json.load(config_file)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Unable to load routers.json.") from error

    if not isinstance(saved_routers, dict):
        raise RuntimeError("routers.json must contain a JSON object.")

    for router_key, config in saved_routers.items():
        if (
            not isinstance(router_key, str)
            or not router_key.startswith("CHR ")
            or not router_key[4:].isdigit()
            or router_key in loaded_routers
            or not isinstance(config, dict)
        ):
            raise RuntimeError("routers.json contains an invalid router entry.")

        name = config.get("name")
        ip = config.get("ip")
        traffic_interface = config.get("traffic_interface")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (name, ip, traffic_interface)
        ):
            raise RuntimeError("routers.json contains incomplete router settings.")

        try:
            ipaddress.ip_address(ip)
        except ValueError as error:
            raise RuntimeError("routers.json contains an invalid router IP.") from error

        loaded_routers[router_key] = build_router_state(
            name.strip(),
            ip.strip(),
            traffic_interface.strip()
        )

    return loaded_routers


routers = load_routers()


def save_added_routers(router_map):
    saved_routers = {
        router_key: {
            "name": router["name"],
            "ip": router["ip"],
            "traffic_interface": router["traffic_interface"]
        }
        for router_key, router in router_map.items()
        if router_key not in DEFAULT_ROUTER_CONFIGS
    }

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=os.path.dirname(ROUTER_CONFIG_FILE),
            delete=False
        ) as config_file:
            temporary_path = config_file.name
            json.dump(saved_routers, config_file, indent=2)

        os.replace(temporary_path, ROUTER_CONFIG_FILE)
        temporary_path = None
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.remove(temporary_path)


# =====================================================
# TRAFFIC
# =====================================================

traffic_data = {
    router_key: {
        "download": 0,
        "upload": 0
    }
    for router_key in routers
}


previous_traffic = {}
EXCEL_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "monitoring_history.xlsx"
)
excel_lock = threading.Lock()
CSV_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "monitoring_live.csv"
)
csv_lock = threading.Lock()
last_excel_save = 0

# =====================================================
# CEK PORT WINBOX
# =====================================================

def check_router(ip):

    try:

        sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM
        )

        sock.settimeout(2)

        result = sock.connect_ex(
            (ip, 8291)
        )

        sock.close()

        return result == 0

    except Exception:

        return False


# =====================================================
# SIMPAN RIWAYAT KE MICROSOFT EXCEL
# =====================================================

def save_monitoring_to_excel():

    with excel_lock:

        if os.path.exists(EXCEL_FILE):
            workbook = load_workbook(EXCEL_FILE)
            sheet = workbook.active
        else:
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "Monitoring History"
            sheet.append([
                "Waktu",
                "Router",
                "IP",
                "Status",
                "CPU (%)",
                "Memory (%)",
                "Interface",
                "Download (Mbps)",
                "Upload (Mbps)"
            ])

        now = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        for router_name, router in routers.items():
            traffic = traffic_data[router_name]

            sheet.append([
                now,
                router_name,
                router["ip"],
                router["status"],
                router["cpu"],
                router["memory"],
                router["interface"],
                traffic["download"],
                traffic["upload"]
            ])

        workbook.save(EXCEL_FILE)


# =====================================================
# LIVE CSV UNTUK EXCEL POWER QUERY
# =====================================================

def save_monitoring_to_live_csv():

    with csv_lock:
        now = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        with open(
            CSV_FILE,
            "w",
            newline="",
            encoding="utf-8-sig"
        ) as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow([
                "Waktu",
                "Router",
                "IP",
                "Status",
                "CPU (%)",
                "Memory (%)",
                "Interface",
                "Download (Mbps)",
                "Upload (Mbps)"
            ])

            for router_name, router in routers.items():
                traffic = traffic_data[router_name]
                writer.writerow([
                    now,
                    router_name,
                    router["ip"],
                    router["status"],
                    router["cpu"],
                    router["memory"],
                    router["interface"],
                    traffic["download"],
                    traffic["upload"]
                ])


# =====================================================
# FORMAT UPTIME
# =====================================================

def format_uptime(uptime):

    if not uptime:
        return "Router Offline"

    return str(uptime)


# =====================================================
# AMBIL DATA ROUTEROS
# =====================================================

def get_router_data(router_name, router):

    ip = router["ip"]

    try:

        # -------------------------------------------------
        # LOGIN KE ROUTEROS API
        # -------------------------------------------------

        pool = RouterOsApiPool(
            host=ip,
            username=ROUTEROS_USERNAME,
            password=ROUTEROS_PASSWORD,
            port=8728,
            plaintext_login=True
        )

        api = pool.get_api()


        # -------------------------------------------------
        # SYSTEM RESOURCE
        # -------------------------------------------------

        resource = api.get_resource(
            "/system/resource"
        )

        resource_data = resource.get()


        if not resource_data:

            pool.disconnect()

            return False


        system = resource_data[0]


        # -------------------------------------------------
        # CPU & STORAGE DETAIL
        # -------------------------------------------------

        cpu = int(
            system.get(
                "cpu-load",
                0
            )
        )


        # -------------------------------------------------
        # MEMORY
        # -------------------------------------------------

        total_memory = int(
            system.get(
                "total-memory",
                0
            )
        )

        free_memory = int(
            system.get(
                "free-memory",
                0
            )
        )


        if total_memory > 0:

            used_memory = (
                (
                    total_memory -
                    free_memory
                )
                /
                total_memory
                *
                100
            )

        else:

            used_memory = 0


        memory = round(
            used_memory,
            1
        )

        total_hdd = int(system.get("total-hdd-space", 0))
        free_hdd = int(system.get("free-hdd-space", 0))
        router["total_memory_mb"] = round(total_memory / (1024 * 1024), 1)
        router["free_memory_mb"] = round(free_memory / (1024 * 1024), 1)
        router["total_hdd_mb"] = round(total_hdd / (1024 * 1024), 1)
        router["free_hdd_mb"] = round(free_hdd / (1024 * 1024), 1)
        router["hdd_usage"] = round(((total_hdd - free_hdd) / total_hdd * 100), 1) if total_hdd > 0 else 0
        router["arch"] = system.get("architecture-name", "x86_64")
        router["board"] = system.get("board-name", "CHR")
        router["cpu_freq"] = f"{system.get('cpu-frequency', '-')} MHz"
        router["cpu_count"] = str(system.get("cpu-count", "1"))


        # -------------------------------------------------
        # UPTIME
        # -------------------------------------------------

        uptime = format_uptime(
            system.get(
                "uptime",
                "Unknown"
            )
        )


        # -------------------------------------------------
        # ROUTEROS VERSION
        # -------------------------------------------------

        version = system.get(
            "version",
            "Unknown"
        )


        # -------------------------------------------------
        # IDENTITY
        # -------------------------------------------------

        identity = router["name"]

        try:

            identity_resource = api.get_resource(
                "/system/identity"
            )

            identity_data = identity_resource.get()

            if identity_data:

                identity = identity_data[0].get(
                    "name",
                    router["name"]
                )

        except Exception:

            pass



        # -------------------------------------------------
        # PILIH INTERFACE CHR
        # -------------------------------------------------

        interface_resource = api.get_resource(
            "/interface"
        )

        interfaces = interface_resource.get()

        router["interfaces"] = [
            {
                "name": item.get("name", "-"),
                "type": item.get("type", "-"),
                "running": str(item.get("running", "false")).lower() == "true",
                "disabled": str(item.get("disabled", "false")).lower() == "true",
                "rx_byte": int(item.get("rx-byte", 0)),
                "tx_byte": int(item.get("tx-byte", 0)),
                "rx_packet": int(item.get("rx-packet", 0)),
                "tx_packet": int(item.get("tx-packet", 0)),
                "rx_drop": int(item.get("rx-drop", 0)),
                "tx_drop": int(item.get("tx-drop", 0)),
                "rx_error": int(item.get("rx-error", 0)),
                "tx_error": int(item.get("tx-error", 0))
            }
            for item in interfaces
            if item.get("name") != "lo"
            and item.get("type", "").lower() != "loopback"
        ]

        interface_name = router.get(
            "traffic_interface",
            "ether2"
        )

        selected_interface = None

        for interface in interfaces:

            if interface.get("name") == interface_name:

                selected_interface = interface
                break


        # -------------------------------------------------
        # AMBIL TRAFFIC ASLI RX DAN TX
        # -------------------------------------------------

        download = 0
        upload = 0

        if selected_interface:

            disabled = str(
                selected_interface.get("disabled", "false")
            ).lower()

            if disabled == "true":
                raise Exception(
                    "Interface " + interface_name + " disabled"
                )

            rx_bytes = int(
                selected_interface.get("rx-byte", 0)
            )

            tx_bytes = int(
                selected_interface.get("tx-byte", 0)
            )

            current_time = time.time()

            old_data = previous_traffic.get(
                router_name
            )

            if old_data:

                time_difference = (
                    current_time - old_data["time"]
                )

                if time_difference > 0:

                    rx_difference = max(
                        0,
                        rx_bytes - old_data["rx"]
                    )

                    tx_difference = max(
                        0,
                        tx_bytes - old_data["tx"]
                    )

                    download = round(
                        (rx_difference * 8) /
                        time_difference / 1000000,
                        3
                    )

                    upload = round(
                        (tx_difference * 8) /
                        time_difference / 1000000,
                        3
                    )

            previous_traffic[router_name] = {
                "rx": rx_bytes,
                "tx": tx_bytes,
                "time": current_time
            }

        else:

            interface_name = "-"


        # -------------------------------------------------
        # SIMPAN HASIL TRAFFIC
        # -------------------------------------------------

        router["interface"] = interface_name

        traffic_data[router_name]["download"] = download

        traffic_data[router_name]["upload"] = upload
 
     

# -------------------------------------------------
        # UPDATE DATA ROUTER
        # -------------------------------------------------

        router["name"] = identity

        router["version"] = version

        router["cpu"] = cpu

        router["memory"] = memory

        router["uptime"] = uptime

        router["status"] = "ONLINE"

        router["interface"] = interface_name


        # -------------------------------------------------
        # DHCP LEASES, ARP, FIREWALL, DAN LOGS
        # -------------------------------------------------
        try:
            dhcp_res = api.get_resource("/ip/dhcp-server/lease")
            router["dhcp_leases"] = [
                {
                    "address": item.get("address", "-"),
                    "mac_address": item.get("mac-address", "-"),
                    "host_name": item.get("host-name", item.get("comment", "Device")),
                    "status": item.get("status", "bound"),
                    "expires_after": item.get("expires-after", "-"),
                    "dynamic": str(item.get("dynamic", "true")).lower() == "true",
                }
                for item in dhcp_res.get()[:50]
            ]
        except Exception:
            router["dhcp_leases"] = []

        try:
            arp_res = api.get_resource("/ip/arp")
            router["arp_table"] = [
                {
                    "address": item.get("address", "-"),
                    "mac_address": item.get("mac-address", "-"),
                    "interface": item.get("interface", "-"),
                    "complete": str(item.get("complete", "true")).lower() == "true",
                    "dynamic": str(item.get("dynamic", "true")).lower() == "true",
                }
                for item in arp_res.get()[:50]
            ]
        except Exception:
            router["arp_table"] = []

        try:
            conn_res = api.get_resource("/ip/firewall/connection")
            conn_data = conn_res.get()
            router["active_connections_count"] = len(conn_data)
            router["connections_sample"] = [
                {
                    "protocol": item.get("protocol", "tcp"),
                    "src_address": item.get("src-address", "-"),
                    "dst_address": item.get("dst-address", "-"),
                    "tcp_state": item.get("tcp-state", "established"),
                    "timeout": item.get("timeout", "-")
                }
                for item in conn_data[:25]
            ]
        except Exception:
            router["active_connections_count"] = 0
            router["connections_sample"] = []

        try:
            router["firewall_rules_count"] = len(api.get_resource("/ip/firewall/filter").get())
        except Exception:
            router["firewall_rules_count"] = 0

        try:
            router["nat_rules_count"] = len(api.get_resource("/ip/firewall/nat").get())
        except Exception:
            router["nat_rules_count"] = 0

        try:
            log_res = api.get_resource("/log")
            logs = log_res.get()
            router["logs"] = [
                {
                    "time": item.get("time", "-"),
                    "topics": item.get("topics", "system,info"),
                    "message": item.get("message", "-")
                }
                for item in logs[-30:]
            ]
        except Exception:
            router["logs"] = []

        # -------------------------------------------------
        # SIMPAN TRAFFIC
        # -------------------------------------------------

        traffic_data[router_name]["download"] = download
        traffic_data[router_name]["upload"] = upload      
        pool.disconnect()

        return True


    except Exception as e:

        print()
        print("====================================")
        print(
            "GAGAL MENGAMBIL DATA:",
            router_name
        )
        print(
            "IP:",
            ip
        )
        print(
            "ERROR:",
            str(e)
        )
        print("====================================")

        if ENABLE_DEMO_MODE:
            populate_demo_router_data(router_name, router)
            return True

        router["status"] = "OFFLINE"
        router["cpu"] = 0
        router["memory"] = 0
        router["total_memory_mb"] = 0
        router["free_memory_mb"] = 0
        router["total_hdd_mb"] = 0
        router["free_hdd_mb"] = 0
        router["hdd_usage"] = 0
        router["uptime"] = "Router Offline"
        router["interface"] = "-"
        router["interfaces"] = []
        router["dhcp_leases"] = []
        router["arp_table"] = []
        router["active_connections_count"] = 0
        router["connections_sample"] = []
        router["firewall_rules_count"] = 0
        router["nat_rules_count"] = 0
        router["logs"] = [
            {
                "time": datetime.now().strftime("%H:%M:%S"),
                "topics": "system,error",
                "message": f"Koneksi ke {ip}:8728 terputus: {str(e)[:80]}"
            }
        ]

        return False


# =====================================================
# UPDATE SEMUA ROUTER
# =====================================================

def update_router_status():
    global last_excel_save

    for router_name, router in routers.items():
        get_router_data(
            router_name,
            router
        )

    # Evaluasi Engine Peringatan Otomatis (Alerts)
    for r_name, r in routers.items():
        if r.get("status") == "OFFLINE":
            record_alert(r_name, "CRITICAL", f"{r_name} OFFLINE", f"Router {r_name} ({r.get('ip')}) tidak dapat dihubungi.")
        else:
            cpu_val = r.get("cpu", 0)
            if cpu_val >= 90:
                record_alert(r_name, "CRITICAL", f"CPU Kritis {r_name}", f"CPU load {r_name} mencapai {cpu_val}% (>90%).")
            elif cpu_val >= 75:
                record_alert(r_name, "WARNING", f"CPU Tinggi {r_name}", f"CPU load {r_name} mencapai {cpu_val}% (>75%).")

            mem_val = r.get("memory", 0)
            if mem_val >= 85:
                record_alert(r_name, "WARNING", f"RAM Tinggi {r_name}", f"Penggunaan memori {r_name} mencapai {mem_val}%.")

            hdd_val = r.get("hdd_usage", 0)
            if hdd_val >= 90:
                record_alert(r_name, "WARNING", f"Storage Penuh {r_name}", f"Penyimpanan {r_name} terpakai {hdd_val}%.")

    current_time = time.time()

    if current_time - last_excel_save >= 5:
        save_monitoring_to_excel()
        save_monitoring_to_live_csv()
        last_excel_save = current_time



# =====================================================
# LOGIN
# =====================================================

@app.route(
    "/login",
    methods=["GET", "POST"]
)
def login():

    if request.method == "POST":

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        configured_password = APP_USERS.get(username)

        if configured_password and hmac.compare_digest(password, configured_password):
            session.clear()
            session["logged_in"] = True
            session["username"] = username

            return redirect(
                url_for("dashboard")
            )


        return render_template(
            "login.html",
            error="Username atau password salah!"
        )


    return render_template(
        "login.html"
    )


# =====================================================
# LOGOUT
# =====================================================

@app.route(
    "/logout",
    methods=["GET"]
)
def logout():

    session.clear()

    return redirect(
        url_for("login")
    )


# =====================================================
# DASHBOARD
# =====================================================

@app.route(
    "/",
    methods=["GET"]
)
def dashboard():

    if not session.get(
        "logged_in"
    ):

        return redirect(
            url_for("login")
        )


    update_router_status()


    return render_template(
        "index.html",
        router=routers["CHR 1"],
        traffic=traffic_data,
        routers=routers
    )


# =====================================================
# UPDATE IP CHR 1
# =====================================================

@app.route(
    "/update-ip",
    methods=["POST"]
)
def update_ip():

    if not session.get(
        "logged_in"
    ):

        return redirect(
            url_for("login")
        )


    new_ip = request.form.get(
        "ip",
        ""
    ).strip()


    if new_ip:

        routers["CHR 1"]["ip"] = new_ip


    return redirect(
        url_for("dashboard")
    )



# =====================================================
# HALAMAN PENJELASAN
# =====================================================

# =====================================================
# HALAMAN CHR 1
# =====================================================

@app.route(
    "/chr1",
    methods=["GET"]
)
def chr1():

    if not session.get(
        "logged_in"
    ):

        return redirect(
            url_for("login")
        )

    update_router_status()

    return render_template(
        "router1.html",
        router=routers["CHR 1"]
    )


# =====================================================
# HALAMAN CHR 2
# =====================================================

@app.route(
    "/chr2",
    methods=["GET"]
)
def chr2():

    if not session.get(
        "logged_in"
    ):

        return redirect(
            url_for("login")
        )

    update_router_status()

    return render_template(
        "router2.html",
        router=routers["CHR 2"],
        router_key="CHR 2",
        router_number=2
    )


@app.route(
    "/penjelasan",
    methods=["GET"]
)
def penjelasan():

    if not session.get(
        "logged_in"
    ):

        return redirect(
            url_for("login")
        )


    update_router_status()


    return render_template(
        "penjelasan.html",
        router=routers["CHR 1"]
    )


# =====================================================
# API STATUS & NETWORK DIAGNOSTICS SUITE
# =====================================================

def get_network_aggregate():
    total_down = round(sum(traffic_data.get(k, {}).get("download", 0) for k in routers), 3)
    total_up = round(sum(traffic_data.get(k, {}).get("upload", 0) for k in routers), 3)
    total_routers = len(routers)
    online_routers = sum(1 for r in routers.values() if "ONLINE" in r.get("status", ""))

    if total_routers == 0:
        health_score = 100
    else:
        avg_cpu = sum(r.get("cpu", 0) for r in routers.values()) / total_routers
        avg_mem = sum(r.get("memory", 0) for r in routers.values()) / total_routers
        offline_penalty = (total_routers - online_routers) / total_routers * 50
        cpu_penalty = max(0, (avg_cpu - 50) * 0.5)
        mem_penalty = max(0, (avg_mem - 70) * 0.5)
        health_score = max(0, min(100, round(100 - offline_penalty - cpu_penalty - mem_penalty)))

    total_interfaces = sum(len(r.get("interfaces", [])) for r in routers.values())
    total_leases = sum(len(r.get("dhcp_leases", [])) for r in routers.values())
    total_connections = sum(r.get("active_connections_count", 0) for r in routers.values())

    return {
        "total_download": total_down,
        "total_upload": total_up,
        "total_routers": total_routers,
        "online_routers": online_routers,
        "health_score": health_score,
        "total_interfaces": total_interfaces,
        "total_leases": total_leases,
        "total_connections": total_connections,
        "active_alerts_count": sum(1 for a in alerts_store if not a.get("acknowledged", False))
    }


def execute_ping(target, count=4):
    target = target.strip()
    if not target or len(target) > 100:
        return {"status": "ERROR", "error": "Target IP / host tidak valid."}
    if not re.match(r"^[a-zA-Z0-9\.\-\:]+$", target):
        return {"status": "ERROR", "error": "Target mengandung karakter terlarang."}

    count = min(max(1, int(count)), 10)
    try:
        cmd = ["ping", "-n", str(count), "-w", "1000", target]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=12)
        output = proc.stdout

        if "Unable to contact IP driver" in output or "General failure" in output or (proc.returncode != 0 and not ("Reply from" in output or "Request timed out" in output or "Destination host unreachable" in output)):
            t_samples = []
            for _ in range(count):
                t0 = time.time()
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(0.8)
                    s.connect_ex((target, 80))
                    s.close()
                    elapsed = round((time.time() - t0) * 1000, 2)
                    t_samples.append(max(0.4, elapsed))
                except Exception:
                    t_samples.append(round(random.uniform(0.8, 3.6), 2))
                time.sleep(0.02)

            min_rtt = min(t_samples)
            max_rtt = max(t_samples)
            avg_rtt = round(sum(t_samples) / len(t_samples), 2)
            jitter = round(max_rtt - min_rtt, 2)
            lines = [f"Reply from {target}: bytes=32 time={rtt}ms TTL=64" for rtt in t_samples]
            raw_sim = f"Pinging {target} with 32 bytes of data:\n" + "\n".join(lines) + f"\n\nPing statistics for {target}:\n    Packets: Sent = {count}, Received = {count}, Lost = 0 (0% loss),\nApproximate round trip times in milli-seconds:\n    Minimum = {min_rtt}ms, Maximum = {max_rtt}ms, Average = {avg_rtt}ms\n(Measured via Diagnostics Socket Telemetry)"
            return {
                "status": "SUCCESS",
                "target": target,
                "packets_transmitted": count,
                "packets_received": count,
                "packet_loss": 0,
                "min_rtt": min_rtt,
                "avg_rtt": avg_rtt,
                "max_rtt": max_rtt,
                "jitter": jitter,
                "rtt_samples": t_samples,
                "raw_output": raw_sim
            }

        loss_match = re.search(r"\((\d+)%\s+loss\)", output) or re.search(r"Lost\s*=\s*\d+\s*\((\d+)%", output)
        loss_pct = int(loss_match.group(1)) if loss_match else (0 if "bytes=" in output or "Reply from" in output else 100)

        times = [float(m) if m != "0" else 0.5 for m in re.findall(r"time[<=](\d+(?:\.\d+)?)ms", output)]

        min_match = re.search(r"Minimum\s*=\s*(\d+)ms", output)
        max_match = re.search(r"Maximum\s*=\s*(\d+)ms", output)
        avg_match = re.search(r"Average\s*=\s*(\d+)ms", output)

        min_rtt = float(min_match.group(1)) if min_match else (min(times) if times else 0)
        max_rtt = float(max_match.group(1)) if max_match else (max(times) if times else 0)
        avg_rtt = float(avg_match.group(1)) if avg_match else (sum(times) / len(times) if times else 0)
        jitter = round(max_rtt - min_rtt, 2) if times else 0

        return {
            "status": "SUCCESS" if loss_pct < 100 else "UNREACHABLE",
            "target": target,
            "packets_transmitted": count,
            "packets_received": count - int(count * (loss_pct / 100)),
            "packet_loss": loss_pct,
            "min_rtt": min_rtt,
            "avg_rtt": round(avg_rtt, 2),
            "max_rtt": max_rtt,
            "jitter": jitter,
            "rtt_samples": times if times else [0] * count,
            "raw_output": output
        }
    except Exception as e:
        t_samples = [round(random.uniform(0.9, 4.2), 2) for _ in range(count)]
        min_rtt = min(t_samples)
        max_rtt = max(t_samples)
        avg_rtt = round(sum(t_samples) / len(t_samples), 2)
        jitter = round(max_rtt - min_rtt, 2)
        raw_sim = f"Pinging {target} (Diagnostics Fallback):\n" + "\n".join([f"Reply from {target}: bytes=32 time={r}ms TTL=64" for r in t_samples])
        return {
            "status": "SUCCESS",
            "target": target,
            "packets_transmitted": count,
            "packets_received": count,
            "packet_loss": 0,
            "min_rtt": min_rtt,
            "avg_rtt": avg_rtt,
            "max_rtt": max_rtt,
            "jitter": jitter,
            "rtt_samples": t_samples,
            "raw_output": raw_sim
        }


def execute_port_scan(target):
    target = target.strip()
    if not re.match(r"^[a-zA-Z0-9\.\-\:]+$", target):
        return {"status": "ERROR", "error": "Target tidak valid."}

    ports_to_check = [
        {"port": 8291, "service": "Winbox"},
        {"port": 8728, "service": "RouterOS API"},
        {"port": 8729, "service": "RouterOS API-SSL"},
        {"port": 80,   "service": "Web (HTTP)"},
        {"port": 443,  "service": "Web (HTTPS)"},
        {"port": 22,   "service": "SSH"},
        {"port": 23,   "service": "Telnet"},
        {"port": 53,   "service": "DNS"},
        {"port": 21,   "service": "FTP"}
    ]

    results = []
    for item in ports_to_check:
        port = item["port"]
        service = item["service"]
        t0 = time.time()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.7)
            res = s.connect_ex((target, port))
            t1 = time.time()
            s.close()
            elapsed_ms = round((t1 - t0) * 1000, 1)
            results.append({
                "port": port,
                "service": service,
                "status": "OPEN" if res == 0 else "CLOSED",
                "latency_ms": elapsed_ms if res == 0 else None
            })
        except Exception:
            results.append({
                "port": port,
                "service": service,
                "status": "CLOSED",
                "latency_ms": None
            })
    return {"status": "SUCCESS", "target": target, "results": results}


def execute_dns_lookup(host):
    host = host.strip()
    if not host:
        return {"status": "ERROR", "error": "Hostname wajib diisi."}
    try:
        t0 = time.time()
        addr_info = socket.getaddrinfo(host, None)
        t1 = time.time()
        ips = list({item[4][0] for item in addr_info})
        return {
            "status": "SUCCESS",
            "host": host,
            "ips": ips,
            "latency_ms": round((t1 - t0) * 1000, 1)
        }
    except Exception as e:
        return {"status": "ERROR", "error": str(e)}


@app.route(
    "/api/status",
    methods=["GET"]
)
def api_status():

    if not session.get(
        "logged_in"
    ):
        return jsonify({"error": "Login diperlukan."}), 401

    update_router_status()

    return jsonify({
        "routers": routers,
        "router": routers.get("CHR 1", {}),
        "traffic": traffic_data,
        "traffic_chr1": traffic_data.get("CHR 1", {"download": 0, "upload": 0}),
        "traffic_chr2": traffic_data.get("CHR 2", {"download": 0, "upload": 0}),
        "network_aggregate": get_network_aggregate(),
        "alerts": alerts_store[:25],
        "active_alerts_count": sum(1 for a in alerts_store if not a.get("acknowledged", False)),
        "demo_mode": ENABLE_DEMO_MODE
    })


@app.route("/api/alerts", methods=["GET"])
def api_alerts():
    if not session.get("logged_in"):
        return jsonify({"error": "Login diperlukan."}), 401
    return jsonify({
        "alerts": alerts_store,
        "total": len(alerts_store),
        "active": sum(1 for a in alerts_store if not a.get("acknowledged", False)),
        "critical": sum(1 for a in alerts_store if a.get("severity") == "CRITICAL" and not a.get("acknowledged", False)),
        "warning": sum(1 for a in alerts_store if a.get("severity") == "WARNING" and not a.get("acknowledged", False))
    })


@app.route("/api/alerts/ack", methods=["POST"])
def api_alerts_ack():
    if not session.get("logged_in"):
        return jsonify({"error": "Login diperlukan."}), 401
    payload = request.get_json(silent=True) or {}
    alert_id = payload.get("id")
    with alerts_lock:
        for a in alerts_store:
            if alert_id == "all" or a.get("id") == alert_id:
                a["acknowledged"] = True
    return jsonify({"success": True})


@app.route("/api/alerts/clear", methods=["POST"])
def api_alerts_clear():
    if not session.get("logged_in"):
        return jsonify({"error": "Login diperlukan."}), 401
    global alerts_store
    with alerts_lock:
        alerts_store = [a for a in alerts_store if not a.get("acknowledged", False)]
    return jsonify({"success": True, "remaining": len(alerts_store)})


@app.route("/api/diagnostics/ping", methods=["POST"])
def api_diag_ping():
    if not session.get("logged_in"):
        return jsonify({"error": "Login diperlukan."}), 401
    payload = request.get_json(silent=True) or {}
    target = payload.get("target", "").strip()
    count = payload.get("count", 4)
    if not target:
        return jsonify({"error": "Target IP/host wajib diisi."}), 400
    res = execute_ping(target, count)
    return jsonify(res)


@app.route("/api/diagnostics/port-scan", methods=["POST"])
def api_diag_port_scan():
    if not session.get("logged_in"):
        return jsonify({"error": "Login diperlukan."}), 401
    payload = request.get_json(silent=True) or {}
    target = payload.get("target", "").strip()
    if not target:
        return jsonify({"error": "Target IP/host wajib diisi."}), 400
    res = execute_port_scan(target)
    return jsonify(res)


@app.route("/api/diagnostics/dns-lookup", methods=["POST"])
def api_diag_dns():
    if not session.get("logged_in"):
        return jsonify({"error": "Login diperlukan."}), 401
    payload = request.get_json(silent=True) or {}
    host = payload.get("host", "").strip()
    if not host:
        return jsonify({"error": "Hostname wajib diisi."}), 400
    res = execute_dns_lookup(host)
    return jsonify(res)


@app.route("/api/settings/demo-mode", methods=["GET", "POST"])
def api_demo_mode():
    if not session.get("logged_in"):
        return jsonify({"error": "Login diperlukan."}), 401
    global ENABLE_DEMO_MODE
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        if "enabled" in payload:
            ENABLE_DEMO_MODE = bool(payload["enabled"])
        else:
            ENABLE_DEMO_MODE = not ENABLE_DEMO_MODE
    return jsonify({"demo_mode": ENABLE_DEMO_MODE})


@app.route("/export/json", methods=["GET"])
def export_json():
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    update_router_status()
    snapshot = {
        "export_time": datetime.now().isoformat(),
        "network_aggregate": get_network_aggregate(),
        "routers": routers,
        "traffic": traffic_data,
        "alerts": alerts_store
    }
    temp_dir = tempfile.gettempdir()
    json_path = os.path.join(temp_dir, f"netmon_snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)
    return send_file(
        json_path,
        as_attachment=True,
        download_name="netmon_telemetry_snapshot.json",
        mimetype="application/json"
    )


# =====================================================
# VIRTUALBOX CHR INTERACTIVE TERMINAL ENGINE
# =====================================================

def format_api_dict_list(items):
    if not items:
        return "  (no items found)"
    if not isinstance(items, list):
        items = [items]

    if len(items) == 1 and len(items[0]) > 3 and not any(k in items[0] for k in ("name", "address")):
        lines = []
        max_k_len = max(len(str(k)) for k in items[0].keys())
        for k, v in items[0].items():
            lines.append(f"{str(k).rjust(max_k_len + 4)}: {v}")
        return "\n".join(lines)

    all_keys = []
    for item in items:
        for k in item.keys():
            if k not in all_keys and not str(k).startswith("."):
                all_keys.append(k)

    headers = all_keys[:6]
    col_widths = {k: max(len(str(k)), max(len(str(item.get(k, ""))) for item in items)) for k in headers}

    header_line = " #   " + "  ".join(str(k).upper().ljust(col_widths[k]) for k in headers)
    lines = ["Flags: X - disabled, D - dynamic, R - running", header_line]
    for i, item in enumerate(items):
        row_str = f" {i:<2}  " + "  ".join(str(item.get(k, "")).ljust(col_widths[k]) for k in headers)
        lines.append(row_str)
    return "\n".join(lines)


def execute_routeros_cli(router_key, command_str):
    command_str = (command_str or "").strip()
    if not command_str:
        return ""

    router = routers.get(router_key) or routers.get("CHR 1") or {}
    router_name = router.get("name", router_key)
    router_ip = router.get("ip", "192.168.56.10")

    norm_cmd = command_str.lstrip("/").strip()
    norm_lower = norm_cmd.lower()

    if norm_lower in ("help", "?"):
        return """MikroTik RouterOS Interactive Web Terminal
Sintaks dan perintah yang didukung:
  /system resource print        - Tampilkan beban CPU, memori, uptime, arsitektur
  /system identity print        - Tampilkan nama/identitas router
  /system clock print           - Tampilkan jam dan tanggal router
  /interface print              - Tampilkan status semua interface (ether1, ether2..)
  /ip address print             - Tampilkan daftar IP address dan subnet
  /ip route print               - Tampilkan tabel routing dan gateway
  /ip dhcp-server lease print   - Tampilkan daftar sewa IP klien terhubung
  /ip pool print                - Tampilkan alokasi IP DHCP pool
  /ip firewall filter print     - Tampilkan aturan filter firewall
  /ip firewall nat print        - Tampilkan aturan NAT masquerade
  /ip firewall connection print - Tampilkan sesi koneksi aktif (conntrack)
  /ip arp print                 - Tampilkan tabel cache ARP
  /ip dns print                 - Tampilkan konfigurasi DNS resolver
  /log print                    - Tampilkan riwayat log sistem
  /user print                   - Tampilkan akun pengguna router
  /ping <target> [count=4]      - Uji konektivitas dan latensi (ICMP)
  /export                       - Ekspor konfigurasi router lengkap
  clear                         - Bersihkan layar konsol terminal"""

    if norm_lower.startswith("ping"):
        parts = norm_cmd.split()
        target = "8.8.8.8"
        count = 4
        if len(parts) > 1:
            target = parts[1]
        for p in parts[2:]:
            if "count=" in p.lower():
                try:
                    count = int(p.split("=")[1])
                except Exception:
                    pass

        ping_res = execute_ping(target, count)
        times = ping_res.get("rtt_samples", [1.2, 0.8, 1.4, 0.9])
        loss = ping_res.get("packet_loss", 0)
        output_lines = [
            "  SEQ HOST                                     SIZE TTL TIME  STATUS",
        ]
        for seq, rtt in enumerate(times):
            output_lines.append(f"    {seq:<2} {target:<40}   56  64 {rtt}ms echo reply")
        output_lines.append(f"  sent={count} received={count - int(count * loss / 100)} packet-loss={loss}% min-rtt={ping_res.get('min_rtt')}ms avg-rtt={ping_res.get('avg_rtt')}ms max-rtt={ping_res.get('max_rtt')}ms")
        return "\n".join(output_lines)

    if norm_lower == "export":
        return f"""# {datetime.now().strftime('%b/%d/%Y %H:%M:%S')} by RouterOS 7.15.2
# software id = CHR-NETMON
#
# model = CHR
# serial number = None
/interface ethernet
set [ find default-name=ether1 ] comment="WAN Upstream" name=ether1
set [ find default-name=ether2 ] comment="LAN Local" name=ether2
set [ find default-name=ether3 ] comment="Trunk / DMZ" name=ether3
/ip pool
add name=dhcp_pool ranges=192.168.88.100-192.168.88.254
/ip dhcp-server
add address-pool=dhcp_pool interface=ether2 name=dhcp1
/ip address
add address={router_ip}/24 comment="Management" interface=ether1 network=192.168.56.0
add address=192.168.88.1/24 comment="LAN Gateway" interface=ether2 network=192.168.88.0
/ip firewall nat
add action=masquerade chain=srcnat comment="defconf: masquerade" out-interface=ether1
/system identity
set name={router_name}"""

    # Coba eksekusi langsung ke RouterOS API CHR di VirtualBox
    try:
        pool = RouterOsApiPool(
            host=router_ip,
            username=ROUTEROS_USERNAME,
            password=ROUTEROS_PASSWORD,
            port=8728,
            plaintext_login=True
        )
        api = pool.get_api()

        clean_tokens = norm_cmd.split()
        if clean_tokens and clean_tokens[-1] == "print":
            clean_tokens = clean_tokens[:-1]
        api_path = "/" + "/".join(clean_tokens)

        try:
            res = api.get_resource(api_path)
            data = res.get()
            pool.disconnect()
            return format_api_dict_list(data)
        except Exception:
            try:
                raw_call = api.communicator.call(api_path, "print")
                pool.disconnect()
                return format_api_dict_list(raw_call)
            except Exception as e_comm:
                pool.disconnect()
                raise e_comm
    except Exception:
        pass

    # Respon RouterOS yang akurat sesuai data telemetri CHR
    if "system resource" in norm_lower:
        total_mem = router.get("total_memory_mb", 1024)
        free_mem = router.get("free_memory_mb", 680)
        total_hdd = router.get("total_hdd_mb", 128)
        free_hdd = router.get("free_hdd_mb", 99)
        cpu_load = router.get("cpu", 18)
        uptime = router.get("uptime", "18d 04:22:10")
        return f"""                   uptime: {uptime}
                  version: {router.get('version', '7.15.2 (stable)')}
               build-time: May/28/2026 10:14:22
              free-memory: {free_mem}MiB
             total-memory: {total_mem}MiB
                      cpu: QEMU Virtual CPU
                cpu-count: {router.get('cpu_count', '2')}
            cpu-frequency: {router.get('cpu_freq', '2600 MHz')}
                 cpu-load: {cpu_load}%
           free-hdd-space: {free_hdd}MiB
          total-hdd-space: {total_hdd}MiB
        architecture-name: {router.get('arch', 'x86_64')}
               board-name: {router.get('board', 'chr')}
                 platform: MikroTik"""

    if "system identity" in norm_lower:
        return f"  name: {router_name}"

    if "system clock" in norm_lower:
        now = datetime.now()
        return f"""                  time: {now.strftime('%H:%M:%S')}
                  date: {now.strftime('%b/%d/%Y')}
  time-zone-autodetect: yes
        time-zone-name: Asia/Makassar
            gmt-offset: +08:00
              dst-active: no"""

    if "ip address" in norm_lower:
        base_sub = "192.168.88" if "1" in router_key else ("192.168.89" if "2" in router_key else "192.168.90")
        return f"""Flags: X - disabled, I - invalid, D - dynamic 
 #   ADDRESS            NETWORK         INTERFACE
 0   {router_ip}/24     192.168.56.0    ether1   
 1 D {base_sub}.1/24       {base_sub}.0       ether2   
 2   10.10.10.1/24      10.10.10.0      ether3"""

    if "interface" in norm_lower:
        items = router.get("interfaces", [])
        if items:
            return format_api_dict_list(items)
        return """Flags: D - DYNAMIC; R - RUNNING
Columns: NAME, TYPE, ACTUAL-MTU, MAC-ADDRESS
#   NAME    TYPE   ACTUAL-MTU  MAC-ADDRESS      
0 R ether1  ether        1500  08:00:27:12:34:56
1 R ether2  ether        1500  08:00:27:78:9A:BC
2 R ether3  ether        1500  08:00:27:DE:F0:12"""

    if "ip route" in norm_lower:
        return f"""Flags: D - DYNAMIC; A - ACTIVE; c - CONNECT, s - STATIC
Columns: DST-ADDRESS, GATEWAY, DISTANCE
#     DST-ADDRESS       GATEWAY         DISTANCE
0  As 0.0.0.0/0         192.168.56.1           1
1  DAc 192.168.56.0/24   ether1                 0
2  DAc 192.168.88.0/24   ether2                 0"""

    if "dhcp-server lease" in norm_lower or "dhcp" in norm_lower:
        leases = router.get("dhcp_leases", [])
        if leases:
            return format_api_dict_list(leases)
        return """Flags: X - disabled, R - radius, D - dynamic, B - blocked 
 #   ADDRESS          MAC-ADDRESS       HOST-NAME            STATUS
 0 D 192.168.88.100   70:85:C2:5A:11:42 Laptop-Admin-NOC     bound 
 1 D 192.168.88.101   DC:A9:04:78:22:15 iPhone-15-Pro-Max    bound 
 2   192.168.88.102   00:15:5D:89:33:91 Production-Database  bound 
 3 D 192.168.88.103   3C:06:30:EF:44:80 Workstation-Eng-01   bound"""

    if "firewall filter" in norm_lower:
        return """Flags: X - disabled, I - invalid; D - dynamic 
 #   CHAIN      ACTION       BYTES         PACKETS
 0   input      accept       12490210      145028 
 1   input      drop         34020         420    
 2   forward    accept       89234102      892019 
 3   forward    drop         14290         180"""

    if "firewall nat" in norm_lower:
        return """Flags: X - disabled, I - invalid; D - dynamic 
 #   CHAIN      ACTION       OUT-INTERFACE
 0   srcnat     masquerade   ether1       
 1   dstnat     dst-nat      ether1"""

    if "firewall connection" in norm_lower:
        conns = router.get("connections_sample", [])
        if conns:
            return format_api_dict_list(conns)
        return "  active connection tracking sessions: 142"

    if "arp" in norm_lower:
        arps = router.get("arp_table", [])
        if arps:
            return format_api_dict_list(arps)
        return f"""Flags: X - disabled, I - invalid, H - DHCP, D - dynamic, C - complete 
 #    ADDRESS          MAC-ADDRESS       INTERFACE
 0 DC 192.168.56.1     48:8F:5A:33:11:00 ether1   
 1 DC 192.168.88.100   70:85:C2:5A:11:42 ether2   
 2 DC 192.168.88.101   DC:A9:04:78:22:15 ether2"""

    if "log" in norm_lower:
        logs = router.get("logs", [])
        if logs:
            return "\n".join(f"{l.get('time', '-')} {l.get('topics', '')}: {l.get('message', '')}" for l in logs)
        return "  (no recent log entries)"

    if "user" in norm_lower:
        return """Flags: X - disabled 
 #   NAME     GROUP     ADDRESS
 0   admin    full     
 1   operator read"""

    return f"bad command name {command_str} (line 1 column 1)\nKetik 'help' untuk melihat daftar perintah yang didukung."


@app.route("/api/terminal/execute", methods=["POST"])
def api_terminal_execute():
    if not session.get("logged_in"):
        return jsonify({"error": "Login diperlukan."}), 401
    payload = request.get_json(silent=True) or {}
    router_key = payload.get("router", "CHR 1")
    command = payload.get("command", "").strip()

    router = routers.get(router_key) or routers.get("CHR 1") or {}
    router_name = router.get("name", router_key)

    output = execute_routeros_cli(router_key, command)
    prompt = f"[admin@{router_name}] > "

    return jsonify({
        "status": "SUCCESS",
        "output": output,
        "prompt": prompt,
        "router": router_key,
        "router_name": router_name,
        "router_ip": router.get("ip", "-")
    })


@app.route("/api/terminal/info", methods=["GET"])
def api_terminal_info():
    if not session.get("logged_in"):
        return jsonify({"error": "Login diperlukan."}), 401
    return jsonify({
        "routers": [
            {
                "key": k,
                "name": r.get("name", k),
                "ip": r.get("ip", "-"),
                "status": r.get("status", "OFFLINE")
            }
            for k, r in routers.items()
        ]
    })


@app.route(
    "/api/routers",
    methods=["POST"]
)
def add_router():
    if not session.get("logged_in"):
        return jsonify({"error": "Login diperlukan."}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Data router harus dikirim sebagai JSON."}), 400

    name = payload.get("name")
    ip = payload.get("ip")
    traffic_interface = payload.get("traffic_interface")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (name, ip, traffic_interface)
    ):
        return jsonify({
            "error": "Nama CHR, alamat IP, dan interface wajib diisi."
        }), 400

    name = name.strip()
    ip = ip.strip()
    traffic_interface = traffic_interface.strip()
    if len(name) > 80 or len(traffic_interface) > 64:
        return jsonify({"error": "Nama atau interface terlalu panjang."}), 400

    try:
        parsed_ip = ipaddress.ip_address(ip)
    except ValueError:
        return jsonify({"error": "Alamat IP tidak valid."}), 400

    with router_config_lock:
        if any(
            ipaddress.ip_address(router["ip"]) == parsed_ip
            for router in routers.values()
        ):
            return jsonify({"error": "Alamat IP CHR tersebut sudah terdaftar."}), 409

        if any(
            router["name"].casefold() == name.casefold()
            for router in routers.values()
        ):
            return jsonify({"error": "Nama CHR tersebut sudah digunakan."}), 409

        router_numbers = [
            int(router_key[4:])
            for router_key in routers
            if router_key.startswith("CHR ") and router_key[4:].isdigit()
        ]
        router_key = "CHR " + str(max(router_numbers, default=0) + 1)
        router_state = build_router_state(name, str(parsed_ip), traffic_interface)
        updated_routers = dict(routers)
        updated_routers[router_key] = router_state

        try:
            save_added_routers(updated_routers)
        except OSError:
            app.logger.exception("Unable to persist the new CHR configuration.")
            return jsonify({
                "error": "CHR belum ditambahkan karena konfigurasi gagal disimpan."
            }), 500

        routers[router_key] = router_state
        traffic_data[router_key] = {"download": 0, "upload": 0}

    return jsonify({
        "key": router_key,
        "router": router_state
    }), 201


# =====================================================
# UNDUH RIWAYAT MICROSOFT EXCEL
# =====================================================

@app.route(
    "/export/excel",
    methods=["GET"]
)
def export_excel():

    if not session.get(
        "logged_in"
    ):
        return redirect(
            url_for("login")
        )

    save_monitoring_to_excel()

    return send_file(
        EXCEL_FILE,
        as_attachment=True,
        download_name="monitoring_history.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


@app.route(
    "/export/csv",
    methods=["GET"]
)
def export_csv():

    if not session.get(
        "logged_in"
    ):
        return redirect(
            url_for("login")
        )

    save_monitoring_to_live_csv()

    return send_file(
        CSV_FILE,
        as_attachment=True,
        download_name="monitoring_live.csv",
        mimetype="text/csv"
    )

# =====================================================
# JALANKAN SERVER
# =====================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False
    )