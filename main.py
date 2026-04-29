import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

# ============================================================
# Transhare / VStock IPO Monitor
# ============================================================
# GitHub Secret required:
#   DISCORD_WEBHOOK_URL
#
# Optional GitHub Variables / env vars:
#   SEC_USER_AGENT="Mathew Coatney your-email@example.com"
#   BACKFILL_DAYS="45"
#   ONGOING_DAYS="7"
#   RESET_SEEN="false"
# ============================================================

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

SEC_USER_AGENT = os.environ.get(
    "SEC_USER_AGENT",
    "TranshareMonitor mathewcoatney@gmail.com",
).strip()

SEEN_FILE = Path("seen_filings.json")
STATE_FILE = Path("monitor_state.json")

TRANSFER_AGENTS = ["Transhare Corporation", "VStock Transfer"]
FORMS = ["S-1", "F-1", "S-1/A", "F-1/A", "424B4"]

IPO_FORMS = {"S-1", "F-1", "S-1/A", "F-1/A"}
PERIODIC_FORMS = {"10-K", "10-Q", "20-F", "40-F"}

SEC_DELAY_SECONDS = 0.65
MAX_RETRIES = 5
TIMEOUT = 30

_session = requests.Session()
_last_sec_request_at = 0.0


def today_utc() -> datetime:
    return datetime.now(timezone.utc)


