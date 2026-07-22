"""
sectors.py — a curated NSE symbol → sector map (dependency-free, offline).

WHY THIS EXISTS
---------------
The bhavcopy we ingest (`db.eod_bars`) has prices for the whole market but NO
sector/industry field, and NSE's sector-index constituent CSVs are just more
Akamai-gated downloads. Sector rotation ("money is flowing INTO IT and OUT of
FMCG") is one of the most reliable swing signals, so `sector_scan.py` needs to
know which names belong to which sector. This module ships that mapping as
static data — no network, works off-hours, and any symbol we don't recognise is
simply left unclassified (robust by design).

SCOPE
-----
Curated, not exhaustive: the F&O universe + the most liquid cash names, grouped
into the ~16 sectors traders actually rotate between. Symbols are NSE trading
symbols (as they appear in the bhavcopy). Over-inclusion is harmless — a symbol
not present in `eod_bars` is skipped by the scanner; a misspelling just goes
unclassified. Keep it current when tickers get renamed (e.g. L&TFH → LTF).
"""

# sector name → constituent NSE symbols. Order within a sector is cosmetic.
SECTORS = {
    "Banks": [
        "HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK", "INDUSINDBK",
        "BANKBARODA", "PNB", "CANBK", "IDFCFIRSTB", "FEDERALBNK", "AUBANK",
        "BANDHANBNK", "RBLBANK", "INDIANB", "UNIONBANK", "YESBANK", "BANKINDIA",
        "MAHABANK", "CENTRALBK", "UCOBANK", "IOB", "KARURVYSYA", "CITYUNIONBANK",
    ],
    "Financial Services": [
        "BAJFINANCE", "BAJAJFINSV", "JIOFIN", "CHOLAFIN", "SHRIRAMFIN", "SBICARD",
        "MUTHOOTFIN", "LICHSGFIN", "PFC", "RECLTD", "IRFC", "M&MFIN", "ABCAPITAL",
        "POONAWALLA", "MANAPPURAM", "LTF", "HDFCAMC", "PEL", "IEX", "BSE", "CDSL",
        "ANGELONE", "360ONE", "CAMS", "MCX", "PAYTM", "POLICYBZR", "IREDA",
        "HDFCLIFE", "SBILIFE", "ICICIPRULI", "ICICIGI", "LICI", "MAXFIN",
    ],
    "IT": [
        "TCS", "INFY", "HCLTECH", "WIPRO", "TECHM", "LTIM", "PERSISTENT",
        "COFORGE", "MPHASIS", "LTTS", "OFSS", "TATAELXSI", "BSOFT", "KPITTECH",
        "CYIENT", "ZENSARTECH", "SONATSOFTW", "MASTEK", "INTELLECT", "NEWGEN",
    ],
    "Auto": [
        "MARUTI", "M&M", "TATAMOTORS", "BAJAJ-AUTO", "EICHERMOT", "HEROMOTOCO",
        "TVSMOTOR", "ASHOKLEY", "BHARATFORG", "MOTHERSON", "BOSCHLTD", "BALKRISIND",
        "MRF", "APOLLOTYRE", "EXIDEIND", "TIINDIA", "SONACOMS", "UNOMINDA",
        "CEATLTD", "ENDURANCE", "SCHAEFFLER", "ZFCVINDIA",
    ],
    "Pharma": [
        "SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB", "LUPIN", "AUROPHARMA",
        "ZYDUSLIFE", "ALKEM", "TORNTPHARM", "BIOCON", "GLENMARK", "LAURUSLABS",
        "IPCALAB", "ABBOTINDIA", "MANKIND", "GRANULES", "AJANTPHARM", "NATCOPHARM",
        "GLAND", "PPLPHARMA", "SYNGENE",
    ],
    "Healthcare": [
        "APOLLOHOSP", "MAXHEALTH", "FORTIS", "LALPATHLAB", "METROPOLIS",
        "NH", "GLOBALHLTH", "ASTERDM", "MEDANTA", "KIMS", "RAINBOW",
    ],
    "FMCG": [
        "HINDUNILVR", "ITC", "NESTLEIND", "BRITANNIA", "DABUR", "MARICO",
        "GODREJCP", "COLPAL", "TATACONSUM", "VBL", "UBL", "RADICO", "EMAMILTD",
        "PGHH", "BALRAMCHIN", "PATANJALI", "MCDOWELL-N", "JUBLFOOD", "DEVYANI",
    ],
    "Metals": [
        "TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "JINDALSTEL", "SAIL",
        "NMDC", "NATIONALUM", "HINDZINC", "APLAPOLLO", "JSL", "RATNAMANI",
        "HINDCOPPER", "WELCORP", "JINDALSAW", "LLOYDSME",
    ],
    "Energy": [
        "RELIANCE", "ONGC", "NTPC", "POWERGRID", "COALINDIA", "IOC", "BPCL",
        "GAIL", "HINDPETRO", "TATAPOWER", "ADANIPOWER", "ADANIENSOL", "NHPC",
        "OIL", "PETRONET", "TORNTPOWER", "JSWENERGY", "SJVN", "NLCINDIA", "IGL",
        "GUJGASLTD", "MGL", "ATGL", "ADANIGREEN", "CASTROLIND",
    ],
    "Cement": [
        "ULTRACEMCO", "GRASIM", "SHREECEM", "AMBUJACEM", "ACC", "DALBHARAT",
        "JKCEMENT", "RAMCOCEM", "INDIACEM", "JKLAKSHMI", "NUVOCO", "BIRLACORPN",
    ],
    "Capital Goods": [
        "LT", "SIEMENS", "ABB", "BHEL", "BEL", "HAL", "CUMMINSIND", "THERMAX",
        "POLYCAB", "HAVELLS", "KEI", "BDL", "MAZDOCK", "COCHINSHIP", "GRINDWELL",
        "HONAUT", "CGPOWER", "KAYNES", "SUPREMEIND", "SKFINDIA", "TIMKEN",
        "AIAENG", "KIRLOSENG", "GMRAIRPORT",
    ],
    "Realty": [
        "DLF", "GODREJPROP", "OBEROIRLTY", "PRESTIGE", "LODHA", "PHOENIXLTD",
        "BRIGADE", "SOBHA", "ANANTRAJ", "MAHLIFE", "RAYMOND", "SUNTECK",
    ],
    "Telecom": [
        "BHARTIARTL", "IDEA", "INDUSTOWER", "TATACOMM", "HFCL", "TEJASNET",
        "ITI", "ROUTE", "STLTECH",
    ],
    "Consumer Durables": [
        "TITAN", "DMART", "TRENT", "VOLTAS", "DIXON", "CROMPTON", "WHIRLPOOL",
        "BLUESTARCO", "KALYANKJIL", "PAGEIND", "BATAINDIA", "RELAXO", "VGUARD",
        "AMBER", "KAJARIACER", "CERA", "ORIENTELEC", "TTKPRESTIG",
    ],
    "Chemicals": [
        "PIDILITIND", "SRF", "DEEPAKNTR", "AARTIIND", "TATACHEM", "PIIND",
        "NAVINFLUOR", "ATUL", "VINATIORGA", "FLUOROCHEM", "COROMANDEL", "UPL",
        "SUMICHEM", "LINDEINDIA", "GNFC", "CHAMBLFERT", "SOLARINDS", "EIDPARRY",
    ],
    "Paints": [
        "ASIANPAINT", "BERGEPAINT", "KANSAINER", "INDIGOPNTS", "AKZOINDIA",
    ],
    "Media": [
        "ZEEL", "PVRINOX", "SUNTV", "NAZARA", "SAREGAMA", "TV18BRDCST",
        "NETWORK18", "DISHTV",
    ],
    "Infrastructure": [
        "ADANIPORTS", "ADANIENT", "IRB", "GMRINFRA", "NBCC", "RVNL", "IRCON",
        "NCC", "KEC", "KALPATPOWR", "HUDCO", "CONCOR", "IRCTC", "RITES",
        "GESHIP", "GPPL",
    ],
}


def _canon(sym):
    return (sym or "").upper().strip()


# reverse index {SYMBOL: sector} — first sector wins if a symbol is listed twice.
SYMBOL_SECTOR = {}
for _sec, _syms in SECTORS.items():
    for _s in _syms:
        SYMBOL_SECTOR.setdefault(_canon(_s), _sec)


def all_sectors():
    """Sector names in a stable (sorted) order."""
    return sorted(SECTORS.keys())


def sector_of(symbol):
    """The sector for an NSE symbol, or None if unclassified."""
    return SYMBOL_SECTOR.get(_canon(symbol))


def symbols(sector):
    """Constituent symbols for a sector (empty list if unknown)."""
    return list(SECTORS.get(sector, ()))


def coverage():
    """{sectors, symbols} — how many names the map classifies (for status/tests)."""
    return {"sectors": len(SECTORS), "symbols": len(SYMBOL_SECTOR)}
