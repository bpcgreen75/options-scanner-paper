"""One-time Robinhood sign-in, account read, and renewal check for Render."""

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path
from time import time
from urllib.parse import parse_qs, urlencode

from paper_preview import money


HOST = "agent.robinhood.com"
MCP_PATH = "/mcp/trading"
RESOURCE = f"https://{HOST}{MCP_PATH}"
CALLBACK_PATH = "/robinhood/callback"
CALLBACK = f"https://options-scanner-paper.onrender.com{CALLBACK_PATH}"
MAX_RESPONSE = 1024 * 1024


class ProbeError(Exception):
    """A failed check with a credential-free message."""


def signing_client(directory):
    data = json.loads((directory / "robinhood-paper-client.json").read_text(encoding="utf-8"))
    if data.get("redirect_uri") != CALLBACK or not isinstance(data.get("client_id"), str) or not data["client_id"]:
        raise ProbeError("Saved registration does not match the Render callback")
    return data["client_id"]


def open_store(directory):
    path = directory / "robinhood-paper-probe.sqlite3"
    os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    return sqlite3.connect(path, timeout=2)


def state_key(value):
    return hashlib.sha256(value.encode()).hexdigest()


def begin(directory):
    client_id = signing_client(directory)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    with closing(open_store(directory)) as connection, connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS probe (id INTEGER PRIMARY KEY CHECK(id=1), "
            "state TEXT NOT NULL, verifier TEXT, expires INTEGER NOT NULL, report TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO probe VALUES (1, ?, ?, ?, ?)",
            (state_key(state), verifier, int(time()) + 600, '{"status":"awaiting_sign_in"}'),
        )
    query = urlencode(dict(response_type="code", client_id=client_id, redirect_uri=CALLBACK,
                           resource=RESOURCE, scope="internal", state=state,
                           code_challenge=challenge, code_challenge_method="S256"))
    return f"https://robinhood.com/oauth?{query}"


def report_for(directory):
    if not (directory / "robinhood-paper-probe.sqlite3").exists():
        return {"status": "not_started"}
    with closing(open_store(directory)) as connection:
        row = connection.execute("SELECT verifier, expires, report FROM probe WHERE id=1").fetchone()
    if row is None:
        return {"status": "not_started"}
    return {"status": "expired"} if row[0] and row[1] <= int(time()) else json.loads(row[2])


def decode_object(raw):
    if len(raw) > MAX_RESPONSE:
        raise ProbeError("Robinhood response exceeded the test size limit")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ProbeError("Robinhood response did not contain a JSON object")
    return data


def stream_reply(response, request_id):
    if response.headers.get_content_type() != "text/event-stream":
        return decode_object(response.read(MAX_RESPONSE + 1))
    message, total = [], 0
    while True:
        line = response.readline(MAX_RESPONSE + 1)
        if not line:
            raise ProbeError("Robinhood closed the response stream before replying")
        total += len(line)
        if total > MAX_RESPONSE:
            raise ProbeError("Robinhood stream exceeded the test size limit")
        if line.startswith(b"data:"):
            message.append(line[5:].lstrip(b" ").rstrip(b"\r\n"))
        elif not line.strip() and message:
            result = decode_object(b"\n".join(message))
            if result.get("id") == request_id:
                return result
            message.clear()


def post(host, path, body, headers, label, *, request_id=None, notification=False):
    # Fixed HTTPS endpoints. http.client does not follow redirects with credentials.
    with closing(http.client.HTTPSConnection(host, timeout=8)) as connection:
        connection.request("POST", path, body=body, headers=headers)
        with connection.getresponse() as response:
            if response.status != 200 and not (notification and response.status in (202, 204)):
                raise ProbeError(f"{label} returned HTTP {response.status}")
            if notification:
                return {}, None
            data = stream_reply(response, request_id)
            return data, response.getheader("MCP-Session-Id")


