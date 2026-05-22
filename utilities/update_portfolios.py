import requests
import xml.etree.ElementTree as ET
import re
from collections import defaultdict
import time
from utilities.database_access import get_worksheet

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
HEADERS = {"User-Agent": "your-name your-email@example.com"}

def get_latest_13f_accession(cik: str) -> str:
    url = f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json"
    resp = requests.get(url, headers=HEADERS)
    resp.raise_for_status()
    data = resp.json()
    filings = data["filings"]["recent"]
    for i, form in enumerate(filings["form"]):
        if form == "13F-HR":
            return filings["accessionNumber"][i]
    raise ValueError("No 13F-HR filing found")


def get_infotable_url(accession: str, cik: str) -> str:
    """Scrape the filing index page to find the raw infotable XML filename."""
    acc_nodash = accession.replace("-", "")
    cik_plain = str(int(cik))

    index_url = (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{cik_plain}/{acc_nodash}/{accession}-index.htm"
    )
    print(f"Index page: {index_url}")

    # Assuming HEADERS is defined globally in your script
    resp = requests.get(index_url, headers=HEADERS)
    resp.raise_for_status()

    # Find all hrefs ending in .xml in the page
    xml_links = re.findall(r'href="([^"]+\.xml)"', resp.text, re.IGNORECASE)
    print(f"XML files found in index: {xml_links}")

    selected_link = None

    # Prefer the one that mentions 'infotable' AND is NOT an XSL styled version
    for link in xml_links:
        name = link.lower()
        if ("infotable" in name or "information" in name) and "xsl" not in name:
            selected_link = link
            break

    # Fallback logic if the ideal match isn't found
    if not selected_link:
        # Get all clean links that don't have XSL formatting
        clean_links = [link for link in xml_links if "xsl" not in link.lower()]

        if clean_links:
            selected_link = clean_links[-1]  # Fall back to the last clean XML file
        elif xml_links:
            selected_link = xml_links[-1]  # Absolute fallback

    if selected_link.startswith("/"):
        return f"https://www.sec.gov{selected_link}"

    # Relative URL
    return (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{cik_plain}/{acc_nodash}/{selected_link.split('/')[-1]}"
    )


def fetch_holdings(infotable_url: str) -> list[dict]:
    resp = requests.get(infotable_url, headers=HEADERS)
    resp.raise_for_status()

    print(resp.content)
    root = ET.fromstring(resp.content)
    ns = {"ns": "http://www.sec.gov/edgar/document/thirteenf/informationtable"}

    holdings = []
    for info in root.findall("ns:infoTable", namespaces=ns):
        holdings.append({
            "name":     info.findtext("ns:nameOfIssuer", namespaces=ns),
            "cusip":    info.findtext("ns:cusip", namespaces=ns),
            "value":    int(info.findtext("ns:value", namespaces=ns) or 0),  # plain dollars
            "shares":   int(info.findtext("ns:shrsOrPrnAmt/ns:sshPrnamt", namespaces=ns) or 0),
            "put_call": info.findtext("ns:putCall", namespaces=ns),
        })
    return holdings


def aggregate_holdings(holdings: list[dict]) -> list[dict]:
    """Aggregate multiple rows per company into a single position."""
    aggregated = defaultdict(lambda: {"shares": 0, "value": 0})

    for h in holdings:
        name = h["name"]
        aggregated[name]["shares"] += h["shares"]
        aggregated[name]["value"] += h["value"]
        aggregated[name]["cusip"] = h["cusip"]

    return sorted(
        [{"name": name, **vals} for name, vals in aggregated.items()],
        key=lambda h: h["value"],
        reverse=True
    )


def cusips_to_tickers(cusips: list[str]) -> dict[str, str]:
    """
    Map a list of CUSIPs to tickers using the OpenFIGI API.
    Returns a dict of {cusip: ticker}.
    Batches requests in groups of 100 (API limit).
    """
    result = {}
    batch_size = 10

    for i in range(0, len(cusips), batch_size):
        batch = cusips[i:i + batch_size]
        payload = [
            {"idType": "ID_CUSIP", "idValue": cusip, "exchCode": "US"}
            for cusip in batch
        ]
        resp = requests.post(
            OPENFIGI_URL,
            json=payload,
            headers={"Content-Type": "application/json"}
        )
        resp.raise_for_status()

        for cusip, item in zip(batch, resp.json()):
            if "data" in item and item["data"]:
                # Pick the first common stock result, or just first result
                for entry in item["data"]:
                    if entry.get("securityType") == "Common Stock":
                        result[cusip] = entry["ticker"]
                        break
                else:
                    result[cusip] = item["data"][0]["ticker"]
            else:
                result[cusip] = None  # not found

        # OpenFIGI rate limit: 25 requests/min without API key
        if i + batch_size < len(cusips):
            time.sleep(2.5)

    return result


