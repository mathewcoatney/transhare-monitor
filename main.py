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
            hits = r.json().get('hits', {}).get('hits', [])
            return hits
        else:
            print(f"SEC API returned {r.status_code}")
    except Exception as e:
        print(f"SEC search error: {e}")
    return []

def get_filing_text(cik, accession):
    accession_clean = accession.replace('-', '')
    index_url = f'https://www.sec.gov/Archives/edgar/data/{cik}/{accession_clean}/{accession}-index.htm'
    try:
        r = requests.get(index_url, headers=HEADERS, timeout=30)
        index_text = r.text.lower()

        # First check index page itself for transfer agent mention
        if 'transhare' in index_text or 'vstock' in index_text or 'v-stock' in index_text:
            return index_text

        # Find all .htm links in index
        links = re.findall(r'href="(/archives/edgar/data/[^"]+\.htm)"', r.text, re.IGNORECASE)

        if not links:
            return index_text

        # Try first document link
        doc_url = f'https://www.sec.gov{links[0]}'
        doc_r = requests.get(doc_url, headers=HEADERS, timeout=60, stream=True)
        content = b''
        for chunk in doc_r.iter_content(chunk_size=8192):
            content += chunk
            if len(content) > 800000:
                break
        return content.decode('utf-8', errors='ignore').lower()

    except Exception as e:
        print(f"Error fetching filing: {e}")
        return ''

def get_exchange(text):
    if not text:
        return None
    if 'nasdaq' in text:
        return 'NASDAQ'
    if 'nyse american' in text:
        return 'NYSE American'
    if re.search(r'\bnyse\b', text):
        return 'NYSE'
    return None

def is_actual_ipo(cik, s1_date):
    url = f'https://data.sec.gov/submissions/CIK{str(cik).zfill(10)}.json'
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            return True
        data = r.json()
        filings = data.get('filings', {}).get('recent', {})
        forms = filings.get('form', [])
        dates = filings.get('filingDate', [])
        for form, date in zip(forms, dates):
            if form in ['10-K', '10-Q', '20-F'] and date < s1_date:
                return False
        return True
    except:
        return True

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

def get_stage(cik):
    url = f'https://data.sec.gov/submissions/CIK{str(cik).zfill(10)}.json'
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            return 'PIPELINE', 0
        data = r.json()
        filings = data.get('filings', {}).get('recent', {})
        forms = filings.get('form', [])
        dates = filings.get('filingDate', [])

        # Check if priced in last 7 days
        for form, date in zip(forms, dates):
            if form == '424B4':
                days_since = (datetime.now() - datetime.strptime(date, '%Y-%m-%d')).days
                if days_since <= 7:
                    return 'LIVE', 0

        # Count amendments
        amendments = sum(1 for f in forms if f in ['S-1/A', 'F-1/A'])

        if amendments >= 5:
            return 'IMMINENT', amendments
        return 'PIPELINE', amendments

    except Exception as e:
        print(f"Stage check error: {e}")
        return 'PIPELINE', 0

def parse_red_flags(text):
    if not text:
        return []
    flags = []
    seen_flags = set()
    checks = [
        ('convertible note', 'Convertible notes'),
        ('convertible promissory', 'Convertible notes'),
        ('pre-funded warrant', 'Pre-funded warrants'),
        ('prefunded warrant', 'Pre-funded warrants'),
        ('variable conversion', 'Variable conversion'),
        ('floating conversion', 'Variable conversion'),
        ('maxim group', 'Maxim Group'),
        ('thinkequity', 'ThinkEquity'),
        ('univest', 'Univest'),
        ('boustead', 'Boustead'),
        ('aegis capital', 'Aegis Capital'),
        ('variable interest entity', 'China VIE'),
        (' vie ', 'China VIE'),
    ]
    for keyword, label in checks:
        if keyword in text and label not in seen_flags:
            flags.append(label)
            seen_flags.add(label)
    return flags

def send_discord(company, form_type, ticker, stage, amendments, exchange, flags, filing_url):
    if stage == 'LIVE':
        stage_text = '🔥 JUST PRICED'
    elif stage == 'IMMINENT':
        stage_text = f'⚠️ PRICING SOON ({amendments} amends)'
    else:
        stage_text = f'📊 PIPELINE ({amendments} amends)'

    ticker_text = f'**{ticker}**' if ticker else '**TBD**'
    flag_text = ', '.join(flags) if flags else '✓ Clean'

    msg = '🚨 **Transhare/VStock IPO**\n\n'
    msg += f'{ticker_text} • {company}\n'
    msg += f'{stage_text} • {exchange}\n'
    msg += f'Form: `{form_type}`\n'
    if flags:
        msg += f'🚩 {flag_text}\n'
    else:
        msg += f'{flag_text}\n'
    msg += f'\n[View Filing]({filing_url})'

    if len(msg) > 1950:
        msg = msg[:1950] + '...'

    try:
        r = requests.post(DISCORD_WEBHOOK, json={'content': msg}, timeout=10)
        if r.status_code == 204:
            print(f'✓ Discord sent: {company} | {stage}')
        else:
            print(f'✗ Discord error {r.status_code}: {r.text}')
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

                print(f'  Checking: {company}')
                filing_text = get_filing_text(cik, accession)
                time.sleep(0.3)

                # Verify transfer agent in actual filing
                if not any(x in filing_text for x in ['transhare', 'vstock', 'v-stock']):
                    print(f'  ✗ Transfer agent not confirmed: {company}')
                    continue

                # Exchange filter
                exchange = get_exchange(filing_text)
                if not exchange:
                    print(f'  ✗ Not NASDAQ/NYSE: {company}')
                    continue

                # IPO verification for initial filings only
                if form_type in ['S-1', 'F-1']:
                    if not is_actual_ipo(cik, filing_date):
                        print(f'  ✗ Already public: {company}')
                        continue
                    time.sleep(0.2)

                ticker = get_ticker(cik)
                time.sleep(0.2)

                stage, amendments = get_stage(cik)
                time.sleep(0.2)

                flags = parse_red_flags(filing_text)

                send_discord(
                    company=company,
                    form_type=form_type,
                    ticker=ticker,
                    stage=stage,
                    amendments=amendments,
                    exchange=exchange,
                    flags=flags,
                    filing_url=filing_url
                )

                new_count += 1

    print(f'Done. {new_count} new filings sent to Discord.')
    save_seen(seen)

if __name__ == '__main__':
    main()

