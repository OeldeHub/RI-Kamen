import argparse
import hashlib
import json
import logging
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, getaddresses
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("ratsinfo-kamen-monitor")
TZ = ZoneInfo("Europe/Berlin")

BRAND_COLOR = "#003366"
BRAND_BG_TINT = "#f0f4f8"
BRAND_NAME = "Stadt Kamen"
BRAND_FOOTER = "Ratsinformationssystem der Stadt Kamen"
MAIL_SUBJECT_PREFIX = "Ratsinfo Kamen"

CONFIG_FILE = "config.json"

_DEFAULTS = {
    "base_url": "https://ratsportal.kamen.de/bi/",
    "info_page": "info.asp",
    "referer": "https://ratsportal.kamen.de/",
    "session_href_regex": r"si\d+\.asp\?.*__ksinr=",
    "vorlage_href_regex": r"vo\d+\.asp",
    "hash_file": "last_hash.txt",
    "state_file": "last_state.json",
    "links_file": "last_links.json",
    "deleted_file": "deleted_sessions.json",
    "email_hash_file": "last_email_hash.txt",
    "run_slots_file": "last_run_slots.json",
    "deleted_retention_days": 3,
    "request_delay": 1,
    # Ziel-Uhrzeiten (Berliner Zeit) für den GitHub-Cron-Fallback. Die primären
    # Läufe (10:00/18:00) kommen über cron-job.org per workflow_dispatch und
    # umgehen diesen Guard; der GitHub-Cron dient nur als Sicherheitsnetz um 21:00.
    "run_hours": [21],
    # Länge des Akzeptanzfensters je Ziel-Uhrzeit in Stunden. Fängt
    # Verzögerungen des GitHub-Schedulers ab (Cron-Jobs starten oft 30+ Min spät).
    "run_window_hours": 4,
}


def _load_config():
    cfg = dict(_DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("config.json ist korrupt, nutze Defaults: %s", e)
    cfg["url"] = cfg["base_url"] + cfg["info_page"]
    return cfg


CFG = _load_config()
BASE_URL = CFG["base_url"]
URL = CFG["url"]
HASH_FILE = CFG["hash_file"]
STATE_FILE = CFG["state_file"]
LINKS_FILE = CFG["links_file"]
DELETED_FILE = CFG["deleted_file"]
EMAIL_HASH_FILE = CFG["email_hash_file"]
RUN_SLOTS_FILE = CFG["run_slots_file"]
DELETED_RETENTION_DAYS = CFG["deleted_retention_days"]
REQUEST_DELAY = CFG["request_delay"]
RUN_HOURS = sorted(CFG["run_hours"])
RUN_WINDOW_HOURS = CFG["run_window_hours"]
REFERER = CFG["referer"]
SESSION_HREF_RE = re.compile(CFG["session_href_regex"])
VORLAGE_HREF_RE = re.compile(CFG["vorlage_href_regex"])


def current_slot(now=None):
    """Return the label of the active run window for `now`, or None if outside.

    A slot is identified by its target hour (e.g. "10", "18"). Its window spans
    RUN_WINDOW_HOURS hours after the target so that runs delayed by GitHub's
    scheduler still land inside it. Windows are kept non-overlapping by config.
    """
    now = now or datetime.now(TZ)
    for target in RUN_HOURS:
        if target <= now.hour < target + RUN_WINDOW_HOURS:
            return f"{target:02d}"
    return None


def load_last_run_slots():
    """Load mapping of slot label -> ISO date on which it last completed."""
    if os.path.exists(RUN_SLOTS_FILE):
        try:
            with open(RUN_SLOTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Run-Slots-Datei %s ist korrupt, ignoriere: %s", RUN_SLOTS_FILE, e)
    return {}


def save_last_run_slots(slots):
    with open(RUN_SLOTS_FILE, "w", encoding="utf-8") as f:
        json.dump(slots, f, ensure_ascii=False, indent=2)


def is_connectivity_error(exc):
    """True, wenn der Fehler ein reines Erreichbarkeits-/Netzwerkproblem ist.

    Solche Fehler (Server nicht erreichbar, Timeout, DNS) liegen außerhalb
    unseres Codes – meist ist das Ratsportal kurz down oder blockt die
    Runner-IP. Sie sollen den Lauf nicht als "failed" markieren.
    """
    return isinstance(exc, (requests.exceptions.ConnectionError,
                            requests.exceptions.Timeout))


def fetch_page(url=None, retries=5):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        "Referer": REFERER,
    }
    target = url or URL
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(target, headers=headers, timeout=30)
            response.raise_for_status()
            return response.text
        except requests.RequestException as e:
            if attempt < retries:
                # Exponentielles Backoff, aber bei 60s gedeckelt: 2, 4, 8, 16 ...
                wait = min(2 ** attempt, 60)
                logger.warning("Versuch %d/%d fehlgeschlagen für %s: %s – warte %ds",
                               attempt, retries, target, e, wait)
                time.sleep(wait)
            else:
                raise


# ---------------------------------------------------------------------------
# Startseite: Sitzungen extrahieren
# ---------------------------------------------------------------------------

def extract_sessions(html):
    """Extract sessions from the BI start page."""
    soup = BeautifulSoup(html, "html.parser")
    sessions = []
    # Session links look like si0057.asp?__ksinr=XXXX (pattern configurable)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if SESSION_HREF_RE.search(href):
            ksinr = href.split("__ksinr=")[-1].split("&")[0]
            text = a.get_text(strip=True)
            # Try to find date/time near this link
            parent = a.find_parent(["div", "li", "tr"])
            date_text = ""
            time_text = ""
            if parent:
                full_text = parent.get_text(" ", strip=True)
                date_match = re.search(r'(\d{2}\.\d{2}\.\d{4})', full_text)
                if date_match:
                    date_text = date_match.group(1)
                time_match = re.search(r'(\d{1,2}:\d{2}-\d{1,2}:\d{2}\s*Uhr)', full_text)
                if time_match:
                    time_text = time_match.group(1)
            detail_url = urljoin(BASE_URL, href)
            sessions.append({
                "ksinr": ksinr,
                "name": text,
                "date": date_text,
                "time": time_text,
                "detail_url": detail_url,
            })
    return sessions


# ---------------------------------------------------------------------------
# Detailseite: Tagesordnung + Dokumente
# ---------------------------------------------------------------------------

def extract_session_details(detail_url):
    """Fetch a session detail page and extract agenda items + documents."""
    try:
        html = fetch_page(detail_url)
    except Exception as e:
        print(f"  Warnung: Konnte {detail_url} nicht laden: {e}")
        return {"tops": [], "docs": [], "title": ""}

    soup = BeautifulSoup(html, "html.parser")

    # Session title from heading
    title_tag = soup.find("h1") or soup.find("h2")
    title = title_tag.get_text(strip=True) if title_tag else ""

    # Agenda items from the table, tracking Ö/N sections
    tops = []
    section = "Ö"
    table = soup.find("table", class_=lambda c: c and "smctablesitzung" in c)
    if table:
        for row in table.find_all("tr"):
            # Section separator rows (class "totrenn")
            td_trenn = row.find("td", class_="totrenn")
            if td_trenn:
                trenn_text = td_trenn.get_text(strip=True).lower()
                if "nicht" in trenn_text and "ffentlich" in trenn_text:
                    section = "N"
                elif "ffentlich" in trenn_text:
                    section = "Ö"
                continue
            tds = row.find_all("td")
            if len(tds) >= 2:
                top_num = tds[0].get_text(strip=True)
                top_text = tds[1].get_text(strip=True)[:200]
                # Skip section headers (e.g. "Ö" / "N" without a number)
                if top_num and re.search(r'\d', top_num):
                    tops.append({"num": top_num, "text": top_text, "section": section})

    # Pass 1: Collect getfile links from session page (with TOP context)
    docs = []
    seen_urls = set()
    vorlage_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "getfile" in href.lower():
            # Skip icon-only download buttons (class "btn"), keep text links
            classes = " ".join(a.get("class", []))
            if "btn" in classes:
                continue
            full_url = urljoin(BASE_URL, href)
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)
            text = a.get_text(strip=True)
            parent_row = a.find_parent("tr")
            top_context = ""
            if parent_row:
                num_td = parent_row.find("td", class_="tofnum")
                if num_td:
                    top_context = num_td.get_text(strip=True)
            docs.append({
                "href": full_url,
                "text": text or "Dokument",
                "top": top_context,
            })
        elif VORLAGE_HREF_RE.search(href):
            full_url = urljoin(BASE_URL, href)
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)
            vo_num = a.get_text(strip=True)
            vo_title = a.get("title", "")
            if vo_title.startswith("Vorlage anzeigen: "):
                vo_title = vo_title[len("Vorlage anzeigen: "):]
            vo_label = f"Vorlage {vo_num}: {vo_title}" if vo_title else f"Vorlage {vo_num}"
            vorlage_links.append({"url": full_url, "label": vo_label})

    # Pass 2: Follow Vorlage links to find additional PDFs not already seen
    for vo in vorlage_links:
        try:
            vo_html = fetch_page(vo["url"])
            vo_soup = BeautifulSoup(vo_html, "html.parser")
            found_pdf = False
            for vo_a in vo_soup.find_all("a", href=True):
                vo_href = vo_a["href"]
                if "getfile" in vo_href.lower():
                    vo_classes = " ".join(vo_a.get("class", []))
                    if "btn" in vo_classes:
                        continue
                    pdf_url = urljoin(BASE_URL, vo_href)
                    if pdf_url not in seen_urls:
                        seen_urls.add(pdf_url)
                        pdf_text = vo_a.get_text(strip=True)
                        docs.append({
                            "href": pdf_url,
                            "text": f"{vo['label']} – {pdf_text}" if pdf_text else vo["label"],
                            "top": "",
                        })
                        found_pdf = True
            if not found_pdf:
                docs.append({"href": vo["url"], "text": vo["label"], "top": ""})
        except Exception:
            docs.append({"href": vo["url"], "text": vo["label"], "top": ""})

    return {"tops": tops, "docs": docs, "title": title}


