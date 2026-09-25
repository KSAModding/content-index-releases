# Contributing

Almost nothing here needs a human, and design discussion belongs in [content-manager-design](https://github.com/KSAModding/content-manager-design/discussions).

There are few reasons to open a pull request.
A pull request is labelled `release` or `amendment` once the checks finish, which says which of the two shapes it has, and one that holds both carries both labels.

## An amendment

An amendment records something learned after a release was published.
Anyone may narrow what a release claims:

- a yank, with an optional reason
- adding or lowering `game_max`, or raising `game_min`
- tightening a dependency or loader bound
- adding a dependency entry that was missing, including a conflict

The verified owner of the listing may also widen it (RFC 0079):

- lowering `game_min`, or raising or removing `game_max`
- adding, changing or removing `os`
- loosening or removing a loader or dependency bound, while the loader stays the same
- removing a dependency entry, or changing its kind
- taking back a yank

A dependency that the archive's `mod.toml` declares can be tightened and can change its kind, but it stays in the release, because the loader acts on it.
The check reads the archive to know which dependencies those are.

Identity, the version, the download, the install data and `changelog_text` are never amendments.
The way forward for those is a new version, or a yank.

One amendment may touch several release files of one listing, which is what makes "this mod breaks above game build X" a single pull request instead of one per past release.

You may amend a listing you are the verified owner of, through the same ownership check the authored repository uses.
A steward may narrow any.
A steward, or anyone else, who widens a release for its owner puts the owner's request into the pull request description as one line, `Requested by the author: <link>`, and a steward merges it after reading that request.
A widening without the owner and without that line fails.

### Making an amendment

There are three ways to make the edit.

- **By hand.** Open the release file, make the change, and open a pull request. Fine for a one-line yank.
- **With `tools/amend.py`.** You name the change and it writes the files, and it measures its own output against the same invariant the pull request is measured by.
- **From a content manager.** A manager such as [Borea](https://github.com/KSAModding/Borea) can produce the same pull request for you.

`tools/amend.py` covers the common cases:

```sh
# Breaks above game build 2026.8.19.5261, from 0.7.2 downwards.
python3 tools/amend.py --listing <id> --up-to 0.7.2 --game-max 2026.8.19.5261

# This one build is broken outright.
python3 tools/amend.py --listing <id> --version 0.7.2 --yank --reason "It corrupts saves."

# It works on a newer game build after all. Only the owner widens.
python3 tools/amend.py --listing <id> --version 0.7.2 --game-max 2026.9.10.5438 --owner

# It needs a newer loader than it was stamped with.
python3 tools/amend.py --listing <id> --all --loader-min 0.4.6

# A dependency it never declared, and a version of it that conflicts.
python3 tools/amend.py --listing <id> --all --add-dependency SomeMod:conflict --dependency-max SomeMod=1.2.0
```

`--dry-run` writes nothing.
`--up-to` selects by SemVer precedence, so `0.10.0` is above `0.9.0`, and `--version` is repeatable when the set is not a range.

Whichever way you make it, measure it the way the checks will before you open anything:

```sh
python3 tools/validate.py --changed releases/<id>/<version>.json --base-ref main
```

Open a pull request with those files and nothing else.
When it validates and your ownership of the listing verifies, it merges itself.

The authored document in [content-index](https://github.com/KSAModding/content-index) is a separate file, and an amendment does not touch it.
A bound that applies to future releases belongs there too, or the next release is stamped without it.

## A release

A listing without a `[releases]` section gets its releases by pull request, one new file per release.

`tools/stamp_release.py` derives it from the authored document and the archive, and the checks derive it again.

Write the facts of the release the way your host shows them into a small JSON file:

```json
{
  "tag": "v1.2.0",
  "release_date": "2026-09-02T09:48:05Z",
  "url": "https://example.org/downloads/MyMod-1.2.0.zip",
  "content_type": "application/zip",
  "prerelease": false,
  "changelog": "https://example.org/mymod/changes"
}
```

Then stamp, with a checkout of [content-index](https://github.com/KSAModding/content-index) beside this repository:

```sh
python3 tools/stamp_release.py --listing ../content-index/listings/<id>.toml --archive MyMod-1.2.0.zip --release release.json --out releases/<id>/1.2.0.json
```

The file is named after the stored version, which fills a short tag such as `v1.2` to `1.2.0`.

Validate it the way the checks will, which downloads the archive from the URL the file names:

```sh
python3 tools/validate.py --changed releases/<id>/1.2.0.json --base-ref main
```

Open a pull request with that one file and nothing else.
When it validates and your ownership of the listing verifies, through the repository the listing links to, it merges itself.

A version is stamped exactly once and a release that turns out to be broken is yanked, and a corrected one gets a new version.
A listing that names a release host under `[releases]` does not use this path, because the watcher stamps its releases, and a dispatch with `backfill` stamps older ones.

## Tooling and workflows

Ordinary code review applies.

## Licensing your contribution

By opening a pull request you dedicate the metadata in it to the public domain under [CC0 1.0](LICENSE), and contribute any code or configuration under [MIT](LICENSE-MIT).
