# Contributing

Almost nothing here needs a human, and design discussion belongs in [content-manager-design](https://github.com/KSAModding/content-manager-design/discussions).

There are few reasons to open a pull request.

## An amendment

An amendment records something learned after a release was published, and it can only narrow what that release claims:

- a yank, with an optional reason
- adding or lowering `game_max`, or raising `game_min`
- tightening a dependency or loader bound
- adding a dependency entry that was missing, including a conflict

Widening a bound, removing an entry, and touching identity, the version, the download or the install data are never amendments.
The way forward for those is a new version, or a yank.

One amendment may touch several release files of one listing, which is what makes "this mod breaks above game build X" a single pull request instead of one per past release.

You may amend a listing you are the verified owner of, through the same ownership check the authored repository uses.
A steward may amend any.

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

## Tooling and workflows

Ordinary code review applies.

## Licensing your contribution

By opening a pull request you dedicate the metadata in it to the public domain under [CC0 1.0](LICENSE), and contribute any code or configuration under [MIT](LICENSE-MIT).
