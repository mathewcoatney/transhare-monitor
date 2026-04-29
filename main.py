import requests
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

DISCORD_WEBHOOK = os.environ['DISCORD_WEBHOOK_URL']
HEADERS = {'User-Agent': 'TranshareMonitor mathewcoatney@gmail.com'}
SEEN_FILE = Path('seen_filings.json')

def load_seen():
    if SEEN_FILE.exists():
        data = json.loads(SEEN_FILE.read_text())
        return set(x for x in data if x)
    return set()

def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(list(seen)[-2000:]))

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
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=30)
        if r.status_code == 200:
            return r.json().get('hits', {}).get('hits', [])
        else:
            print(f"SEC API returned {r.status_code}")
    except Exception as e:
        print(f"SEC search error: {e}")
    return []

def get_ticker(cik):
    url = f'https://data.sec.gov/submissions/CIK{str(cik).zfill(10)}.json'
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code == 200:
            tickers = r.json().get('tickers', [])
            if tickers:
                return tickers[0].upper()
    except:
        pass
    return None

def get_company_data(cik):
    """Get full company data including exchange and filing history"""
    url = f'https://data.sec.gov/submissions/CIK{str(cik).zfill(10)}.json'
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return None

def is_nasdaq_or_nyse(company_data):
    """Check if listed on NASDAQ or NYSE based on submissions data"""
    if not company_data:
        return False, 'Unknown'
    
    exchanges = company_data.get('exchanges', [])
    if not exchanges:
        # Not yet listed - might be IPO in progress, allow it
        return True, 'Pending'
    
    for ex in exchanges:
        ex_lower = ex.lower() if ex else ''
        if 'nasdaq' in ex_lower:
            return True, 'NASDAQ'
        if 'nyse' in ex_lower:
            return True, 'NYSE'
    
    return False, exchanges[0] if exchanges else 'Unknown'

def is_actual_ipo(company_data, s1_date):
    """Check if company has prior periodic reports before S-1"""
    if not company_data:
        return True
    
    filings = company_data.get('filings', {}).get('recent', {})
    forms = filings.get('form', [])
    dates = filings.get('filingDate', [])
    
    for form, date in zip(forms, dates):
        if form in ['10-K', '10-Q', '20-F'] and date < s1_date:
            return False
    return True

def get_stage(company_data):
    if not company_data:
        return 'PIPELINE', 0
    
    filings = company_data.get('filings', {}).get('recent', {})
    forms = filings.get('form', [])
    dates = filings.get('filingDate', [])

    # Check 424B4 in last 7 days
    for form, date in zip(forms, dates):
        if form == '424B4':
            try:
                days_since = (datetime.now() - datetime.strptime(date, '%Y-%m-%d')).days
                if days_since <= 7:
                    return 'LIVE', 0
            except:
                pass

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

    try:
        r = requests.post(DISCORD_WEBHOOK, json={'content': msg}, timeout=10)
        if r.status_code == 204:
            print(f'✓ Sent: {company} | {stage}')
        elif r.status_code == 429:
            print(f'⚠️ Rate limited, waiting...')
            time.sleep(10)
        else:
            print(f'✗ Discord error {r.status_code}')
        time.sleep(3)
    except Exception as e:
        print(f'✗ Discord failed: {e}')

def main():
    print(f'Running at {datetime.now()}')
    seen = load_seen()
    new_count = 0

    agents = ['Transhare Corporation', 'VStock Transfer']
    forms = ['S-1', 'F-1', 'S-1/A', 'F-1/A', '424B4']

    for agent in agents:
        for form_type in forms:
            print(f'Searching: {agent} | {form_type}')
            hits = search_sec(agent, form_type, days_back=45)
            print(f'  {len(hits)} hits')

            for hit in hits:
                source = hit.get('_source', {})
                accession = source.get('accession_no', '')
                cik = source.get('cik', '')
                filing_date = source.get('file_date', '')
                display_names = source.get('display_names', [])
                company = display_names[0] if display_names else 'Unknown'

                if not accession or not cik:
                    continue

                if accession in seen:
                    continue

                seen.add(accession)

                filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{accession}-index.htm"

                # Get company data once
                company_data = get_company_data(cik)
                time.sleep(0.2)

                # Exchange filter - only NASDAQ/NYSE
                is_major, exchange = is_nasdaq_or_nyse(company_data)
                if not is_major:
                    print(f'  ✗ Not NASDAQ/NYSE ({exchange}): {company}')
                    continue

                # IPO check for initial filings
                if form_type in ['S-1', 'F-1']:
                    if not is_actual_ipo(company_data, filing_date):
                        print(f'  ✗ Already public: {company}')
                        continue

                # Get ticker
                ticker = None
                if company_data:
                    tickers = company_data.get('tickers', [])
                    if tickers:
                        ticker = tickers[0].upper()

                # Get stage
                stage, amendments = get_stage(company_data)

                send_discord(
                    company=company,
                    form_type=form_type,
                    ticker=ticker,
                    stage=stage,
                    amendments=amendments,
                    exchange=exchange,
                    filing_url=filing_url
                )

                new_count += 1

    print(f'Done. {new_count} new filings sent to Discord.')
    save_seen(seen)

if __name__ == '__main__':
    main()
