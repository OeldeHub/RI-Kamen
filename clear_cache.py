#!/usr/bin/env python3
"""Löscht die GitHub-Actions-Caches des Monitor-Repositories.

Das Repository wird aus der Umgebungsvariable ``GITHUB_REPOSITORY`` gelesen
(z. B. ``deinname/kamen-ratsinfo``). Alternativ direkt setzen:

    GITHUB_REPOSITORY=deinname/kamen-ratsinfo GITHUB_TOKEN=ghp_xxx python clear_cache.py
"""

import os
import sys

import requests

REPO = os.environ.get("GITHUB_REPOSITORY", "").strip()


def main():
    if not REPO or "/" not in REPO:
        print("Fehler: GITHUB_REPOSITORY nicht gesetzt (Format: benutzer/repo).")
        print("Nutzung: GITHUB_REPOSITORY=benutzer/repo GITHUB_TOKEN=ghp_xxx python clear_cache.py")
        sys.exit(1)

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("Fehler: GITHUB_TOKEN Umgebungsvariable nicht gesetzt.")
        print("Erstelle einen Token unter: https://github.com/settings/tokens")
        print("Benötigte Berechtigung: Actions (read & write)")
        print()
        print("Nutzung: GITHUB_REPOSITORY=benutzer/repo GITHUB_TOKEN=ghp_xxx python clear_cache.py")
        sys.exit(1)

    api = f"https://api.github.com/repos/{REPO}/actions/caches"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # Caches auflisten
    resp = requests.get(api, headers=headers)
    if resp.status_code != 200:
        print(f"Fehler beim Abrufen der Caches: {resp.status_code} {resp.text}")
        sys.exit(1)

    caches = resp.json().get("actions_caches", [])
    if not caches:
        print("Keine Caches vorhanden.")
        return

    print(f"{len(caches)} Cache(s) gefunden:\n")
    for c in caches:
        size_mb = c['size_in_bytes'] / 1024 / 1024
        print(f"  [{c['id']}] {c['key']}  ({size_mb:.1f} MB, Branch: {c['ref']})")

    print()
    answer = input("Alle Caches löschen? [j/N] ").strip().lower()
    if answer != "j":
        print("Abgebrochen.")
        return

    for c in caches:
        r = requests.delete(f"{api}/{c['id']}", headers=headers)
        if r.status_code == 204:
            print(f"  Gelöscht: {c['key']}")
        else:
            print(f"  Fehler bei {c['key']}: {r.status_code} {r.text}")

    print("\nFertig. Starte den Workflow jetzt manuell über Actions → Run workflow.")


if __name__ == "__main__":
    main()
