"""Validate stable source promotion; never build, tag, push, or publish a release."""

import argparse
import base64
import hashlib
import json
import re
import subprocess
import tomllib
from pathlib import Path


RELEASE_FILES = {
    "CHANGELOG.md",
    "docs/next/CHANGELOG.md",
    "docs/next/README.md",
    "docs/next/README.zh-CN.md",
    "docs/next/product-announcement.json",
    "skills/herdr/SKILL.md",
}
ASSETS = {
    "herdr-linux-x86_64",
    "herdr-linux-aarch64",
    "herdr-macos-x86_64",
    "herdr-macos-aarch64",
    "herdr-windows-x86_64.zip",
}
PINS_FILE = "nix/pins.json"
# Nix systems and the release asset the binary flake installs on each.
NIX_ASSETS = {
    "x86_64-linux": "herdr-linux-x86_64",
    "aarch64-linux": "herdr-linux-aarch64",
    "x86_64-darwin": "herdr-macos-x86_64",
    "aarch64-darwin": "herdr-macos-aarch64",
}


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def version_tuple(version: str) -> tuple[int, ...]:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError(f"invalid stable version: {version}")
    return tuple(map(int, version.split(".")))


def resolve(ref: str) -> str:
    return git("rev-parse", "--verify", f"{ref}^{{commit}}")


def ancestor(base: str, commit: str) -> bool:
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, commit], check=False
    ).returncode == 0


def published_preview(tag: str, repo: str) -> str:
    if not re.fullmatch(r"preview-[A-Za-z0-9.-]+", tag):
        raise ValueError("select a published preview-… tag")
    commit = resolve(f"refs/tags/{tag}")
    payload = json.loads(subprocess.check_output(
        ["gh", "api", f"repos/{repo}/releases/tags/{tag}"], text=True
    ))
    if (
        payload.get("tag_name") != tag
        or payload.get("draft") is not False
        or payload.get("prerelease") is not True
        or payload.get("immutable") is not True
        or not ASSETS.issubset({asset["name"] for asset in payload.get("assets", [])})
    ):
        raise ValueError(f"{tag} must be an immutable published preview with all five assets")
    # Resolve the remote tag too: a local tag must not substitute different source.
    remote = git("ls-remote", f"https://github.com/{repo}.git", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}")
    refs = dict(line.split()[::-1] for line in remote.splitlines())
    remote_commit = refs.get(f"refs/tags/{tag}^{{}}", refs.get(f"refs/tags/{tag}"))
    if commit != remote_commit:
        raise ValueError(f"local preview tag {tag} does not match its published commit")
    return commit


def normalized_cargo(text: str, path: str) -> tuple[dict, str]:
    data = tomllib.loads(text)
    if path == "Cargo.toml":
        version = data["package"].pop("version")
    else:
        packages = [p for p in data["package"] if p["name"] == "herdr" and "source" not in p]
        if len(packages) != 1:
            raise ValueError("expected exactly one local herdr package in Cargo.lock")
        version = packages[0].pop("version")
    return data, version


def validate_diff(preview: str, candidate: str, version: str | None = None) -> None:
    if not ancestor(preview, candidate):
        raise ValueError("release must descend from the selected preview; do not rebase onto master")
    changed = git("diff", "--no-renames", "--name-only", preview, candidate).splitlines()
    for path in changed:
        if path in {"Cargo.toml", "Cargo.lock"}:
            before, _ = normalized_cargo(git("show", f"{preview}:{path}"), path)
            after, _ = normalized_cargo(git("show", f"{candidate}:{path}"), path)
            if before != after:
                raise ValueError(f"{path}: only the herdr package version may change")
        elif path not in RELEASE_FILES and not (
            path.startswith("docs/next/website/src/content/docs/")
            and path.endswith((".md", ".mdx"))
        ):
            raise ValueError(f"unpreviewed change: {path}; publish a new preview first")
        # Documentation exceptions must not turn into symlinks or submodules.
        entry = git("ls-tree", candidate, "--", path)
        if (not entry and path in RELEASE_FILES) or (entry and not entry.startswith("100644 blob ")):
            raise ValueError(f"release preparation must preserve regular files: {path}")
    versions = [normalized_cargo(git("show", f"{candidate}:{path}"), path)[1]
                for path in ("Cargo.toml", "Cargo.lock")]
    if versions[0] != versions[1] or (version is not None and versions[0] != version):
        raise ValueError("release version must match Cargo.toml and Cargo.lock")


