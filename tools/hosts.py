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
import hashlib
import http.client
import json
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from stamp_release import (
    Archive,
    StampError,
    as_archive,
    derived_dependencies,
    normalize_version,
    open_archive,
    read_mod_toml,
)

GITHUB_API = "https://api.github.com"
GITHUB_API_HOST = urllib.parse.urlsplit(GITHUB_API).hostname
SPACEDOCK = "https://spacedock.info"

USER_AGENT = "KSAModding-content-index-watcher"

ARCHIVE_CONTENT_TYPES = frozenset(
    {"application/zip", "application/x-zip-compressed", "application/octet-stream"}
)

MIB = 1024 * 1024

# 4 GiB. Twice the largest KSA archive on SpaceDock (1,778 MiB) and above
# GitHub's 2 GiB file limit. An archive streams to disk, so one fits the 14 GB
# a runner guarantees with room to spare, and at the 13 MiB/s SpaceDock served
# a check it downloads in about five minutes, inside both check timeouts.
MAX_ARCHIVE_BYTES = 4096 * MIB

# 64 MiB. A release list or a raw file is JSON or text and held in memory, so
# the archive limit does not apply to it.
MAX_RESPONSE_BYTES = 64 * MIB

# What an archive download reads and writes at a time.
CHUNK_BYTES = MIB

LINK_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


class HostError(Exception):
    """The host could not be evaluated this tick. Transient by assumption."""


class OversizeError(HostError):
    """The response blew the size limit. Permanent for an archive, so `download`
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
    # "" when the host reports no notes, and None when its answer does not say, which the
    # watcher must not read as notes that were removed.
    changelog_text: str | None = None
    asset_name: str | None = None
    # The archives the host offered when none could be picked, so the error the
    # author reads names them instead of claiming there was nothing there.
    candidates: tuple = ()
    # The download count of the picked archive, or None.
    downloads: int | None = None

    def facts(self):
        """The release facts the stamper takes."""
        return {
            "tag": self.tag,
            "release_date": self.release_date,
            "url": self.url,
            "content_type": self.content_type,
            "prerelease": self.prerelease,
            "changelog": self.changelog,
            "changelog_text": self.changelog_text,
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

        The body is held in memory, up to `limit` or MAX_RESPONSE_BYTES. An
        archive goes through `archive` instead.

        Raises HostError for anything transient and urllib's HTTPError for a
        status the caller has to interpret itself, such as 404.
        """
        return self._send(
            url,
            self._headers(accept, etag, api),
            lambda answer: Response(
                answer.status,
                dict(answer.headers),
                _read(answer, limit or MAX_RESPONSE_BYTES),
            ),
        )

    def archive(self, url, api=False, limit=None):
        """GET an archive into an anonymous temporary file.

        Returns (Archive, headers). The SHA-256 and size are taken while the
        body streams, and a retry starts the file again. Raises what `get`
        raises, and OversizeError past `limit` or MAX_ARCHIVE_BYTES.
        """
        return self._send(
            url,
            self._headers(None, None, api),
            lambda answer: (
                _stream(answer, limit or MAX_ARCHIVE_BYTES),
                dict(answer.headers),
            ),
        )

    def _headers(self, accept, etag, api):
        headers = {"User-Agent": USER_AGENT}
        if accept:
            headers["Accept"] = accept
        if etag:
            headers["If-None-Match"] = etag
        if api and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        return headers

    def _send(self, url, headers, read):
        """The retry loop around one GET, with `read` turning the answer into the result."""
        last = None
        for attempt in range(self.retries):
            request = urllib.request.Request(url, headers=headers, method="GET")
            try:
                self.requests += 1
                with urllib.request.urlopen(request, timeout=self.timeout) as answer:
                    return read(answer)
            except urllib.error.HTTPError as error:
                if error.code == 304:
                    return Response(304, dict(error.headers), b"")
                if error.code in (403, 429) and _rate_limited(error.headers):
                    raise HostError(f"{url}: rate limited by the host") from error
                if error.code < 500 and error.code != 429:
                    raise
                last = error
            # IncompleteRead is a body that ends before its length. Other
            # HTTPException kinds, such as InvalidURL, are not transient.
            except (urllib.error.URLError, TimeoutError, OSError, http.client.IncompleteRead) as error:
                last = error

            if attempt + 1 < self.retries:
                time.sleep(2 ** attempt)

        raise HostError(f"{url}: {last}")


