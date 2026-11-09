#!/usr/bin/env python3
"""
K3s Cluster Telegram Admin Bot
Lightweight Telegram bot using Python standard library to monitor and manage the Kubernetes cluster.
"""

import os
import sys
import json
import time
import urllib.request
import urllib.error
import subprocess
import html
import logging
import fcntl
from typing import Dict, Any, List, Optional

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("cluster-bot")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        logger.error(f"Config file not found at {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


CONFIG = load_config()
BOT_TOKEN = CONFIG.get("bot_token", "").strip()
ALLOWED_USER_IDS = set(int(uid) for uid in CONFIG.get("allowed_user_ids", []))
SERVER_LAN_IP = CONFIG.get("server_lan_ip", "192.168.1.100")
SERVER_TAILSCALE_IP = CONFIG.get("server_tailscale_ip", "100.x.y.z")
DEFAULT_NAMESPACE = CONFIG.get("default_namespace", "homelab")

# Known Web Applications & their ports
KNOWN_WEBAPPS = [
    {"name": "Homepage", "port": 30050, "path": "", "app_label": "homepage"},
    {"name": "Nextcloud", "port": 30080, "path": "/login", "app_label": "nextcloud"},
    {"name": "Paperless-ngx", "port": 30082, "path": "", "app_label": "paperless-web"},
    {"name": "Firefly III", "port": 30090, "path": "", "app_label": "firefly-app"},
    {"name": "Firefly Importer", "port": 30091, "path": "", "app_label": "firefly-importer"},
    {"name": "Grafana", "port": 30030, "path": "", "app_label": "grafana"},
    {"name": "Headlamp (K3s)", "port": 80, "path": "/", "app_label": "headlamp", "ns": "kube-system"},
    {"name": "Syncthing", "port": 8384, "path": "", "app_label": "syncthing"},
]

# App groupings for power management toggling
APP_GROUPS = {
    "nextcloud": {"label": "Nextcloud", "deps": ["nextcloud"]},
    "paperless": {"label": "Paperless", "deps": ["paperless-web"]},
    "firefly": {"label": "Firefly III", "deps": ["firefly-app", "firefly-importer"]},
    "homepage": {"label": "Homepage", "deps": ["homepage"]},
    "grafana": {"label": "Grafana", "deps": ["grafana"]},
    "garmin": {"label": "Garmin Fetcher", "deps": ["garmin-fetch-data"]},
    "mariadb": {"label": "MariaDB (Firefly)", "deps": ["firefly-db"]},
    "redis": {"label": "Redis (Paperless)", "deps": ["paperless-redis"]},
    "influxdb": {"label": "InfluxDB (Garmin)", "deps": ["influxdb"]},
    "prometheus": {"label": "Prometheus", "deps": ["prometheus"]},
}


# ==============================================================================
# Telegram API Helpers (Pure Standard Library)
# ==============================================================================

def api_call(method: str, data: Optional[dict] = None) -> Optional[dict]:
    token = CONFIG.get("bot_token", "").strip()
    if not token or "YOUR_TELEGRAM_BOT_TOKEN" in token:
        logger.warning("Bot token not configured in config.json")
        return None

    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        req_data = None
        headers = {}
        if data is not None:
            req_data = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=req_data, headers=headers)
        with urllib.request.urlopen(req, timeout=40) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="ignore")
        if "message is not modified" in err_msg:
            return None  # Harmless duplicate edit
        logger.error(f"Telegram API HTTP error {e.code}: {err_msg}")
        return None
    except Exception as e:
        logger.error(f"Telegram API connection error: {e}")
        return None


def send_message(chat_id: int, text: str, reply_markup: Optional[dict] = None) -> Optional[dict]:
    if len(text) > 4000:
        chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)]
        for idx, chunk in enumerate(chunks):
            markup = reply_markup if idx == len(chunks) - 1 else None
            api_call("sendMessage", {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": markup
            })
        return None

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return api_call("sendMessage", payload)


def edit_message(chat_id: int, message_id: int, text: str, reply_markup: Optional[dict] = None) -> Optional[dict]:
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return api_call("editMessageText", payload)