# ---------------------------------------------------------------------------
# Alten Code beibehalten: Links von Startseite (Fallback)
# ---------------------------------------------------------------------------

def extract_links(html):
    """Extract all file/document links from the page."""
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        if any(ext in href.lower() for ext in [".pdf", ".doc", ".xls", ".zip", "download", "file", "getfile"]):
            links.append({"href": href, "text": text})
    return links


def compute_hash(html):
    """Compute hash of the page content (ignoring dynamic timestamps)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "meta"]):
        tag.decompose()
    clean_text = soup.get_text(separator="\n", strip=True)
    return hashlib.md5(clean_text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_last_state():
    """Load previous state. Returns (last_hash, last_sessions_data, last_links)."""
    last_hash = None
    last_sessions = {}
    last_links = []

    if os.path.exists(HASH_FILE):
        with open(HASH_FILE, "r") as f:
            last_hash = f.read().strip()

    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                last_sessions = json.load(f)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("State-Datei %s ist korrupt, starte neu: %s", STATE_FILE, e)
    elif os.path.exists(LINKS_FILE):
        try:
            with open(LINKS_FILE, "r", encoding="utf-8") as f:
                last_links = json.load(f)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Links-Datei %s ist korrupt, ignoriere: %s", LINKS_FILE, e)

    return last_hash, last_sessions, last_links


def load_deleted_sessions():
    """Load tracked deleted sessions with their deletion timestamps."""
    if os.path.exists(DELETED_FILE):
        try:
            with open(DELETED_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Deleted-Datei %s ist korrupt, ignoriere: %s", DELETED_FILE, e)
    return {}


def save_deleted_sessions(deleted):
    """Save deleted sessions, pruning entries older than DELETED_RETENTION_DAYS."""
    cutoff = (datetime.now(TZ) - timedelta(days=DELETED_RETENTION_DAYS)).isoformat()
    pruned = {k: v for k, v in deleted.items() if v.get("deleted_at", "") >= cutoff}
    with open(DELETED_FILE, "w", encoding="utf-8") as f:
        json.dump(pruned, f, ensure_ascii=False, indent=2)
    return pruned


def mark_removed_as_notified(ksinrs):
    """Mark removed sessions as notified so they're not piggybacked again."""
    if not ksinrs:
        return
    tracker = load_deleted_sessions()
    dirty = False
    for k in ksinrs:
        if k in tracker and not tracker[k].get("notified"):
            tracker[k]["notified"] = True
            dirty = True
    if dirty:
        save_deleted_sessions(tracker)



def is_session_past(session, now=None):
    """Return True if a session has already taken place.

    A session counts as past when its date is before today, or when its date is
    today AND its end time (parsed from the ``time`` field like ``10:00-12:00
    Uhr``) is already in the past. Malformed dates default to "not past" so we
    don't accidentally drop genuine future news.
    """
    now = now or datetime.now(TZ)
    date_str = session.get("date", "")
    try:
        session_date = datetime.strptime(date_str, "%d.%m.%Y").date()
    except ValueError:
        return False
    if session_date < now.date():
        return True
    if session_date > now.date():
        return False
    m = re.search(r"-\s*(\d{1,2}):(\d{2})", session.get("time", "") or "")
    if not m:
        return False
    end_hour, end_minute = int(m.group(1)), int(m.group(2))
    end_dt = now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    return now >= end_dt



def load_last_email_hash():
    """Load hash of the most recently sent email's content."""
    if os.path.exists(EMAIL_HASH_FILE):
        try:
            with open(EMAIL_HASH_FILE, "r", encoding="utf-8") as f:
                return f.read().strip() or None
        except OSError as e:
            logger.warning("Email-Hash-Datei %s nicht lesbar: %s", EMAIL_HASH_FILE, e)
    return None


def save_last_email_hash(hash_str):
    with open(EMAIL_HASH_FILE, "w", encoding="utf-8") as f:
        f.write(hash_str)


def _signature_for_changed(changed):
    """Stable, JSON-serializable representation of per-session changes."""
    result = {}
    for ksinr, c in changed.items():
        result[ksinr] = {
            "new_tops": sorted([t["num"], t["text"]] for t in c.get("new_tops", [])),
            "removed_tops": sorted([t["num"], t["text"]] for t in c.get("removed_tops", [])),
            "modified_tops": sorted(
                [t["num"], t.get("old_text", ""), t.get("new_text", "")]
                for t in c.get("modified_tops", [])
            ),
            "new_docs": sorted([d.get("text", ""), d.get("href", "")] for d in c.get("new_docs", [])),
            "removed_docs": sorted([d.get("text", ""), d.get("href", "")] for d in c.get("removed_docs", [])),
            "updated_docs": sorted(
                [d.get("text", ""), d.get("new_href", "")] for d in c.get("updated_docs", [])
            ),
        }
    return result


def compute_email_signature(changes=None, new_links=None, removed_links=None,
                            is_test=False, sessions_data=None):
    """Hash the content-relevant inputs to detect duplicate emails.

    Excludes dynamic things (timestamps) so two runs producing the same
    notification content yield the same signature.
    """
    payload = {
        "is_test": bool(is_test),
        "changes_added": sorted((changes or {}).get("added", {}).keys()),
        "changes_removed": sorted((changes or {}).get("removed", {}).keys()),
        "changes_changed": _signature_for_changed((changes or {}).get("changed", {})),
        "new_links": sorted(l.get("href", "") for l in (new_links or [])),
        "removed_links": sorted(l.get("href", "") for l in (removed_links or [])),
    }
    if is_test:
        # First-run test mail also lists current sessions; include ksinrs.
        payload["test_sessions"] = sorted((sessions_data or {}).keys())
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(canonical.encode("utf-8")).hexdigest()


