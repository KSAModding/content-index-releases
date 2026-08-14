# content-index-releases

The generated half of the Kitten Space Agency content index.

One stamped JSON document per release, written by tooling.
The authored documents live in the other half, [content-index](https://github.com/KSAModding/content-index).

The index is defined by [RFC 0031](https://github.com/KSAModding/content-manager-design/blob/main/rfcs/0031-content-metadata-format.md) (the metadata format) and [RFC 0033](https://github.com/KSAModding/content-manager-design/blob/main/rfcs/0033-content-index.md) (the index itself), in [content-manager-design](https://github.com/KSAModding/content-manager-design).
Design discussion belongs there, not here.

## Nobody hand-writes a release file

A watcher polls each listing's release host, downloads the archive, computes the checksum and the sizes, reads the code dependencies out of the archive, merges them with the authored document, and commits the result.

For content the watcher does not watch, the same file arrives by pull request, and the checks re-download the archive and recompute every stamped field from the actual bytes.

## Clients do not read this repository

A client fetches one snapshot artifact, which merges both halves of the index plus the game release list into a single document:

```text
https://ksamodding.github.io/content-index-releases/v1/index.json
```

## Layout

| Path | Contents |
|---|---|
| `releases/<id>/<version>.json` | One stamped document per release. |
| `game-versions.json` | Every KSA production build we know of, ordered by revision. |

## The game release list

`game-versions.json` is what an authored month bound such as `2026.7` resolves against, and the snapshot ships it so a client needs no separate request.

It is seeded from `Content/Versions/`, the dated history every installed copy of the game already carries, and kept current by an hourly poll of the master server.

That poll only ever sees the build that is current when it runs, so a build superseded within the hour can be missing from it. The copy on your own disk stays the complete source.

## The watcher

`.github/workflows/watcher.yml` runs every ten minutes as the org App.
Each tick asks every listing's authority host for its releases and stamps every release that appeared after the newest one already stamped, so a patch for an older line, tagged after a newer version exists, is stamped too.

A listing's first tick stamps its newest release only, and its back catalogue stays unstamped.

There is no queue. What is stamped here is the whole of the watcher's state, which is why a tick GitHub delays, drops or cancels costs latency and not data, and why a re-run stamps nothing twice.

| Tool | What it does |
|---|---|
| `tools/stamp_release.py` | Authored document plus release archive in, release file out. The one place a release file is derived, shared with the release pull request checks so the two paths cannot disagree. Needs no token. |
| `tools/hosts.py` | The release hosts, GitHub and SpaceDock, behind one interface. GitHub is polled conditionally against a stored ETag, so an unchanged listing costs no rate limit at all. |
| `tools/watch.py` | One tick: scan, stamp, commit, append a mirror that only appeared later, keep one error issue per listing current on the authored repository, and sweep its open pull requests. |
| `tools/verify_examples.py` | Re-derives the design repository's hand-stamped `examples/` from their release hosts and diffs. |

A release the watcher cannot stamp, a tag that does not parse or an archive whose install root is neither derivable nor authored, becomes one open issue per listing on the authored repository, kept current rather than reopened every tick.

A version is stamped exactly once. A tag that reappears with different bytes is rejected and never overwritten, and both hashes are named in that issue.

`download.mirrors` is the one field the watcher may append to after publish, and only after downloading the other host's archive and finding it byte-identical.

An authored `game_max` naming a month that was still running at stamp time is stamped with no upper bound, and a later tick resolves and adds the bound once the month completes.

To run a tick by hand, dispatch the workflow: `listing` narrows it to one id, and `dry_run` derives everything and writes nothing. Locally, against a checkout of the authored half:

```text
python3 tools/watch.py --authored ../content-index --dry-run
```

## A published release is immutable

Identity, the version, the download and the install data never change.
A release that turns out to be broken is yanked, or superseded by a new version.

The one narrow exception is an amendment, which can only make a release claim less than it claimed before: a yank, a tightened game or dependency bound, or a dependency entry that was missing.

A release file can never become more permissive after publish, and a check enforces that mechanically.

## License

Metadata is dedicated to the public domain under [CC0 1.0](LICENSE).
That means `releases/`, the game release list, and the published snapshot.

A mirror, a client, or a website can therefore copy and re-serve the whole index with no conditions attached, which is the point.

Code and configuration is licensed under [MIT](LICENSE-MIT).
That means `.github/` and any tooling.
