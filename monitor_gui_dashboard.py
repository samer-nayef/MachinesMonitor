#!/usr/bin/env python3
# to produce executable for linux : pyinstaller --onefile --noconsole --icon=icon.ico monitor_gui_dashboard.py

import customtkinter as ctk
import tkinter as tk
from tkinter import ttk, messagebox
import threading
import paramiko
import yaml
import os
import sys
from customtkinter import CTkFont
from pymongo import MongoClient
from datetime import datetime, date

# ---------------------------
# CONFIG
# ---------------------------
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

SSH_TIMEOUT = 5
MACHINES_YML = "machines.yml"
APP_NAME = "Servers Services Monitor"

# ---------------------------
# YAML utilities
# ---------------------------
def load_yaml(path):
    if os.path.exists(path):
        with open(path, "r") as f:
            return yaml.safe_load(f)
    return {"machines": {}}

# ---------------------------
# SSH Check
# ---------------------------
def check_service(host, user, password, service):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(
            hostname=host,
            username=user,
            password=password,
            timeout=SSH_TIMEOUT,
            allow_agent=False,
            look_for_keys=False,
        )
        _, stdout, _ = ssh.exec_command(f"systemctl is-active {service}")
        return stdout.read().decode().strip() or "unknown"
    except Exception:
        return "error"
    finally:
        ssh.close()

# ---------------------------
# MongoDB Activity Check
# ---------------------------
def check_mongo_activity(machine_name, service_name, machine, mongo_cfg):
    if "mongodb_query" not in machine:
        return None

    for conn_name, queries in machine["mongodb_query"].items():
        if service_name not in queries:
            continue
        conn_info = mongo_cfg.get(conn_name)
        if not conn_info:
            continue
        coll_name = queries[service_name]["collection"]
        filter_field = queries[service_name]["filter_field"]
        try:
            client = MongoClient(
                conn_info["uri"],
                username=conn_info.get("username"),
                password=conn_info.get("password"),
            )
            db = client[conn_info["database"]]
            coll = db[coll_name]
            latest_doc = coll.find({filter_field: {"$exists": True}}).sort("crawling_date", -1).limit(1)
            latest_doc = list(latest_doc)
            if not latest_doc:
                return "inactive"
            doc_date = latest_doc[0].get("crawling_date")
            if isinstance(doc_date, datetime):
                doc_date = doc_date.date()
            elif isinstance(doc_date, str):
                doc_date = datetime.fromisoformat(doc_date).date()
            else:
                return "inactive"
            return "active" if doc_date >= date.today() else "inactive"
        except Exception as e:
            print(f"Mongo check error {machine_name}/{service_name}: {e}")
            return "error"
    return None

# ---------------------------
# Restart service
# ---------------------------
def restart_service(host, user, password, service):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(
            hostname=host,
            username=user,
            password=password,
            timeout=SSH_TIMEOUT,
            allow_agent=False,
            look_for_keys=False,
        )
        stdin, stdout, stderr = ssh.exec_command(
            f"sudo -S systemctl restart {service}",
            get_pty=True
        )
        stdin.write(password + "\n")
        stdin.flush()
        stdout.channel.recv_exit_status()  # wait for command to complete
        return True
    except Exception as e:
        print(f"Restart error: {e}")
        return False
    finally:
        ssh.close()

