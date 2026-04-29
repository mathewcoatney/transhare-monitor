import requests
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

DISCORD_WEBHOOK = os.environ['DISCORD_WEBHOOK_URL']
HEADERS = {
    'User-Agent': 'TranshareMonitor mathewcoatney@gmail.com',
    'Accept': 'application/json',
    'Host': 'efts.sec.gov'
}
DATA_HEADERS = {
    'User-Agent': 'TranshareMonitor mathewcoatney@gmail.com',
    'Accept': 'application/json'
}
SEEN_FILE = Path('seen_filings.json')

def load_seen():
    if SEEN_FILE.exists():
        try:
            data = json.loads(SEEN_FILE.read_text())
            return set(x for x in data if x and isinstance(x, str))
        except:
            return set()
    return set()

def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(list(seen)[-3000:]))

def sec_request_with_retry(url, params=None, headers=None, max_retries=3):
    """Make SEC request with exponential backoff for rate limits"""
    if headers is None:
        headers = DATA_HEADERS
    
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=30)
            if r.status_code == 200:
                return r
            elif r.status_code in [429, 500, 502, 503]:
                wait = (attempt + 1) * 5
                print(f"  Got {r.status_code}, waiting {wait}s...")
                time.sleep(wait)
            else:
                print(f"  Got {r.status_code}")
                return None
        except Exception as e:
            print(f"  Request error: {e}")
            time.sleep(3)
    return None

def search_sec(query, form_type, days_back=45):
    start_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
    end_date = datetime.now().strftime('%Y-%m-%d')
    url = 'https://efts.sec.gov/LATEST/search-index'
    params = {
        'q': f'"{query}"',
        'dateRange': 'custom',
        'startdt': start_date,
        'enddt': end_date,
        'forms': form_type
    }
    
    search_headers = {
        'User-Agent': 'TranshareMonitor mathewcoatney@gmail.com',
        'Accept': 'application/json'
    }
    
    r = sec_request_with_retry(url, params=params, headers=search_headers)
    if r:
        try:
            return r.json().get('hits', {}).get('hits', [])
        except:
            return []
    return []

def get_company_data(cik):
    """Get company submissions data"""
    url = f'https://data.sec.gov/submissions/CIK{str(cik).zfill(10)}.json'
    r = sec_request_with_retry(url)
    if r:
        try:
            return r.json()
        except:
            return None
    return None

def get_exchange_info(company_data):
    """Determine if NASDAQ/NYSE listed or pending"""
    if not company_data:
        return None
    
    exchanges = company_data.get('exchanges', [])
    
    if not exchanges:
        # Pre-IPO - allow it through, label as Pending
        return 'Pending'
    
    for ex in exchanges:
        if not ex:
            continue
        ex_lower = ex.lower()
        if 'nasdaq' in ex_lower:
            return 'NASDAQ'
        if 'nyse arca' in ex_lower:
            return 'NYSE Arca'
        if 'nyse american' in ex_lower or 'nyse mkt' in ex_lower:
            return 'NYSE American'
        if 'nyse' in ex_lower:
            return 'NYSE'
    
    return None  # OTC or other - skip

def is_actual_ipo(company_data, s1_date):
    """Check if company has prior periodic reports before S-1"""
    if not company_data:
        return True
    
    filings = company_data.get('filings', {}).get('recent', {})
    forms = filings.get('form', [])
    dates = filings.get('filingDate', [])
    
    for form, date in zip(forms, dates):
        if form in ['10-K', '10-Q', '20-F', '10-K/A', '10-Q/A', '20-F/A'] and date < s1_date:
            return False
    return True

def get_stage_and_amendments(company_data):
    """Determine LIVE / IMMINENT / PIPELINE"""
    if not company_data:
        return 'PIPELINE', 0
    
    filings = company_data.get('filings', {}).get('recent', {})
    forms = filings.get('form', [])
    dates = filings.get('filingDate', [])

    # Check for 424B4 in last 7 days = LIVE
    for form, date in zip(forms, dates):
        if form == '424B4':
            try:
                days_since = (datetime.now() - datetime.strptime(date, '%Y-%m-%d')).days
                if days_since <= 7:
                    return 'LIVE', 0
            except:
                pass

    # Count amendments
    amendments = sum(1 for f in forms if f in ['S-1/A', 'F-1/A'])
    
    if amendments >= 5:
        return 'IMMINENT', amendments
    return 'PIPELINE', amendments