def ymd(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def headers_for(host: str) -> dict:
    return {
        "User-Agent": SEC_USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
        "Host": host,
    }


def sec_get(url: str, *, params: Optional[dict] = None, host: str = "www.sec.gov") -> Optional[requests.Response]:
    """
    SEC-friendly GET with:
    - 0.65s delay between SEC requests
    - retries
    - exponential backoff for 429/500/502/503/504
    """
    global _last_sec_request_at

    for attempt in range(1, MAX_RETRIES + 1):
        elapsed = time.monotonic() - _last_sec_request_at
        if elapsed < SEC_DELAY_SECONDS:
            time.sleep(SEC_DELAY_SECONDS - elapsed)

        try:
            _last_sec_request_at = time.monotonic()
            response = _session.get(
                url,
                params=params,
                headers=headers_for(host),
                timeout=TIMEOUT,
            )

            if response.status_code == 200:
                return response

            if response.status_code in {429, 500, 502, 503, 504}:
                wait = min(60, (2 ** attempt) + (attempt * 0.25))
                print(f"SEC GET {response.status_code}; retry {attempt}/{MAX_RETRIES} after {wait:.1f}s: {url}")
                time.sleep(wait)
                continue

            print(f"SEC GET failed {response.status_code}: {url}")
            return response

        except requests.RequestException as exc:
            wait = min(60, (2 ** attempt) + (attempt * 0.25))
            print(f"SEC GET exception; retry {attempt}/{MAX_RETRIES} after {wait:.1f}s: {exc}")
            time.sleep(wait)

    print(f"SEC GET gave up after {MAX_RETRIES} retries: {url}")
    return None


def post_discord(content: str) -> None:
    if not DISCORD_WEBHOOK:
        print("DISCORD_WEBHOOK_URL missing; printing alert instead:")
        print(content)
        return

    for attempt in range(1, 4):
        try:
            r = requests.post(DISCORD_WEBHOOK, json={"content": content}, timeout=15)

            if r.status_code in {200, 204}:
                return

            if r.status_code == 429:
                try:
                    wait = float(r.json().get("retry_after", 5))
                except Exception:
                    wait = 5
                print(f"Discord rate limited; waiting {wait}s")
                time.sleep(wait)
                continue

            print(f"Discord error {r.status_code}: {r.text[:300]}")
            return

        except requests.RequestException as exc:
            print(f"Discord exception attempt {attempt}: {exc}")
            time.sleep(2 * attempt)


def load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception as exc:
        print(f"Could not read {path}: {exc}")
    return default


def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def load_seen() -> set:
    if os.environ.get("RESET_SEEN", "").lower() in {"1", "true", "yes"}:
        print("RESET_SEEN=true; starting with empty seen set")
        return set()

    data = load_json(SEEN_FILE, [])

    if isinstance(data, list):
        return {str(x) for x in data if x}

    if isinstance(data, dict):
        return {str(x) for x in data.keys() if x}

    return set()


def save_seen(seen: set) -> None:
    save_json(SEEN_FILE, sorted(seen)[-5000:])


def determine_days_back() -> int:
    """
    First successful run = 45-day backfill.
    After that = 7-day ongoing search.
    """
    if os.environ.get("BACKFILL_DAYS"):
        try:
            return int(os.environ["BACKFILL_DAYS"])
        except ValueError:
            pass

    state = load_json(STATE_FILE, {})
    if not state.get("last_successful_run_utc"):
        return 45

    try:
        return int(os.environ.get("ONGOING_DAYS", "7"))
    except ValueError:
        return 7


def search_efts(agent: str, form_type: str, days_back: int) -> List[Dict[str, Any]]:
    enddt = ymd(today_utc())
    startdt = ymd(today_utc() - timedelta(days=days_back))

    url = "https://efts.sec.gov/LATEST/search-index"

    params = {
        "q": f'"{agent}"',
        "dateRange": "custom",
        "startdt": startdt,
        "enddt": enddt,
        "forms": form_type,
        "from": "0",
        "size": "100",
    }

    response = sec_get(url, params=params, host="efts.sec.gov")

    if not response or response.status_code != 200:
        return []

    try:
        data = response.json()
    except ValueError:
        print(f"EFTS returned non-JSON for {agent} {form_type}")
        return []

    hits = data.get("hits", {}).get("hits", [])

    if not isinstance(hits, list):
        print(f"Unexpected EFTS shape for {agent} {form_type}: {str(data)[:500]}")
        return []

    return hits


def normalize_cik(cik: Any) -> str:
    return str(cik or "").strip().lstrip("0")


def padded_cik(cik: Any) -> str:
    return normalize_cik(cik).zfill(10)


def accession_no_dashes(accession: str) -> str:
    return accession.replace("-", "")


def filing_index_url(cik: Any, accession: str) -> str:
    cik_clean = normalize_cik(cik)
    acc_no_dash = accession_no_dashes(accession)
    return f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{acc_no_dash}/{accession}-index.htm"


def filing_txt_url(cik: Any, accession: str) -> str:
    cik_clean = normalize_cik(cik)
    acc_no_dash = accession_no_dashes(accession)
    return f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{acc_no_dash}/{accession}.txt"


class FilingIndexParser(HTMLParser):
    """
    Parses SEC filing index page and extracts document-table rows.
    This avoids blindly downloading the wrong exhibit/document.
    """

    def __init__(self):
        super().__init__()
        self.in_tr = False
        self.in_td = False
        self.in_a = False
        self.current_cells: List[str] = []
        self.current_cell_parts: List[str] = []
        self.current_href: Optional[str] = None
        self.row_hrefs: List[str] = []
        self.rows: List[Dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        attrs_dict = dict(attrs)

        if tag == "tr":
            self.in_tr = True
            self.current_cells = []
            self.row_hrefs = []

        elif tag == "td" and self.in_tr:
            self.in_td = True
            self.current_cell_parts = []

        elif tag == "a" and self.in_td:
            self.in_a = True
            href = attrs_dict.get("href")
            if href:
                self.current_href = href

    def handle_data(self, data: str) -> None:
        if self.in_td:
            self.current_cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if tag == "a" and self.in_a:
            if self.current_href:
                self.row_hrefs.append(self.current_href)
            self.current_href = None
            self.in_a = False

        elif tag == "td" and self.in_td:
            text = " ".join("".join(self.current_cell_parts).split())
            self.current_cells.append(unescape(text))
            self.current_cell_parts = []
            self.in_td = False

        elif tag == "tr" and self.in_tr:
            if self.current_cells or self.row_hrefs:
                self.rows.append(
                    {
                        "cells": self.current_cells[:],
                        "hrefs": self.row_hrefs[:],
                    }
                )
            self.in_tr = False


def absolute_sec_url(href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return "https://www.sec.gov" + href
    return "https://www.sec.gov/" + href


def get_primary_document_url(cik: Any, accession: str, expected_form: str) -> Optional[str]:
    index_url = filing_index_url(cik, accession)
    response = sec_get(index_url, host="www.sec.gov")

    if not response or response.status_code != 200:
        return None

    parser = FilingIndexParser()
    parser.feed(response.text)

    expected_form_upper = expected_form.upper()
    html_doc_candidates = []

    for row in parser.rows:
        cells = [c.strip() for c in row.get("cells", [])]
        hrefs = row.get("hrefs", [])

        if not hrefs:
            continue

        href = hrefs[0]
        href_lower = href.lower()

        if not (href_lower.endswith(".htm") or href_lower.endswith(".html")):
            continue

        if "ixviewer" in href_lower:
            continue

        row_text = " | ".join(cells).upper()

        if expected_form_upper in row_text:
            return absolute_sec_url(href)

        if any(x in href_lower for x in ["s-1", "s1", "f-1", "f1", "424b4"]):
            html_doc_candidates.append(absolute_sec_url(href))

    if html_doc_candidates:
        return html_doc_candidates[0]

    for row in parser.rows:
        for href in row.get("hrefs", []):
            href_lower = href.lower()
            if (href_lower.endswith(".htm") or href_lower.endswith(".html")) and "ixviewer" not in href_lower:
                return absolute_sec_url(href)

    return None


def strip_html(html: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def get_filing_text(cik: Any, accession: str, expected_form: str) -> Tuple[str, str]:
    """
    First downloads the true primary filing document from the SEC index page.
    If that fails, falls back to the complete accession .txt file.
    """
    primary_url = get_primary_document_url(cik, accession, expected_form)

    if primary_url:
        response = sec_get(primary_url, host="www.sec.gov")
        if response and response.status_code == 200 and len(response.text) > 500:
            return strip_html(response.text), primary_url

    txt_url = filing_txt_url(cik, accession)
    response = sec_get(txt_url, host="www.sec.gov")

    if response and response.status_code == 200:
        return strip_html(response.text), txt_url

    return "", filing_index_url(cik, accession)


def has_transfer_agent_keyword(text: str) -> Tuple[bool, str]:
    low = text.lower()

    if "transhare corporation" in low or "transhare" in low:
        return True, "Transhare Corporation"

    if "vstock transfer" in low or "vstock" in low:
        return True, "VStock Transfer"

    return False, "Unknown"


def extract_exchange(text: str) -> Tuple[bool, str]:
    """
    Reads exchange language directly from the S-1/F-1/424B4 text.
    This is necessary because the submissions API often says Pending before IPO.
    """
    clean = re.sub(r"\s+", " ", text)
    low = clean.lower()

    exchange_patterns = [
        ("NASDAQ", r"nasdaq\s+(capital|global|select)\s+market"),
        ("NASDAQ", r"the\s+nasdaq"),
        ("NASDAQ", r"nasdaq"),
        ("NYSE AMERICAN", r"nyse\s+american"),
        ("NYSE", r"new\s+york\s+stock\s+exchange"),
        ("NYSE", r"\bnyse\b"),
    ]

    for label, pattern in exchange_patterns:
        if re.search(pattern, low, flags=re.I):
            return True, label

    otc_patterns = [
        r"otcqb",
        r"otcqx",
        r"otc\s+pink",
        r"pink\s+open\s+market",
        r"otc\s+markets",
    ]

    if any(re.search(p, low, flags=re.I) for p in otc_patterns):
        return False, "OTC"

    return False, "Unknown/Pending"


def extract_ticker(text: str, company_data: Optional[dict]) -> Optional[str]:
    if company_data:
        tickers = company_data.get("tickers") or []
        if tickers:
            return str(tickers[0]).upper()

    patterns = [
        r"under\s+the\s+symbol\s+[\"'“”‘’]?([A-Z]{1,6})[\"'“”‘’]?",
        r"under\s+the\s+ticker\s+symbol\s+[\"'“”‘’]?([A-Z]{1,6})[\"'“”‘’]?",
        r"trading\s+symbol\s+[\"'“”‘’]?([A-Z]{1,6})[\"'“”‘’]?",
        r"proposed\s+symbol\s+[\"'“”‘’]?([A-Z]{1,6})[\"'“”‘’]?",
        r"symbol\s*[:：]\s*[\"'“”‘’]?([A-Z]{1,6})[\"'“”‘’]?",
    ]

    for pattern in patterns:
        m = re.search(pattern, text, flags=re.I)
        if m:
            sym = m.group(1).upper()
            if sym not in {"NASDAQ", "NYSE", "OTC", "THE", "AND"}:
                return sym

    return None


def extract_flags(text: str) -> List[str]:
    low = text.lower()
    flags = []

    if "pre-funded warrant" in low or "prefunded warrant" in low:
        flags.append("🚩 Pre-funded warrants")

    if "warrant" in low and "pre-funded" not in low and "prefunded" not in low:
        flags.append("🚩 Warrants")

    if "china" in low or "hong kong" in low or "prc" in low or "cayman islands" in low:
        flags.append("🇨🇳 China/HK/PRC/Cayman language")

    if "variable interest entity" in low or "vie" in low:
        flags.append("🚩 VIE language")

    if "reverse split" in low:
        flags.append("🚩 Reverse split")

    if "best efforts" in low:
        flags.append("🚩 Best efforts offering")

    if "firm commitment" in low:
        flags.append("Firm commitment")

    return flags[:5]


def get_company_data(cik: Any) -> Optional[dict]:
    url = f"https://data.sec.gov/submissions/CIK{padded_cik(cik)}.json"
    response = sec_get(url, host="data.sec.gov")

    if not response or response.status_code != 200:
        return None

    try:
        return response.json()
    except ValueError:
        return None


def company_name_from_hit(source: dict, company_data: Optional[dict]) -> str:
    if company_data and company_data.get("name"):
        return str(company_data["name"]).strip()

    display_names = source.get("display_names") or []

    if display_names:
        first = str(display_names[0])
        return re.sub(r"\s*\(CIK.*?\)\s*$", "", first).strip()

    return "Unknown Company"


def already_public_before_ipo(company_data: Optional[dict], filing_date: str) -> bool:
    if not company_data:
        return False

    recent = company_data.get("filings", {}).get("recent", {})
    forms = recent.get("form", []) or []
    dates = recent.get("filingDate", []) or []

    for form, date in zip(forms, dates):
        form = str(form).upper()
        date = str(date)

        if form in PERIODIC_FORMS and date and filing_date and date < filing_date:
            return True

    return False


def count_amendments(company_data: Optional[dict]) -> int:
    if not company_data:
        return 0

    forms = company_data.get("filings", {}).get("recent", {}).get("form", []) or []

    return sum(1 for f in forms if str(f).upper() in {"S-1/A", "F-1/A"})


def has_recent_424b4(company_data: Optional[dict], days: int = 7) -> bool:
    if not company_data:
        return False

    recent = company_data.get("filings", {}).get("recent", {})
    forms = recent.get("form", []) or []
    dates = recent.get("filingDate", []) or []
    cutoff = ymd(today_utc() - timedelta(days=days))

    for form, date in zip(forms, dates):
        if str(form).upper() == "424B4" and str(date) >= cutoff:
            return True

    return False


def categorize_stage(form_type: str, company_data: Optional[dict]) -> Tuple[str, str, int]:
    amendments = count_amendments(company_data)

    if form_type.upper() == "424B4" or has_recent_424b4(company_data, 7):
        return "LIVE", "🔥 JUST PRICED", amendments

    if amendments >= 5:
        return "IMMINENT", f"⚠️ PRICING SOON ({amendments} amendments)", amendments

    return "PIPELINE", f"🧱 PIPELINE ({amendments} amendments)", amendments


def filing_key(cik: Any, accession: str, form_type: str) -> str:
    return f"{padded_cik(cik)}:{accession}:{form_type.upper()}"


def extract_source(hit: dict) -> dict:
    source = hit.get("_source", {}) if isinstance(hit, dict) else {}

    if not isinstance(source, dict):
        return {}

    return source


def get_source_field(source: dict, *names: str) -> str:
    for name in names:
        value = source.get(name)

        if value not in (None, ""):
            return str(value)

    return ""


def build_alert(
    *,
    ticker: Optional[str],
    company: str,
    stage_text: str,
    exchange: str,
    form_type: str,
    filing_date: str,
    agent_found: str,
    flags: List[str],
    filing_url: str,
) -> str:
    ticker_text = f"**{ticker}**" if ticker else "**TBD**"
    flag_text = "\n".join(flags) if flags else ""

    message = (
        "🚨 **Transhare/VStock IPO**\n\n"
        f"{ticker_text} • {company}\n"
        f"{stage_text} • {exchange}\n"
        f"Form: `{form_type}` • Filed: `{filing_date}`\n"
        f"Transfer agent: `{agent_found}`\n"
    )

    if flag_text:
        message += f"{flag_text}\n"

    message += f"\n[View Filing]({filing_url})"

    return message[:1950]


def process_hit(hit: dict, expected_form: str, seen: set) -> bool:
    source = extract_source(hit)

    accession = get_source_field(source, "accession_no", "accessionNo", "adsh")
    cik = get_source_field(source, "cik", "ciks")
    filing_date = get_source_field(source, "file_date", "fileDate", "filing_date")
    form_type = get_source_field(source, "form", "file_type", "formType") or expected_form

    if isinstance(source.get("ciks"), list) and source.get("ciks"):
        cik = str(source["ciks"][0])

    if not accession or not cik:
        print(f"Skipping hit with missing accession/cik: {source}")
        return False

    key = filing_key(cik, accession, form_type)

    if key in seen:
        print(f"Already seen: {key}")
        return False

    company_data = get_company_data(cik)
    company = company_name_from_hit(source, company_data)

    if form_type.upper() in IPO_FORMS and already_public_before_ipo(company_data, filing_date):
        print(f"Reject already-public filer: {company} {form_type} {filing_date}")
        seen.add(key)
        return False

    filing_text, actual_filing_url = get_filing_text(cik, accession, form_type)

    if not filing_text:
        print(f"Reject no filing text: {company} {accession}")
        return False

    has_keyword, agent_found = has_transfer_agent_keyword(filing_text)

    if not has_keyword:
        print(f"Reject keyword not confirmed in primary/txt filing: {company} {accession}")
        return False

    is_major_exchange, exchange = extract_exchange(filing_text)

    if not is_major_exchange:
        exchanges = []
        if company_data:
            exchanges = company_data.get("exchanges") or []

        joined = " ".join(str(x).upper() for x in exchanges)

        if "NASDAQ" in joined:
            is_major_exchange, exchange = True, "NASDAQ"
        elif "NYSE" in joined:
            is_major_exchange, exchange = True, "NYSE"

    if not is_major_exchange:
        print(f"Reject non-NASDAQ/NYSE or unknown exchange: {company} | {exchange}")
        seen.add(key)
        return False

    ticker = extract_ticker(filing_text, company_data)
    stage, stage_text, amendments = categorize_stage(form_type, company_data)
    flags = extract_flags(filing_text)

    alert = build_alert(
        ticker=ticker,
        company=company,
        stage_text=stage_text,
        exchange=exchange,
        form_type=form_type.upper(),
        filing_date=filing_date,
        agent_found=agent_found,
        flags=flags,
        filing_url=actual_filing_url or filing_index_url(cik, accession),
    )

    post_discord(alert)
    seen.add(key)

    print(
        f"ALERT SENT: {ticker or 'TBD'} | {company} | "
        f"{stage} | {form_type} | {amendments} amendments"
    )

    return True


def main() -> None:
    print(f"Running Transhare/VStock monitor at {today_utc().isoformat()}")
    print(f"SEC User-Agent: {SEC_USER_AGENT}")

    days_back = determine_days_back()
    print(f"Search window: last {days_back} days")

    seen = load_seen()
    print(f"Loaded seen filings: {len(seen)}")

    sent = 0
    total_hits = 0
    processed_accessions = set()

    for agent in TRANSFER_AGENTS:
        for form_type in FORMS:
            print(f"Searching EFTS: {agent} | {form_type}")
            hits = search_efts(agent, form_type, days_back)
            print(f"Hits: {len(hits)}")

            total_hits += len(hits)

            for hit in hits:
                source = extract_source(hit)

                accession = get_source_field(source, "accession_no", "accessionNo", "adsh")
                cik = get_source_field(source, "cik", "ciks")

                if isinstance(source.get("ciks"), list) and source.get("ciks"):
                    cik = str(source["ciks"][0])

                dedupe = f"{cik}:{accession}:{form_type}"

                if dedupe in processed_accessions:
                    continue

                processed_accessions.add(dedupe)

                try:
                    if process_hit(hit, form_type, seen):
                        sent += 1
                except Exception as exc:
                    print(f"ERROR processing hit: {exc} | hit={str(hit)[:500]}")

                time.sleep(0.25)

    save_seen(seen)

    save_json(
        STATE_FILE,
        {
            "last_successful_run_utc": today_utc().isoformat(),
            "last_days_back": days_back,
        },
    )

    print(
        f"Done. Total EFTS hits: {total_hits}. "
        f"Discord alerts sent: {sent}. Seen saved: {len(seen)}"
    )


if __name__ == "__main__":
    main()