# ---------------------------
# Check VPN group
# ---------------------------
def check_vpn_group(group_name, machines, mongo_connections):
    def worker():
        result_table.delete(*result_table.get_children())
        status_label.configure(text=f"Checking Server: {group_name}")
        progress.set(0)
        progress_label.configure(text="0%")

        tasks = []
        for m_name, m in machines.items():
            for svc in m.get("services", []):
                tasks.append((m_name, m, svc, "ssh"))
            for conn_name, svc_dict in m.get("mongodb_query", {}).items():
                for svc_name in svc_dict.keys():
                    tasks.append((m_name, m, svc_name, "mongo"))

        total = len(tasks)
        if total == 0:
            messagebox.showwarning("Empty", "No services in this group")
            return

        for idx, (machine_name, machine, service_name, svc_type) in enumerate(tasks):
            status_label.configure(text=f"Checking {machine_name} -> {service_name}")
            root.update_idletasks()

            if svc_type == "ssh":
                status = check_service(machine["host"], machine["user"], machine["password"], service_name)
            else:
                status = check_mongo_activity(machine_name, service_name, machine, mongo_connections)

            tag = "ok" if status == "active" else "bad"
            result_table.insert(
                "",
                "end",
                text="",
                image=restart_icon,
                values=(group_name, machine_name, service_name, status),
                tags=(tag, "restart"),
            )
            result_table.tag_configure("ok", background="#1f3d2b", foreground="#4cff9a")
            result_table.tag_configure("bad", background="#3d1f1f", foreground="#ff6b6b")

            progress_value = (idx + 1) / total
            progress.set(progress_value)
            progress_label.configure(text=f"{int(progress_value*100)}%")

        status_label.configure(text=f"{group_name} completed")
        progress_label.configure(text="100%")
    threading.Thread(target=worker, daemon=True).start()

# ---------------------------
# Show SERVER Details
# ---------------------------
def show_group_details(vpn_name, machines):
    detail_win = ctk.CTkToplevel(root)
    detail_win.title(f"{vpn_name} Services")
    detail_win.geometry("400x400")
    text = ""
    for m_name, m in machines.items():
        text += f"{m_name}:\n"
        for svc in m.get("services", []):
            text += f"  - [SSH] {svc}\n"
        for conn_name, svc_dict in m.get("mongodb_query", {}).items():
            for svc_name, svc_info in svc_dict.items():
                coll = svc_info.get("collection","?")
                field = svc_info.get("filter_field","?")
                text += f"  - [Mongo:{conn_name}] {svc_name} -> {coll}.{field}\n"
        text += "\n"
    textbox = ctk.CTkTextbox(detail_win)
    textbox.pack(expand=True, fill="both", padx=10, pady=10)
    textbox.insert("0.0", text)
    textbox.configure(state="disabled")

# ---------------------------
# MAIN GUI
# ---------------------------
root = ctk.CTk()
root.title(APP_NAME)
root.geometry("1350x650")

main = ctk.CTkFrame(root)
main.pack(expand=True, fill="both", padx=10, pady=10)

left = ctk.CTkFrame(main, width=300)
left.pack(side="left", fill="y", padx=(0, 10))

right = ctk.CTkFrame(main)
right.pack(side="right", expand=True, fill="both")

# ---------------------------
# Load machines and Mongo connections
# ---------------------------
raw = load_yaml(MACHINES_YML).get("machines", {})
mongo_connections = load_yaml(MACHINES_YML).get("mongo_connections", {})

vpn_groups = {}
for name, machine in raw.items():
    vpn = machine.get("vpn_group", "ungrouped")
    vpn_groups.setdefault(vpn, {})[name] = machine

# ---------------------------
# SERVER Cards with info icon on LEFT
# ---------------------------
ctk.CTkLabel(left, text="SERVERS", font=ctk.CTkFont(size=16, weight="bold")).pack(pady=10)
for vpn_name, machines in vpn_groups.items():
    card = ctk.CTkFrame(left, corner_radius=14)
    card.pack(fill="x", padx=10, pady=8)
    top_row = ctk.CTkFrame(card)
    top_row.pack(fill="x", padx=10, pady=(8,4))
    ctk.CTkLabel(top_row, text=vpn_name, anchor="w", font=ctk.CTkFont(weight="bold")).pack(side="left", fill="x", expand=True)
    ctk.CTkButton(
        top_row,
        text="i",
        width=30, height=30,
        fg_color="#2a2a2a",
        hover_color="#444444",
        command=lambda g=vpn_name, m=machines: show_group_details(g,m)
    ).pack(side="right")
    ctk.CTkButton(
        card,
        text="Check Server",
        command=lambda g=vpn_name, m=machines: check_vpn_group(g,m,mongo_connections)
    ).pack(fill="x", padx=10, pady=(0,10))