def token(**parameters):
    data, _ = post("api.robinhood.com", "/oauth2/token/",
                   urlencode({**parameters, "resource": RESOURCE}).encode(),
                   {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                   "Robinhood token request")
    access = data.get("access_token")
    if not isinstance(access, str) or not access or data.get("token_type", "Bearer").lower() != "bearer":
        raise ProbeError("Robinhood did not issue a usable access token")
    return data


def read_account(access_token):
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}

    def rpc(method, params, request_id=None):
        request = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            request["id"] = request_id
        data, session = post(HOST, MCP_PATH, json.dumps(request).encode(), headers, "Robinhood MCP",
                             request_id=request_id, notification=request_id is None)
        if request_id is None:
            return {}, session
        if data.get("id") != request_id or not isinstance(data.get("result"), dict):
            raise ProbeError(f"Robinhood did not complete {method}")
        return data["result"], session

    init, session = rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                       "clientInfo": {"name": "Options Scanner Paper", "version": "1.0"}}, 1)
    version = init.get("protocolVersion")
    if version not in ("2025-03-26", "2025-06-18", "2025-11-25"):
        raise ProbeError("Robinhood negotiated an unsupported MCP version")
    headers["MCP-Protocol-Version"] = version
    if session:
        headers["MCP-Session-Id"] = session
    rpc("notifications/initialized", {})

    def read(tool, arguments, request_id):
        result, _ = rpc("tools/call", {"name": tool, "arguments": arguments}, request_id)
        if result.get("isError"):
            raise ProbeError(f"Robinhood {tool} could not be read")
        payload = result.get("structuredContent")
        if payload is None:
            chunks = result.get("content", [])
            raw = next((item.get("text") for item in chunks if item.get("type") == "text"), None)
            payload = decode_object(raw.encode()) if isinstance(raw, str) else {}
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProbeError(f"Robinhood {tool} returned an unexpected response")
        return data

    accounts = read("get_accounts", {}, 2).get("accounts") or []
    dedicated = [account for account in accounts if account and account.get("agentic_allowed")]
    if len(dedicated) != 1:
        raise ProbeError("Could not identify exactly one dedicated Robinhood account")
    account = dedicated[0]
    number = account["account_number"]
    power = read("get_portfolio", {"account_number": number}, 3).get("buying_power") or {}
    if power.get("display_currency") != "USD":
        raise ProbeError("Missing US-dollar buying power")
    return number, {"account_ending": number[-4:], "options_level": account.get("option_level"),
                    "buying_power_usd": str(money(power["buying_power"]))}


def complete(directory, query):
    if len(query) > 8192:
        return 400, {"status": "invalid_callback"}
    try:
        params = parse_qs(query, keep_blank_values=True, max_num_fields=12)
    except ValueError:
        return 400, {"status": "invalid_callback"}
    if any(len(values) != 1 for values in params.values()):
        return 400, {"status": "invalid_callback"}
    state = params.get("state", [""])[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", state):
        return 400, {"status": "invalid_callback"}
    key = state_key(state)
    if not (directory / "robinhood-paper-probe.sqlite3").is_file():
        return 400, {"status": "expired_or_used_sign_in"}
    try:
        with closing(open_store(directory)) as connection, connection:
            # Take the verifier once, before the token exchange.
            cursor = connection.execute("SELECT verifier FROM probe WHERE state=? AND expires>?",
                                        (key, int(time())))
            row = cursor.fetchone()
            if row is None or not row[0]:
                return 400, {"status": "expired_or_used_sign_in"}
            consumed = connection.execute(
                "UPDATE probe SET verifier=NULL, report=? WHERE state=? AND verifier=?",
                ('{"status":"checking"}', key, row[0]),
            ).rowcount
            if consumed != 1:
                return 400, {"status": "expired_or_used_sign_in"}
        if "error" in params:
            result = {"status": "authorization_declined"}
        else:
            result = check_grant(directory, params, row[0])
        with closing(open_store(directory)) as connection, connection:
            connection.execute("UPDATE probe SET report=? WHERE state=?", (json.dumps(result), key))
    except sqlite3.Error:
        return 503, {"status": "probe_storage_unavailable"}
    return 200, {"status": result["status"], "details": "View the result in Render Shell"}


def check_grant(directory, params, verifier):
    stage = "sign_in"
    result = {}
    try:
        if params.get("iss", [""])[0] != RESOURCE or not params.get("code", [""])[0]:
            raise ProbeError("Missing authorization code or unexpected issuer")
        client_id = signing_client(directory)
        grant = token(grant_type="authorization_code", client_id=client_id, redirect_uri=CALLBACK,
                      code=params["code"][0], code_verifier=verifier)
        stage = "account_read"
        number, result = read_account(grant["access_token"])
        stage = "renewal"
        if not grant.get("refresh_token"):
            return {**result, "status": "account_read_ok", "renewal": "no_refresh_token"}
        renewed = token(grant_type="refresh_token", client_id=client_id, refresh_token=grant["refresh_token"])
        stage = "renewed_account_read"
        repeated, _ = read_account(renewed["access_token"])
        if repeated != number:
            raise ProbeError("Renewed access returned a different dedicated account")
        return {**result, "status": "account_read_ok", "renewal": "verified"}
    except (ProbeError, OSError, ValueError, TypeError, KeyError, AttributeError,
            ArithmeticError, http.client.HTTPException) as exc:
        message = str(exc) if isinstance(exc, ProbeError) else "Connection failed or provider response was invalid"
        return {**result, "status": "account_read_ok" if result else "failed",
                "stage": stage, "error": message}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("authorize", "status"))
    parser.add_argument("--storage", type=Path, default=Path("/app/storage"))
    args = parser.parse_args()
    if args.action == "authorize" and os.getenv("PAPER_ROBINHOOD_PROBE") != "1":
        parser.error("Enable PAPER_ROBINHOOD_PROBE=1 in Render before signing in")
    try:
        if args.action == "authorize":
            print("Open this private sign-in link within 10 minutes:")
            print(begin(args.storage))
        else:
            print(json.dumps(report_for(args.storage), indent=2))
    except (OSError, AttributeError, ValueError, sqlite3.Error, ProbeError) as exc:
        parser.error(str(exc) if isinstance(exc, ProbeError) else "Probe storage or registration is unavailable")


if __name__ == "__main__":
    main()