def answer_callback_query(callback_query_id: str, text: Optional[str] = None):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    api_call("answerCallbackQuery", payload)


# ==============================================================================
# Kubernetes / kubectl Command Runners
# ==============================================================================

def run_kubectl(args: List[str], timeout: int = 15) -> tuple[int, str]:
    cmd = ["kubectl"] + args
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout
        )
        output = proc.stdout if proc.returncode == 0 else proc.stderr
        return proc.returncode, output.strip()
    except subprocess.TimeoutExpired:
        return 124, "Timeout nell'esecuzione del comando kubectl."
    except Exception as e:
        return 1, f"Errore interno: {e}"


def check_http_status(url: str, timeout: float = 3.0) -> tuple[int, str]:
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "K8sClusterBot/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, "OK"
    except urllib.error.HTTPError as e:
        return e.code, e.reason
    except Exception as e:
        return 0, str(e)


def get_homelab_deployment_replicas() -> Dict[str, int]:
    """Return dictionary of deployment_name -> replicas in homelab."""
    ret, out = run_kubectl(["get", "deployments", "-n", DEFAULT_NAMESPACE, "-o", "jsonpath={range .items[*]}{.metadata.name}{\":\"}{.spec.replicas}{\"\\n\"}{end}"])
    res = {}
    if ret == 0 and out.strip():
        for line in out.strip().split("\n"):
            if ":" in line:
                name, reps = line.split(":", 1)
                try:
                    res[name.strip()] = int(reps.strip())
                except ValueError:
                    res[name.strip()] = 0
    return res


# ==============================================================================
# Core Views & Features
# ==============================================================================

def get_main_menu_markup() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "📊 Stato Pod", "callback_data": "menu_pods"},
                {"text": "⚡ CPU / RAM", "callback_data": "menu_top"}
            ],
            [
                {"text": "🔌 Accendi / Spegni", "callback_data": "menu_power"},
                {"text": "🌐 Web App & Link", "callback_data": "menu_apps"}
            ],
            [
                {"text": "🔄 Riavvia App", "callback_data": "menu_restart"},
                {"text": "📜 Log Recenti", "callback_data": "menu_logs"}
            ],
            [
                {"text": "⚠️ Eventi Cluster", "callback_data": "menu_events"},
                {"text": "💻 Nodo & Info", "callback_data": "menu_nodes"}
            ]
        ]
    }


def view_power_menu() -> tuple[str, dict]:
    """Interactive Power Management (Turn ON/OFF all or individual apps)."""
    current_reps = get_homelab_deployment_replicas()
    
    total_running = sum(1 for r in current_reps.values() if r > 0)
    total_deps = len(current_reps)

    lines = [
        "🔌 <b>Gestione Accensione & Spegnimento (Power Control)</b>\n",
        f"Stato complessivo: <b>{total_running}/{total_deps}</b> deployment attivi.\n",
        "• <b>Spegni Tutto</b>: Imposta 0 repliche per tutte le app.",
        "• <b>Accendi Tutto</b>: Riporta a 1 replica tutte le app.",
        "• Clicca sui singoli pulsanti per accendere (⚪) o spegnere (🟢).\n"
    ]

    buttons = [
        [
            {"text": "▶️ ACCENDI TUTTO", "callback_data": "power:all:on"},
            {"text": "🛑 SPEGNI TUTTO", "callback_data": "power:all:off"}
        ]
    ]

    row = []
    for key, data in APP_GROUPS.items():
        label = data["label"]
        deps = data["deps"]
        
        # Determine status: active if all deps in the group have replicas > 0
        is_active = any(current_reps.get(d, 0) > 0 for d in deps)
        icon = "🟢" if is_active else "⚪"
        next_action = "off" if is_active else "on"
        btn_text = f"{icon} {label}"
        cb = f"power:toggle:{key}:{next_action}"

        row.append({"text": btn_text, "callback_data": cb})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([
        {"text": "🔄 Aggiorna Stato", "callback_data": "menu_power"},
        {"text": "⬅️ Menu Principale", "callback_data": "menu_main"}
    ])

    return "\n".join(lines), {"inline_keyboard": buttons}


