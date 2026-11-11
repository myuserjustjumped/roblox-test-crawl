import http.client
import json
import random
import socket
import time
import urllib.parse

DEFAULT_FRIENDS_BASE = "https://friends.roblox.com"
DEFAULT_USERS_BASE = "https://users.roblox.com"
PAGE_LIMIT = 50
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


class RobloxError(Exception):
    pass


class RobloxAuthError(RobloxError):
    pass


class RobloxUserError(RobloxError):
    pass


class RobloxRateLimited(RobloxError):
    def __init__(self, message, retry_after):
        super().__init__(message)
        self.retry_after = retry_after


class RobloxClient:
    def __init__(
        self,
        cookie,
        friends_base=DEFAULT_FRIENDS_BASE,
        users_base=DEFAULT_USERS_BASE,
        timeout=30.0,
        attempts=6,
        interval=0.05,
        max_wait=120.0,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ):
        self._cookie = cookie
        self.friends_base = friends_base.rstrip("/")
        self.users_base = users_base.rstrip("/")
        self.timeout = timeout
        self.attempts = attempts
        self.interval = interval
        self.max_wait = max_wait
        self._sleep = sleep
        self._monotonic = monotonic
        self._csrf = None
        self._last = 0.0
        self._connections = {}
        self._remaining = None
        self._reset_at = None

    def redact(self, value):
        text = str(value)
        if self._cookie:
            text = text.replace(self._cookie, "[redacted]")
        return text

    def close(self):
        for connection in self._connections.values():
            try:
                connection.close()
            except Exception:
                pass
        self._connections.clear()

    def _pace(self):
        now = self._monotonic()
        wait = self.interval - (now - self._last)
        if self._remaining is not None and self._reset_at is not None:
            left = self._reset_at - now
            if self._remaining <= 0:
                wait = max(wait, left + 0.25)
            elif left > 0:
                wait = max(wait, left / (self._remaining + 1))
        if wait > 0:
            self._sleep(wait)
        self._last = self._monotonic()

    def _update_quota(self, response):
        remaining = response.getheader("x-ratelimit-remaining")
        reset = response.getheader("x-ratelimit-reset")
        if remaining is not None:
            try:
                self._remaining = int(float(remaining))
            except (TypeError, ValueError):
                pass
        if reset is not None:
            try:
                self._reset_at = self._monotonic() + max(0.0, float(reset))
            except (TypeError, ValueError):
                pass

    def _backoff(self, attempt):
        return min(60.0, 2.0 ** attempt) * (0.5 + random.random() / 2.0)

    def _parse_retry_after(self, value):
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return None

    def _split(self, url):
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme or "https"
        host = parsed.hostname or ""
        port = parsed.port or (443 if scheme == "https" else 80)
        target = parsed.path or "/"
        if parsed.query:
            target = target + "?" + parsed.query
        return scheme, host, port, target

    def _connection(self, scheme, host, port):
        key = (scheme, host, port)
        connection = self._connections.get(key)
        if connection is None:
            if scheme == "https":
                connection = http.client.HTTPSConnection(host, port, timeout=self.timeout)
            else:
                connection = http.client.HTTPConnection(host, port, timeout=self.timeout)
            connection.connect()
            try:
                connection.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except (AttributeError, OSError):
                pass
            self._connections[key] = connection
        return connection

    def _drop(self, scheme, host, port):
        connection = self._connections.pop((scheme, host, port), None)
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    def _send(self, method, url, data, headers):
        scheme, host, port, target = self._split(url)
        for attempt in (0, 1):
            try:
                connection = self._connection(scheme, host, port)
                connection.request(method, target, body=data, headers=headers)
                response = connection.getresponse()
                raw = response.read()
                if response.will_close:
                    self._drop(scheme, host, port)
                return response.status, response, raw
            except (http.client.HTTPException, OSError):
                self._drop(scheme, host, port)
                if attempt == 1:
                    raise

    def _request(self, method, url, params=None, body=None):
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        data = None
        headers = {
            "Cookie": ".ROBLOSECURITY=" + self._cookie,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Connection": "keep-alive",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        attempt = 0
        while True:
            self._pace()
            if self._csrf:
                headers["X-CSRF-Token"] = self._csrf
            try:
                status, response, raw = self._send(method, url, data, headers)
            except (http.client.HTTPException, OSError) as error:
                attempt += 1
                if attempt >= self.attempts:
                    raise RobloxError(
                        self.redact("network failure on %s: %s" % (url, error))
                    )
                self._sleep(self._backoff(attempt))
                continue
            self._update_quota(response)
            if status == 200:
                if not raw:
                    return {}
                try:
                    return json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise RobloxError(
                        self.redact("malformed json from %s: %s" % (url, error))
                    )
            if status == 403:
                token = response.getheader("x-csrf-token")
                if token and token != self._csrf:
                    self._csrf = token
                    continue
                raise RobloxAuthError(self.redact("403 forbidden from " + url))
            if status == 401:
                raise RobloxAuthError(self.redact("401 unauthorized from " + url))
            if status == 429:
                wait = self._parse_retry_after(response.getheader("retry-after"))
                self._remaining = 0
                attempt += 1
                if wait is None:
                    wait = self._backoff(attempt)
                if attempt >= self.attempts or wait > self.max_wait:
                    raise RobloxRateLimited(
                        self.redact("429 rate limited on " + url), wait
                    )
                self._reset_at = self._monotonic() + wait
                self._sleep(wait)
                continue
            if 500 <= status < 600:
                attempt += 1
                if attempt >= self.attempts:
                    raise RobloxError(
                        self.redact("%d from %s" % (status, url))
                    )
                self._sleep(self._backoff(attempt))
                continue
            if status in (400, 404):
                raise RobloxUserError(self.redact("%d from %s" % (status, url)))
            raise RobloxError(self.redact("%d from %s" % (status, url)))

    def resolve_user_id(self, seed):
        value = str(seed).strip()
        if not value:
            raise RobloxError("seed user is empty")
        if value.isdigit():
            user_id = int(value)
            if user_id <= 0:
                raise RobloxError("seed user id must be positive")
            return user_id
        payload = self._request(
            "POST",
            self.users_base + "/v1/usernames/users",
            body={"usernames": [value], "excludeBannedUsers": False},
        )
        matches = payload.get("data") or []
        if not matches:
            raise RobloxError("seed username could not be resolved")
        try:
            return int(matches[0]["id"])
        except (KeyError, TypeError, ValueError):
            raise RobloxError("seed username returned no usable id")

    def friends_page(self, user_id, cursor=None):
        params = {"limit": PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor
        url = "%s/v1/users/%d/friends/find" % (self.friends_base, int(user_id))
        payload = self._request("GET", url, params=params)
        if not isinstance(payload, dict):
            raise RobloxError("unexpected friends payload")
        ids = []
        for item in payload.get("PageItems") or []:
            if not isinstance(item, dict):
                continue
            try:
                value = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            if value > 0:
                ids.append(value)
        next_cursor = payload.get("NextCursor") or None
        if not isinstance(next_cursor, str):
            next_cursor = None
        return ids, next_cursor
