"""Tunable settings. City modules live in cities.py, capital rules in allocator.py."""

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
AWC_METAR = "https://aviationweather.gov/api/data/metar"
HKO_MAXMIN = ("https://data.weather.gov.hk/weatherAPI/hko_data/regional-weather/"
              "latest_since_midnight_maxmin.csv")

# Detection
MAX_NO_PRICE = 0.99        # only flag asks at or below this price
MARGIN = 1                 # observed max must exceed bucket top by >= MARGIN (source units)
SPIKE_GUARD = 4.0          # °C jump vs previous report that counts as suspicious
DEFAULT_FEE_RATE = 0.05    # weather taker fee: fee = shares * rate * p * (1 - p)
