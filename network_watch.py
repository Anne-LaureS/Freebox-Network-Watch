#!/usr/bin/env python3
"""Détecte les appareils inconnus connectés au réseau via l'API Freebox OS."""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from freebox_api import Freepybox

APP_DESC = {
    "app_id": "fr.anne-laures.network-watch",
    "app_name": "Network Watch",
    "app_version": "1.0",
    "device_name": "network-watch-script",
}

WHITELIST_PATH = Path(__file__).parent / "whitelist.json"
TOKEN_FILE = Path(__file__).parent / ".freebox_token.json"


def load_whitelist() -> dict:
    if not WHITELIST_PATH.exists():
        return {}
    with open(WHITELIST_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_whitelist(whitelist: dict) -> None:
    with open(WHITELIST_PATH, "w", encoding="utf-8") as f:
        json.dump(whitelist, f, indent=2, ensure_ascii=False, sort_keys=True)


def extract_mac(host: dict) -> str | None:
    l2ident = host.get("l2ident") or {}
    return l2ident.get("id")


async def get_current_hosts(fbx: Freepybox) -> list[dict]:
    hosts = await fbx.lan.get_hosts_list()
    return [h for h in hosts if extract_mac(h)]


async def connect() -> Freepybox:
    fbx = Freepybox(app_desc=APP_DESC, token_file=str(TOKEN_FILE))
    await fbx.open(host="mafreebox.freebox.fr", port=443)
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


async def cmd_check() -> int:
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
    if not unknown:
        print(f"[{timestamp}] OK — aucun appareil inconnu actif ({len(hosts)} appareil(s) vu(s)).")
        return 0

    print(f"[{timestamp}] ⚠️  {len(unknown)} appareil(s) INCONNU(S) détecté(s) :")
    for host in unknown:
        mac = extract_mac(host)
        name = host.get("primary_name") or "(sans nom)"
        vendor = host.get("vendor_name") or "constructeur inconnu"
        print(f"  - {name} | MAC: {mac} | {vendor}")
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
    args = parser.parse_args()

    if not args.learn and not args.check:
        parser.print_help()
        sys.exit(1)

    if args.learn:
        asyncio.run(cmd_learn())
    if args.check:
        exit_code = asyncio.run(cmd_check())
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
