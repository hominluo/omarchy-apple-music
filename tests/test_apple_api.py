"""The API client's transport: 401 handling, retries, paging (F40, F41, C4).

requests is never allowed onto the network: the session's request method
and the scraper's requests.get are replaced, and sleeps are recorded instead
of slept.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import apple, config  # noqa: E402
from engine.apple import ApiError, AppleApi  # noqa: E402


def response(status=200, body=None, headers=None):
    answer = mock.Mock()
    answer.status_code = status
    answer.headers = headers or {}
    payload = {} if body is None else body
    answer.content = json.dumps(payload).encode()
    answer.json.return_value = payload
    answer.text = json.dumps(payload)
    return answer


class ApiCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="am-api-")
        self.env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.tmp.name,
                                                "XDG_DATA_HOME": self.tmp.name})
        self.env.start()
        config.devtoken_path().write_text(json.dumps(
            {"token": "DEV1", "expiresAt": time.time() + 86400}), "utf-8")
        apple._last_scrape = None
        self.sleeps = []
        self.sleep = mock.patch.object(apple.time, "sleep", side_effect=self.sleeps.append)
        self.sleep.start()
        self.api = AppleApi(media_user_token="USER", storefront="gb")
        self.calls = []

    def tearDown(self):
        self.sleep.stop()
        self.env.stop()
        self.tmp.cleanup()
        apple._last_scrape = None

    def answer(self, *responses):
        queue = list(responses)

        def request(method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        self.api.session.request = mock.Mock(side_effect=request)


class Unauthorized(ApiCase):
    def test_a_failed_rescrape_is_an_api_error(self):
        """F40: the forced re-scrape ran outside the conversion and leaked a
        raw ConnectionError, which left a build spinning."""
        self.answer(response(401))
        with mock.patch.object(apple.requests, "get",
                               side_effect=requests.ConnectionError("https://x/?sig=secret")):
            with self.assertRaises(ApiError) as caught:
                self.api._request("GET", "/v1/me/account")
        self.assertEqual(caught.exception.code, "network")
        self.assertNotIn("secret", caught.exception.message)

    def test_rescrape_http_error_is_an_api_error(self):
        self.answer(response(401))
        with mock.patch.object(apple.requests, "get", return_value=response(503)):
            with self.assertRaises(ApiError) as caught:
                self.api._request("GET", "/v1/me/account")
        self.assertEqual(caught.exception.code, "unavailable")

    def test_401_after_a_fresh_developer_token_is_the_user_token(self):
        """F22/C5: a 401 that survives the re-scrape used to come back as a
        generic 'error 401' and never marked the session."""
        seen = []
        self.api.on_unauthorized = lambda api, token, error: seen.append((token, error.status))
        self.answer(response(401), response(401))
        with mock.patch.object(apple, "scrape_developer_token",
                               return_value={"token": "DEV2", "expiresAt": time.time() + 86400}):
            with self.assertRaises(ApiError) as caught:
                self.api._request("GET", "/v1/me/library/songs")
        self.assertEqual(caught.exception.code, "unauthorized")
        self.assertEqual(seen, [("USER", 401)])
        self.assertEqual(self.calls[1][2]["headers"]["authorization"], "Bearer DEV2")

    def test_403_reports_the_session_but_not_from_the_licence(self):
        seen = []
        self.api.on_unauthorized = lambda api, token, error: seen.append(error.status)
        self.answer(response(403), response(403))
        with self.assertRaises(ApiError):
            self.api._request("POST", apple.WEBPLAYBACK, payload={})
        with self.assertRaises(ApiError):
            self.api._request("POST", apple.LICENSE, payload={})
        self.assertEqual(seen, [403])

    def test_a_burst_of_401s_scrapes_once(self):
        scrape = mock.Mock(return_value={"token": "DEV2", "expiresAt": time.time() + 86400})
        with mock.patch.object(apple, "scrape_developer_token", scrape):
            first = AppleApi(media_user_token="USER")
            second = AppleApi(media_user_token="USER")
            first._developer = second._developer = "DEV1"
            self.assertEqual(first._ensure_token(force=True), "DEV2")
            # Another request that 401'd on DEV1 picks up DEV2, no scrape.
            self.assertEqual(second._ensure_token(force=True), "DEV2")
            # DEV2 401s too, seconds later: still no second download.
            self.assertEqual(second._ensure_token(force=True), "DEV2")
        self.assertEqual(scrape.call_count, 1)


class Retries(ApiCase):
    def test_transient_status_is_retried_with_backoff(self):
        self.answer(response(503), response(502), response(200, {"data": [1]}))
        self.assertEqual(self.api._request("GET", "/v1/x"), {"data": [1]})
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(self.sleeps), 2)
        self.assertLess(self.sleeps[0], self.sleeps[1])
        self.assertLessEqual(sum(self.sleeps), 3)

    def test_retry_after_is_honoured(self):
        self.answer(response(429, headers={"Retry-After": "2"}), response(200, {"ok": 1}))
        self.assertEqual(self.api._request("GET", "/v1/x"), {"ok": 1})
        self.assertEqual(self.sleeps, [2.0])

    def test_a_long_retry_after_fails_at_once(self):
        self.answer(response(429, headers={"Retry-After": "120"}))
        with self.assertRaises(ApiError) as caught:
            self.api._request("GET", "/v1/x")
        self.assertEqual(caught.exception.status, 429)
        self.assertIn("120", caught.exception.message)
        self.assertEqual(self.sleeps, [])

    def test_gives_up_after_the_last_attempt(self):
        self.answer(*(response(503) for _ in range(apple.RETRY_ATTEMPTS)))
        with self.assertRaises(ApiError) as caught:
            self.api._request("GET", "/v1/x")
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(len(self.calls), apple.RETRY_ATTEMPTS)

    def test_connection_errors_are_retried_read_timeouts_are_not(self):
        self.answer(requests.ConnectionError("reset"), response(200, {"ok": 1}))
        self.assertEqual(self.api._request("GET", "/v1/x"), {"ok": 1})
        self.calls.clear()
        self.answer(requests.ReadTimeout("slow"))
        with self.assertRaises(ApiError) as caught:
            self.api._request("GET", "/v1/x")
        self.assertEqual(caught.exception.code, "network")
        self.assertEqual(len(self.calls), 1)

    def test_a_connect_timeout_is_not_retried(self):
        """G9: ConnectTimeout is a ConnectionError too, and a network that
        drops SYNs cost three 30 s timeouts per request."""
        self.answer(requests.ConnectTimeout("no SYN-ACK"))
        with self.assertRaises(ApiError) as caught:
            self.api._request("GET", "/v1/x")
        self.assertEqual(caught.exception.code, "network")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.sleeps, [])

    def test_a_refused_connection_is_still_retried_to_the_last_attempt(self):
        self.answer(*(requests.ConnectionError("refused") for _ in range(apple.RETRY_ATTEMPTS)))
        with self.assertRaises(ApiError):
            self.api._request("GET", "/v1/x")
        self.assertEqual(len(self.calls), apple.RETRY_ATTEMPTS)

    def test_the_licence_exchange_is_tried_once(self):
        self.answer(response(503))
        with self.assertRaises(ApiError):
            self.api._request("POST", apple.LICENSE, payload={})
        self.assertEqual(len(self.calls), 1)

    def test_retry_after_as_a_date(self):
        from email.utils import format_datetime
        from datetime import datetime, timedelta, timezone
        when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=3), usegmt=True)
        wait = apple._retry_after(response(429, headers={"Retry-After": when}))
        self.assertTrue(0 <= wait <= 3.5)


class Paging(ApiCase):
    def collection(self, next_uri="/v1/next?offset=100"):
        return response(200, {"data": [{
            "id": "p1", "type": "playlists", "attributes": {"name": "Big"},
            "relationships": {"tracks": {"data": [{"id": "1", "type": "songs"}], "next": next_uri}},
        }]})

    def page(self, track_id, next_uri=None):
        return response(200, {"data": [{"id": track_id, "type": "songs"}], "next": next_uri})

    def test_a_failed_page_keeps_what_came_before(self):
        """F41: one 503 on page 9 threw the whole playlist away."""
        self.answer(self.collection(), self.page("2", "/v1/next?offset=200"),
                    *(response(503) for _ in range(apple.RETRY_ATTEMPTS)))
        with self.assertLogs("apple-music.api", "WARNING"):
            result = asyncio.run(self.api.playlist("p1"))
        self.assertEqual([t["id"] for t in result["tracks"]], ["1", "2"])
        self.assertTrue(result["truncated"])

    def test_the_page_cap_is_reported(self):
        pages = [self.page(str(i), f"/v1/next?offset={i}") for i in range(apple.COLLECTION_PAGES)]
        self.answer(self.collection(), *pages)
        result = asyncio.run(self.api.playlist("p1"))
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["tracks"]), apple.COLLECTION_PAGES + 1)

    def test_a_complete_list_is_not_truncated(self):
        self.answer(self.collection(), self.page("2"))
        self.assertFalse(asyncio.run(self.api.playlist("p1"))["truncated"])

    def test_a_dead_session_on_a_later_page_is_not_hidden(self):
        self.answer(self.collection(), response(403))
        with self.assertRaises(ApiError) as caught:
            asyncio.run(self.api.playlist("p1"))
        self.assertEqual(caught.exception.code, "unauthorized")

    def test_artist_albums_carry_truncated(self):
        artist = response(200, {"data": [{
            "id": "a1", "type": "artists", "attributes": {"name": "A"},
            "relationships": {"albums": {"data": [{"id": "al1", "type": "albums"}],
                                         "next": "/v1/next"}},
        }]})
        self.answer(artist, *(response(500) for _ in range(apple.RETRY_ATTEMPTS)))
        with self.assertLogs("apple-music.api", "WARNING"):
            result = asyncio.run(self.api.artist("a1"))
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["albums"]), 1)

    def test_library_next_is_an_offset(self):
        """C4: {items, next} with next the offset to ask for, or None."""
        songs = [{"id": f"i.{n}", "type": "library-songs"} for n in range(3)]
        self.answer(response(200, {"data": songs, "next": "/v1/me/library/songs?offset=103"}),
                    response(200, {"data": songs}))
        first = asyncio.run(self.api.library("songs", 100, 100))
        self.assertEqual(first["next"], 103)
        self.assertEqual(self.calls[0][2]["params"]["offset"], 100)
        self.assertIsNone(asyncio.run(self.api.library("songs", 100, 103))["next"])

    def test_library_offset_without_a_number_in_next(self):
        self.assertEqual(apple._next_offset("/v1/me/library/songs?cursor=x", 40, 25), 65)
        self.assertIsNone(apple._next_offset(None, 40, 25))


class Storefront(ApiCase):
    def test_the_account_sets_the_catalogue_storefront(self):
        self.answer(response(200, {"data": [], "meta": {"subscription": {"storefront": "JP"}}}))
        asyncio.run(self.api.fetch_account())
        self.assertEqual(self.api.storefront, "jp")


if __name__ == "__main__":
    unittest.main()
