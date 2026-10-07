# Release process

The repository uses Git branches as its only add-on release-channel boundary:

| Channel | Branch | Repository URL | Add-on version |
| --- | --- | --- | --- |
| Stable | `main` | `https://github.com/tannerln7/Petlibro-Home-Assistant` | stable `x.y.z` |
| Development | `develop` | `https://github.com/tannerln7/Petlibro-Home-Assistant#develop` | `x.y.z-beta.N` |

Each installation tracks one branch. Do not add both URLs to one Home Assistant
instance. A `develop` change is invisible to `main` users, and a `main` change
is invisible to `develop` users until it is carried forward.

## Stable releases

`main` contains only code ready for normal users. A stable release:

1. promotes code already validated on `develop` into `main`;
2. sets `addon/config.yaml` to a stable SemVer version such as `0.3.10`;
3. finalizes `addon/CHANGELOG.md`;
4. passes the repository validation suite;
5. pushes the stable commit to `main`;
6. creates and pushes the immutable matching tag, such as `v0.3.10`.

The add-on image workflow publishes both
`ghcr.io/tannerln7/ha-addon-petlibro-local:0.3.10` and `latest`. The stable
image is rebuilt from the final stable commit; a beta image is never renamed or
repurposed. GitHub Release objects are optional metadata and are not currently
required by the repository's release mechanics.

`main` must never contain a prerelease add-on version.

## Development releases

`develop` integrates changes that need physical feeder testing. Every
publishable candidate must advance the prerelease suffix in
`addon/config.yaml`, for example:

```text
0.3.10-beta.1
0.3.10-beta.2
0.3.10-beta.3
```

After tests pass, push the candidate commit and its immutable matching tag,
such as `v0.3.10-beta.1`. The workflow publishes only the exact versioned GHCR
tag. It must never publish or overwrite `latest`. GitHub Pre-release objects
are optional and are not needed for Home Assistant update notifications.

The workflow fails closed when `main` contains a prerelease version, when
`develop` contains anything other than `x.y.z-beta.N`, or when the configured
versioned image tag already exists. Increment the version instead of moving a
published tag or overwriting a published image.

## Promotion to stable

After `develop` version `x.y.z-beta.N` passes physical testing:

1. merge or otherwise promote that tested code into `main` without rewriting
   public history;
2. change the add-on version to `x.y.z` and finalize the changelog;
3. run validation and perform the stable release steps above;
4. carry the stable result back into `develop`;
5. start the next `-beta.1` version only when additional development begins.

## Stable hotfix invariant

If `main` receives a hotfix while `develop` contains newer unfinished work, the
same fix **must** immediately be merged or cherry-picked into `develop`, and
`develop` must publish a new beta version.

For example:

```text
before:  main 0.3.9,  develop 0.4.0-beta.1
hotfix:  main 0.3.10
carry:   develop 0.4.0-beta.2 with the same fix
```

This is required because development installations observe only `develop`;
they do not also consume `main` updates.

## State Agent releases

State Agent releases remain separate signed ARM artifacts published through
the `state-agent-releases` branch and signed-manifest process. They do not share
the add-on version or branch lifecycle. Follow the
[State Agent release instructions](../state-agent/README.md#signed-updates).
An add-on-only change must not create a State Agent release.
