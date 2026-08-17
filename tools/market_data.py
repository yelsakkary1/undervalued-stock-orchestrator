"""
Market data tools for the fundamentals and technical agents.
Wraps yfinance so each function can be exposed as a Claude tool.

Note: yfinance's `.info` dict doesn't expose everything a real fundamentals
agent would want (credit rating, going-concern language, institutional
ownership %). Those are left out or set to None here rather than faked --
some get properly addressed in Project 4 (filing extraction), others may
need a different data source down the line.
"""

import pandas as pd
import yfinance as yf


def get_valuation_snapshot(ticker: str) -> dict:
    """Core valuation ratios: P/E, P/B, EV/EBITDA, PEG, FCF yield."""
    info = yf.Ticker(ticker).info
    return {
        "pe_ratio": info.get("trailingPE"),
        "forward_pe": info.get("forwardPE"),
        "pb_ratio": info.get("priceToBook"),
        "ev_to_ebitda": info.get("enterpriseToEbitda"),
        "peg_ratio": info.get("pegRatio"),
        "fcf_yield": _compute_fcf_yield(ticker, info),
    }


def _compute_fcf_yield(ticker: str, info: dict):
    """FCF yield = (operating cash flow - capex) / market cap."""
    try:
        cashflow = yf.Ticker(ticker).cashflow
        market_cap = info.get("marketCap")
        if cashflow is None or cashflow.empty or not market_cap:
            return None
        ocf = cashflow.loc["Operating Cash Flow"].iloc[0]
        capex = cashflow.loc["Capital Expenditure"].iloc[0]  # usually negative
        return round((ocf + capex) / market_cap, 4)
    except Exception:
        return None


def get_financial_health(ticker: str) -> dict:
    """Debt load and efficiency: debt/equity, current ratio, ROE."""
    info = yf.Ticker(ticker).info
    debt_to_equity = info.get("debtToEquity")
    return {
        "debt_to_equity": round(debt_to_equity / 100, 4) if debt_to_equity is not None else None,
        "current_ratio": info.get("currentRatio"),
        "return_on_equity": info.get("returnOnEquity"),
    }


def get_growth_trend(ticker: str, years: int = 5) -> dict:
    """Multi-year revenue trend, most recent year first.

    Truncates at the first missing year rather than passing NaN through:
    yfinance pads the oldest slots for companies with shorter filing
    history, and a bare NaN is neither valid JSON nor readable to a model
    as "no data". years_available then reflects real years, not slots.
    """
    financials = yf.Ticker(ticker).financials
    if financials is None or financials.empty or "Total Revenue" not in financials.index:
        return {"revenue_trend": None, "years_available": 0}

    revenue = []
    for value in financials.loc["Total Revenue"].iloc[:years]:
        if pd.isna(value):
            break
        revenue.append(float(value))

    return {"revenue_trend": revenue, "years_available": len(revenue)}


def get_ownership_signals(ticker: str) -> dict:
    """Recent insider transactions."""
    try:
        insider = yf.Ticker(ticker).insider_transactions
        recent = insider.head(5).to_dict("records") if insider is not None and not insider.empty else []
    except Exception:
        recent = []
    return {"insider_transactions": recent}


def get_trend_signals(ticker: str) -> dict:
    """50/200-day moving average position and 52-week range."""
    hist = yf.Ticker(ticker).history(period="1y")
    if hist.empty:
        return {}
    close = hist["Close"]
    sma50 = close.rolling(50).mean().iloc[-1] if len(close) >= 50 else None
    sma200 = close.rolling(200).mean().iloc[-1] if len(close) >= 200 else None
    price = close.iloc[-1]
    high, low = close.max(), close.min()
    position = (price - low) / (high - low) if high != low else None
    trend = "uptrend" if sma50 and sma200 and sma50 > sma200 else "downtrend" if sma50 and sma200 else "unknown"
    return {
        "current_price": round(price, 2),
        "sma_50": round(sma50, 2) if sma50 else None,
        "sma_200": round(sma200, 2) if sma200 else None,
        "trend": trend,
        "position_in_52wk_range": round(position, 2) if position is not None else None,
    }


def get_momentum_indicators(ticker: str) -> dict:
    """RSI, beta, and recent average volume."""
    hist = yf.Ticker(ticker).history(period="6mo")
    if hist.empty:
        return {}
    delta = hist["Close"].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = -delta.where(delta < 0, 0).rolling(14).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    last_rsi = rsi.iloc[-1] if not rsi.empty else None
    vol_mean = hist["Volume"].tail(10).mean()
    return {
        "rsi_14": round(last_rsi, 2) if pd.notna(last_rsi) else None,
        "beta": yf.Ticker(ticker).info.get("beta"),
        "avg_volume_10d": int(vol_mean) if pd.notna(vol_mean) else None,
    }


def get_relative_strength(ticker: str, benchmark: str = "SPY") -> dict:
    """6-month return vs. a benchmark (default S&P 500 ETF)."""
    t_hist = yf.Ticker(ticker).history(period="6mo")["Close"]
    b_hist = yf.Ticker(benchmark).history(period="6mo")["Close"]
    if t_hist.empty or b_hist.empty:
        return {}
    t_return = (t_hist.iloc[-1] / t_hist.iloc[0]) - 1
    b_return = (b_hist.iloc[-1] / b_hist.iloc[0]) - 1
    return {
        "ticker_6mo_return": round(t_return, 4),
        "benchmark_6mo_return": round(b_return, 4),
        "relative_strength": round(t_return - b_return, 4),
    }


def get_sector_profile(ticker: str) -> dict:
    """Sector, industry, size and beta -- the inputs to a concentration check.

    Only the portfolio layer has any use for this. The single-ticker agents
    deliberately don't get it: whether a name is a good buy is independent
    of what else you already own, and mixing the two questions is how you
    end up with five semiconductor positions that each looked fine alone.
    """
    info = yf.Ticker(ticker).info
    return {
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "market_cap": info.get("marketCap"),
        "beta": info.get("beta"),
    }


def get_risk_flags(ticker: str) -> dict:
    """Short interest as a percent of float (other risk fields TBD -- see module docstring)."""
    info = yf.Ticker(ticker).info
    return {
        "short_percent_of_float": info.get("shortPercentOfFloat"),
    }