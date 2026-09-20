#!/usr/bin/env python3
"""Détecte les appareils inconnus connectés au réseau via l'API Freebox OS."""

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from freebox_api import Freepybox

load_dotenv()

APP_DESC = {
    "app_id": "fr.anne-laures.network-watch",
    "app_name": "Network Watch",
    "app_version": "1.0",
    "device_name": "network-watch-script",
}

WHITELIST_PATH = Path(__file__).parent / "whitelist.json"
TOKEN_FILE = Path(__file__).parent / ".freebox_token.json"
STATE_PATH = Path(__file__).parent / ".alert_state.json"


def load_whitelist() -> dict:
    if not WHITELIST_PATH.exists():
        return {}
    with open(WHITELIST_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_whitelist(whitelist: dict) -> None:
    # Copie de sécurité : la liste blanche n'est pas versionnée (gitignorée), donc pas de retour arrière
    if WHITELIST_PATH.exists():
        shutil.copy2(WHITELIST_PATH, WHITELIST_PATH.with_suffix(".json.bak"))
    with open(WHITELIST_PATH, "w", encoding="utf-8") as f:
        json.dump(whitelist, f, indent=2, ensure_ascii=False, sort_keys=True)


def extract_mac(host: dict) -> str | None:
    l2ident = host.get("l2ident") or {}
    return l2ident.get("id")


MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")


def normalize_mac(value: str) -> str:
    """Accepte AA:BB:..., aa-bb-... ; retourne le format de la Freebox (majuscules, ':')."""
    mac = value.strip().upper().replace("-", ":")
    if not MAC_RE.match(mac):
        raise argparse.ArgumentTypeError(f"adresse MAC invalide : {value!r} (attendu AA:BB:CC:DD:EE:FF)")
    return mac


def extract_last_seen(host: dict) -> tuple[str | None, str | None]:
    """Retourne (ip, heure_derniere_activite) à partir de la connectivité la plus récente."""
    connectivities = host.get("l3connectivities") or []
    if not connectivities:
        return None, None
    latest = max(connectivities, key=lambda c: c.get("last_activity") or 0)
    ip = latest.get("addr")
    ts = latest.get("last_activity")
    when = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else None
    return ip, when


def extract_first_seen(host: dict) -> str | None:
    ts = host.get("first_activity")
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else None


# Toast Windows. Le titre et le corps viennent d'appareils du réseau (le nom d'un appareil est
# choisi par son propriétaire) : ils sont passés par variables d'environnement (WSLENV), jamais
# insérés dans la commande PowerShell, et CreateTextNode échappe le XML.
TOAST_SCRIPT = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $xml.GetElementsByTagName('text')
$texts.Item(0).AppendChild($xml.CreateTextNode($env:WATCH_TITLE)) | Out-Null
$texts.Item(1).AppendChild($xml.CreateTextNode($env:WATCH_BODY)) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""


# cron ne fournit qu'un PATH minimal (/usr/bin:/bin) : powershell.exe n'y est pas, d'où le chemin absolu.
POWERSHELL_FALLBACK = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"


def notify(title: str, body: str) -> str | None:
    """Affiche une notification Windows depuis WSL. Retourne None si OK, sinon la raison de l'échec."""
    powershell = shutil.which("powershell.exe") or (POWERSHELL_FALLBACK if Path(POWERSHELL_FALLBACK).exists() else None)
    if not powershell:
        return "powershell.exe introuvable (WSL requis)"
    env = dict(os.environ, WATCH_TITLE=title, WATCH_BODY=body)
    wslenv = [v for v in env.get("WSLENV", "").split(":") if v]
    env["WSLENV"] = ":".join(wslenv + ["WATCH_TITLE/u", "WATCH_BODY/u"])
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", TOAST_SCRIPT],
            env=env, capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return "PowerShell n'a pas répondu en 60 s"
    except OSError as exc:
        return f"impossible de lancer PowerShell ({exc})"
    if result.returncode != 0:
        return f"PowerShell a échoué (code {result.returncode}) : {result.stderr.strip()[:200]}"
    return None


def load_alerted() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_alerted(alerted: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(alerted, f, indent=2, sort_keys=True)


def describe(host: dict) -> str:
    name = host.get("primary_name") or "sans nom"
    vendor = host.get("vendor_name") or "constructeur inconnu"
    ip = extract_last_seen(host)[0] or "IP inconnue"
    return f"{name} ({vendor}) — {ip}"


async def get_current_hosts(fbx: Freepybox) -> list[dict]:
    hosts = await fbx.lan.get_hosts_list()
    return [h for h in hosts if extract_mac(h)]


async def connect() -> Freepybox:
    fbx = Freepybox(app_desc=APP_DESC, token_file=str(TOKEN_FILE))
    # "mafreebox.freebox.fr" peut résoudre vers l'infra publique de Free plutôt que la box
    # locale selon l'environnement réseau (observé sous WSL2). Le certificat TLS de la box
    # n'étant valide que pour son domaine unique (pas pour l'IP brute), FREEBOX_HOST doit
    # pointer vers ce domaine (trouvé via /api_version) si le nom générique ne fonctionne
    # pas depuis ta machine. Ces valeurs sont propres à ta box : elles vivent dans .env
    # (jamais commité), pas en dur dans ce script.
    host = os.environ.get("FREEBOX_HOST", "mafreebox.freebox.fr")
    port = int(os.environ.get("FREEBOX_PORT", "443"))
    await fbx.open(host=host, port=port)
    return fbx


async def cmd_learn() -> None:
    fbx = await connect()
    try:
        hosts = await get_current_hosts(fbx)
    finally:
        await fbx.close()

    whitelist = load_whitelist()
    added = 0
    for host in hosts:
        mac = extract_mac(host)
        if mac not in whitelist:
            whitelist[mac] = host.get("primary_name", mac)
            added += 1

    save_whitelist(whitelist)
    print(f"[+] {added} nouvel(aux) appareil(s) ajouté(s) à la liste blanche.")
    print(f"[+] {len(whitelist)} appareil(s) au total dans {WHITELIST_PATH.name}")


async def cmd_list() -> None:
    """Affiche la liste blanche avec l'état actuel de chaque appareil selon la Freebox."""
    whitelist = load_whitelist()
    fbx = await connect()
    try:
        hosts = {extract_mac(h): h for h in await get_current_hosts(fbx)}
    finally:
        await fbx.close()

    rows = []
    for mac, name in whitelist.items():
        host = hosts.get(mac)
        if host is None:
            state, last = "non vu", ""
        else:
            state = "actif" if (host.get("active") or host.get("reachable")) else "inactif"
            last = extract_last_seen(host)[1] or ""
        rows.append((state != "actif", name.lower(), name, mac, state, last))

    for _, _, name, mac, state, last in sorted(rows):
        print(f"{state:8} | {name[:32]:32} | {mac} | {last}")
    actifs = sum(1 for r in rows if r[4] == "actif")
    print(f"[+] {len(rows)} appareil(s) en liste blanche, dont {actifs} actif(s) en ce moment.")


async def cmd_allow(mac: str, name: str | None) -> None:
    whitelist = load_whitelist()
    if mac in whitelist:
        print(f"[=] {mac} est déjà en liste blanche ({whitelist[mac]}).")
        return
    if not name:
        # Reprend le nom vu par la Freebox si l'appareil est connu d'elle
        fbx = await connect()
        try:
            hosts = {extract_mac(h): h for h in await get_current_hosts(fbx)}
        finally:
            await fbx.close()
        name = (hosts.get(mac) or {}).get("primary_name") or mac
    whitelist[mac] = name
    save_whitelist(whitelist)
    print(f"[+] {mac} ({name}) ajouté. {len(whitelist)} appareil(s) au total.")


def find_by_name(whitelist: dict, name: str) -> list[str]:
    """MAC des appareils portant ce nom (correspondance exacte, sinon partielle, sans tenir compte de la casse)."""
    q = name.strip().lower()
    exact = [m for m, n in whitelist.items() if n.strip().lower() == q]
    return exact or [m for m, n in whitelist.items() if q in n.lower()]


def remove_entry(whitelist: dict, mac: str) -> int:
    name = whitelist.pop(mac)
    save_whitelist(whitelist)
    print(f"[-] {mac} ({name}) retiré. {len(whitelist)} appareil(s) restant(s).")
    return 0


def cmd_remove_mac(mac: str) -> int:
    whitelist = load_whitelist()
    if mac not in whitelist:
        print(f"[!] {mac} n'est pas dans la liste blanche.")
        return 1
    return remove_entry(whitelist, mac)


def cmd_remove_name(name: str) -> int:
    whitelist = load_whitelist()
    matches = find_by_name(whitelist, name)
    if not matches:
        print(f"[!] Aucun appareil de la liste blanche ne porte le nom {name!r}.")
        return 1
    if len(matches) > 1:
        print(f"[!] {len(matches)} appareils correspondent à {name!r} — rien n'a été retiré.")
        print("    Précise le nom complet ou utilise --remove-mac :")
        for mac in sorted(matches, key=lambda m: whitelist[m].lower()):
            print(f"    - {whitelist[mac]} | {mac}")
        return 1
    return remove_entry(whitelist, matches[0])


async def cmd_check(send_notification: bool = True) -> int:
    whitelist = load_whitelist()
    if not whitelist:
        print("[!] Liste blanche vide — lance d'abord '--learn' pour l'initialiser.")
        return 1

    fbx = await connect()
    try:
        hosts = await get_current_hosts(fbx)
    finally:
        await fbx.close()

    unknown = []
    for host in hosts:
        mac = extract_mac(host)
        if mac in whitelist:
            continue
        if host.get("active") or host.get("reachable"):
            unknown.append(host)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # On n'alerte que sur un appareil inconnu qui n'a pas déjà été signalé. Un appareil qui
    # disparaît est oublié : s'il revient, il déclenche une nouvelle alerte.
    alerted = load_alerted()
    current = {extract_mac(h) for h in unknown}
    new_unknown = [h for h in unknown if extract_mac(h) not in alerted]
    save_alerted({m: alerted.get(m, timestamp) for m in current})
    if new_unknown and send_notification:
        lines = "\n".join(describe(h) for h in new_unknown[:3])
        if len(new_unknown) > 3:
            lines += f"\n… et {len(new_unknown) - 3} autre(s)"
        title = f"Freebox Network Watch : {len(new_unknown)} appareil(s) inconnu(s)"
        error = notify(title, lines)
        if error:
            print(f"[!] Notification Windows impossible : {error}")

    if not unknown:
        print(f"[{timestamp}] OK — aucun appareil inconnu actif ({len(hosts)} appareil(s) vu(s)).")
        return 0

    RED = "\033[91m"
    RESET = "\033[0m"

    print(f"[{timestamp}] ⚠️  {len(unknown)} appareil(s) INCONNU(S) détecté(s) :")
    for host in unknown:
        mac = extract_mac(host)
        name = host.get("primary_name") or f"{RED}INCONNU{RESET}"
        vendor = host.get("vendor_name") or "constructeur inconnu"
        ip, last_seen = extract_last_seen(host)
        ip = ip or "IP inconnue"
        last_seen = last_seen or "heure inconnue"
        first_seen = extract_first_seen(host) or "heure inconnue"
        print(
            f"  - {name} | MAC: {mac} | {vendor} | IP: {ip} | "
            f"1ère connexion: {first_seen} | dernière activité: {last_seen}"
        )
    return 2


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--learn",
        action="store_true",
        help="Ajoute tous les appareils actuellement connus à la liste blanche.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Vérifie les appareils actifs contre la liste blanche.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Affiche la liste blanche avec l'état actuel de chaque appareil.",
    )
    parser.add_argument(
        "--allow",
        metavar="MAC",
        type=normalize_mac,
        help="Ajoute un appareil à la liste blanche (nom optionnel avec --name).",
    )
    parser.add_argument("--name", help="Nom à associer à --allow (sinon celui vu par la Freebox).")
    parser.add_argument(
        "--remove-mac",
        metavar="MAC",
        type=normalize_mac,
        help="Retire un appareil de la liste blanche par son adresse MAC.",
    )
    parser.add_argument(
        "--remove-name",
        metavar="NOM",
        help="Retire un appareil par son nom (entre guillemets s'il contient des espaces). "
        "Rien n'est retiré si plusieurs appareils correspondent.",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Avec --check : n'affiche pas de notification Windows.",
    )
    parser.add_argument(
        "--test-alert",
        action="store_true",
        help="Envoie une notification Windows de test, sans interroger la Freebox.",
    )
    args = parser.parse_args()

    if not (args.learn or args.check or args.list or args.allow or args.remove_mac or args.remove_name
            or args.test_alert):
        parser.print_help()
        sys.exit(1)
    if args.name and not args.allow:
        parser.error("--name s'utilise avec --allow")

    if args.test_alert:
        error = notify("Freebox Network Watch", "Notification de test : si tu lis ceci, les alertes fonctionnent.")
        print(f"[!] Échec de la notification Windows : {error}" if error else "[+] Notification envoyée.")
        sys.exit(1 if error else 0)
    if args.list:
        asyncio.run(cmd_list())
    if args.allow:
        asyncio.run(cmd_allow(args.allow, args.name))
    if args.remove_mac:
        sys.exit(cmd_remove_mac(args.remove_mac))
    if args.remove_name:
        sys.exit(cmd_remove_name(args.remove_name))

    if args.learn:
        asyncio.run(cmd_learn())
    if args.check:
        exit_code = asyncio.run(cmd_check(send_notification=not args.no_notify))
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