def save_state(current_hash, sessions_data, current_links):
    with open(HASH_FILE, "w") as f:
        f.write(current_hash)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sessions_data, f, ensure_ascii=False, indent=2)
    with open(LINKS_FILE, "w", encoding="utf-8") as f:
        json.dump(current_links, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Vergleich
# ---------------------------------------------------------------------------

def compare_sessions(old_sessions, new_sessions):
    """Compare old and new session data. Returns dict with changes."""
    old_ids = set(old_sessions.keys())
    new_ids = set(new_sessions.keys())

    added_ids = new_ids - old_ids
    removed_ids = old_ids - new_ids
    common_ids = old_ids & new_ids

    added = {k: new_sessions[k] for k in added_ids}

    # Track deleted sessions with timestamps. Removals don't trigger a dedicated
    # mail anymore – they piggyback on the next mail with real news within the
    # retention window (default 3 days) and otherwise silently expire.
    now = datetime.now(TZ).isoformat()
    deleted_tracker = load_deleted_sessions()

    # Add newly removed sessions to tracker
    for k in removed_ids:
        if k not in deleted_tracker:
            deleted_tracker[k] = {
                **old_sessions[k],
                "deleted_at": now,
                "notified": False,
            }

    # Remove sessions that reappeared
    for k in new_ids:
        deleted_tracker.pop(k, None)

    # Save and prune (removes entries older than DELETED_RETENTION_DAYS)
    deleted_tracker = save_deleted_sessions(deleted_tracker)

    # Report all pending (unnotified) removed sessions still in retention window
    removed = {k: v for k, v in deleted_tracker.items() if not v.get("notified", False)}

    changed = {}
    for k in common_ids:
        old = old_sessions[k]
        new = new_sessions[k]

        old_tops = {t["num"]: t["text"] for t in old.get("tops", [])}
        new_tops = {t["num"]: t["text"] for t in new.get("tops", [])}

        old_docs = {d["href"] for d in old.get("docs", [])}
        new_docs = {d["href"] for d in new.get("docs", [])}

        new_top_nums = set(new_tops.keys()) - set(old_tops.keys())
        removed_top_nums = set(old_tops.keys()) - set(new_tops.keys())
        modified_tops = [
            {"num": n, "old_text": old_tops[n], "new_text": new_tops[n]}
            for n in sorted(set(old_tops.keys()) & set(new_tops.keys()))
            if old_tops[n] != new_tops[n]
        ]
        new_doc_hrefs = new_docs - old_docs
        removed_doc_hrefs = old_docs - new_docs

        # Detect updated docs: same name, different URL
        old_docs_by_name = {d["text"]: d for d in old.get("docs", []) if d["href"] in removed_doc_hrefs}
        new_docs_by_name = {d["text"]: d for d in new.get("docs", []) if d["href"] in new_doc_hrefs}
        updated_doc_names = set(old_docs_by_name.keys()) & set(new_docs_by_name.keys())
        updated_docs = [
            {"text": name, "old_href": old_docs_by_name[name]["href"], "new_href": new_docs_by_name[name]["href"]}
            for name in sorted(updated_doc_names)
        ]
        # Remove updated docs from new/removed lists
        updated_old_hrefs = {old_docs_by_name[n]["href"] for n in updated_doc_names}
        updated_new_hrefs = {new_docs_by_name[n]["href"] for n in updated_doc_names}
        truly_new_docs = [d for d in new.get("docs", []) if d["href"] in new_doc_hrefs - updated_new_hrefs]
        truly_removed_docs = [d for d in old.get("docs", []) if d["href"] in removed_doc_hrefs - updated_old_hrefs]

        if new_top_nums or removed_top_nums or modified_tops or truly_new_docs or truly_removed_docs or updated_docs:
            changed[k] = {
                "name": new.get("name", old.get("name", "")),
                "title": new.get("title", ""),
                "date": new.get("date", ""),
                "time": new.get("time", ""),
                "detail_url": new.get("detail_url", ""),
                "new_tops": [{"num": n, "text": new_tops[n]} for n in sorted(new_top_nums)],
                "removed_tops": [{"num": n, "text": old_tops[n]} for n in sorted(removed_top_nums)],
                "modified_tops": modified_tops,
                "new_docs": truly_new_docs,
                "removed_docs": truly_removed_docs,
                "updated_docs": updated_docs,
            }

    # Vergangene Sitzungen werden einmalig gemeldet und danach ignoriert.
    added = {k: v for k, v in added.items() if not is_session_past(v)}
    changed = {k: c for k, c in changed.items() if not is_session_past(c)}

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
    }


def find_new_links(old_links, new_links):
    old_hrefs = {l["href"] for l in old_links}
    return [l for l in new_links if l["href"] not in old_hrefs]


def find_removed_links(old_links, new_links):
    new_hrefs = {l["href"] for l in new_links}
    return [l for l in old_links if l["href"] not in new_hrefs]


# ---------------------------------------------------------------------------
# E-Mail
# ---------------------------------------------------------------------------

_WOCHENTAGE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _format_date(date_str, time_str=""):
    """Format 'DD.MM.YYYY' to 'Mo, 15.04.2026' with optional time."""
    parts = [date_str]
    try:
        dt = datetime.strptime(date_str, "%d.%m.%Y")
        parts = [f"{_WOCHENTAGE[dt.weekday()]}, {date_str}"]
    except (ValueError, IndexError):
        pass
    if time_str:
        parts.append(time_str)
    return " ".join(parts)


def _sort_sessions(sessions_dict):
    """Sort a sessions dict by date (earliest first)."""
    def _sort_key(item):
        date_str = item[1].get("date", "")
        try:
            return datetime.strptime(date_str, "%d.%m.%Y")
        except ValueError:
            return datetime.max
    return dict(sorted(sessions_dict.items(), key=_sort_key))

_CARD_VARIANTS = {
    "neu":        {"border": "#38a169", "bg": "#f1faf4", "cls": "card-green"},
    "geaendert":  {"border": BRAND_COLOR, "bg": BRAND_BG_TINT, "cls": "card-tint"},
    "entfernt":   {"border": "#e53e3e", "bg": "#fdf3f3", "cls": "card-red"},
    "abgelaufen": {"border": "#a0aec0", "bg": "#f5f7fa", "cls": "card-gray"},
}

_BADGE_VARIANTS = {
    "neu":        {"bg": "#38a169", "cls": "badge-neu"},
    "geaendert":  {"bg": BRAND_COLOR, "cls": "badge-geaendert"},
    "entfernt":   {"bg": "#e53e3e", "cls": "badge-entfernt"},
    "abgelaufen": {"bg": "#6c7686", "cls": "badge-abgelaufen"},
}


def _email_wrapper(title_html, body_content, now_full):
    """Wrap email content in a responsive HTML template with dark-mode support."""
    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>{BRAND_NAME}</title>
