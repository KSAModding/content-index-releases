#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""The release hosts the watcher polls, behind one interface.

A host answers which releases exist and what the bytes of one of them are. The
authority from `[releases]` defines which releases exist; every other host is
only checked for a byte-identical archive, which is how `download.mirrors` gets
populated (RFC 0031, RFC 0033).

The two failure kinds are reported differently and must not be confused:
HostError means this tick could not evaluate the host and the next one rescans,
StampError means the release itself is wrong and the author has to act.

GitHub is polled conditionally against a stored ETag; SpaceDock serves no
validator, so a SpaceDock authority costs one request per tick.
"""

import dataclasses
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from stamp_release import StampError, normalize_version

GITHUB_API = "https://api.github.com"
SPACEDOCK = "https://spacedock.info"

USER_AGENT = "KSAModding-content-index-watcher"

ARCHIVE_CONTENT_TYPES = frozenset(
    {"application/zip", "application/x-zip-compressed", "application/octet-stream"}
)

# 512 MiB. A mod archive is orders of magnitude smaller, and a runner that
# streams something enormous has already lost the tick for every other listing.
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024

LINK_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


class HostError(Exception):
    """The host could not be evaluated this tick. Transient by assumption."""


class OversizeError(HostError):
    """The response blew the size limit. Permanent for an archive, so `_download`
    turns it into a StampError."""


@dataclasses.dataclass(frozen=True)
class HostRelease:
    """One release as a host describes it, before anything is derived from it."""

    host: str
    tag: str
    version: str | None
    release_date: str | None
    url: str | None
    content_type: str = "application/zip"
    size: int | None = None
    prerelease: bool = False
    changelog: str | None = None
    asset_name: str | None = None
    # The archives the host offered when none could be picked, so the error the
    # author reads names them instead of claiming there was nothing there.
    candidates: tuple = ()

    def facts(self):
        """The release facts the stamper takes."""
        return {
            "tag": self.tag,
            "release_date": self.release_date,
            "url": self.url,
            "content_type": self.content_type,
            "prerelease": self.prerelease,
            "changelog": self.changelog,
        }


@dataclasses.dataclass(frozen=True)
class Response:
    status: int
    headers: dict
    body: bytes


class Http:
    """Plain urllib with the retry and rate limit behavior a tick needs."""

    def __init__(self, token=None, timeout=60, retries=3, log=None):
        self.token = token
        self.timeout = timeout
        self.retries = retries
        self.log = log or (lambda message: None)
        self.requests = 0

    def get(self, url, accept=None, etag=None, api=False, limit=None):
        """GET `url`, returning a Response. A 304 comes back with an empty body.

        Raises HostError for anything transient and urllib's HTTPError for a
        status the caller has to interpret itself, such as 404.
        """
        headers = {"User-Agent": USER_AGENT}
        if accept:
            headers["Accept"] = accept
        if etag:
            headers["If-None-Match"] = etag
        if api and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
            headers["X-GitHub-Api-Version"] = "2022-11-28"

        last = None
        for attempt in range(self.retries):
            request = urllib.request.Request(url, headers=headers, method="GET")
            try:
                self.requests += 1
                with urllib.request.urlopen(request, timeout=self.timeout) as answer:
                    return Response(
                        answer.status, dict(answer.headers), _read(answer, limit)
                    )
            except urllib.error.HTTPError as error:
                if error.code == 304:
                    return Response(304, dict(error.headers), b"")
                if error.code in (403, 429) and _rate_limited(error.headers):
                    raise HostError(f"{url}: rate limited by the host") from error
                if error.code < 500 and error.code != 429:
                    raise
                last = error
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last = error

            if attempt + 1 < self.retries:
                time.sleep(2 ** attempt)

        raise HostError(f"{url}: {last}")


def _read(answer, limit):
    limit = limit or MAX_ARCHIVE_BYTES
    body = answer.read(limit + 1)
    if len(body) > limit:
        raise OversizeError(f"the response is larger than the {limit} byte limit")
    return body


def _rate_limited(headers):
    lower = {key.lower(): value for key, value in headers.items()}
    return lower.get("x-ratelimit-remaining") == "0" or "retry-after" in lower


def _utc(timestamp):
    """A host timestamp as the ISO 8601 UTC form a release file carries.

    A timestamp that does not parse yields None rather than passing the raw
    string through: `release_date` is stamped exactly once, and the stamper
    rejects a release without one, which scopes the failure to that release.
    """
    if not timestamp:
        return None
    text = timestamp.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _version_of(tag):
    try:
        return normalize_version(tag)
    except StampError:
        return None


def _parse_json(url, body):
    """The body as JSON, or HostError: a 200 carrying HTML is a bad moment."""
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise HostError(f"{url}: the answer is not JSON, {error}") from error


def _on_host(base, value, what):
    """`value` resolved against `base`, and only if it stayed on that host.

    `urljoin` returns an absolute or protocol-relative value unchanged, so a
    host that answers `https://elsewhere/x.zip` would otherwise put a foreign
    address into a stamped file. The index never trusts a fact it was handed,
    and a URL is a fact like any other.
    """
    if not value:
        return None
    resolved = urllib.parse.urljoin(base, str(value))
    wanted, got = urllib.parse.urlsplit(base), urllib.parse.urlsplit(resolved)
    if (got.scheme, got.netloc) != (wanted.scheme, wanted.netloc):
        raise StampError(
            f"{what} '{value}' resolves to {resolved}, which is not on "
            f"{wanted.netloc}"
        )
    return resolved


class Host:
    """A release host of one listing."""

    kind = ""

    @property
    def key(self):
        """The cache key of this host's release list."""
        raise NotImplementedError

    def releases(self, etag=None):
        """(releases, etag), or (None, etag) when the host answers 'unchanged'."""
        raise NotImplementedError

    def download(self, release):
        """The archive's bytes, and the content type the host serves them as."""
        raise NotImplementedError


