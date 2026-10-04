import json
import urllib.parse
import urllib.request

UA = {"User-Agent": "weather-bot-scanner/0.1 (read-only)"}


def get(url, params=None, as_json=True, timeout=15):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8")
    return json.loads(body) if as_json else body
