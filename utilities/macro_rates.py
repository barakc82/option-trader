import yfinance as yf


def fetch_macro_rates():
    # 1. Fetch Risk-Free Rate (13-Week Treasury Bill)
    # yfinance returns IRX as a raw percentage (e.g., 5.25 for 5.25%)
    irx = yf.Ticker("^IRX")
    risk_free_rate_pct = irx.info.get('regularMarketPrice')  # Current yield

    # Convert to decimal for math (e.g., 0.0525)
    r = risk_free_rate_pct / 100.0 if risk_free_rate_pct else None

    # 2. Fetch Dividend Yield (Using SPY as a proxy for the S&P 500)
    spy = yf.Ticker("SPY")
    # yfinance returns this as a decimal (e.g., 0.015 for 1.5%)
    q = spy.info.get('dividendYield')

    print(f"Risk-Free Rate (r): {r * 100:.2f}%" if r else "r: Not found")
    print(f"Dividend Yield (q): {q:.2f}%" if q else "q: Not found")

    return r, q


fetch_macro_rates()