class GitHubHost(Host):
    """A GitHub repository's releases. Polled conditionally, drafts ignored."""

    kind = "github"

    def __init__(self, repository, http, listing_id=None, max_pages=5):
        self.repository = str(repository).strip("/")
        self.http = http
        self.listing_id = listing_id or self.repository.split("/")[-1]
        self.max_pages = max_pages
        # True when the last scan hit max_pages with more pages left, so the
        # caller can report the tail instead of silently never seeing it.
        self.truncated = False

    @property
    def key(self):
        return f"github:{self.repository.lower()}"

    def releases(self, etag=None):
        url = f"{GITHUB_API}/repos/{self.repository}/releases?per_page=100"
        try:
            first = self.http.get(
                url, accept="application/vnd.github+json", etag=etag, api=True
            )
        except urllib.error.HTTPError as error:
            if error.code in (404, 451):
                raise StampError(
                    f"the authority host has no repository '{self.repository}' the "
                    "watcher can read: it was renamed, made private, or removed"
                ) from error
            raise HostError(f"{url}: HTTP {error.code}") from error

        if first.status == 304:
            return None, etag

        payloads = [_parse_json(url, first.body)]
        following = LINK_NEXT.search(first.headers.get("Link", "") or "")
        pages = 1
        while following and pages < self.max_pages:
            try:
                answer = self.http.get(
                    following.group(1), accept="application/vnd.github+json", api=True
                )
            except urllib.error.HTTPError as error:
                raise HostError(f"{following.group(1)}: HTTP {error.code}") from error
            payloads.append(_parse_json(following.group(1), answer.body))
            following = LINK_NEXT.search(answer.headers.get("Link", "") or "")
            pages += 1
        self.truncated = bool(following)
        if following:
            self.http.log(
                f"{self.repository}: more than {self.max_pages * 100} releases, "
                "the older ones are not scanned"
            )

        releases = [
            self._release(payload)
            for page in payloads
            for payload in page
            if not payload.get("draft")
        ]
        return releases, first.headers.get("ETag") or etag

    def _release(self, payload):
        tag = payload.get("tag_name") or ""
        asset, candidates = self._asset(payload)
        return HostRelease(
            host=self.kind,
            tag=tag,
            version=_version_of(tag),
            release_date=_utc(payload.get("published_at") or payload.get("created_at")),
            url=asset.get("browser_download_url") if asset else None,
            content_type=(asset or {}).get("content_type") or "application/zip",
            size=(asset or {}).get("size"),
            prerelease=bool(payload.get("prerelease")),
            changelog=payload.get("html_url"),
            asset_name=(asset or {}).get("name"),
            candidates=() if asset else tuple(candidates),
        )

    def _asset(self, payload):
        """The release's archive.

        One archive is the normal case. Where a release carries several, the one
        named after the listing wins, because that is what every archive in the
        index is named. Anything still ambiguous is reported to the author
        rather than guessed at: picking the wrong asset would stamp a hash
        clients then verify against the wrong file.
        """
        uploaded = [
            asset
            for asset in payload.get("assets") or []
            if asset.get("state") == "uploaded"
        ]
        assets = [
            asset for asset in uploaded if asset.get("name", "").lower().endswith(".zip")
        ] or [
            asset
            for asset in uploaded
            if asset.get("content_type") in ("application/zip", "application/x-zip-compressed")
        ]
        names = [asset.get("name", "") for asset in assets]
        if len(assets) <= 1:
            return (assets[0] if assets else None), names

        identifier = self.listing_id.lower()
        tag = (payload.get("tag_name") or "").lstrip("vV").lower()
        for wanted in (f"{identifier}.zip", f"{identifier}-{tag}.zip", f"{identifier}_{tag}.zip"):
            for asset in assets:
                if asset["name"].lower() == wanted:
                    return asset, names
        return None, names

    def download(self, release):
        return _download(self.http, release)