def handle_power_action(action: str, target: str, target_state: Optional[str] = None) -> str:
    """Execute scale actions for all or single apps."""
    if action == "all":
        if target == "off":
            ret, out = run_kubectl(["scale", "deployment", "--all", "-n", DEFAULT_NAMESPACE, "--replicas=0"])
            return "🛑 <b>Tutti i servizi dell'Homelab sono stati spenti (0 repliche).</b>" if ret == 0 else f"❌ Errore: {out}"
        elif target == "on":
            # Turn on all deployments (excluding garmin-fetch-data if user wants to keep it separate, or including all)
            core_deps = [
                "firefly-db", "firefly-app", "firefly-importer",
                "paperless-redis", "paperless-web",
                "nextcloud", "homepage", "grafana",
                "influxdb", "prometheus"
            ]
            ret, out = run_kubectl(["scale", "deployment"] + core_deps + ["-n", DEFAULT_NAMESPACE, "--replicas=1"])
            return "🟢 <b>Tutte le web app e i database sono stati accesi (1 replica)!</b>" if ret == 0 else f"❌ Errore: {out}"

    elif action == "toggle":
        group = APP_GROUPS.get(target)
        if not group:
            return "App non trovata."
        replicas = "1" if target_state == "on" else "0"
        deps = group["deps"]
        scale_args = ["scale", "deployment"] + deps + ["-n", DEFAULT_NAMESPACE, f"--replicas={replicas}"]
        ret, out = run_kubectl(scale_args)
        status_word = "acceso 🟢" if replicas == "1" else "spento ⚪"
        return f"✅ <b>{group['label']}</b> è stato {status_word}!" if ret == 0 else f"❌ Errore: {out}"

    return "Azione sconosciuta."


def view_pods(namespace: Optional[str] = None) -> tuple[str, dict]:
    ns = namespace or DEFAULT_NAMESPACE
    ret, out = run_kubectl(["get", "pods", "-n", ns, "-o", "json"])
    if ret != 0:
        return f"❌ <b>Errore nel recupero pod:</b>\n<pre>{html.escape(out)}</pre>", get_main_menu_markup()

    try:
        data = json.loads(out)
        items = data.get("items", [])
    except Exception as e:
        return f"❌ Errore parsing JSON: {e}", get_main_menu_markup()

    if not items:
        return f"Nessun pod in esecuzione nel namespace <code>{ns}</code> (tutti spenti).", get_main_menu_markup()

    lines = [f"📊 <b>Pod Kubernetes</b> (Namespace: <code>{ns}</code>)\n"]
    for pod in items:
        pname = pod["metadata"]["name"]
        phase = pod.get("status", {}).get("phase", "Unknown")
        cstatuses = pod.get("status", {}).get("containerStatuses", [])
        
        restarts = sum(cs.get("restartCount", 0) for cs in cstatuses)
        ready_count = sum(1 for cs in cstatuses if cs.get("ready", False))
        total_count = len(cstatuses)

        if phase == "Running" and ready_count == total_count:
            icon = "🟢"
        elif phase in ["Pending", "ContainerCreating"]:
            icon = "🟡"
        elif any(cs.get("state", {}).get("waiting", {}).get("reason") in ["CrashLoopBackOff", "ImagePullBackOff", "Error"] for cs in cstatuses):
            icon = "🔴"
        else:
            icon = "⚪"

        restart_str = f" 🔄 {restarts}" if restarts > 0 else ""
        lines.append(f"{icon} <b>{html.escape(pname)}</b>\n   Stato: {phase} ({ready_count}/{total_count}){restart_str}")

    markup = {
        "inline_keyboard": [
            [
                {"text": "🏠 Homelab", "callback_data": "pods_ns:homelab"},
                {"text": "⚙️ Kube-System", "callback_data": "pods_ns:kube-system"},
                {"text": "🐙 ArgoCD", "callback_data": "pods_ns:argocd"}
            ],
            [{"text": "🔄 Aggiorna", "callback_data": f"pods_ns:{ns}"}, {"text": "⬅️ Menu", "callback_data": "menu_main"}]
        ]
    }
    return "\n".join(lines), markup


