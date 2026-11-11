import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request

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
        interval=0.6,
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

    def redact(self, value):
        text = str(value)
        if self._cookie:
            text = text.replace(self._cookie, "[redacted]")
        return text

    def _pace(self):
        gap = self._monotonic() - self._last
        if gap < self.interval:
            self._sleep(self.interval - gap)
        self._last = self._monotonic()

    def _backoff(self, attempt):
        return min(60.0, 2.0 ** attempt) * (0.5 + random.random() / 2.0)

    def _retry_after(self, error):
        raw = error.headers.get("retry-after") if error.headers else None
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return None

    def _send(self, method, url, data, headers):
        request = urllib.request.Request(url, data=data, headers=dict(headers), method=method)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = response.read().decode("utf-8")
        if not body:
            return {}
        return json.loads(body)

    def _request(self, method, url, params=None, body=None):
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        data = None
        headers = {
            "Cookie": ".ROBLOSECURITY=" + self._cookie,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
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
                return self._send(method, url, data, headers)
            except urllib.error.HTTPError as error:
                error.close()
                if error.code == 403:
                    token = error.headers.get("x-csrf-token") if error.headers else None
                    if token and token != self._csrf:
                        self._csrf = token
                        continue
                    raise RobloxAuthError(self.redact("403 forbidden from " + url))
                if error.code in (401,):
                    raise RobloxAuthError(self.redact("401 unauthorized from " + url))
                if error.code == 429:
                    wait = self._retry_after(error)
                    if wait is None:
                        wait = self._backoff(attempt)
                    attempt += 1
                    if attempt >= self.attempts or wait > self.max_wait:
                        raise RobloxRateLimited(
                            self.redact("429 rate limited on " + url), wait
                        )
                    self._sleep(wait)
                    continue
                if 500 <= error.code < 600:
                    attempt += 1
                    if attempt >= self.attempts:
                        raise RobloxError(self.redact("%d from %s" % (error.code, url)))
                    self._sleep(self._backoff(attempt))
                    continue
                if error.code in (400, 404):
                    raise RobloxUserError(self.redact("%d from %s" % (error.code, url)))
                raise RobloxError(self.redact("%d from %s" % (error.code, url)))
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                attempt += 1
                if attempt >= self.attempts:
                    raise RobloxError(self.redact("network failure on %s: %s" % (url, error)))
                self._sleep(self._backoff(attempt))
            except json.JSONDecodeError as error:
                raise RobloxError(self.redact("malformed json from %s: %s" % (url, error)))

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
