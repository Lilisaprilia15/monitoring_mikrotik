from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import requests
import socket
import time
from routeros_api import RouterOsApiPool


app = Flask(__name__)
app.secret_key = "NETMON_SECRET_2026"


GOOGLE_SCRIPT_URL = "https://script.google.com/macros/s/AKfycbyHmBA3tcVU5i0hcpcmcVdZgBvOyxQ6ynPBdWbJoDniQBIkT0sAasuIINREz6VnYCDx5Q/exec"


# =====================================================
# LOGIN ROUTEROS
# =====================================================

ROUTEROS_USERNAME = "admin"

# GANTI dengan password login MikroTik kamu
ROUTEROS_PASSWORD = "123"


# =====================================================
# DATA ROUTER
# =====================================================

routers = {

    "CHR 1": {
        "name": "MikroTik-CHR-1",
        "ip": "192.168.56.10",
        "version": "RouterOS 7.x",
        "cpu": 0,
        "memory": 0,
        "uptime": "Router Offline",
        "status": "OFFLINE",
        "interface": "-"
    },

    "CHR 2": {
        "name": "MikroTik-CHR-2",
        "ip": "192.168.56.11",
        "version": "RouterOS 7.x",
        "cpu": 0,
        "memory": 0,
        "uptime": "Router Offline",
        "status": "OFFLINE",
        "interface": "-"
    }

}


# =====================================================
# TRAFFIC
# =====================================================

traffic_data = {
    "download": 0,
    "upload": 0
}


# =====================================================
# DATA TRAFFIC SEBELUMNYA
# =====================================================

previous_traffic = {}


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
        # CPU
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
        # CARI INTERFACE RUNNING
        # -------------------------------------------------

        interface_resource = api.get_resource(
            "/interface"
        )

        interfaces = interface_resource.get()


        selected_interface = None


        for interface in interfaces:

            running = interface.get(
                "running",
                "false"
            )

            disabled = interface.get(
                "disabled",
                "false"
            )

            interface_name = interface.get(
                "name",
                ""
            )


            if (
                str(running).lower() == "true"
                and
                str(disabled).lower() != "true"
                and
                interface_name != "lo"
            ):

                selected_interface = interface

                break


        # -------------------------------------------------
        # TRAFFIC
        # -------------------------------------------------

        download = 0
        upload = 0


        if selected_interface:

            interface_name = selected_interface.get(
                "name",
                "-"
            )

            rx_bytes = int(
                selected_interface.get(
                    "rx-byte",
                    0
                )
            )

            tx_bytes = int(
                selected_interface.get(
                    "tx-byte",
                    0
                )
            )


            current_time = time.time()


            old_data = previous_traffic.get(
                router_name
            )


            if old_data:

                time_difference = (
                    current_time -
                    old_data["time"]
                )


                if time_difference > 0:

                    download = (
                        (
                            rx_bytes -
                            old_data["rx"]
                        )
                        *
                        8
                        /
                        time_difference
                        /
                        1_000_000
                    )


                    upload = (
                        (
                            tx_bytes -
                            old_data["tx"]
                        )
                        *
                        8
                        /
                        time_difference
                        /
                        1_000_000
                    )


                    download = max(
                        0,
                        round(
                            download,
                            2
                        )
                    )


                    upload = max(
                        0,
                        round(
                            upload,
                            2
                        )
                    )


            previous_traffic[
                router_name
            ] = {

                "rx": rx_bytes,

                "tx": tx_bytes,

                "time": current_time

            }


        else:

            interface_name = "-"


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
        # SIMPAN TRAFFIC CHR 1
        # -------------------------------------------------

        if router_name == "CHR 1":

            traffic_data["download"] = download

            traffic_data["upload"] = upload


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


        router["status"] = "OFFLINE"

        router["cpu"] = 0

        router["memory"] = 0

        router["uptime"] = "Router Offline"

        router["interface"] = "-"


        return False


# =====================================================
# UPDATE SEMUA ROUTER
# =====================================================

def update_router_status():

    for router_name, router in routers.items():

        get_router_data(
            router_name,
            router
        )


# =====================================================
# KIRIM KE GOOGLE SPREADSHEET
# =====================================================

def send_to_spreadsheet():

    router = routers["CHR 1"]


    data = {

        "router_name":
            router["name"],

        "ip":
            router["ip"],

        "version":
            router["version"],

        "cpu":
            router["cpu"],

        "memory":
            router["memory"],

        "uptime":
            router["uptime"],

        "download":
            traffic_data["download"],

        "upload":
            traffic_data["upload"]

    }


    try:

        response = requests.post(
            GOOGLE_SCRIPT_URL,
            json=data,
            timeout=10
        )


        print()
        print("====================================")
        print(
            "STATUS CODE:",
            response.status_code
        )

        print(
            "DATA DIKIRIM KE GOOGLE SPREADSHEET"
        )

        print(
            response.text
        )

        print("====================================")


        return True


    except Exception as e:

        print()
        print("====================================")
        print(
            "GAGAL MENGIRIM KE SPREADSHEET"
        )

        print(
            str(e)
        )

        print("====================================")


        return False


# =====================================================
# LOGIN
# =====================================================

@app.route(
    "/login",
    methods=["GET", "POST"]
)
def login():

    if request.method == "POST":

        username = request.form.get(
            "username"
        )

        password = request.form.get(
            "password"
        )


        if (
            username == "admin"
            and
            password == "admin123"
        ):

            session["logged_in"] = True

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
# KIRIM KE SPREADSHEET
# =====================================================

@app.route(
    "/send-to-sheet",
    methods=["POST"]
)
def send_to_sheet():

    if not session.get(
        "logged_in"
    ):

        return redirect(
            url_for("login")
        )


    update_router_status()

    send_to_spreadsheet()


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
        router=routers["CHR 2"]
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
# API STATUS
# =====================================================

@app.route(
    "/api/status",
    methods=["GET"]
)
def api_status():

    update_router_status()


    return jsonify({

        "routers":
            routers,

        "router":
            routers["CHR 1"],

        "traffic":
            traffic_data

    })


# =====================================================
# JALANKAN SERVER
# =====================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True
    )