class SpaceDockHost(Host):
    """A SpaceDock mod's versions.

    SpaceDock serves no ETag on its API, so a SpaceDock host costs one request
    per tick. It also has no draft or pre-release flag: every version it lists
    is a published one.
    """

    kind = "spacedock"

    def __init__(self, mod_id, http):
        try:
            self.mod_id = int(mod_id)
        except (TypeError, ValueError):
            raise StampError(
                f"'{mod_id}' is not a SpaceDock mod id, which is a number"
            ) from None
        self.http = http

    @property
    def key(self):
        return f"spacedock:{self.mod_id}"

    def releases(self, etag=None):
        url = f"{SPACEDOCK}/api/mod/{self.mod_id}"
        try:
            answer = self.http.get(url, accept="application/json")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise StampError(
                    f"SpaceDock has no mod {self.mod_id}"
                ) from error
            raise HostError(f"{url}: HTTP {error.code}") from error

        payload = _parse_json(url, answer.body)

        page = payload.get("url") or f"/mod/{self.mod_id}"
        changelog = _on_host(SPACEDOCK, page, "the mod page")
        releases = []
        for version in payload.get("versions") or []:
            tag = (version.get("friendly_version") or "").strip()
            releases.append(
                HostRelease(
                    host=self.kind,
                    tag=tag,
                    version=_version_of(tag),
                    release_date=_utc(version.get("created")),
                    url=_on_host(SPACEDOCK, version.get("download_path"), "the download path"),
                    content_type="application/zip",
                    prerelease=False,
                    changelog=changelog,
                )
            )
        return releases, None

    def download(self, release):
        return _download(self.http, release)


def _download(http, release):
    if not release.url:
        if release.candidates:
            raise StampError(
                f"the release carries {len(release.candidates)} archives and none of them "
                f"is named after the listing ({', '.join(release.candidates)}), so there "
                "is nothing to stamp: the watcher does not guess which archive a client "
                "should verify against"
            )
        raise StampError("the release carries no archive to download")
    if release.size and release.size > MAX_ARCHIVE_BYTES:
        raise StampError(
            f"the archive is {release.size} bytes, above the "
            f"{MAX_ARCHIVE_BYTES} byte limit"
        )
    try:
        answer = http.get(release.url, api=release.url.startswith(GITHUB_API))
    except OversizeError as error:
        # Permanent, unlike the transient failures HostError stands for: the
        # release stays too large next tick too, so the author hears about it
        # instead of the watcher downloading and discarding it forever.
        raise StampError(f"the archive at {release.url}: {error}") from error
    except urllib.error.HTTPError as error:
        # A gone archive is a fact about the release, reported to the author.
        # Everything else is the host having a bad moment this tick.
        if error.code in (404, 410, 451):
            raise StampError(
                f"the archive at {release.url} is gone (HTTP {error.code})"
            ) from error
        raise HostError(f"{release.url}: HTTP {error.code}") from error
    served = (answer.headers.get("Content-Type") or "").split(";")[0].strip()
    # What the host says the asset is beats what it happens to serve it as, and
    # the stamper has the bytes to fall back on either way.
    content_type = release.content_type or served
    return answer.body, content_type


def build(releases_section, http, listing_id=None):
    """The hosts of one listing, and its authority.

    Returns (authority, mirrors). With one host key that host is the authority;
    with several, `authority` names which one, and the rest are mirror
    candidates. No `[releases]` section at all means the listing does not enter
    the index through the watcher, and this returns (None, []).
    """
    section = releases_section or {}
    named = {}
    if section.get("github"):
        named["github"] = GitHubHost(section["github"], http, listing_id)
    if section.get("spacedock"):
        named["spacedock"] = SpaceDockHost(section["spacedock"], http)

    if not named:
        return None, []

    if len(named) == 1:
        (authority,) = named.values()
        return authority, []

    chosen = section.get("authority")
    if chosen not in named:
        raise StampError(
            "[releases] names several hosts, so it needs an 'authority' key naming "
            f"one of {', '.join(sorted(named))}"
        )
    return named[chosen], [host for name, host in sorted(named.items()) if name != chosen]