<style>
  body {{ margin:0; padding:0; word-spacing:normal; }}
  table {{ border-collapse:collapse; }}
  a {{ text-decoration:none; }}
  @media only screen and (max-width: 600px) {{
    .container {{ padding: 12px 8px !important; }}
    .panel     {{ border-radius: 0 !important; box-shadow: 0 2px 8px rgba(0,0,0,0.08) !important; }}
    .header-pad{{ padding: 24px 18px 22px !important; }}
    .body-pad  {{ padding: 18px 18px 4px !important; }}
    .cta-pad   {{ padding: 8px 18px 24px !important; }}
    .footer-pad{{ padding: 16px 18px !important; }}
    .session-card {{ padding: 14px 16px !important; margin: 10px 0 !important; }}
    .section-heading {{ font-size: 14px !important; }}
    .stat-num  {{ font-size: 22px !important; }}
    .stat-cell {{ padding: 8px 4px !important; }}
    .session-title-text {{ font-size: 15px !important; }}
    .top-row   {{ font-size: 13px !important; }}
    .doc-link  {{ font-size: 12px !important; }}
    .title-h   {{ font-size: 20px !important; }}
    .brand-logo-wrap img {{ width: 120px !important; max-width: 120px !important; }}
  }}
  @media (prefers-color-scheme: dark) {{
    body, .bg {{ background-color: #121214 !important; }}
    .panel    {{ background-color: #1c1c1f !important; box-shadow: 0 4px 24px rgba(0,0,0,0.5) !important; }}
    .accent-bar {{ background-color: #4a78a8 !important; }}
    .header-pad, .body-pad {{ background-color: #1c1c1f !important; }}
    .header-pad {{ border-bottom-color: #2a2a2d !important; }}
    .footer-pad {{ background-color: #161618 !important; border-top-color: #4a78a8 !important; }}
    .title-accent {{ background-color: #4a78a8 !important; }}
    .text-strong {{ color: #f0f0f0 !important; }}
    .text-default {{ color: #d2d2d4 !important; }}
    .text-muted {{ color: #9a9a9c !important; }}
    .text-faint {{ color: #6e6e72 !important; }}
    .text-strike {{ color: #7a7a7e !important; }}
    .divider {{ border-top-color: #2a2a2d !important; }}
    .stat-divider {{ border-left-color: #2a2a2d !important; }}
    .card-tint  {{ background-color: #1c2733 !important; border-left-color: #4a78a8 !important; }}
    .card-green {{ background-color: #16261a !important; border-left-color: #4d9a6a !important; }}
    .card-red   {{ background-color: #2a1818 !important; border-left-color: #c25555 !important; }}
    .card-gray  {{ background-color: #1f1f22 !important; border-left-color: #6a6a6e !important; }}
    .badge-neu        {{ background-color: #4d9a6a !important; }}
    .badge-geaendert  {{ background-color: #4a78a8 !important; }}
    .badge-entfernt   {{ background-color: #c25555 !important; }}
    .badge-abgelaufen {{ background-color: #6a6a6e !important; }}
    .doc-link, a.doc-link {{ color: #9ec6e8 !important; }}
    a.title-link, .title-link {{ color: #f0f0f0 !important; }}
    .cta-btn {{ background-color: #4a78a8 !important; color: #ffffff !important; }}
    .nonpublic-divider {{ border-top-color: #2a2a2d !important; color: #7a7a7e !important; }}
  }}
</style>
</head>
<body class="bg" style="margin:0;padding:0;background-color:#f0f0f2;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#222;line-height:1.5;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="bg" style="background-color:#f0f0f2;">
<tr><td align="center" class="container" style="padding:24px 16px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="panel" style="max-width:640px;background-color:#ffffff;border-radius:4px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">

<tr><td class="accent-bar" style="background:{BRAND_COLOR};height:5px;font-size:0;line-height:0;">&nbsp;</td></tr>

<tr><td class="header-pad" style="background:#ffffff;padding:32px 36px 28px;border-bottom:1px solid #ececee;text-align:center;">
<div class="brand-logo-wrap" style="display:inline-block;background:#ffffff;padding:4px 6px;border-radius:4px;margin-bottom:18px;font-size:0;line-height:0;">
<img src="cid:logo" alt="{BRAND_NAME}" width="140" style="display:block;border:0;outline:none;text-decoration:none;height:auto;max-width:140px;">
</div>
{title_html}
<div class="title-accent" style="width:48px;height:3px;background:{BRAND_COLOR};border-radius:2px;margin:14px auto 0;font-size:0;line-height:0;">&nbsp;</div>
</td></tr>

<tr><td class="body-pad" style="padding:22px 32px 8px;">
{body_content}
</td></tr>

<tr><td class="cta-pad" style="padding:8px 32px 28px;text-align:center;">
<a class="cta-btn" href="{URL}" style="display:inline-block;background:{BRAND_COLOR};color:#ffffff;font-size:13px;font-weight:700;padding:13px 36px;border-radius:4px;text-decoration:none;text-transform:uppercase;letter-spacing:1.2px;">Zur Seite &rarr;</a>
</td></tr>

<tr><td class="footer-pad" style="background:#f7f7f9;padding:18px 32px;text-align:center;border-top:2px solid {BRAND_COLOR};">
<div class="text-faint" style="font-size:11px;color:#999;">Automatische Benachrichtigung &middot; {now_full}</div>
<div class="text-faint" style="font-size:11px;color:#b5b5b8;margin-top:3px;">{BRAND_FOOTER}</div>
</td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""


def _card(content, variant="geaendert"):
    """Render a card box with left-border accent. Variant: neu/geaendert/entfernt/abgelaufen."""
    v = _CARD_VARIANTS[variant]
    return (f'<div class="session-card {v["cls"]}" '
            f'style="background:{v["bg"]};border-left:4px solid {v["border"]};'
            f'border-radius:3px;padding:14px 18px;margin:12px 0;">{content}</div>')


def _badge(text, variant):
    """Render a small colored badge/label. Variant: neu/geaendert/entfernt/abgelaufen."""
    v = _BADGE_VARIANTS[variant]
    return (f'<span class="{v["cls"]}" '
            f'style="display:inline-block;background:{v["bg"]};color:#ffffff;'
            f'font-size:10px;font-weight:700;padding:3px 10px;border-radius:3px;'
            f'letter-spacing:1px;text-transform:uppercase;vertical-align:middle;">{text}</span>')


def _doc_li(d, new_doc_hrefs):
    """Render one document <li>, marking newly-added docs with a small NEU badge."""
    is_new = d["href"] in new_doc_hrefs
    marker = (_badge("Neu", "neu") + '&nbsp;') if is_new else (
        '<span class="text-faint" style="color:#b0b0b0;margin-right:6px;">&rsaquo;</span>')
    return (f'<li style="padding:2px 0;">{marker}'
            f'<a class="doc-link" href="{d["href"]}" '
            f'style="color:{BRAND_COLOR};text-decoration:none;font-size:12px;">'
            f'{d["text"]}</a></li>')


def _render_tops_with_sections(tops, docs_by_top, max_tops=15, new_doc_hrefs=None):
    """Render TOPs list with Ö/N section separators and document links."""
    items = ""
    current_section = None
    new_doc_hrefs = new_doc_hrefs or set()

    unassigned = docs_by_top.get("", [])
    if unassigned:
        doc_links = "".join(_doc_li(d, new_doc_hrefs) for d in unassigned)
        items += ('<li class="text-muted" style="padding:6px 0 2px;font-size:11px;'
                  'font-weight:700;color:#666;list-style:none;text-transform:uppercase;'
                  'letter-spacing:0.7px;">Allgemein</li>')
        items += f'<ul style="margin:2px 0 10px;padding-left:14px;list-style:none;">{doc_links}</ul>'

    for t in tops[:max_tops]:
        section = t.get("section", "Ö")
        if section != current_section:
            current_section = section
            if section == "N":
                items += ('<li class="nonpublic-divider text-faint" '
                          'style="padding:10px 0 4px;font-size:10px;font-weight:700;'
                          'color:#9a9a9c;border-top:1px solid #ececee;margin-top:8px;'
                          'list-style:none;text-transform:uppercase;letter-spacing:1px;">'
                          'Nicht&shy;öffentlicher Teil</li>')
        is_nonpublic = section == "N"
        color = "#9a9a9c" if is_nonpublic else "#444"
        text_class = "text-faint" if is_nonpublic else "text-default"
        items += (f'<li class="top-row {text_class}" '
                  f'style="padding:3px 0;font-size:13px;color:{color};list-style:none;">'
                  f'<b style="color:{color};display:inline-block;min-width:28px;">{t["num"]}</b>'
                  f'{t["text"]}')
        top_docs = docs_by_top.get(t["num"], [])
        if top_docs:
            doc_links = "".join(_doc_li(d, new_doc_hrefs) for d in top_docs)
            items += f'<ul style="margin:4px 0 6px;padding-left:14px;list-style:none;">{doc_links}</ul>'
        items += '</li>'

    if not items:
        return ""
    return f'<ul style="margin:10px 0 0;padding-left:0;list-style:none;">{items}</ul>'


def _session_header(badge_variant, badge_text, date_str, time_str, name, url,
                    name_strikethrough=False):
    """Render the top portion of a session card: badge + date row, then title."""
    date_text = _format_date(date_str, time_str)
    badge = _badge(badge_text, badge_variant)
    if name_strikethrough:
        title = (f'<span class="text-strike" style="color:#888;text-decoration:line-through;'
                 f'font-size:15px;font-weight:700;">{name}</span>')
    elif url:
        title = (f'<a class="title-link text-strong" href="{url}" '
                 f'style="color:#222;text-decoration:none;font-size:15px;font-weight:700;'
                 f'line-height:1.35;">{name}</a>')
    else:
        title = (f'<span class="text-strong" style="color:#222;font-size:15px;font-weight:700;">'
                 f'{name}</span>')
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td style="vertical-align:middle;">{badge}</td>'
        f'<td class="text-muted" style="vertical-align:middle;text-align:right;'
        f'font-size:12px;color:#666;">{date_text}</td>'
        '</tr></table>'
        f'<div class="session-title-text" style="margin-top:8px;line-height:1.35;">{title}</div>'
    )


def _section_label(text):
    """Small uppercase label used inside change cards."""
    return (f'<div class="text-muted" style="font-size:10px;font-weight:700;color:#666;'
            f'text-transform:uppercase;letter-spacing:0.7px;margin:12px 0 4px;">{text}</div>')


def _render_changed_body(c):
    """Render the body of a 'changed' session card: TOP / doc diffs."""
    out = ""

    if c.get("new_tops"):
        lis = "".join(
            f'<li class="text-default" style="padding:2px 0;font-size:13px;color:#222;'
            f'list-style:none;">'
            f'<b style="display:inline-block;min-width:28px;">{t["num"]}</b>{t["text"]}</li>'
            for t in c["new_tops"]
        )
        out += _section_label("Neue Tagesordnungspunkte")
        out += f'<ul style="margin:0;padding-left:0;list-style:none;">{lis}</ul>'

    if c.get("removed_tops"):
        lis = "".join(
            f'<li class="text-strike" style="padding:2px 0;font-size:13px;color:#999;'
            f'text-decoration:line-through;list-style:none;">'
            f'<span style="display:inline-block;min-width:28px;font-weight:700;">{t["num"]}</span>'
            f'{t["text"]}</li>'
            for t in c["removed_tops"]
        )
        out += _section_label("Entfernte Tagesordnungspunkte")
        out += f'<ul style="margin:0;padding-left:0;list-style:none;">{lis}</ul>'

    if c.get("modified_tops"):
        lis = "".join(
            f'<li class="text-default" style="padding:2px 0;font-size:13px;color:#222;'
            f'list-style:none;">'
            f'<b style="display:inline-block;min-width:28px;">{t["num"]}</b>'
            f'<span class="text-strike" style="color:#999;text-decoration:line-through;">'
            f'{t["old_text"]}</span> &rarr; {t["new_text"]}</li>'
            for t in c["modified_tops"]
        )
        out += _section_label("Geänderte Tagesordnungspunkte")
        out += f'<ul style="margin:0;padding-left:0;list-style:none;">{lis}</ul>'

    if c.get("new_docs"):
        lis = "".join(
            f'<li style="padding:2px 0;list-style:none;">'
            f'<a class="doc-link" href="{d["href"]}" '
            f'style="color:{BRAND_COLOR};text-decoration:none;font-size:13px;">{d["text"]}</a></li>'
            for d in c["new_docs"]
        )
        out += _section_label("Neue Dokumente")
        out += f'<ul style="margin:0;padding-left:0;list-style:none;">{lis}</ul>'

    if c.get("updated_docs"):
        lis = "".join(
            f'<li style="padding:2px 0;list-style:none;">'
            f'<a class="doc-link" href="{d["new_href"]}" '
            f'style="color:{BRAND_COLOR};text-decoration:none;font-size:13px;">{d["text"]}</a>'
            f' <span class="text-faint" style="font-size:11px;color:#999;">aktualisiert</span></li>'
            for d in c["updated_docs"]
        )
        out += _section_label("Aktualisierte Dokumente")
        out += f'<ul style="margin:0;padding-left:0;list-style:none;">{lis}</ul>'

    if c.get("removed_docs"):
        lis = "".join(
            f'<li class="text-strike" style="padding:2px 0;font-size:13px;color:#999;'
            f'text-decoration:line-through;list-style:none;">{d["text"]}</li>'
            for d in c["removed_docs"]
        )
        out += _section_label("Entfernte Dokumente")
        out += f'<ul style="margin:0;padding-left:0;list-style:none;">{lis}</ul>'

    return out


def build_session_changes_html(changes):
    """Build per-session cards covering added, changed, and removed sessions, sorted by date."""

    def _date_key(s):
        try:
            return datetime.strptime(s.get("date", ""), "%d.%m.%Y")
        except ValueError:
            return datetime.max

    entries = []
    for k, s in changes.get("added", {}).items():
        entries.append((_date_key(s), "added", s))
    for k, c in changes.get("changed", {}).items():
        entries.append((_date_key(c), "changed", c))
    for k, s in changes.get("removed", {}).items():
        entries.append((_date_key(s), "removed", s))

    entries.sort(key=lambda e: e[0])

    items_html = ""
    for _, kind, s in entries:
        if kind == "added":
            name = s.get("name", "Unbekannt")
            url = s.get("detail_url", "")
            tops = s.get("tops", [])
            docs = s.get("docs", [])
            docs_by_top = {}
            for d in docs:
                docs_by_top.setdefault(d.get("top", ""), []).append(d)
            all_doc_hrefs = {d["href"] for d in docs}
            top_list = _render_tops_with_sections(tops, docs_by_top, new_doc_hrefs=all_doc_hrefs)
            header = _session_header("neu", "Neu", s.get("date", ""), s.get("time", ""), name, url)
            items_html += _card(header + top_list, variant="neu")

        elif kind == "removed":
            name = s.get("name", "Unbekannt")
            deleted_at = s.get("deleted_at", "")
            deleted_info = ""
            if deleted_at:
                try:
                    dt = datetime.fromisoformat(deleted_at)
                    deleted_info = (f'<div class="text-faint" style="margin-top:8px;'
                                    f'font-size:11px;color:#9a9a9c;">'
                                    f'entfernt am {dt.strftime("%d.%m.%Y")}</div>')
                except ValueError:
                    pass
            header = _session_header("entfernt", "Entfernt", s.get("date", ""), "",
                                     name, "", name_strikethrough=True)
            items_html += _card(header + deleted_info, variant="entfernt")

        elif kind == "changed":
            name = s.get("name", "") or s.get("title", "Unbekannt")
            header = _session_header("geaendert", "Geändert", s.get("date", ""),
                                     s.get("time", ""), name, s.get("detail_url", ""))
            items_html += _card(header + _render_changed_body(s), variant="geaendert")

    return items_html



_TITLES = {"herr", "frau", "dr", "prof"}
GREETING_PLACEHOLDER = "__KREIS_GREETING__"
GREETING_BLOCK_HTML = (
    f'<p class="greeting text-default" '
    f'style="font-size:14px;color:#444;margin:0 0 18px;font-weight:400;">'
    f'{GREETING_PLACEHOLDER}</p>'
)
RECIPIENTS_FILE = "recipients.json"

# Optionale dedizierte Adresse für die automatische Erstlauf-Testmail
# (Umgebungsvariable TEST_MAIL_RECIPIENT). Ist sie gesetzt, geht die
# Erstlauf-Testmail ausschließlich dorthin; ist sie leer, an den regulären
# Verteiler. Der explizite Test-Modus via --test-email bleibt unberührt.
TEST_MAIL_RECIPIENT = os.environ.get("TEST_MAIL_RECIPIENT", "").strip()


def test_mail_recipients():
    """Empfängerliste für die Erstlauf-Testmail: nur ``TEST_MAIL_RECIPIENT``.

    Bevorzugt den passenden Eintrag aus ``recipients.json`` (damit Name und
    Anrede erhalten bleiben); fehlt er dort, wird ein Eintrag mit
    Standard-Anrede erzeugt.
    """
    if not TEST_MAIL_RECIPIENT:
        # Keine dedizierte Test-Adresse konfiguriert: Erstlauf-Testmail geht
        # an den regulären Verteiler.
        return load_recipients()
    for rec in load_recipients():
        if rec["email"].lower() == TEST_MAIL_RECIPIENT.lower():
            return [rec]
    return [{
        "email": TEST_MAIL_RECIPIENT,
        "name": None,
        "greeting": personal_greeting(None),
    }]


def _intro_paragraph(text):
    """Render an introductory sentence shown right below the salutation."""
    return (f'<p class="intro text-default" '
            f'style="font-size:14px;color:#444;margin:0 0 16px;">{text}</p>')


def parse_recipients(raw):
    """Parse 'Max <m@x>, anna@y' → list of (name_or_None, addr) tuples."""
    pairs = []
    for name, addr in getaddresses([raw or ""]):
        addr = addr.strip()
        if not addr or "@" not in addr:
            continue
        pairs.append((name.strip() or None, addr))
    return pairs


def personal_greeting(name):
    """Render a fallback salutation when no custom greeting is configured."""
    if not name:
        return "Hallo,"
    parts = name.split()
    if not parts:
        return "Hallo,"
    head = parts[0].lower().rstrip(".")
    if head in _TITLES and len(parts) >= 2:
        return f"Hallo {parts[0]} {parts[1]},"
    return f"Hallo {parts[0]},"


def _normalize_greeting(text):
    text = (text or "").strip()
    if not text:
        return "Hallo,"
    if not text.endswith(("!", ",", ".")):
        text = text + ","
    return text


def load_recipients():
    """Load recipients from ``recipients.json`` (preferred) or EMAIL_RECIPIENT.

    File format: list of objects with ``email`` (required), ``greeting``
    (optional – falls back to ``Hallo,``/``Hallo <Vorname>,``) and ``name``
    (optional – shown in the To-header).
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), RECIPIENTS_FILE)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            result = []
            for entry in data:
                addr = (entry.get("email") or "").strip()
                if not addr or "@" not in addr:
                    continue
                name = (entry.get("name") or "").strip() or None
                greeting = (entry.get("greeting") or "").strip()
                if not greeting:
                    greeting = personal_greeting(name)
                result.append({
                    "email": addr,
                    "name": name,
                    "greeting": _normalize_greeting(greeting),
                })
            if result:
                logger.info("Empfänger aus %s geladen: %d", RECIPIENTS_FILE, len(result))
                return result
            logger.warning("%s enthält keine gültigen Einträge – nutze EMAIL_RECIPIENT-Fallback.",
                           RECIPIENTS_FILE)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning("%s ist korrupt (%s) – nutze EMAIL_RECIPIENT-Fallback.",
                           RECIPIENTS_FILE, e)
    return [
        {"email": addr, "name": name, "greeting": personal_greeting(name)}
        for name, addr in parse_recipients(os.environ.get("EMAIL_RECIPIENT", ""))
    ]


def send_email(changes=None, page_changed=False, is_test=False,
        all_current_links=None, new_links=None, removed_links=None,
        sessions_data=None, dry_run=False, recipients=None):
    sender = os.environ["EMAIL_SENDER"]
    if recipients is None:
        recipients = load_recipients()
    if not recipients:
        logger.error("Keine Empfänger konfiguriert (weder %s noch EMAIL_RECIPIENT).",
                     RECIPIENTS_FILE)
        return
    password = os.environ["EMAIL_PASSWORD"]
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT") or "587")

    if all_current_links is None:
        all_current_links = []
    if new_links is None:
        new_links = []
    if removed_links is None:
        removed_links = []
    if sessions_data is None:
        sessions_data = {}

    subject = None

    now_str = datetime.now(TZ).strftime('%d.%m.%Y %H:%M')
    now_full = datetime.now(TZ).strftime('%d.%m.%Y %H:%M:%S')
    num_sessions = len(sessions_data)
    total_tops = sum(len(s.get("tops", [])) for s in sessions_data.values())
    total_docs = sum(len(s.get("docs", [])) for s in sessions_data.values())

    if is_test:
        subject = f"{MAIL_SUBJECT_PREFIX} \u2013 Testmail erfolgreich \u2013 {now_str}"

        session_overview = ""
        if sessions_data:
            s_items = ""
            for k, s in _sort_sessions(sessions_data).items():
                name = s.get("name", "")
                url = s.get("detail_url", "")
                n_tops = len(s.get("tops", []))
                n_docs = len(s.get("docs", []))
                date_time = _format_date(s.get("date", ""), s.get("time", ""))
                if url:
                    link = (f'<a class="title-link text-strong" href="{url}" '
                            f'style="color:#222;text-decoration:none;font-size:15px;'
                            f'font-weight:700;line-height:1.35;">{name}</a>')
                else:
                    link = (f'<span class="text-strong" style="color:#222;font-size:15px;'
                            f'font-weight:700;">{name}</span>')
                tops = s.get("tops", [])
                docs = s.get("docs", [])
                docs_by_top = {}
                for d in docs:
                    docs_by_top.setdefault(d.get("top", ""), []).append(d)
                top_html = _render_tops_with_sections(tops, docs_by_top, max_tops=50)
                s_items += _card(
                    f'<div class="text-muted" style="font-size:12px;color:#666;margin-bottom:6px;">{date_time}</div>'
                    f'<div class="session-title-text" style="line-height:1.35;">{link}</div>'
                    f'<div class="text-muted" style="font-size:12px;color:#666;margin-top:6px;">{n_tops} TOPs &middot; {n_docs} Dokumente</div>'
                    f'{top_html}',
                    variant="geaendert",
                )
            session_overview = (
                '<div style="margin-top:24px;">'
                '<div class="section-heading text-strong" '
                'style="font-size:14px;font-weight:700;color:#222;margin-bottom:10px;'
                'text-transform:uppercase;letter-spacing:0.7px;">'
                f'Aktuell erfasste Sitzungen ({num_sessions})</div>'
                f'{s_items}</div>'
            )

        title_html = ('<div class="title-h text-strong" '
                      'style="font-size:22px;font-weight:800;color:#222;line-height:1.3;">'
                      'Monitor erfolgreich eingerichtet</div>')
        stats_card = _card(
            '<div class="text-muted" style="font-size:11px;font-weight:700;color:#666;'
            'text-transform:uppercase;letter-spacing:0.7px;margin-bottom:10px;">Ausgangszustand</div>'
            '<table role="presentation" cellpadding="0" cellspacing="0" width="100%"><tr>'
            f'<td class="stat-cell" style="text-align:center;padding:10px 4px;">'
            f'<div class="stat-num text-strong" style="font-size:24px;font-weight:700;color:#222;">{num_sessions}</div>'
            f'<div class="text-muted" style="font-size:11px;color:#666;text-transform:uppercase;letter-spacing:0.5px;">Sitzungen</div></td>'
            f'<td class="stat-cell" style="text-align:center;padding:10px 4px;">'
            f'<div class="stat-num text-strong" style="font-size:24px;font-weight:700;color:#222;">{total_tops}</div>'
            f'<div class="text-muted" style="font-size:11px;color:#666;text-transform:uppercase;letter-spacing:0.5px;">TOPs</div></td>'
            f'<td class="stat-cell" style="text-align:center;padding:10px 4px;">'
            f'<div class="stat-num text-strong" style="font-size:24px;font-weight:700;color:#222;">{total_docs}</div>'
            f'<div class="text-muted" style="font-size:11px;color:#666;text-transform:uppercase;letter-spacing:0.5px;">Dokumente</div></td>'
            '</tr></table>',
            variant="geaendert",
        )
        body_content = (
            GREETING_BLOCK_HTML
            + '<p class="text-default" style="font-size:14px;color:#444;margin:0 0 14px;">'
              'Dies ist eine <b>Testmail</b> beim ersten Durchlauf. Ab jetzt wird die Seite '
              'regelmäßig geprüft und du erhältst eine E-Mail, sobald sich etwas ändert.</p>'
            + stats_card + session_overview
        )
        body_html = _email_wrapper(title_html, body_content, now_full)

    elif changes and (changes["added"] or changes["removed"] or changes["changed"]):
        # Detailed session-level changes
        n_added = len(changes.get("added", {}))
        n_removed = len(changes.get("removed", {}))
        n_changed = len(changes.get("changed", {}))

        def _pl(n, singular, plural):
            return singular if n == 1 else plural

        subject_parts = []
        if n_added:
            subject_parts.append(f"{n_added} neue {_pl(n_added, 'Sitzung', 'Sitzungen')}")
        if n_changed:
            subject_parts.append(f"{n_changed} geänderte {_pl(n_changed, 'Sitzung', 'Sitzungen')}")
        if n_removed:
            subject_parts.append(f"{n_removed} entfernte {_pl(n_removed, 'Sitzung', 'Sitzungen')}")
        subject_detail = ", ".join(subject_parts)

        subject = f"{MAIL_SUBJECT_PREFIX} \u2013 {subject_detail} \u2013 {now_str}"

        stat_cells = ""
        if n_added:
            stat_cells += (f'<td class="stat-cell" style="text-align:center;padding:10px 6px;">'
                           f'<div class="stat-num" style="font-size:26px;font-weight:700;color:#38a169;">{n_added}</div>'
                           f'<div class="text-muted" style="font-size:10px;color:#666;text-transform:uppercase;letter-spacing:0.7px;">Neu</div></td>')
        if n_changed:
            stat_cells += (f'<td class="stat-cell" style="text-align:center;padding:10px 6px;">'
                           f'<div class="stat-num" style="font-size:26px;font-weight:700;color:{BRAND_COLOR};">{n_changed}</div>'
                           f'<div class="text-muted" style="font-size:10px;color:#666;text-transform:uppercase;letter-spacing:0.7px;">Geändert</div></td>')
        if n_removed:
            stat_cells += (f'<td class="stat-cell" style="text-align:center;padding:10px 6px;">'
                           f'<div class="stat-num" style="font-size:26px;font-weight:700;color:#e53e3e;">{n_removed}</div>'
                           f'<div class="text-muted" style="font-size:10px;color:#666;text-transform:uppercase;letter-spacing:0.7px;">Entfernt</div></td>')
        stat_cells += (f'<td class="stat-cell stat-divider" style="text-align:center;padding:10px 6px;border-left:1px solid #e2e8f0;">'
                       f'<div class="stat-num text-strong" style="font-size:26px;font-weight:700;color:#222;">{num_sessions}</div>'
                       f'<div class="text-muted" style="font-size:10px;color:#666;text-transform:uppercase;letter-spacing:0.7px;">Gesamt</div></td>')

        changes_html = build_session_changes_html(changes)

        title_html = ('<div class="title-h text-strong" '
                      'style="font-size:22px;font-weight:800;color:#222;line-height:1.3;">'
                      'Änderungen erkannt</div>')
        overview_card = _card(
            '<div class="text-muted" style="font-size:11px;font-weight:700;color:#666;'
            'text-transform:uppercase;letter-spacing:0.7px;margin-bottom:10px;">Übersicht</div>'
            '<table role="presentation" cellpadding="0" cellspacing="0" width="100%"><tr>'
            f'{stat_cells}'
            '</tr></table>',
            variant="geaendert",
        )
        intro_html = _intro_paragraph(
            "im Ratsinformationssystem der Stadt Kamen hat sich etwas getan. "
            "Hier ist deine Übersicht der erkannten Änderungen:"
        )
        body_content = (
            GREETING_BLOCK_HTML
            + intro_html
            + overview_card
            + '<div class="divider" style="border-top:1px solid #e8ecf1;margin:18px 0 6px;"></div>'
            + changes_html
        )
        body_html = _email_wrapper(title_html, body_content, now_full)

    elif new_links:
        # Fallback: only link-level changes (no session details)
        subject = f"{MAIL_SUBJECT_PREFIX} \u2013 Neue Datei \u2013 {now_str}"

        new_links_items = ""
        for l in new_links:
            new_links_items += _card(
                f'<a class="doc-link title-link" href="{l["href"]}" '
                f'style="color:{BRAND_COLOR};text-decoration:none;font-weight:600;font-size:14px;">'
                f'{l["text"] or l["href"]}</a>',
                variant="neu",
            )

        removed_html = ""
        if removed_links:
            for l in removed_links:
                removed_html += _card(
                    f'<span class="text-strike" style="color:#888;text-decoration:line-through;font-size:14px;">'
                    f'{l["text"] or l["href"]}</span>',
                    variant="entfernt",
                )
            removed_html = (
                '<div style="margin-top:20px;">'
                '<div style="margin-bottom:10px;">'
                f'{_badge("Entfernt", "entfernt")} '
                '<span class="section-heading text-strong" style="font-size:14px;font-weight:700;'
                'color:#222;margin-left:8px;text-transform:uppercase;letter-spacing:0.7px;">'
                'Entfernte Dokumente</span></div>'
                f'{removed_html}</div>'
            )

        title_html = ('<div class="title-h text-strong" '
                      'style="font-size:22px;font-weight:800;color:#222;line-height:1.3;">'
                      'Neue Dateien gefunden</div>')
        intro_html = _intro_paragraph(
            "im Ratsinformationssystem der Stadt Kamen wurden neue Dateien "
            "veröffentlicht:"
        )
        body_content = (
            GREETING_BLOCK_HTML
            + intro_html
            + '<div style="margin-bottom:8px;">'
            '<div style="margin-bottom:10px;">'
            f'{_badge("Neu", "neu")} '
            '<span class="section-heading text-strong" style="font-size:14px;font-weight:700;'
            'color:#222;margin-left:8px;text-transform:uppercase;letter-spacing:0.7px;">'
            'Neue Dateien</span></div>'
            f'{new_links_items}</div>'
            f'{removed_html}'
        )
        body_html = _email_wrapper(title_html, body_content, now_full)

    else:
        # No concrete changes – do not send email
        return

    # Plaintext fallback from HTML (strip tags)
    from html import escape as html_escape, unescape
    text_body_template = unescape(re.sub(r'<[^>]+>', ' ', body_html))
    text_body_template = re.sub(r'[ \t]+', ' ', text_body_template).strip()
    text_body_template = re.sub(r'\n{3,}', '\n\n', text_body_template)

    logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "assets", "logo.png")
    try:
        with open(logo_path, "rb") as f:
            logo_bytes = f.read()
    except OSError as e:
        logger.warning("Logo nicht ladbar (%s) – Mail ohne eingebettetes Logo.", e)
        logo_bytes = None

    def _build_message(rec):
        greeting = rec["greeting"]
        html_body = body_html.replace(GREETING_PLACEHOLDER, html_escape(greeting))
        plain_body = text_body_template.replace(GREETING_PLACEHOLDER, greeting)

        outer = MIMEMultipart("related")
        outer["From"] = sender
        outer["To"] = formataddr((rec.get("name") or "", rec["email"]))
        outer["Subject"] = subject

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(plain_body, "plain", "utf-8"))
        alt.attach(MIMEText(html_body, "html", "utf-8"))
        outer.attach(alt)

        if logo_bytes is not None:
            img = MIMEImage(logo_bytes, _subtype="png")
            img.add_header("Content-ID", "<logo>")
            img.add_header("Content-Disposition", "inline", filename="logo.png")
            outer.attach(img)
        return outer

    if dry_run:
        first = recipients[0]
        preview = _build_message(first)
        addr_list = ", ".join(r["email"] for r in recipients)
        logger.info("--dry-run: E-Mail würde gesendet an %d Empfänger (%s)",
                    len(recipients), addr_list)
        logger.info("Betreff: %s", subject)
        print(f"\n--- E-Mail-Vorschau (HTML, personalisiert für {first['email']}) ---")
        html_part = preview.get_payload()[0].get_payload()[1].get_payload(decode=True)
        print(html_part.decode("utf-8"))
        print("--- Ende Vorschau ---\n")
        return

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(sender, password)
        for rec in recipients:
            msg = _build_message(rec)
            server.sendmail(sender, [rec["email"]], msg.as_string())
            logger.info("E-Mail gesendet an %s", rec["email"])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def run_test_mode(test_email, dry_run):
    """Send a one-off test mail only to ``test_email``. Touches no state files.

    ``test_email`` may be a bare address or the ``Name <addr>`` form so the test
    mail can use a personal greeting. The configured recipient list
    (recipients.json / EMAIL_RECIPIENT) is bypassed entirely.
    """
    parsed = parse_recipients(test_email)
    if not parsed or not _EMAIL_RE.match(parsed[0][1]):
        logger.error("Ungültige Test-Mail-Adresse: %r", test_email)
        sys.exit(1)
    if len(parsed) > 1:
        logger.warning("Mehrere Adressen im Test-Eingabefeld – nur die erste wird genutzt.")
    name, addr = parsed[0]

    test_recipient = {
        "email": addr,
        "name": name,
        "greeting": personal_greeting(name),
    }
    pretty = formataddr((name or "", addr))
    logger.info("Test-Modus: Mail geht nur an %s, gespeicherter Zustand bleibt unverändert.", pretty)

    logger.info("Prüfe %s ...", URL)
    try:
        html = fetch_page()
    except Exception as e:
        if is_connectivity_error(e):
            logger.warning("Seite nicht erreichbar (Netzwerk/Timeout): %s – "
                           "Test-Lauf wird ohne Fehler übersprungen.", e)
            sys.exit(0)
        logger.error("Fehler beim Abrufen der Seite: %s", e)
        sys.exit(1)

    current_links = extract_links(html)
    sessions = extract_sessions(html)
    sessions_data = {}
    for s in sessions:
        details = extract_session_details(s["detail_url"])
        sessions_data[s["ksinr"]] = {
            "name": s["name"],
            "date": s["date"],
            "time": s["time"],
            "detail_url": s["detail_url"],
            "title": details["title"],
            "tops": details["tops"],
            "docs": details["docs"],
        }
        time.sleep(REQUEST_DELAY)

    try:
        send_email(
            is_test=True,
            all_current_links=current_links,
            sessions_data=sessions_data,
            dry_run=dry_run,
            recipients=[test_recipient],
        )
    except Exception as e:
        logger.error("E-Mail-Fehler: %s", e)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Ratsinfo Kamen Monitor")
    parser.add_argument("--dry-run", action="store_true",
                        help="E-Mail nur anzeigen, nicht senden")
    parser.add_argument("--force", action="store_true",
                        help="Zeit-Guard umgehen (immer ausführen)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Ausführliche Logausgabe (DEBUG)")
    parser.add_argument("--test-email", metavar="ADRESSE",
                        default=os.environ.get("TEST_EMAIL", "").strip() or None,
                        help="Testmodus: schickt nur an diese Adresse, "
                             "Zustand wird nicht verändert (Fallback: env TEST_EMAIL)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.test_email:
        run_test_mode(args.test_email.strip(), args.dry_run)
        return

    # Scheduled runs only fire inside a target window (default 10:00 / 18:00
    # Berlin, DST-aware) and only once per slot per day. GitHub's scheduler can
    # delay cron jobs by an hour or more, so each target has a multi-hour
    # acceptance window plus a per-slot dedup guard instead of an exact-hour
    # check. Manual (workflow_dispatch), --force and --dry-run runs bypass both.
    is_manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    bypass_schedule = args.force or args.dry_run or is_manual
    run_slots = {}
    active_slot = None
    if not bypass_schedule:
        now = datetime.now(TZ)
        active_slot = current_slot(now)
        if active_slot is None:
            logger.info(
                "Berliner Zeit %s liegt in keinem Lauf-Fenster (Ziel: %s Uhr) – übersprungen.",
                now.strftime("%H:%M"), ", ".join(f"{h:02d}" for h in RUN_HOURS))
            return
        run_slots = load_last_run_slots()
        if run_slots.get(active_slot) == now.date().isoformat():
            logger.info("Slot %s Uhr lief heute bereits – übersprungen.", active_slot)
            return
        logger.info("Lauf-Fenster %s Uhr aktiv (Berliner Zeit %s).",
                    active_slot, now.strftime("%H:%M"))

    def mark_slot_done():
        """Record that the active scheduled slot completed today (dedup guard)."""
        if bypass_schedule or active_slot is None:
            return
        run_slots[active_slot] = datetime.now(TZ).date().isoformat()
        save_last_run_slots(run_slots)

    logger.info("Prüfe %s ...", URL)

    try:
        html = fetch_page()
    except Exception as e:
        if is_connectivity_error(e):
            # Ratsportal vorübergehend nicht erreichbar – kein Code-Fehler.
            # Sauber beenden (Exit 0), damit der Workflow nicht rot wird. Der
            # Slot bleibt ungemarkt, sodass der nächste Lauf es erneut versucht.
            logger.warning("Seite nicht erreichbar (Netzwerk/Timeout): %s – "
                           "Lauf wird ohne Fehler übersprungen, nächster Versuch "
                           "beim nächsten Lauf.", e)
            sys.exit(0)
        logger.error("Fehler beim Abrufen der Seite: %s", e)
        sys.exit(1)

    current_hash = compute_hash(html)
    current_links = extract_links(html)
    sessions = extract_sessions(html)

    last_hash, last_sessions, last_links = load_last_state()

    logger.info("Aktueller Hash: %s", current_hash)
    logger.info("Letzter Hash:   %s", last_hash or "(noch keiner)")
    logger.info("Sitzungen auf Startseite: %d", len(sessions))

    # Build detailed session data by fetching each detail page
    sessions_data = {}
    for s in sessions:
        ksinr = s["ksinr"]
        logger.debug("Lade Details für: %s (%s)", s["name"], s["date"])
        details = extract_session_details(s["detail_url"])
        sessions_data[ksinr] = {
            "name": s["name"],
            "date": s["date"],
            "time": s["time"],
            "detail_url": s["detail_url"],
            "title": details["title"],
            "tops": details["tops"],
            "docs": details["docs"],
        }
        time.sleep(REQUEST_DELAY)

    total_tops = sum(len(s["tops"]) for s in sessions_data.values())
    total_docs = sum(len(s["docs"]) for s in sessions_data.values())
    logger.info("Gesamt: %d Sitzungen, %d TOPs, %d Dokumente",
                len(sessions_data), total_tops, total_docs)

    if last_hash is None:
        # First run
        if not args.dry_run:
            save_state(current_hash, sessions_data, current_links)
        logger.info("Erster Durchlauf – Ausgangszustand gespeichert.")
        try:
            send_email(is_test=True, all_current_links=current_links,
                       sessions_data=sessions_data, dry_run=args.dry_run,
                       recipients=test_mail_recipients())
        except Exception as e:
            logger.error("E-Mail-Fehler: %s", e)
            sys.exit(1)
        mark_slot_done()
        return

    if current_hash != last_hash:
        # Compare session-level changes
        changes = compare_sessions(last_sessions, sessions_data)

        # Also check link-level changes as fallback
        new_links = find_new_links(last_links, current_links)
        removed_links = find_removed_links(last_links, current_links)

        # Entfernte Sitzungen lösen alleine keine Mail aus – sie reisen nur als
        # Beiwerk mit, wenn es ohnehin Neuigkeiten gibt. Außerhalb des
        # Retention-Fensters (3 Tage) verfallen sie still.
        has_changes = (
            changes["added"] or changes["changed"]
            or new_links or removed_links
        )

        if has_changes:
            logger.info("Neue Sitzungen: %d", len(changes["added"]))
            logger.info("Entfernte Sitzungen (anhängig): %d", len(changes["removed"]))
            logger.info("Geänderte Sitzungen: %d", len(changes["changed"]))

            signature = compute_email_signature(
                changes=changes,
                new_links=new_links,
                removed_links=removed_links,
            )
            last_signature = load_last_email_hash()

            if signature == last_signature and not args.dry_run:
                logger.info("E-Mail-Inhalt identisch zur letzten Mail – wird unterdrückt.")
            else:
                try:
                    send_email(
                        changes=changes,
                        page_changed=True,
                        new_links=new_links,
                        removed_links=removed_links,
                        all_current_links=current_links,
                        sessions_data=sessions_data,
                        dry_run=args.dry_run,
                    )
                except Exception as e:
                    logger.error("E-Mail-Fehler: %s", e)
                    sys.exit(1)

                if not args.dry_run:
                    save_last_email_hash(signature)
                    mark_removed_as_notified(list(changes["removed"].keys()))
        else:
            if changes["removed"]:
                logger.info(
                    "Entfernte Sitzungen anhängig (%d), aber keine sonstigen Neuigkeiten – warte auf nächste Mail.",
                    len(changes["removed"]),
                )
            else:
                logger.info("Hash geändert, aber keine konkreten Unterschiede – keine E-Mail.")

        if not args.dry_run:
            save_state(current_hash, sessions_data, current_links)
    else:
        logger.info("Keine Änderung festgestellt.")

    mark_slot_done()


if __name__ == "__main__":
    main()