def tag_metadata(tag: str) -> tuple[str, str]:
    if not re.fullmatch(r"(stage-)?v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise ValueError("expected a stable stage-vX.Y.Z or vX.Y.Z tag")
    if git("cat-file", "-t", f"refs/tags/{tag}") != "tag":
        raise ValueError("stable releases require an annotated tag with preview provenance")
    message = git("for-each-ref", "--format=%(contents)", f"refs/tags/{tag}")
    fields = []
    for key in ("Preview", "Previous-Stable"):
        values = re.findall(rf"^{key}: (\S+)$", message, re.MULTILINE)
        if len(values) != 1:
            raise ValueError(f"release tag requires exactly one {key}: trailer")
        fields.append(values[0])
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", fields[1]):
        raise ValueError("invalid Previous-Stable tag")
    return fields[0], fields[1]


def validate_release(
    preview_tag: str, candidate: str, version: str, previous: str, repo: str, tagged: str | None = None
) -> str:
    preview = published_preview(preview_tag, repo)
    validate_diff(preview, candidate, version)
    current = json.loads(git("show", "origin/master:distribution/latest.json"))["version"]
    if version_tuple(version) <= version_tuple(previous.removeprefix("v")):
        raise ValueError("stable version must increase from Previous-Stable")
    if current == version:
        # A retry after distribution publication must still validate the original boundary.
        if resolve(f"refs/tags/v{version}") != resolve(tagged or candidate):
            raise ValueError("published stable version points at different source")
    elif previous != f"v{current}":
        raise ValueError("Previous-Stable must name the currently published stable release")
    resolve(f"refs/tags/{previous}")
    return preview


def pins_json(version: str, assets_dir: Path) -> str:
    assets = {}
    for system, name in NIX_ASSETS.items():
        digest = hashlib.sha256((assets_dir / name).read_bytes()).digest()
        assets[system] = {"name": name, "hash": "sha256-" + base64.b64encode(digest).decode()}
    return json.dumps({"version": version, "assets": assets}, indent=2) + "\n"


def pinned_source(tag: str, version: str) -> str:
    """Return commit A for a final tag on P, where P = A + nix/pins.json only."""
    parents = git("rev-list", "--parents", "-n", "1", f"{tag}^{{commit}}").split()[1:]
    if len(parents) != 1:
        raise ValueError(f"{tag} must point at a single-parent commit that only pins the Nix binaries")
    source = parents[0]
    if git("diff", "--no-renames", "--name-only", source, tag).splitlines() != [PINS_FILE]:
        raise ValueError(f"{tag} may change only {PINS_FILE} on top of the staged commit")
    if git("ls-tree", tag, "--", PINS_FILE).split()[:2] != ["100644", "blob"]:
        raise ValueError(f"{PINS_FILE} must be a regular file")
    stage = f"stage-v{version}"
    if resolve(f"refs/tags/{stage}") != source:
        raise ValueError(f"{tag} must sit directly on the commit tagged {stage}, whose assets were staged")
    if tag_metadata(stage) != tag_metadata(tag):
        raise ValueError(f"{tag} and {stage} must name the same Preview and Previous-Stable")
    pins = json.loads(git("show", f"{tag}:{PINS_FILE}"))
    if pins.get("version") != version or {
        system: asset.get("name") for system, asset in pins.get("assets", {}).items()
    } != NIX_ASSETS:
        raise ValueError(f"{PINS_FILE} must pin the {version} assets for every Nix system")
    return source


def check_tag(tag: str, repo: str) -> tuple[str, str, str, str]:
    """Validate a stage or final tag; return preview commit, previous tag, version and source commit."""
    preview_tag, previous = tag_metadata(tag)
    version = tag.removeprefix("stage-")[1:]
    if tag.startswith("stage-"):
        if subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/v{version}"],
            check=False, capture_output=True,
        ).returncode == 0:
            raise ValueError(f"v{version} is already tagged; staging is closed")
        source = resolve(tag)
        return validate_release(preview_tag, tag, version, previous, repo), previous, version, source
    source = pinned_source(tag, version)
    return validate_release(preview_tag, source, version, previous, repo, tagged=tag), previous, version, source