def _read(answer, limit):
    body = answer.read(limit + 1)
    if len(body) > limit:
        raise OversizeError(f"the response is larger than the {_mib(limit)} limit")
    return body


def _stream(answer, limit):
    """The body as an Archive in an anonymous temporary file.

    A Content-Length above `limit` rejects before any of the body is read, and
    the streamed count is the backstop for a host that sends none.
    """
    length = (answer.headers.get("Content-Length") or "").strip()
    if length.isdigit() and int(length) > limit:
        raise OversizeError(f"the archive is {_mib(int(length))}, above the {_mib(limit)} limit")

    file = tempfile.TemporaryFile()
    try:
        digest, size = hashlib.sha256(), 0
        while chunk := answer.read(CHUNK_BYTES):
            size += len(chunk)
            if size > limit:
                raise OversizeError(f"the archive is larger than the {_mib(limit)} limit")
            digest.update(chunk)
            file.write(chunk)
        # http.client returns a short body when the connection closes early
        # instead of raising, so the length decides whether the body is whole.
        if length.isdigit() and size != int(length):
            raise http.client.IncompleteRead(b"", int(length) - size)
        file.seek(0)
        return Archive(file, digest.hexdigest(), size)
    except BaseException:
        file.close()
        raise


def _mib(count):
    """A byte count in whole MiB, the unit the limits are set in."""
    return f"{count / MIB:,.0f} MiB"


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