def view_top() -> tuple[str, dict]:
    lines = ["⚡ <b>Utilizzo Risorse Cluster</b>\n"]
    
    ret_node, out_node = run_kubectl(["top", "nodes"])
    if ret_node == 0:
        lines.append("💻 <b>Nodo Master (Fedora Server):</b>")
        lines.append(f"<pre>{html.escape(out_node)}</pre>\n")
    else:
        lines.append("💻 <i>Metriche del nodo non disponibili al momento.</i>\n")

    ret_pod, out_pod = run_kubectl(["top", "pods", "-n", DEFAULT_NAMESPACE])
    if ret_pod == 0:
        lines.append(f"📦 <b>Pod ({DEFAULT_NAMESPACE}):</b>")
        lines.append(f"<pre>{html.escape(out_pod)}</pre>")
    else:
        lines.append("📦 <i>Nessun pod attivo o metriche non disponibili.</i>")

    markup = {
        "inline_keyboard": [
            [{"text": "🔄 Aggiorna", "callback_data": "menu_top"}, {"text": "⬅️ Menu", "callback_data": "menu_main"}]
        ]
    }
    return "\n".join(lines), markup


def view_apps() -> tuple[str, dict]:
    lines = [
        "🌐 <b>Stato e Collegamenti Web App</b>\n",
        f"📍 <b>LAN:</b> <code>{SERVER_LAN_IP}</code>",
        f"🔒 <b>Tailscale:</b> <code>{SERVER_TAILSCALE_IP}</code>\n"
    ]

    for app in KNOWN_WEBAPPS:
        name = app["name"]
        port = app["port"]
        path = app["path"]
        
        url_lan = f"http://{SERVER_LAN_IP}:{port}{path}" if port != 80 else f"http://{SERVER_LAN_IP}{path}"
        url_ts = f"http://{SERVER_TAILSCALE_IP}:{port}{path}" if port != 80 else f"http://{SERVER_TAILSCALE_IP}{path}"
        
        code, msg = check_http_status(url_lan, timeout=1.5)
        if code in [200, 302, 301, 401]:
            status_icon = "🟢"
            status_desc = f"HTTP {code}"
        elif code == 0:
            status_icon = "⚪"
            status_desc = "Spento / Inattivo"
        else:
            status_icon = "🟡"
            status_desc = f"HTTP {code}"

        lines.append(
            f"{status_icon} <b>{name}</b> ({status_desc})\n"
            f"   • <a href=\"{url_lan}\">LAN ({port})</a> | <a href=\"{url_ts}\">Tailscale</a>"
        )

    markup = {
        "inline_keyboard": [
            [{"text": "🔄 Ricontrolla", "callback_data": "menu_apps"}, {"text": "⬅️ Menu", "callback_data": "menu_main"}]
        ]
    }
    return "\n".join(lines), markup


def view_deployments_list(action: str) -> tuple[str, dict]:
    ret, out = run_kubectl(["get", "deployments", "-n", DEFAULT_NAMESPACE, "-o", "jsonpath={.items[*].metadata.name}"])
    if ret != 0 or not out.strip():
        return f"❌ Errore nel recupero deployment: {out}", get_main_menu_markup()

    deps = sorted(out.strip().split())
    buttons = []
    
    title = "🔄 <b>Seleziona l'app da riavviare</b> (Rollout Restart):" if action == "restart" else "📜 <b>Seleziona l'app di cui visualizzare i log:</b>"

    row = []
    for d in deps:
        cb = f"{action}:{d}"
        row.append({"text": d, "callback_data": cb})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([{"text": "⬅️ Annulla / Menu", "callback_data": "menu_main"}])
    return title, {"inline_keyboard": buttons}