def asset_digests(assets_dir: Path) -> dict[str, str]:
    return {name: hashlib.sha256((assets_dir / name).read_bytes()).hexdigest() for name in sorted(ASSETS)}


def write_stage_record(version: str, assets_dir: Path, record_dir: Path) -> None:
    """Record what CI built, so later steps never trust the editable draft release."""
    record_dir.mkdir(parents=True, exist_ok=True)
    (record_dir / "sha256.json").write_text(json.dumps(asset_digests(assets_dir), indent=2) + "\n", encoding="utf-8")
    (record_dir / "pins.json").write_text(pins_json(version, assets_dir), encoding="utf-8")


def verify_staged(assets_dir: Path, record_dir: Path) -> None:
    recorded = json.loads((record_dir / "sha256.json").read_text(encoding="utf-8"))
    if recorded != asset_digests(assets_dir):
        raise ValueError("draft release assets differ from what the stage run built")


def fetch_stage_record(repo: str, version: str, source: str, directory: Path) -> None:
    """Download the record of the successful stage-vX.Y.Z run on the staged commit."""
    stage = f"stage-v{version}"
    version_tuple(version)
    runs = json.loads(subprocess.check_output(
        ["gh", "api", f"repos/{repo}/actions/workflows/release.yml/runs"
         f"?event=push&branch={stage}&status=success&head_sha={source}&per_page=100"],
        text=True,
    ))["workflow_runs"]
    # Only an admin tag push runs release.yml for this ref at the staged commit.
    runs = [run for run in runs if run["head_branch"] == stage and run["head_sha"] == source and run["event"] == "push"]
    if not runs:
        raise ValueError(f"no successful {stage} release run at {source}")
    run_id = str(max(run["id"] for run in runs))
    subprocess.run(
        ["gh", "run", "download", run_id, "--repo", repo, "--name", "stage-record", "--dir", str(directory)],
        check=True,
    )


def fetch_assets(repo: str, tag: str, names: list[str], directory: Path) -> None:
    """Download release assets by name, including from a draft release only admins and CI can see."""
    releases = json.loads(subprocess.check_output(
        ["gh", "api", "--paginate", "--slurp", f"repos/{repo}/releases?per_page=100"], text=True
    ))
    matches = [release for page in releases for release in page if release["tag_name"] == tag]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one release for {tag}, found {len(matches)}")
    available = {asset["name"]: asset["id"] for asset in matches[0]["assets"]}
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        if name not in available:
            raise ValueError(f"release {tag} has no asset {name}")
        with (directory / name).open("wb") as output:
            subprocess.run(
                ["gh", "api", "-H", "Accept: application/octet-stream",
                 f"repos/{repo}/releases/assets/{available[name]}"],
                stdout=output, check=True,
            )


def select_hotfix(branch: str, base: str) -> str:
    if not re.fullmatch(r"release/[A-Za-z0-9][A-Za-z0-9._-]*", branch):
        raise ValueError("hotfix previews require an explicit release/* branch")
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", base):
        raise ValueError("hotfix base must be the current published stable tag")
    commit = resolve(f"refs/remotes/origin/{branch}")
    if not ancestor(resolve(f"refs/tags/{base}"), commit):
        raise ValueError(f"hotfix branch must descend from {base}")
    files = git("ls-tree", "--name-only", commit, "--", "scripts/release.py")
    if not files or "scripts/release.py check-tag" not in git("show", f"{commit}:.github/workflows/release.yml"):
        raise ValueError("hotfix source predates preview promotion; include the promotion tooling before previewing")
    return commit


