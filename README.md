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
