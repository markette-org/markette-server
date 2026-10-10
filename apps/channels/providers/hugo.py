"""Hugo sites published through git.

Each post becomes a page bundle ``<content_dir>/<slug>/index.md`` with its
images next to it; Markette commits and pushes to the configured branch and the
site's own CI (GitHub Actions, GitLab CI, Netlify, Cloudflare Pages…) builds it.
"""

import base64
import fcntl
import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings
from django.utils.text import slugify

from .base import BaseProvider, Candidate, Capabilities, CredentialField, ProviderError, PublishResult

GIT_TIMEOUT = 180


def _yaml_scalar(value):
    # JSON strings are valid YAML scalars and handle all escaping for us.
    return json.dumps(value, ensure_ascii=False)


def front_matter(fields, fmt):
    if fmt == "toml":
        lines = ["+++"]
        for k, v in fields.items():
            lines.append(f"{k} = {json.dumps(v, ensure_ascii=False)}")
        lines.append("+++")
        return "\n".join(lines)
    lines = ["---"]
    for k, v in fields.items():
        if isinstance(v, list):
            lines.append(f"{k}: [{', '.join(_yaml_scalar(x) for x in v)}]")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        else:
            lines.append(f"{k}: {_yaml_scalar(v)}")
    lines.append("---")
    return "\n".join(lines)


def safe_filename(name, index):
    stem, _, ext = (name or f"image-{index}").rpartition(".")
    stem = slugify(stem or name) or f"image-{index}"
    ext = re.sub(r"[^a-z0-9]", "", ext.lower())[:5]
    return f"{stem}.{ext}" if ext else stem


