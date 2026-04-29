import requests
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

DISCORD_WEBHOOK = os.environ['DISCORD_WEBHOOK_URL']
HEADERS = {'User-Agent': 'TranshareMonitor mathewcoatney@gmail.com'}
SEEN_FILE = Path('seen_filings.json')

TRANSFER_AGENTS = ['transhare', 'vstock', 'v-stock']
EXCHANGES = ['nasdaq', 'nyse']
FORM_TYPES = ['S-1', 'F-1', 'S-1/A', 'F-1/A', '424B4']

def load_seen():
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()

def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(list(seen)[-2000:]))

def search_sec(query, form_type):
    """Search SEC full-text search for filings containing query"""
    start_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
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
    except Exception as e:
        print(f"SEC search error: {e}")
    return []

def get_filing_text(cik, accession):
    """Fetch actual filing document text"""
    accession_clean = accession.replace('-', '')
    index_url = f'https://www.sec.gov/Archives/edgar/data/{cik}/{accession_clean}/{accession}-index.htm'
    
    try:
        # Get index page to find main document
        r = requests.get(index_url, headers=HEADERS, timeout=30)
        
        # Find main document link
        lines = r.text.split('\n')
        doc_url = None
        
        for line in lines:
            if '.htm' in line.lower() and any(f in line.lower() for f in ['s-1', 'f-1', '424b4', 'prospectus']):
                # Extract href
                start = line.lower().find('href="') + 6
                end = line.find('"', start)
                if start > 5 and end > start:
                    path = line[start:end]
                    if not path.startswith('http'):
                        doc_url = f'https://www.sec.gov{path}'
                    else:
                        doc_url = path
                    break
        
        if not doc_url:
            return None
            
        # Fetch main document - limit to first 500KB to avoid huge files
        doc_r = requests.get(doc_url, headers=HEADERS, timeout=60, stream=True)
        content = b''
        for chunk in doc_r.iter_content(chunk_size=8192):
            content += chunk
            if len(content) > 500000:  # 500KB limit
                break
        
        return content.decode('utf-8', errors='ignore').lower()
        
    except Exception as e:
        print(f"Error fetching filing: {e}")
        return None

def get_exchange(filing_text):
    """Extract planned exchange from filing"""
    if not filing_text:
        return None
    if 'nasdaq' in filing_text:
        return 'NASDAQ'
    elif 'nyse american' in filing_text:
        return 'NYSE American'
    elif 'new york stock exchange' in filing_text:
        return 'NYSE'
    return None

def is_actual_ipo(cik, s1_date):
    """Verify company has no prior periodic reports (not already public)"""
    url = f'https://data.sec.gov/submissions/CIK{str(cik).zfill(10)}.json'
    
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            return True  # Assume IPO if we can't verify
            
        data = r.json()
        filings = data.get('filings', {}).get('recent', {})
        forms = filings.get('form', [])
        dates = filings.get('filingDate', [])
        
        for form, date in zip(forms, dates):
            if form in ['10-K', '10-Q', '20-F'] and date < s1_date:
                return False  # Already public
                
        return True
        
    except Exception as e:
        print(f"Error checking IPO status: {e}")
        return True

def get_ticker(cik):
    """Get ticker symbol if available"""
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

def classify_stage(form_type, cik):
    """Determine IPO stage based on filing history"""
    url = f'https://data.sec.gov/submissions/CIK{str(cik).zfill(10)}.json'
    
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            return 'PIPELINE', 0
            
        data = r.json()
        filings = data.get('filings', {}).get('recent', {})
        forms = filings.get('form', [])
        
        # Check if priced
        if '424B4' in forms:
            return 'LIVE', 0
        
        # Count amendments
        amendment_count = sum(1 for f in forms if f in ['S-1/A', 'F-1/A'])
        
        if amendment_count >= 5:
            return 'IMMINENT', amendment_count
        else:
            return 'PIPELINE', amendment_count
            
    except Exception as e:
        print(f"Error classifying stage: {e}")
        return 'PIPELINE', 0

