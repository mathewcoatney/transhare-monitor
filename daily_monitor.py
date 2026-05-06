import re
from typing import Any, Optional

import main as monitor


def clean_ticker(symbol: Any) -> Optional[str]:
    if not symbol:
        return None

    cleaned = re.sub(r"[^A-Z0-9.-]", "", str(symbol).upper()).strip(".-")

    if cleaned in {"NASDAQ", "NYSE", "OTC", "THE", "AND", "N/A", "NA"}:
        return None

    return cleaned or None


def alert_identity_key(ticker: Optional[str], cik: Any, company: str) -> str:
    """
    main.py already stores individual filing accessions in seen_filings.json.
    This stores the actual alert target too, so amendments for the same ticker
    do not keep creating repeat Discord pings.
    """
    ticker = clean_ticker(ticker)
    if ticker:
        return f"TICKER:{ticker}"

    cik_key = monitor.padded_cik(cik)
    if cik_key and cik_key != "0000000000":
        return f"CIK:{cik_key}"

    company_key = re.sub(r"[^A-Z0-9]+", "-", company.upper()).strip("-")
    return f"COMPANY:{company_key[:80] or 'UNKNOWN'}"


def process_hit_without_repeat_tickers(hit: dict, expected_form: str, seen: set) -> bool:
    source = monitor.extract_source(hit)

    accession = monitor.get_source_field(source, "accession_no", "accessionNo", "adsh")
    cik = monitor.get_source_field(source, "cik", "ciks")
    filing_date = monitor.get_source_field(source, "file_date", "fileDate", "filing_date")
    form_type = monitor.get_source_field(source, "form", "file_type", "formType") or expected_form

    if isinstance(source.get("ciks"), list) and source.get("ciks"):
        cik = str(source["ciks"][0])

    if not accession or not cik:
        print(f"Skipping hit with missing accession/cik: {source}")
        return False

    key = monitor.filing_key(cik, accession, form_type)

    if key in seen:
        print(f"Already seen filing: {key}")
        return False

    company_data = monitor.get_company_data(cik)
    company = monitor.company_name_from_hit(source, company_data)

    if form_type.upper() in monitor.IPO_FORMS and monitor.already_public_before_ipo(company_data, filing_date):
        print(f"Reject already-public filer: {company} {form_type} {filing_date}")
        seen.add(key)
        return False

    filing_text, actual_filing_url = monitor.get_filing_text(cik, accession, form_type)

    if not filing_text:
        print(f"Reject no filing text: {company} {accession}")
        return False

    has_keyword, agent_found = monitor.has_transfer_agent_keyword(filing_text)

    if not has_keyword:
        print(f"Reject keyword not confirmed in primary/txt filing: {company} {accession}")
        return False

    is_major_exchange, exchange = monitor.extract_exchange(filing_text)

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

    ticker = monitor.extract_ticker(filing_text, company_data)
    identity_key = alert_identity_key(ticker, cik, company)

    if identity_key in seen:
        print(
            f"Reject already-alerted ticker/company: {identity_key} | "
            f"{company} | {form_type} | {filing_date}"
        )
        seen.add(key)
        return False

    stage, stage_text, amendments = monitor.categorize_stage(form_type, company_data)
    flags = monitor.extract_flags(filing_text)

    alert = monitor.build_alert(
        ticker=ticker,
        company=company,
        stage_text=stage_text,
        exchange=exchange,
        form_type=form_type.upper(),
        filing_date=filing_date,
        agent_found=agent_found,
        flags=flags,
        filing_url=actual_filing_url or monitor.filing_index_url(cik, accession),
    )

    monitor.post_discord(alert)
    seen.add(key)
    seen.add(identity_key)

    print(
        f"ALERT SENT: {ticker or 'TBD'} | {company} | "
        f"{stage} | {form_type} | {amendments} amendments | saved {identity_key}"
    )

    return True


monitor.process_hit = process_hit_without_repeat_tickers
monitor.main()