class HugoProvider(BaseProvider):
    key = "hugo"
    label = "Hugo (git)"
    capabilities = Capabilities(
        kind="blog",
        max_chars=None,
        max_media=30,
        supports_title=True,
        has_metrics=False,
        metric_names=(),
        auth="credentials",
        instance_url=True,
    )
    credential_fields = [
        CredentialField(
            "repo_url",
            "Repository URL",
            type="url",
            help="HTTPS (https://github.com/org/site.git) or SSH (git@github.com:org/site.git).",
        ),
        CredentialField("branch", "Branch", default="main"),
        CredentialField(
            "content_dir", "Content section", default="content/posts", help="Folder where new page bundles are created."
        ),
        CredentialField("site_url", "Public site URL", type="url", help="Used to build post links."),
        CredentialField(
            "permalink",
            "Permalink pattern",
            required=False,
            default="/posts/{slug}/",
            help="Placeholders: {slug}, {year}, {month}, {day}, {section}.",
        ),
        CredentialField("front_matter", "Front matter", type="select", default="yaml", choices=["yaml", "toml"]),
        CredentialField(
            "token_user",
            "HTTPS username",
            required=False,
            default="git",
            help="For tokens: 'x-access-token' on GitHub, 'oauth2' on GitLab.",
        ),
        CredentialField("token", "Access token (HTTPS)", type="password", required=False),
        CredentialField("ssh_key", "Deploy key (SSH private key)", type="textarea", required=False),
        CredentialField("author_name", "Commit author", required=False, default="Markette"),
        CredentialField("author_email", "Commit email", required=False, default="markette@localhost"),
    ]

    # --- connection ------------------------------------------------------
    def connect_with_credentials(self, data):
        repo = (data.get("repo_url") or "").strip()
        if not repo:
            raise ProviderError("Repository URL is required.")
        if not (data.get("token") or data.get("ssh_key")) and not repo.startswith("file://"):
            raise ProviderError("Provide an HTTPS access token or an SSH deploy key with write access.")
        creds = {k: data.get(k) or "" for k in ("token", "token_user", "ssh_key")}
        creds["token_user"] = creds["token_user"] or "git"
        if creds["ssh_key"] and not creds["ssh_key"].endswith("\n"):
            creds["ssh_key"] += "\n"
        config = {
            "repo_url": repo,
            "branch": (data.get("branch") or "main").strip(),
            "content_dir": (data.get("content_dir") or "content/posts").strip().strip("/"),
            "site_url": (data.get("site_url") or "").rstrip("/"),
            "permalink": data.get("permalink") or "/posts/{slug}/",
            "front_matter": data.get("front_matter") or "yaml",
            "author_name": data.get("author_name") or "Markette",
            "author_email": data.get("author_email") or "markette@localhost",
        }
        with self._git_env(creds, config) as git:
            out = git("ls-remote", "--heads", repo, config["branch"])
            if not out.strip():
                raise ProviderError(f"Branch {config['branch']} not found in {repo}.")
        host = urlparse(repo if "://" in repo else "ssh://" + repo.replace(":", "/", 1)).netloc
        return [
            Candidate(
                external_id=f"{repo}#{config['branch']}:{config['content_dir']}",
                name=urlparse(config["site_url"]).netloc or repo.rsplit("/", 1)[-1].removesuffix(".git"),
                handle=f"{host} · {config['branch']}",
                profile_url=config["site_url"],
                base_url=config["site_url"],
                credentials=creds,
                config=config,
            )
        ]

    # --- git plumbing ----------------------------------------------------
    @contextmanager
    def _git_env(self, creds, config, cwd=None):
        tmp = tempfile.mkdtemp(prefix="markette-git-")
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": config.get("author_name", "Markette"),
            "GIT_AUTHOR_EMAIL": config.get("author_email", "markette@localhost"),
            "GIT_COMMITTER_NAME": config.get("author_name", "Markette"),
            "GIT_COMMITTER_EMAIL": config.get("author_email", "markette@localhost"),
        }
        pre = []
        try:
            if creds.get("ssh_key"):
                key = Path(tmp, "id")
                key.write_text(creds["ssh_key"])
                key.chmod(0o600)
                known = Path(settings.HUGO_WORKDIR, "known_hosts")
                known.parent.mkdir(parents=True, exist_ok=True)
                env["GIT_SSH_COMMAND"] = (
                    f"ssh -i {key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new "
                    f"-o UserKnownHostsFile={known}"
                )
            elif creds.get("token"):
                basic = base64.b64encode(f"{creds.get('token_user') or 'git'}:{creds['token']}".encode()).decode()
                pre = ["-c", f"http.extraHeader=Authorization: Basic {basic}"]

            def git(*args, check=True):
                proc = subprocess.run(
                    ["git", *pre, *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=GIT_TIMEOUT
                )
                if check and proc.returncode != 0:
                    err = (proc.stderr or proc.stdout).strip()
                    if creds.get("token"):
                        err = err.replace(creds["token"], "***")
                    transient = any(
                        s in err
                        for s in ("Could not resolve host", "timed out", "Connection reset", "early EOF", "503", "502")
                    )
                    auth = any(s in err for s in ("Authentication failed", "Permission denied", "403", "401"))
                    raise ProviderError(f"git {args[0]} failed: {err[:800]}", retryable=transient, auth=auth)
                return proc.stdout

            yield git
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @contextmanager
    def _checkout(self):
        cfg = self.channel.config
        root = Path(settings.HUGO_WORKDIR) / f"channel-{self.channel.pk}"
        root.parent.mkdir(parents=True, exist_ok=True)
        lock_path = root.parent / f"channel-{self.channel.pk}.lock"
        with open(lock_path, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                if not (root / ".git").exists():
                    shutil.rmtree(root, ignore_errors=True)
                    with self._git_env(self.creds, cfg) as git:
                        git("clone", "--depth", "20", "--branch", cfg["branch"], cfg["repo_url"], str(root))
                with self._git_env(self.creds, cfg, cwd=root) as git:
                    git("fetch", "--depth", "20", "origin", cfg["branch"])
                    git("reset", "--hard", f"origin/{cfg['branch']}")
                    git("clean", "-fd")
                    yield root, git
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _push(self, git, branch):
        for _ in range(3):
            proc_ok = True
            try:
                git("push", "origin", f"HEAD:{branch}")
            except ProviderError as exc:
                if "non-fast-forward" not in str(exc) and "fetch first" not in str(exc) and "rejected" not in str(exc):
                    raise
                proc_ok = False
            if proc_ok:
                return
            git("pull", "--rebase", "origin", branch)
        raise ProviderError("Could not push to the Hugo repository after 3 attempts.", retryable=True)

    # --- publishing ------------------------------------------------------
    def permalink(self, slug, when):
        cfg = self.channel.config
        section = cfg.get("content_dir", "content/posts").split("/", 1)[-1]
        path = cfg.get("permalink", "/posts/{slug}/").format(
            slug=slug,
            year=f"{when:%Y}",
            month=f"{when:%m}",
            day=f"{when:%d}",
            section=section,
        )
        return f"{cfg.get('site_url', '')}{path}"

    def publish(self, content):
        cfg = self.channel.config
        opts = content.options
        now = datetime.now(UTC)
        base_slug = slugify(opts.get("slug") or content.title)[:80] or f"post-{now:%Y%m%d%H%M}"
        with self._checkout() as (root, git):
            section = root / cfg.get("content_dir", "content/posts")
            slug, n = base_slug, 1
            while (section / slug).exists():
                n += 1
                slug = f"{base_slug}-{n}"
            bundle = section / slug
            bundle.mkdir(parents=True)
            body = content.text
            images = []
            for i, m in enumerate(content.media, start=1):
                name = safe_filename(m.filename, i)
                shutil.copyfile(m.path, bundle / name)
                images.append(name)
                body = body.replace(m.url, name)
            fields = {
                "title": content.title,
                "date": now.isoformat(timespec="seconds"),
                "draft": opts.get("status") == "draft",
                "slug": slug,
            }
            if opts.get("excerpt"):
                fields["description"] = opts["excerpt"]
            tags = opts.get("tags") or content.tags
            if tags:
                fields["tags"] = tags
            if opts.get("categories"):
                fields["categories"] = opts["categories"]
            if images:
                fields["images"] = images[:1]
            (bundle / "index.md").write_text(
                f"{front_matter(fields, cfg.get('front_matter', 'yaml'))}\n\n{body.strip()}\n"
            )
            rel = bundle.relative_to(root).as_posix()
            git("add", rel)
            git("commit", "-m", f"Publish: {content.title}\n\nPublished with Markette.")
            self._push(git, cfg["branch"])
            sha = git("rev-parse", "HEAD").strip()
        return PublishResult(external_id=rel, url=self.permalink(slug, now), extra={"commit": sha})

    def delete(self, external_id):
        cfg = self.channel.config
        with self._checkout() as (root, git):
            target = (root / external_id).resolve()
            if root.resolve() not in target.parents or not target.exists():
                raise ProviderError("Page bundle not found in the repository.")
            git("rm", "-r", "--quiet", external_id)
            git("commit", "-m", f"Remove {external_id}\n\nRemoved with Markette.")
            self._push(git, cfg["branch"])