def select_preview(ref: str) -> str:
    commit = resolve(ref)
    workflow = git("show", f"{commit}:.github/workflows/preview.yml")
    if not re.search(r'(?m)^on:\n  push:\n    tags:\n      - "preview-\*"$', workflow):
        raise ValueError("preview source predates tag-triggered previews; select a commit with the new publishing workflow")
    if ancestor(commit, "refs/remotes/origin/master"):
        return commit
    base = "v" + json.loads(git("show", "origin/master:distribution/latest.json"))["version"]
    branches = git("for-each-ref", "--format=%(refname:strip=3)", "refs/remotes/origin/release/")
    for branch in branches.splitlines():
        if resolve(f"refs/remotes/origin/{branch}") == commit:
            return select_hotfix(branch, base)
    raise ValueError("preview source must be on master or the tip of a published release/* hotfix branch")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("check-source")
    prepare.add_argument("--preview", required=True)
    prepare.add_argument("--commit", default="HEAD")
    prepare.add_argument("--repo", default="herdrdev/herdr")
    check = commands.add_parser("check")
    check.add_argument("--preview", required=True)
    check.add_argument("--version", required=True)
    check.add_argument("--previous", required=True)
    check.add_argument("--commit", default="HEAD")
    check.add_argument("--repo", default="herdrdev/herdr")
    tag = commands.add_parser("check-tag")
    tag.add_argument("--tag", required=True)
    tag.add_argument("--repo", default="herdrdev/herdr")
    tag.add_argument("--github-output", type=Path)
    pins = commands.add_parser("pins")
    pins.add_argument("--version", required=True)
    pins.add_argument("--assets-dir", type=Path, required=True)
    pins.add_argument("--check", type=Path, required=True)
    record = commands.add_parser("stage-record")
    record.add_argument("--version", required=True)
    record.add_argument("--assets-dir", type=Path, required=True)
    record.add_argument("--dir", type=Path, required=True)
    fetch_record = commands.add_parser("fetch-stage-record")
    fetch_record.add_argument("--version", required=True)
    fetch_record.add_argument("--commit", required=True)
    fetch_record.add_argument("--dir", type=Path, required=True)
    fetch_record.add_argument("--repo", default="herdrdev/herdr")
    verify = commands.add_parser("verify-staged")
    verify.add_argument("--assets-dir", type=Path, required=True)
    verify.add_argument("--record", type=Path, required=True)
    assets = commands.add_parser("fetch-assets")
    assets.add_argument("--tag", required=True)
    assets.add_argument("--dir", type=Path, required=True)
    assets.add_argument("--repo", default="herdrdev/herdr")
    assets.add_argument("--name", action="append", choices=sorted(ASSETS))
    preview = commands.add_parser("preview-source")
    preview.add_argument("--commit", default="HEAD")
    args = parser.parse_args()
    if args.command == "preview-source":
        print(select_preview(args.commit))
    elif args.command == "check-source":
        validate_diff(published_preview(args.preview, args.repo), args.commit)
    elif args.command == "check":
        validate_release(args.preview, args.commit, args.version, args.previous, args.repo)
    elif args.command == "pins":
        version_tuple(args.version)
        if args.check.read_text(encoding="utf-8") != pins_json(args.version, args.assets_dir):
            raise ValueError(f"{args.check} does not pin the staged {args.version} assets")
    elif args.command == "stage-record":
        version_tuple(args.version)
        write_stage_record(args.version, args.assets_dir, args.dir)
    elif args.command == "fetch-stage-record":
        fetch_stage_record(args.repo, args.version, resolve(args.commit), args.dir)
    elif args.command == "verify-staged":
        verify_staged(args.assets_dir, args.record)
    elif args.command == "fetch-assets":
        fetch_assets(args.repo, args.tag, args.name or sorted(ASSETS), args.dir)
    else:
        commit, previous, version, source = check_tag(args.tag, args.repo)
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as output:
                output.write(
                    f"preview_commit={commit}\nprevious_tag={previous}\n"
                    f"version={version}\nsource_commit={source}\n"
                )
        print(f"{args.tag} promotes {commit} as v{version} from {source}; previous stable: {previous}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, subprocess.CalledProcessError, KeyError) as error:
        raise SystemExit(f"error: {error}") from error
