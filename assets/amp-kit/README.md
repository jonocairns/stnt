# Reviewed Amp kit

> This kit adapts Docker's Apache-2.0-licensed `sbx-kits-contrib` Amp kit at
> commit `ef2cc23ae634498161404953796f531bd540c8ad`. Stnt has modified both
> `spec.yaml` and this documentation. See the repository `LICENSE` and `NOTICE`
> files for licensing and attribution.

The original kit was reviewed on 2026-08-11, then migrated to Docker's
schema-v2 grammar after the upstream Amp kit migration:

- `amp/spec.yaml`
- `amp/README.md`
- Docker's “Build your own agent kit” tutorial

The kit retains the sample's `shell-docker` base image, narrow `ampcode.com`
proxy-auth mapping, `*.ampcode.com` network allowance, and `amp` entrypoint.
Stnt supplies `--dangerously-allow-all --hide-welcome` at runtime. The Amp
release is pinned through the installer's supported `AMP_VERSION` variable.

Reviewed repository profiles generate content-addressed variants of this kit.
An explicitly approved GitHub profile adds only `github.com:443` and Docker's
schema-v2 `github` credential injection for Git-over-HTTPS Basic auth. No token
or placeholder value is written into this kit or its generated variants. As a
third-party schema-v2 kit, it also requires the user's separate `amp` /
`apiKey` binding approval for exactly `ampcode.com` and `github` / `apiKey`
approval for exactly `github.com`.

Docker clone mode mirrors the host repository's absolute path inside the VM.
The kit context therefore tells Amp to identify the workspace as a
sandbox-private clone rather than the host checkout whenever it reports its
location.

Remaining mutable inputs are the base-image tag and the live installer script.
The installer verifies the downloaded Amp binary against the release SHA-256,
but the installer itself is not vendored or checksummed. These are spike
limitations, not acceptable final supply-chain controls.

The kit must be passed as this local directory. Do not use the README's mutable
`git+https://github.com/docker/sbx-kits-contrib.git#dir=amp` runtime reference.
