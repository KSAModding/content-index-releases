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

You may amend a listing you are the verified owner of, through the same ownership check the authored repository uses. A steward may amend any.

## Tooling and workflows

Ordinary code review applies.

## Licensing your contribution

By opening a pull request you dedicate the metadata in it to the public domain under [CC0 1.0](LICENSE), and contribute any code or configuration under [MIT](LICENSE-MIT).