def view_events() -> tuple[str, dict]:
    ret, out = run_kubectl(["get", "events", "-A", "--sort-by=.lastTimestamp"])
    if ret != 0:
        return f"❌ Errore lettura eventi: {out}", get_main_menu_markup()

    raw_lines = [l for l in out.strip().split("\n") if l.strip()]
    if len(raw_lines) <= 1:
        return "✅ <b>Nessun evento registrato nel cluster.</b>", get_main_menu_markup()

    warn_lines = [l for l in raw_lines[1:] if "Warning" in l]
    recent_lines = raw_lines[-10:] if len(raw_lines) > 10 else raw_lines[1:]

    msg = ["⚠️ <b>Eventi Recenti del Cluster</b>\n"]
    if warn_lines:
        msg.append("<b>Avvisi (Warnings):</b>")
        for w in warn_lines[-6:]:
            msg.append(f"• <code>{html.escape(w[:120])}</code>")
        msg.append("")

    msg.append("<b>Ultimi Eventi Generali:</b>")
    for r in recent_lines[-6:]:
        msg.append(f"• <code>{html.escape(r[:120])}</code>")

    markup = {
        "inline_keyboard": [
            [{"text": "🔄 Aggiorna", "callback_data": "menu_events"}, {"text": "⬅️ Menu", "callback_data": "menu_main"}]
        ]
    }
    return "\n".join(msg), markup


def view_nodes() -> tuple[str, dict]:
    ret, out = run_kubectl(["get", "nodes", "-o", "wide"])
    if ret != 0:
        return f"❌ Errore nodo: {out}", get_main_menu_markup()

    ret_ver, out_ver = run_kubectl(["version", "--short"])
    version_text = out_ver if ret_ver == 0 else ""

    text = (
        "💻 <b>Informazioni Nodo & Cluster K3s</b>\n\n"
        f"<pre>{html.escape(out)}</pre>\n"
        f"<b>Versione:</b>\n<pre>{html.escape(version_text)}</pre>"
    )

    markup = {
        "inline_keyboard": [
            [{"text": "🔄 Aggiorna", "callback_data": "menu_nodes"}, {"text": "⬅️ Menu", "callback_data": "menu_main"}]
        ]
    }
    return text, markup


# ==============================================================================
# Telegram Event Handlers
# ==============================================================================

def handle_callback(cb: dict):
    cb_id = cb["id"]
    from_id = cb["from"]["id"]
    data = cb.get("data", "")
    msg = cb.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    msg_id = msg.get("message_id")

    if from_id not in ALLOWED_USER_IDS:
        answer_callback_query(cb_id, "⛔ Non autorizzato")
        return

    answer_callback_query(cb_id)

    if data == "menu_main":
        edit_message(chat_id, msg_id, "☸️ <b>Pannello di Controllo Cluster K3s</b>\nScegli un'operazione:", get_main_menu_markup())
    elif data == "menu_power":
        text, markup = view_power_menu()
        edit_message(chat_id, msg_id, text, markup)
    elif data.startswith("power:"):
        parts = data.split(":")
        sub_action = parts[1]
        if sub_action == "all":
            state = parts[2]
            edit_message(chat_id, msg_id, f"⏳ Esecuzione in corso (All -> {state})...")
            handle_power_action("all", state)
            text, markup = view_power_menu()
            edit_message(chat_id, msg_id, text, markup)
        elif sub_action == "toggle":
            target = parts[2]
            target_state = parts[3]
            handle_power_action("toggle", target, target_state)
            text, markup = view_power_menu()
            edit_message(chat_id, msg_id, text, markup)
    elif data == "menu_pods" or data.startswith("pods_ns:"):
        ns = data.split(":", 1)[1] if ":" in data else DEFAULT_NAMESPACE
        text, markup = view_pods(ns)
        edit_message(chat_id, msg_id, text, markup)
    elif data == "menu_top":
        text, markup = view_top()
        edit_message(chat_id, msg_id, text, markup)
    elif data == "menu_apps":
        text, markup = view_apps()
        edit_message(chat_id, msg_id, text, markup)
    elif data == "menu_restart":
        text, markup = view_deployments_list(action="restart")
        edit_message(chat_id, msg_id, text, markup)
    elif data.startswith("restart:"):
        dep = data.split(":", 1)[1]
        edit_message(chat_id, msg_id, f"⏳ Riavvio in corso per <code>{html.escape(dep)}</code>...")
        ret, out = run_kubectl(["rollout", "restart", f"deployment/{dep}", "-n", DEFAULT_NAMESPACE])
        res_text = f"✅ <b>Riavviato {html.escape(dep)}</b> con successo!" if ret == 0 else f"❌ <b>Errore riavvio {html.escape(dep)}:</b>\n<pre>{html.escape(out)}</pre>"
        edit_message(chat_id, msg_id, res_text, get_main_menu_markup())
    elif data == "menu_logs":
        text, markup = view_deployments_list(action="logs")
        edit_message(chat_id, msg_id, text, markup)
    elif data.startswith("logs:"):
        dep = data.split(":", 1)[1]
        edit_message(chat_id, msg_id, f"⏳ Recupero log per <code>{html.escape(dep)}</code>...")
        ret, out = run_kubectl(["logs", f"deployment/{dep}", "-n", DEFAULT_NAMESPACE, "--tail=35"])
        log_content = out if out.strip() else "(Nessun log recente disponibile)"
        res_text = f"📜 <b>Log recenti: {html.escape(dep)}</b>\n\n<pre>{html.escape(log_content[:3500])}</pre>"
        markup = {
            "inline_keyboard": [
                [{"text": "🔄 Aggiorna Log", "callback_data": f"logs:{dep}"}],
                [{"text": "⬅️ Altre App", "callback_data": "menu_logs"}, {"text": "🏠 Menu", "callback_data": "menu_main"}]
            ]
        }
        edit_message(chat_id, msg_id, res_text, markup)
    elif data == "menu_events":
        text, markup = view_events()
        edit_message(chat_id, msg_id, text, markup)
    elif data == "menu_nodes":
        text, markup = view_nodes()
        edit_message(chat_id, msg_id, text, markup)