def send_discord(company, form_type, ticker, stage, amendments, exchange, filing_url):
    if stage == 'LIVE':
        stage_text = '🔥 JUST PRICED'
    elif stage == 'IMMINENT':
        stage_text = f'⚠️ PRICING SOON ({amendments} amends)'
    else:
        stage_text = f'📊 PIPELINE ({amendments} amends)'

    ticker_text = f'**{ticker}**' if ticker else '**TBD**'

    msg = '🚨 **Transhare/VStock IPO**\n\n'
    msg += f'{ticker_text} • {company}\n'
    msg += f'{stage_text} • {exchange}\n'
    msg += f'Form: `{form_type}`\n'
    msg += f'\n[View Filing]({filing_url})'

    if len(msg) > 1950:
        msg = msg[:1950] + '...'

    for attempt in range(3):
        try:
            r = requests.post(DISCORD_WEBHOOK, json={'content': msg}, timeout=15)
            if r.status_code == 204:
                print(f'  ✓ Sent: {company} | {stage} | {form_type}')
                time.sleep(2)
                return True
            elif r.status_code == 429:
                retry_after = r.json().get('retry_after', 5)
                print(f'  ⏸ Rate limited, waiting {retry_after}s')
                time.sleep(retry_after + 1)
            else:
                print(f'  ✗ Discord {r.status_code}: {r.text[:100]}')
                time.sleep(2)
                return False
        except Exception as e:
            print(f'  ✗ Discord exception: {e}')
            time.sleep(3)
    return False

def main():
    print(f'='*60)
    print(f'Running at {datetime.now()}')
    print(f'='*60)
    
    seen = load_seen()
    print(f'Loaded {len(seen)} previously seen filings')
    
    new_count = 0
    sent_companies = set()  # Avoid sending same company multiple times in one run

    agents = ['Transhare Corporation', 'VStock Transfer']
    forms = ['S-1', 'F-1', 'S-1/A', 'F-1/A', '424B4']

    for agent in agents:
        for form_type in forms:
            print(f'\n→ Searching: {agent} | {form_type}')
            hits = search_sec(agent, form_type, days_back=45)
            print(f'  {len(hits)} hits')
            
            time.sleep(1)  # Delay between searches

            for hit in hits:
                source = hit.get('_source', {})
                accession = source.get('accession_no', '')
                ciks = source.get('ciks', [])
                cik = ciks[0] if ciks else source.get('cik', '')
                filing_date = source.get('file_date', '')
                display_names = source.get('display_names', [])
                company = display_names[0] if display_names else 'Unknown'

                if not accession or not cik:
                    continue

                if accession in seen:
                    continue

                seen.add(accession)

                # Avoid duplicate alerts for same company in single run
                company_key = f"{cik}_{form_type}"
                if company_key in sent_companies:
                    continue

                filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{accession}-index.htm"

                print(f'\n  Processing: {company}')
                
                # Get company data
                company_data = get_company_data(cik)
                time.sleep(0.5)

                if not company_data:
                    print(f'  ✗ Could not fetch company data')
                    continue

                # Exchange filter
                exchange = get_exchange_info(company_data)
                if not exchange:
                    print(f'  ✗ Not NASDAQ/NYSE')
                    continue

                # IPO verification (only for initial filings)
                if form_type in ['S-1', 'F-1']:
                    if not is_actual_ipo(company_data, filing_date):
                        print(f'  ✗ Already public (secondary offering)')
                        continue

                # Get ticker
                ticker = None
                tickers = company_data.get('tickers', [])
                if tickers:
                    ticker = tickers[0].upper()

                # Get stage
                stage, amendments = get_stage_and_amendments(company_data)

                # Send to Discord
                if send_discord(
                    company=company,
                    form_type=form_type,
                    ticker=ticker,
                    stage=stage,
                    amendments=amendments,
                    exchange=exchange,
                    filing_url=filing_url
                ):
                    new_count += 1
                    sent_companies.add(company_key)

    print(f'\n{"="*60}')
    print(f'Done. {new_count} new filings sent to Discord.')
    print(f'Total seen: {len(seen)}')
    print(f'{"="*60}')
    save_seen(seen)

if __name__ == '__main__':
    main()