def enrich_holdings(aggregated: list[dict], cusip_to_ticker: dict[str, str]):
    """Add ticker and weight information to aggregated holdings."""
    total = sum(h["value"] for h in aggregated)
    for h in aggregated:
        h["ticker"] = cusip_to_ticker.get(h.get("cusip"), "N/A")
        h["weight"] = h["value"] / total if total > 0 else 0


def get_holdings_for_cik(cik: str) -> list[dict]:
    """Fetch, aggregate, and enrich holdings for a given CIK."""
    accession = get_latest_13f_accession(cik)
    infotable_url = get_infotable_url(accession, cik)
    holdings = fetch_holdings(infotable_url)
    aggregated = aggregate_holdings(holdings)

    # Collect unique CUSIPs and resolve to tickers
    unique_cusips = list({h["cusip"] for h in holdings if h["cusip"]})
    print(f"Resolving {len(unique_cusips)} CUSIPs to tickers...")
    cusip_to_ticker = cusips_to_tickers(unique_cusips)

    enrich_holdings(aggregated, cusip_to_ticker)
    return aggregated


def sync_holdings_to_sheet(aggregated: list[dict], sheet_name: str, ticker_range: str, weight_range: str, total):
    """Sync weights from aggregated holdings to a specific Google Sheet range."""
    print(f"\nSyncing weights with Google Sheet '{sheet_name}'...")
    try:
        sheet = get_worksheet(sheet_name)
        tickers_in_sheet = sheet.get(ticker_range)  # returns list of lists

        # Flatten and clean tickers
        sheet_tickers = [row[0].strip() if row else "" for row in tickers_in_sheet]

        # Create a lookup map for faster access
        weight_map = {h["ticker"]: h["weight"] for h in aggregated if h["ticker"]}

        # Prepare weights for column H
        weights_to_write = [[total]]
        for ticker in sheet_tickers:
            if ticker in weight_map:
                weights_to_write.append([weight_map[ticker]])
            else:
                weights_to_write.append([""])  # clear if not found or empty ticker

        sheet.update(range_name=weight_range, values=weights_to_write)
        print("Google Sheet updated successfully.")
    except Exception as e:
        print(f"Error syncing with Google Sheet: {e}")


def update_portfolio_for_cik(cik: str, column_letter: str):
    """Orchestrate the full workflow: fetch, print summary, and sync to sheet."""
    aggregated = get_holdings_for_cik(cik)

    print(f"\n{'Ticker':<8} {'Company':<35} {'Shares':>15} {'Value ($M)':>12} {'Weight':>8}")
    print("-" * 85)
    for h in aggregated:
        value_m = h["value"] / 1_000_000
        ticker  = h["ticker"] or "N/A"
        print(f"{ticker:<8} {h['name']:<35} {h['shares']:>15,} {value_m:>11,.1f} {h['weight']:>7.2f}%")

    total = sum(h["value"] for h in aggregated)
    print(f"\nTotal positions: {len(aggregated)}")
    print(f"Total portfolio value: ${total/1_000_000_000:.1f}B")

    # Sync with Google Sheet 'Carlson' using fixed ranges
    ticker_range = "C16:C46"
    weight_range = f"{column_letter}15:{column_letter}46"
    sync_holdings_to_sheet(aggregated, "Carlson", ticker_range, weight_range, round(total/1_000_000_000))


def main():
    update_portfolio_for_cik(cik="0001067983", column_letter="H") # buffett
    update_portfolio_for_cik(cik="0001569205", column_letter="I") # smith
    update_portfolio_for_cik(cik="0001647251", column_letter="J") # hohn
    update_portfolio_for_cik(cik="0001112520", column_letter="K")  # akre
    update_portfolio_for_cik(cik="0001697868", column_letter="L")  # akre

    sheet = get_worksheet('Carlson')
    sheet.format("H16:L46", {
        "numberFormat": {
            "type": "PERCENT",
            "pattern": "0.00%"
        }
    })

if __name__ == "__main__":
    main()