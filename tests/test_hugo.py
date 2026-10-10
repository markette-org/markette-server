import subprocess
from pathlib import Path

import pytest

from apps.channels.models import Channel
from apps.channels.providers import Content, MediaItem
from apps.channels.providers.hugo import HugoProvider


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def remote(tmp_path):
    bare = tmp_path / "site.git"
    git("init", "--bare", str(bare))
    git("symbolic-ref", "HEAD", "refs/heads/main", cwd=bare)
    work = tmp_path / "seed"
    git("clone", str(bare), str(work))
    (work / "hugo.toml").write_text('title = "Site"\n')
    git("add", ".", cwd=work)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init", cwd=work)
    git("push", "origin", "HEAD:main", cwd=work)
    return bare


def test_hugo_publishes_page_bundle(workspace, remote, tmp_path):
    cand = HugoProvider().connect_with_credentials(
        {
            "repo_url": f"file://{remote}",
            "branch": "main",
            "content_dir": "content/blog",
            "site_url": "https://site.example",
            "permalink": "/blog/{slug}/",
        }
    )[0]
    ch = Channel.create_for(
        workspace=workspace,
        provider="hugo",
        external_id=cand.external_id,
        name=cand.name,
        base_url=cand.base_url,
        credentials=cand.credentials,
        config=cand.config,
    )
    img = tmp_path / "Photo One.PNG"
    img.write_bytes(b"\x89PNG fake")
    content = Content(
        text="Hello **Hugo**\n\n![pic](https://api.example/media/x.png)",
        title="Hello, Hugo: “quotes”",
        tags=["news"],
        media=[MediaItem(path=img, url="https://api.example/media/x.png", mime="image/png", filename="Photo One.PNG")],
    )
    res = HugoProvider(ch).publish(content)
    assert res.url == "https://site.example/blog/hello-hugo-quotes/"
    check = tmp_path / "check"
    git("clone", str(remote), str(check))
    index = (check / res.external_id / "index.md").read_text()
    assert index.startswith('---\ntitle: "Hello, Hugo: “quotes”"')
    assert 'tags: ["news"]' in index and "![pic](photo-one.png)" in index
    assert (check / res.external_id / "photo-one.png").exists()
    # Same title again gets a unique slug.
    res2 = HugoProvider(ch).publish(Content(text="Again", title="Hello, Hugo: “quotes”"))
    assert res2.external_id.endswith("hello-hugo-quotes-2")
    HugoProvider(ch).delete(res.external_id)
    git("pull", cwd=check)
    assert not Path(check / res.external_id).exists()
