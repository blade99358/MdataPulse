"""Minimal Zabbix JSON-RPC client (standard library only).

Reads ZABBIX_URL (e.g. https://zabbix.local) and ZABBIX_TOKEN from the
environment. Set ZABBIX_INSECURE=1 to accept a self-signed certificate.
"""
import json
import os
import ssl
import sys
import urllib.error
import urllib.request


class ZabbixError(RuntimeError):
    pass


class ZabbixAPI:
    def __init__(self, url=None, token=None, insecure=None):
        url = url or os.environ.get("ZABBIX_URL")
        token = token or os.environ.get("ZABBIX_TOKEN")
        if not url or not token:
            sys.exit("Set ZABBIX_URL and ZABBIX_TOKEN (API token from Users > API tokens).")
        url = url.rstrip("/")
        if not url.endswith("api_jsonrpc.php"):
            url += "/api_jsonrpc.php"
        self.url = url
        self.token = token
        insecure = insecure if insecure is not None else os.environ.get("ZABBIX_INSECURE") == "1"
        self.ctx = ssl._create_unverified_context() if insecure else None
        self._id = 0

    def call(self, method, params=None):
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}, "id": self._id})
        headers = {"Content-Type": "application/json-rpc"}
        if method not in ("apiinfo.version", "user.checkAuthentication"):
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(self.url, body.encode(), headers)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=120) as r:
                resp = json.loads(r.read())
        except urllib.error.URLError as e:
            raise ZabbixError(f"{method}: cannot reach {self.url}: {e}") from e
        if "error" in resp:
            err = resp["error"]
            raise ZabbixError(f"{method}: {err.get('message')} {err.get('data')}")
        return resp["result"]

    def version(self):
        return self.call("apiinfo.version")