def handle_message(msg: dict):
    from_user = msg.get("from", {})
    user_id = from_user.get("id")
    chat_id = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()

    if user_id not in ALLOWED_USER_IDS:
        logger.warning(f"Unauthorized access attempt from user_id={user_id} (@{from_user.get('username')})")
        send_message(chat_id, "⛔ <b>Accesso non autorizzato.</b>")
        return

    cmd = text.split()[0].lower() if text else ""
    args = text.split()[1:] if len(text.split()) > 1 else []

    if cmd in ["/start", "/menu"]:
        send_message(
            chat_id,
            "☸️ <b>Pannello di Controllo K3s Cluster</b>\n\n"
            "Benvenuto! Da qui puoi gestire e monitorare le applicazioni dell'HomeLab:\n"
            "• Spegnere e accendere tutti i servizi o i singoli componenti\n"
            "• Controllare lo stato dei pod e le risorse CPU/RAM\n"
            "• Verificare i link alle web app e leggere i log in diretta.",
            get_main_menu_markup()
        )
    elif cmd in ["/power", "/on", "/off"]:
        text_p, markup_p = view_power_menu()
        send_message(chat_id, text_p, markup_p)
    elif cmd in ["/pods", "/status"]:
        ns = args[0] if args else DEFAULT_NAMESPACE
        body, markup = view_pods(ns)
        send_message(chat_id, body, markup)
    elif cmd in ["/top", "/risorse"]:
        body, markup = view_top()
        send_message(chat_id, body, markup)
    elif cmd in ["/apps", "/webapps"]:
        body, markup = view_apps()
        send_message(chat_id, body, markup)
    elif cmd == "/restart":
        if args:
            dep = args[0]
            ret, out = run_kubectl(["rollout", "restart", f"deployment/{dep}", "-n", DEFAULT_NAMESPACE])
            res = f"✅ <b>Riavviato {html.escape(dep)}</b>!" if ret == 0 else f"❌ Errore: {out}"
            send_message(chat_id, res)
        else:
            body, markup = view_deployments_list(action="restart")
            send_message(chat_id, body, markup)
    elif cmd == "/logs":
        if args:
            dep = args[0]
            ret, out = run_kubectl(["logs", f"deployment/{dep}", "-n", DEFAULT_NAMESPACE, "--tail=40"])
            send_message(chat_id, f"📜 <b>Log: {html.escape(dep)}</b>\n<pre>{html.escape(out[:3500])}</pre>")
        else:
            body, markup = view_deployments_list(action="logs")
            send_message(chat_id, body, markup)
    elif cmd == "/events":
        body, markup = view_events()
        send_message(chat_id, body, markup)
    elif cmd in ["/nodes", "/info"]:
        body, markup = view_nodes()
        send_message(chat_id, body, markup)
    elif cmd in ["/help", "/aiuto"]:
        help_text = (
            "📖 <b>Comandi Disponibili:</b>\n\n"
            "• <code>/menu</code> - Apre la tastiera dei comandi rapidi\n"
            "• <code>/power</code> - Menu Accendi/Spegni (tutto o singoli)\n"
            "• <code>/pods [namespace]</code> - Elenco e stato dei pod\n"
            "• <code>/top</code> - Metriche CPU e RAM di nodo e pod\n"
            "• <code>/apps</code> - Stato e link di tutte le web app\n"
            "• <code>/restart [app]</code> - Esegue un rollout restart\n"
            "• <code>/logs [app]</code> - Mostra gli ultimi log\n"
            "• <code>/events</code> - Mostra gli eventi recenti e avvisi\n"
            "• <code>/nodes</code> - Info sullo stato del nodo K3s"
        )
        send_message(chat_id, help_text)
    else:
        send_message(chat_id, "Comando non riconosciuto. Digita <code>/menu</code> o <code>/help</code> per visualizzare le opzioni.", get_main_menu_markup())