def _count(value):
    """A non-negative integer count, or None. A JSON bool is not a count."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _notes(payload, key):
    """The release notes under `key`, "" when the host reports none, or None when it does not say."""
    if key not in payload:
        return None
    value = payload[key]
    if value is None:
        return ""
    return value if isinstance(value, str) else None


def _parse_json(url, body):
    """The body as JSON, or HostError: a 200 carrying HTML is a bad moment."""
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise HostError(f"{url}: the answer is not JSON, {error}") from error


def _objects(where, value, what):
    """`value` as a list of objects, or HostError."""
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise HostError(f"{where}: {what} is not a list of objects")
    return value


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
        """The archive as an Archive, and the content type the host serves it as."""
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
        # The pages of the last full answer. An ETag covers the first page only.
        self.pages = 0

    @property
    def key(self):
        return f"github:{self.repository.lower()}"

    def releases(self, etag=None):
        self.pages = 0
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

        payloads = [_objects(url, _parse_json(url, first.body), "the release list")]
        following = LINK_NEXT.search(first.headers.get("Link", "") or "")
        pages = 1
        while following and pages < self.max_pages:
            try:
                answer = self.http.get(
                    following.group(1), accept="application/vnd.github+json", api=True
                )
            except urllib.error.HTTPError as error:
                raise HostError(f"{following.group(1)}: HTTP {error.code}") from error
            payloads.append(
                _objects(
                    following.group(1),
                    _parse_json(following.group(1), answer.body),
                    "the release list",
                )
            )
            following = LINK_NEXT.search(answer.headers.get("Link", "") or "")
            pages += 1
        self.pages = pages
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
            changelog_text=_notes(payload, "body"),
            asset_name=(asset or {}).get("name"),
            candidates=() if asset else tuple(candidates),
            downloads=_count((asset or {}).get("download_count")),
        )

    def _asset(self, payload):
        """The release's archive.

        One archive is the normal case. Where a release carries several, the one
        named after the listing wins, because that is what every archive in the
        index is named. Anything still ambiguous is reported to the author
        rather than guessed at: picking the wrong asset would stamp a hash
        clients then verify against the wrong file.
        """
        listed = _objects(
            self.repository,
            payload.get("assets") or [],
            f"the assets of '{payload.get('tag_name')}'",
        )
        uploaded = [asset for asset in listed if asset.get("state") == "uploaded"]
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
        return download(self.http, release)


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
        # The mod's download total from the last answer.
        self.downloads = None

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
        if not isinstance(payload, dict):
            raise HostError(f"{url}: the answer is not an object")
        versions = _objects(url, payload.get("versions") or [], "versions")
        self.downloads = _count(payload.get("downloads"))

        page = payload.get("url") or f"/mod/{self.mod_id}"
        changelog = _on_host(SPACEDOCK, page, "the mod page")
        releases = []
        for version in versions:
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
                    changelog_text=_notes(version, "changelog"),
                    downloads=_count(version.get("downloads")),
                )
            )
        return releases, None

    def download(self, release):
        return download(self.http, release)


def download(http, release):
    """The archive of `release`, as (Archive, content type). The caller closes the Archive.

    Shared with the release pull request check, which downloads from the URL a
    submission names rather than from a host it polled.
    """
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
            f"the archive is {_mib(release.size)}, above the {_mib(MAX_ARCHIVE_BYTES)} limit"
        )
    api = urllib.parse.urlsplit(release.url).hostname == GITHUB_API_HOST
    try:
        archive, headers = http.archive(release.url, api=api)
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
    served = (headers.get("Content-Type") or "").split(";")[0].strip()
    # What the host says the asset is beats what it happens to serve it as, and
    # the stamper has the bytes to fall back on either way.
    content_type = release.content_type or served
    return archive, content_type


def stamped_dependencies(http, document):
    """The dependencies the archive of a stamped release file declares in its mod.toml, read as a stamp reads them.

    A mirror stands in for a download URL that is gone, because the bytes are the same.
    HostError says a later run may succeed, and StampError that no URL serves the stamped archive.
    """
    download_section = document.get("download") or {}
    problem = None
    for url in [download_section.get("url"), *(download_section.get("mirrors") or [])]:
        try:
            return _declared_dependencies(http, document, url)
        except (HostError, StampError) as error:
            if problem is None or isinstance(error, HostError):
                problem = error
    raise problem


def _declared_dependencies(http, document, url):
    download_section = document.get("download") or {}
    digest = (download_section.get("sha256") or "").upper()
    release = HostRelease(
        host="stamped",
        tag=document.get("version"),
        version=document.get("version"),
        release_date=document.get("release_date"),
        url=url,
        content_type=download_section.get("content_type"),
        size=download_section.get("size"),
    )
    try:
        archive, _ = download(http, release)
    except ValueError as error:
        raise StampError(f"the archive at {url} cannot be requested: {error}") from error
    mod_toml = None
    with as_archive(archive) as archive:
        if archive.sha256 != digest:
            raise StampError(f"the archive at {url} no longer matches the stamped sha256")
        if document.get("type") == "mod":
            root = (document.get("install") or {}).get("root", "")
            mod_toml = read_mod_toml(open_archive(archive), root)
    return derived_dependencies(mod_toml)


def named(releases_section, http, listing_id=None):
    """Every host a `[releases]` section names, keyed by kind."""
    section = releases_section or {}
    found = {}
    if section.get("github"):
        found["github"] = GitHubHost(section["github"], http, listing_id)
    if section.get("spacedock"):
        found["spacedock"] = SpaceDockHost(section["spacedock"], http)
    return found


def build(releases_section, http, listing_id=None):
    """The hosts of one listing, and its authority.

    Returns (authority, mirrors). With one host key that host is the authority;
    with several, `authority` names which one, and the rest are mirror
    candidates. No `[releases]` section at all means the listing does not enter
    the index through the watcher, and this returns (None, []).
    """
    section = releases_section or {}
    named_hosts = named(section, http, listing_id)

    if not named_hosts:
        return None, []

    if len(named_hosts) == 1:
        (authority,) = named_hosts.values()
        return authority, []

    chosen = section.get("authority")
    if chosen not in named_hosts:
        raise StampError(
            "[releases] names several hosts, so it needs an 'authority' key naming "
            f"one of {', '.join(sorted(named_hosts))}"
        )
    return named_hosts[chosen], [
        host for name, host in sorted(named_hosts.items()) if name != chosen
    ]
