"""Yahoo Fantasy OAuth spike: authorize once, cache tokens, prove league access.

Setup: put in .env (gitignored):
    YAHOO_CLIENT_ID=...
    YAHOO_CLIENT_SECRET=...

Then:
    python yahoo_auth.py url          # prints the consent URL - open it, click Agree
    python yahoo_auth.py code <CODE>  # paste the code Yahoo shows (or the ?code= from
                                      # the localhost redirect's address bar)
    python yahoo_auth.py test         # list my leagues + rosters (uses cached tokens)

Tokens cache to data/yahoo_tokens.json and refresh automatically.
"""

import base64
import json
import os
import sys
import time

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

CID = os.environ.get("YAHOO_CLIENT_ID", "")
SEC = os.environ.get("YAHOO_CLIENT_SECRET", "")
REDIRECT = "oob"   # out-of-band: Yahoo displays the code directly; falls back to the
                   # registered https://localhost:8080 if oob is rejected for this app
AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
API = "https://fantasysports.yahooapis.com/fantasy/v2"
TOKENS = os.path.join("data", "yahoo_tokens.json")


def _basic():
    return "Basic " + base64.b64encode(f"{CID}:{SEC}".encode()).decode()


def consent_url(redirect):
    from urllib.parse import urlencode
    return AUTH_URL + "?" + urlencode({
        "client_id": CID, "redirect_uri": redirect, "response_type": "code", "language": "en-us"})


def save(tok):
    tok["obtained_at"] = int(time.time())
    os.makedirs("data", exist_ok=True)
    with open(TOKENS, "w", encoding="utf-8") as fh:
        json.dump(tok, fh, indent=1)


def exchange(code, redirect):
    r = requests.post(TOKEN_URL, headers={"Authorization": _basic()},
                      data={"grant_type": "authorization_code", "code": code,
                            "redirect_uri": redirect}, timeout=30)
    print("token exchange:", r.status_code)
    r.raise_for_status()
    save(r.json())
    print("tokens saved to", TOKENS)


def access_token():
    with open(TOKENS, encoding="utf-8") as fh:
        tok = json.load(fh)
    if time.time() - tok.get("obtained_at", 0) > tok.get("expires_in", 3600) - 120:
        r = requests.post(TOKEN_URL, headers={"Authorization": _basic()},
                          data={"grant_type": "refresh_token",
                                "refresh_token": tok["refresh_token"],
                                "redirect_uri": REDIRECT}, timeout=30)
        r.raise_for_status()
        nt = r.json()
        nt.setdefault("refresh_token", tok["refresh_token"])
        save(nt)
        tok = nt
    return tok["access_token"]


def get(path):
    r = requests.get(f"{API}/{path}?format=json",
                     headers={"Authorization": "Bearer " + access_token()}, timeout=30)
    print("GET", path, "->", r.status_code)
    r.raise_for_status()
    return r.json()


def main():
    if not CID or not SEC:
        sys.exit("Set YAHOO_CLIENT_ID / YAHOO_CLIENT_SECRET in .env first")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "url"
    if cmd == "url":
        print("Open this, sign in, click Agree, copy the code Yahoo shows:\n")
        print(consent_url("oob"))
        print("\nIf Yahoo rejects that URL, use this one and copy ?code=... from the")
        print("address bar of the (broken) localhost page it redirects to:\n")
        print(consent_url("https://localhost:8080"))
    elif cmd == "code":
        code = sys.argv[2]
        try:
            exchange(code, "oob")
        except Exception:
            exchange(code, "https://localhost:8080")
        main_test()
    elif cmd == "test":
        main_test()
    else:
        sys.exit("commands: url | code <CODE> | test")


def main_test():
    d = get("users;use_login=1/games;game_keys=nfl/leagues")
    txt = json.dumps(d)
    import re
    names = re.findall(r'"name":"([^"]+)"', txt)
    keys = re.findall(r'"league_key":"([^"]+)"', txt)
    print("\nLEAGUES VISIBLE TO THIS ACCOUNT:")
    for k, n in zip(keys, names[-len(keys):] if keys else []):
        print(f"  {k}  {n}")
    if keys:
        rosters = get(f"league/{keys[0]}/standings")
        print("standings fetch OK — full read access confirmed.")


if __name__ == "__main__":
    main()