def parse_red_flags(filing_text):
    """Extract red flags from filing text"""
    if not filing_text:
        return []
    
    flags = []
    
    if 'convertible note' in filing_text or 'convertible promissory' in filing_text:
        flags.append('Convertible notes')
    if 'pre-funded warrant' in filing_text or 'prefunded warrant' in filing_text:
        flags.append('Pre-funded warrants')
    if 'variable conversion' in filing_text or 'floating conversion' in filing_text:
        flags.append('Variable conversion')
    if 'maxim group' in filing_text:
        flags.append('Maxim Group')
    if 'thinkequity' in filing_text:
        flags.append('ThinkEquity')
    if 'univest' in filing_text:
        flags.append('Univest')
    if 'boustead' in filing_text:
        flags.append('Boustead')
    if 'aegis capital' in filing_text:
        flags.append('Aegis Capital')
    if 'variable interest entity' in filing_text or ' vie ' in filing_text:
        flags.append('China VIE')
    
    return flags

def send_discord(company, form_type, ticker, stage, amendments, exchange, flags, filing_url):
    """Send formatted Discord alert"""
    
    # Stage emoji and label
    if stage == 'LIVE':
        stage_text = '🔥 **JUST PRICED**'
    elif stage == 'IMMINENT':
        stage_text = f'⚠️ **PRICING SOON** ({amendments} amends)'
    else:
        stage_text = f'📊 **PIPELINE** ({amendments} amends)'
    
    # Build message
    ticker_text = f'**{ticker}**' if ticker else '**TBD**'
    flag_text = ', '.join(flags) if flags else '✓ Clean'
    
    msg = f"🚨 **New Transhare/VStock Filing**\n\n"
    msg += f"{ticker_text} • {company}\n"
    msg += f"{stage_text} • {exchange}\n"
    msg += f"Form: `{form_type}`\n"
    
    if flags:
        msg += f"🚩 {flag_text}\n"
    else:
        msg += f"{flag_text}\n"
    
    msg += f"\n[View Filing]({filing_url})"
    
    # Discord 2000 char limit
    if len(msg) > 1950:
        msg = msg[:1950] + '...'
    
    try:
        r = requests.post(
            DISCORD_WEBHOOK,
            json={'content': msg},
            timeout=10
        )
        if r.status_code == 204:
            print(f"✓ Discord alert sent: {company}")
        else:
            print(f"✗ Discord error {r.status_code}: {company}")
    except Exception as e:
        print(f"✗ Discord send failed: {e}")

def main():
    print(f"Running at {datetime.now()}")
    seen = load_seen()
    new_count = 0
    
    for agent in ['Transhare Corporation', 'VStock Transfer']:
        for form_type in FORM_TYPES:
            print(f"Searching: {agent} | {form_type}")
            hits = search_sec(agent, form_type)
            
            for hit in hits:
                source = hit.get('_source', {})
                
                # Get filing details
                accession = source.get('accession_no', '')
                cik = source.get('cik', '')
                filing_date = source.get('file_date', '')
                company = source.get('display_names', ['Unknown'])[0]
                filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{accession}-index.htm"
                
                # Skip if already seen
                if accession in seen:
                    continue
                
                seen.add(accession)
                
                # Get filing text for verification
                filing_text = get_filing_text(cik, accession)
                
                # Verify exchange (NASDAQ/NYSE only)
                exchange = get_exchange(filing_text)
                if not exchange:
                    print(f"  ✗ Skipping (not NASDAQ/NYSE): {company}")
                    continue
                
                # Verify actual IPO (not secondary offering)
                # Only check for initial S-1/F-1, not amendments or 424B4
                if form_type in ['S-1', 'F-1']:
                    if not is_actual_ipo(cik, filing_date):
                        print(f"  ✗ Skipping (already public): {company}")
                        continue
                
                # Get ticker if exists
                ticker = get_ticker(cik)
                
                # Classify stage
                stage, amendments = classify_stage(form_type, cik)
                
                # Parse red flags
                flags = parse_red_flags(filing_text)
                
                # Send Discord alert
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
                
                # Rate limit between filings
                import time
                time.sleep(0.5)
    
    print(f"Done. {new_count} new filings found.")
    save_seen(seen)

if __name__ == '__main__':
    main()
