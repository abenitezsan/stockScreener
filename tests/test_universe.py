from app.analysis.sectors import region_for, sector_group
from app.universe import (
    _wiki_symbols,
    exchange_suffix,
    parse_ishares_holdings,
    us_symbol,
    with_suffix,
)


def test_ticker_translation():
    assert us_symbol("BRK.B") == "BRK-B"
    assert with_suffix("ACS", ".MC") == "ACS.MC"
    assert with_suffix("NOVO B", ".CO") == "NOVO-B.CO"
    assert with_suffix("RR.", ".L") == "RR.L"
    assert with_suffix("BT.A", ".L") == "BT-A.L"
    assert with_suffix("CTC.A", ".TO") == "CTC-A.TO"
    assert with_suffix("ry.to", ".TO") == "RY.TO"


def test_exchange_suffix():
    assert exchange_suffix("Bolsa De Madrid") == ".MC"
    assert exchange_suffix("Nyse Euronext - Euronext Paris") == ".PA"
    assert exchange_suffix("Omx Nordic Exchange Copenhagen A/S") == ".CO"
    assert exchange_suffix("Nasdaq Omx Helsinki Ltd.") == ".HE"
    assert exchange_suffix("Nasdaq Omx Nordic") == ".ST"
    assert exchange_suffix("Warsaw Stock Exchange/Equities/Main Market") is None


def test_parse_ishares_holdings():
    text = """iShares STOXX Europe 600 UCITS ETF (DE)
Fund Holdings as of,"Sep 25, 2026"

Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Exchange,Currency
ASML,ASML HOLDING NV,Information Technology,Equity,"1,000",2.1,Euronext Amsterdam,EUR
NOVO B,NOVO NORDISK A/S,Health Care,Equity,"900",1.9,Omx Nordic Exchange Copenhagen A/S,DKK
SHEL,SHELL PLC,Energy,Equity,"800",1.5,London Stock Exchange,GBP
PKO,PKO BANK POLSKI SA,Financials,Equity,"10",0.1,Warsaw Stock Exchange,PLN
EUR,EUR CASH,Cash,Cash,"5",0.1,-,EUR
"""
    assert parse_ishares_holdings(text) == ["ASML.AS", "NOVO-B.CO", "SHEL.L"]


def test_wiki_table_parsing():
    rows = "".join(f"<tr><td>T{i}.B</td><td>Company {i}</td></tr>" for i in range(12))
    html = f"<table><tr><th>Symbol</th><th>Security</th></tr>{rows}</table>"
    symbols = _wiki_symbols(html, ("Symbol",), us_symbol)
    assert symbols[0] == "T0-B" and len(symbols) == 12


def test_region_and_sector_group():
    assert region_for("AAPL") == "US"
    assert region_for("ENB.TO") == "CA"
    assert region_for("SAN.MC") == "EU"
    assert sector_group("Utilities", "Utilities - Regulated Electric") == "utilities"
    assert sector_group("Communication Services", "Telecom Services") == "utilities"
    assert sector_group("Real Estate", "REIT - Retail") == "reit"
    assert sector_group("Real Estate", "Real Estate Services") == "general"
    assert sector_group("Financial Services", "Banks - Diversified") == "financials"
    assert sector_group("Energy", "Oil & Gas Integrated") == "cyclical"