# ==============================================================================
# Main Polling Loop
# ==============================================================================

def acquire_single_instance_lock():
    lock_file = "/tmp/cluster-bot.lock"
    try:
        lock_fd = open(lock_file, "a+")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.seek(0)
        lock_fd.truncate()
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
        return lock_fd
    except (BlockingIOError, IOError):
        print("\n" + "=" * 65)
        print("💡 [AVVISO] cluster-bot è GIÀ attivo in background via systemd!")
        print("=" * 65)
        print("Non serve tenerlo aperto nel terminale: puoi già usare")
        print("il bot direttamente nella chat di Telegram (@myclusterremotebot)!\n")
        print("• Per seguire i log in tempo reale:")
        print("  journalctl --user -u cluster-bot.service -f\n")
        print("• Per riavviarlo:")
        print("  systemctl --user restart cluster-bot.service\n")
        print("=" * 65 + "\n")
        sys.exit(0)


def main():
    lock_handle = acquire_single_instance_lock()
    logger.info("Avvio K3s Cluster Telegram Admin Bot...")
    last_update_id = 0

    token = CONFIG.get("bot_token", "").strip()
    if not token or "YOUR_TELEGRAM_BOT_TOKEN" in token:
        logger.warning("Bot token non configurato! Inserisci il token in config.json.")
        while True:
            time.sleep(10)
            c = load_config()
            tok = c.get("bot_token", "").strip()
            if tok and "YOUR_TELEGRAM_BOT_TOKEN" not in tok:
                logger.info("Nuovo token rilevato, avvio polling...")
                CONFIG["bot_token"] = tok
                break

    me = api_call("getMe")
    if me and me.get("ok"):
        bot_info = me["result"]
        logger.info(f"Connesso con successo come @{bot_info.get('username')} (ID: {bot_info.get('id')})")
    else:
        logger.error("Impossibile connettersi alle API di Telegram con il token fornito.")

    while True:
        try:
            updates = api_call("getUpdates", {
                "offset": last_update_id + 1,
                "timeout": 30
            })

            if updates and updates.get("ok"):
                for item in updates.get("result", []):
                    last_update_id = item["update_id"]
                    if "message" in item:
                        handle_message(item["message"])
                    elif "callback_query" in item:
                        handle_callback(item["callback_query"])
            elif updates and updates.get("error_code") == 409:
                logger.warning("Conflitto 409 (altra istanza in esecuzione). Attesa 5 secondi...")
                time.sleep(5)

        except KeyboardInterrupt:
            logger.info("Chiusura richiesta dall'utente.")
            break
        except Exception as e:
            logger.error(f"Errore nel polling loop: {e}")
            time.sleep(3)


if __name__ == "__main__":
    main()
