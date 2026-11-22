"""Getting a repository onto disk: GitHub as a zip download (no git needed), other hosts through `git clone`."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx

from . import __version__
from .jobs import Job
from .repo import safe_extract_zip

USER_AGENT = f"Perplexify/{__version__}"
MAX_ZIP_BYTES = 2 << 30

_GITHUB = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([\w.\-]+)/([\w.\-]+?)(?:\.git)?(?:/(?:tree|blob)/([^/]+)(?:/.*)?)?/?(?:[?#].*)?$")
_SHORT = re.compile(r"^([\w\-]+)/([\w.\-]+)$")
_SSH = re.compile(r"^git@github\.com:([\w.\-]+)/([\w.\-]+?)(?:\.git)?$")


@dataclass
class RepoRef:
    owner: str
    repo: str
    ref: str | None = None

    @property
    def slug(self) -> str:
        return f"{self.owner}__{self.repo}"

    @property
    def web_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}"

    def blob_url(self, rel: str) -> str:
        return f"{self.web_url}/blob/{quote(self.ref or 'HEAD')}/{quote(rel)}"


def parse_github(text: str, ref: str | None = None) -> RepoRef | None:
    """https://github.com/owner/repo[/tree/branch], git@github.com:owner/repo.git or owner/repo."""
    text = text.strip()
    for rx in (_GITHUB, _SSH, _SHORT):
        m = rx.match(text)
        if m:
            branch = (m.group(3) if rx is _GITHUB and m.lastindex and m.lastindex >= 3 else None)
            return RepoRef(m.group(1), m.group(2), (ref or "").strip() or branch)
    return None


def download_github(ref: RepoRef, dest_root: Path, token: str, job: Job) -> Path:
    """Download and unpack a repository snapshot into dest_root/<owner>__<repo>. Returns that folder."""
    dest_root.mkdir(parents=True, exist_ok=True)
    zip_path = dest_root / f"{ref.slug}.zip.part"
    if token:  # the API download works for private repositories
        url = f"https://api.github.com/repos/{ref.owner}/{ref.repo}/zipball/{quote(ref.ref or '')}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": USER_AGENT}
    else:
        url = f"https://github.com/{ref.owner}/{ref.repo}/archive/{quote(ref.ref or 'HEAD')}.zip"
        headers = {"User-Agent": USER_AGENT}
    job.update(phase="Downloading", current=0, total=0, message=f"{ref.owner}/{ref.repo}")
    try:
        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60, connect=15)) as c, \
                c.stream("GET", url, headers=headers) as r:
            if r.status_code == 404:
                raise RuntimeError("GitHub could not find that repository or branch. If it is private, add an access token.")
            if r.status_code in (401, 403):
                raise RuntimeError("GitHub refused access. The repository is private, or the token is missing or expired.")
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            written = 0
            with open(zip_path, "wb") as f:
                for block in r.iter_bytes(1 << 20):
                    job.check()
                    written += len(block)
                    if written > MAX_ZIP_BYTES:
                        raise RuntimeError("The repository is larger than 2 GB, which is too big to import.")
                    f.write(block)
                    job.update(current=written, total=total, message=f"{written / 1e6:.0f} MB")
    except httpx.HTTPError as e:
        zip_path.unlink(missing_ok=True)
        raise RuntimeError(f"Could not download from GitHub ({type(e).__name__}). Check your connection.") from e

    job.update(phase="Unpacking", message=zip_path.name)
    staging = dest_root / f"{ref.slug}.new"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        safe_extract_zip(zip_path, staging)
        inner = [p for p in staging.iterdir() if p.is_dir()]
        if len(inner) != 1:
            raise RuntimeError("The downloaded archive was not in the expected layout.")
        final = dest_root / ref.slug
        shutil.rmtree(final, ignore_errors=True)
        inner[0].rename(final)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        zip_path.unlink(missing_ok=True)
    return final


def git_clone(url: str, dest: Path, ref: str | None, job: Job) -> Path:
    """Shallow clone for hosts other than GitHub (needs git installed). Only https/ssh URLs are accepted."""
    if not re.match(r"^(https?://|ssh://|git@)[\w.@:/~+\-]+$", url):
        raise RuntimeError("Enter a repository address like https://host/owner/repo.git")
    git = shutil.which("git")
    if not git:
        raise RuntimeError("Git is not installed on this computer. For GitHub repositories no git is needed; "
                           "for other hosts, install git or use a folder you cloned yourself.")
    staging = dest.with_name(dest.name + ".new")
    shutil.rmtree(staging, ignore_errors=True)
    staging.parent.mkdir(parents=True, exist_ok=True)
    cmd = [git, "clone", "--depth", "1", "--single-branch"] + (["--branch", ref] if ref else []) + ["--", url, str(staging)]
    job.update(phase="Downloading", message=url)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, stdin=subprocess.DEVNULL,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                           creationflags=0x08000000 if os.name == "nt" else 0)
    except subprocess.TimeoutExpired as e:
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError("The clone took too long and was stopped.") from e
    if r.returncode != 0:
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError("git could not clone that repository: " + (r.stderr.strip().splitlines() or ["unknown error"])[-1])
    shutil.rmtree(staging / ".git", ignore_errors=True)
    shutil.rmtree(dest, ignore_errors=True)
    staging.rename(dest)
    return dest