# ---------------------------
# Status + Progress
# ---------------------------
status_label = ctk.CTkLabel(right, text="Idle")
status_label.pack(anchor="w", padx=10, pady=(10,2))
progress_frame = ctk.CTkFrame(right, fg_color=None, height=30)
progress_frame.pack(fill="x", padx=10, pady=(0,10))
progress = ctk.CTkProgressBar(progress_frame)
progress.pack(fill="x", expand=True, padx=5, pady=5)
progress.set(0)
progress_label = ctk.CTkLabel(progress_frame, text="0%", font=CTkFont(size=10, weight="bold"), fg_color=None)
progress_label.place(relx=0.5, rely=0.5, anchor="center")

# ---------------------------
# Result Table
# ---------------------------
columns = ("SERVERS", "Machine", "Service", "Status")
result_table = ttk.Treeview(right, columns=columns, show="tree headings")
result_table.heading("#0", text="Restart")
result_table.column("#0", anchor="center", width=50)
for col in columns:
    result_table.heading(col, text=col)
    result_table.column(col, anchor="center", width=240)

def resource_path(relative_path):
    base_path = getattr(sys, "_MEIPASS", os.path.abspath("."))
    return os.path.join(base_path, relative_path)

try:
    restart_icon = tk.PhotoImage(file=resource_path("restart.png"))
    restart_icon = restart_icon.subsample(
        max(1, restart_icon.width() // 20),
        max(1, restart_icon.height() // 20),
    )
except Exception:
    restart_icon = None

def on_table_click(event):
    region = result_table.identify("region", event.x, event.y)
    if region not in ("tree", "cell"):
        return

    col = result_table.identify_column(event.x)
    row = result_table.identify_row(event.y)

    if not row or col != "#0":  # Restart icon column
        return

    values = result_table.item(row, "values")
    server_group, machine_name, service_name = values[0], values[1], values[2]

    machine = vpn_groups[server_group][machine_name]

    if messagebox.askyesno("Restart Service", f"Restart {service_name} on {machine_name}?"):
        ok = restart_service(
            machine["host"],
            machine["user"],
            machine["password"],
            service_name
        )

        # refresh status
        new_status = check_service(machine["host"], machine["user"], machine["password"], service_name)
        new_tag = "ok" if new_status == "active" else "bad"

        # Update row values and ensure the icon stays
        result_table.item(row, image=restart_icon, values=(server_group, machine_name, service_name, new_status))

        # Manually re-apply the tag to refresh row colors
        result_table.item(row, tags=(new_tag, "restart"))

        messagebox.showinfo(
            "Result",
            "Service restarted successfully" if ok else "Restart failed"
        )




result_table.bind("<Button-1>", on_table_click)

def on_table_motion(event):
    region = result_table.identify("region", event.x, event.y)
    if region not in ("tree", "cell"):
        result_table.configure(cursor="")
        return
    col = result_table.identify_column(event.x)
    row = result_table.identify_row(event.y)
    if row and col == "#0":
        result_table.configure(cursor="hand2")
    else:
        result_table.configure(cursor="")

result_table.bind("<Motion>", on_table_motion)

style = ttk.Style()
style.theme_use("default")
style.configure("Treeview", background="#1e1e1e", foreground="white", rowheight=30, fieldbackground="#1e1e1e")
style.configure("Treeview.Heading", background="#2a2a2a", foreground="white")

result_table.pack(expand=True, fill="both", padx=10, pady=10)
result_table.tag_configure("ok", background="#1f3d2b", foreground="#4cff9a")
result_table.tag_configure("bad", background="#3d1f1f", foreground="#ff6b6b")
result_table.tag_configure("restart", foreground="#4da6ff")

root.mainloop()
