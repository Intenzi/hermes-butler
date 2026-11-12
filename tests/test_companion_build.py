"""Companion helper resolution: download first, compile only as a fallback.

Rule 17 of the plugin catalog ships source, not machine code: this repo commits
no binary and no ``.buildinfo``. The GitHub Actions workflow builds and attests
a universal helper and publishes it as a release asset; ``ensure_binary()``
downloads that one file, verifies it against the release's own SHA-256, and
installs it. Compiling the bundled Swift source stays available as the fallback
for a machine with Command Line Tools.

These tests pin the resolution order and keep every HTTP call mocked: no test
may touch the network.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import types
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from butler_core import overlay  # noqa: E402

SOURCE = b"// ButlerOverlay.swift fixture\n"
BINARY = b"\xcf\xfa\xed\xfeuniversal helper fixture\n"
URL_TEMPLATE = ("https://github.com/Intenzi/hermes-butler/releases/download/"
                "v{version}/ButlerOverlay-universal")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _layout(tmp_path, monkeypatch, *, buildinfo, executable=True,
            stub_tools=True):
    """Fake companion layout; ensure_binary() reads only these paths."""
    exe = tmp_path / "ButlerOverlay"
    exe.write_bytes(b"\xcf\xfa\xed\xfe stub")
    if executable:
        exe.chmod(0o755)
    src = tmp_path / "ButlerOverlay.swift"
    src.write_bytes(SOURCE)
    info = tmp_path / ".buildinfo"
    if buildinfo is not None:
        if isinstance(buildinfo, (str, bytes)):
            info.write_text(buildinfo, encoding="utf-8")
        else:
            info.write_text(json.dumps(buildinfo), encoding="utf-8")
    # The download-failure stamp lives in the real plugin data dir; isolate it or
    # one genuine failure in a live session silently disables every later test.
    monkeypatch.setattr(overlay, "_download_failure_stamp_path",
                        lambda: str(tmp_path / "companion-download.json"))
    # No test may reach the network by accident: the shipped URL template is a
    # live GitHub release, so every layout starts with the fetch disabled and
    # only the download tests opt back in.
    monkeypatch.setenv(overlay.NO_DOWNLOAD_ENV, "1")
    monkeypatch.setattr(overlay, "binary_path", lambda: str(exe))
    monkeypatch.setattr(overlay, "binary_source_path", lambda: str(src))
    monkeypatch.setattr(overlay, "buildinfo_path", lambda: str(info))
    if stub_tools:
        # Keep the tests off the real shutil/subprocess: any rebuild attempt
        # fails loudly instead of silently compiling the fixture source.
        monkeypatch.setattr(overlay, "shutil", types.SimpleNamespace(
            which=lambda name: pytest.fail("unexpected compiler lookup: %s" % name)))
        monkeypatch.setattr(overlay, "subprocess", types.SimpleNamespace(
            run=lambda *args, **kwargs: pytest.fail("unexpected rebuild attempt")))
    return exe, src, info


def _download_layout(tmp_path, monkeypatch, *, buildinfo=None, source=SOURCE,
                     executable=False):
    """A layout with no usable binary and the release URL configured."""
    exe, src, info = _layout(tmp_path, monkeypatch, buildinfo=buildinfo,
                             executable=executable, stub_tools=False)
    if source is None:
        src.unlink()
    (tmp_path / ".companion_url").write_text(URL_TEMPLATE + "\n", encoding="utf-8")
    monkeypatch.delenv(overlay.NO_DOWNLOAD_ENV, raising=False)
    # The real pin names the released v0.4.1 asset, which is not this fixture:
    # pin the fixture bytes here and let the mismatch test override the pin.
    monkeypatch.setattr(overlay, "EXPECTED_HELPER_SHA256", _sha(BINARY))
    manifest = tmp_path / "plugin.yaml"
    manifest.write_text("name: butler\nversion: 9.9.9\n", encoding="utf-8")
    monkeypatch.setattr(overlay, "plugin_yaml_path", lambda: str(manifest))
    monkeypatch.delenv(overlay.NO_DOWNLOAD_ENV, raising=False)
    return exe, src, info


class _Response:
    """Minimal urlopen() response: read() plus a close() the code may call."""

    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload

    def close(self):
        pass


def _mock_http(monkeypatch, assets):
    """Serve ``assets`` by URL; unknown URLs 404 and every call is recorded."""
    calls = []

    def fake_urlopen(url, timeout=None):
        calls.append((url, timeout))
        if url not in assets:
            raise OSError("404 Not Found: %s" % url)
        payload = assets[url]
        return _Response(payload if isinstance(payload, bytes) else payload.encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def _stub_compiler(monkeypatch, *, works=True):
    """Record compiler lookups and fake a swiftc run that writes its output."""
    lookups = []
    runs = []

    def fake_run(args, **kwargs):
        runs.append(list(args))
        if not works:
            Path(args[3]).write_bytes(b"half-written slice")
            raise RuntimeError("swiftc exploded")
        Path(args[3]).write_bytes(b"rebuilt companion")
        Path(args[3]).chmod(0o755)

    monkeypatch.setattr(overlay, "shutil", types.SimpleNamespace(
        which=lambda name: lookups.append(name) or (
            "/usr/bin/swiftc" if name == "swiftc" else None)))
    monkeypatch.setattr(overlay, "subprocess", types.SimpleNamespace(run=fake_run))
    return lookups, runs


# ------------------------------------------------------- present short-circuit

def test_matching_source_hash_is_present_even_when_source_mtime_is_newer(
        tmp_path, monkeypatch):
    # The checkout case: binary mtime is old, source mtime is new. Only the
    # content hash may decide freshness.
    buildinfo = {"source_sha256": _sha(SOURCE), "archs": ["arm64", "x86_64"],
                 "built_at": 1}
    exe, src, _ = _layout(tmp_path, monkeypatch, buildinfo=buildinfo)
    old = time.time() - 3600
    os.utime(exe, (old, old))
    os.utime(src, None)

    assert overlay.ensure_binary() == (True, "present")


def test_a_matching_buildinfo_never_attempts_the_download(tmp_path, monkeypatch):
    buildinfo = {"source_sha256": _sha(SOURCE), "archs": ["arm64", "x86_64"],
                 "built_at": 1}
    exe, _, _ = _download_layout(tmp_path, monkeypatch, buildinfo=buildinfo,
                                 executable=True)
    calls = _mock_http(monkeypatch, {})

    assert overlay.ensure_binary() == (True, "present")
    assert calls == [], "a fresh helper must not touch the network"
    assert exe.stat().st_mode & 0o111


def test_missing_buildinfo_trusts_the_shipped_binary(tmp_path, monkeypatch):
    # A missing sidecar must never cost a user a compiler.
    exe, _, _ = _layout(tmp_path, monkeypatch, buildinfo=None)
    assert exe.stat().st_mode & 0o111

    assert overlay.ensure_binary() == (True, "present")


def test_unreadable_buildinfo_trusts_the_shipped_binary(tmp_path, monkeypatch):
    _layout(tmp_path, monkeypatch, buildinfo="{not json")

    assert overlay.ensure_binary() == (True, "present")


# --------------------------------------------------------------- download path

def test_download_installs_the_verified_helper_and_its_buildinfo(
        tmp_path, monkeypatch):
    exe, _, info = _download_layout(tmp_path, monkeypatch)
    release_info = {"source_sha256": _sha(SOURCE),
                    "archs": ["arm64", "x86_64"], "built_at": 1}
    base = URL_TEMPLATE.format(version="9.9.9")
    release_dir = base.rsplit("/", 1)[0]
    assets = {
        base: BINARY,
        base + ".sha256": "%s  ButlerOverlay-universal\n" % _sha(BINARY),
        # What the workflow publishes: ButlerOverlay-universal.sha256 uploads
        # under its bare basename in the same release directory.
        release_dir + "/.buildinfo": json.dumps(release_info),
    }
    calls = _mock_http(monkeypatch, assets)
    lookups = []
    monkeypatch.setattr(overlay, "shutil", types.SimpleNamespace(
        which=lambda name: lookups.append(name) or None))
    monkeypatch.setattr(overlay, "subprocess", types.SimpleNamespace(
        run=lambda *a, **k: pytest.fail("a downloaded helper must not compile")))

    assert overlay.ensure_binary() == (True, "downloaded")

    assert [url for url, _ in calls] == [
        base, release_dir + "/.buildinfo"]
    assert all(timeout == overlay.DOWNLOAD_TIMEOUT for _, timeout in calls)
    assert exe.read_bytes() == BINARY
    assert exe.stat().st_mode & 0o111, "the helper must be executable"
    assert not Path(str(exe) + ".download").exists(), "no partial file remains"
    recorded = json.loads(info.read_text(encoding="utf-8"))
    # Round 17: a release asset may predate a local edit, so its row says where
    # it came from and never claims the bundled source's hash.
    assert recorded["source"] == "download"
    assert recorded["version"] == "9.9.9"
    assert "source_sha256" not in recorded
    assert recorded["archs"] == ["arm64", "x86_64"]
    assert "swiftc" not in lookups, "the download path must not need a compiler"


def test_download_falls_back_to_a_renamed_buildinfo_sibling(
        tmp_path, monkeypatch):
    # A release may publish the sidecar as ButlerOverlay-universal.buildinfo; an
    # unreadable dotfile asset beside it must not stop the probe.
    exe, _, info = _download_layout(tmp_path, monkeypatch)
    base = URL_TEMPLATE.format(version="9.9.9")
    release_dir = base.rsplit("/", 1)[0]
    release_info = {"source_sha256": "a" * 64, "archs": ["arm64"], "built_at": 2}
    calls = _mock_http(monkeypatch, {
        base: BINARY,
        base + ".sha256": _sha(BINARY),
        release_dir + "/.buildinfo": b"this is not json",
        base + ".buildinfo": json.dumps(release_info),
    })
    monkeypatch.setattr(overlay, "shutil", types.SimpleNamespace(
        which=lambda name: None))
    monkeypatch.setattr(overlay, "subprocess", types.SimpleNamespace(
        run=lambda *a, **k: pytest.fail("a downloaded helper must not compile")))

    assert overlay.ensure_binary() == (True, "downloaded")

    assert [url for url, _ in calls] == [
        base, release_dir + "/.buildinfo",
        base + ".buildinfo"]
    recorded = json.loads(info.read_text(encoding="utf-8"))
    assert recorded["source"] == "download"
    assert recorded["version"] == "9.9.9"
    assert "source_sha256" not in recorded
    assert recorded["archs"] == ["arm64"]
    assert exe.read_bytes() == BINARY


def test_download_without_a_buildinfo_asset_records_the_download_source(
        tmp_path, monkeypatch):
    exe, _, info = _download_layout(tmp_path, monkeypatch)
    base = URL_TEMPLATE.format(version="9.9.9")
    _mock_http(monkeypatch, {
        base: BINARY,
        base + ".sha256": _sha(BINARY),
    })
    monkeypatch.setattr(overlay, "shutil", types.SimpleNamespace(
        which=lambda name: None))
    monkeypatch.setattr(overlay, "subprocess", types.SimpleNamespace(
        run=lambda *a, **k: pytest.fail("a downloaded helper must not compile")))

    assert overlay.ensure_binary() == (True, "downloaded")

    recorded = json.loads(info.read_text(encoding="utf-8"))
    assert "source_sha256" not in recorded, (
        "the release asset is not the bundled checkout's compiled helper")
    assert recorded["source"] == "download"
    assert recorded["version"] == "9.9.9"
    assert isinstance(recorded["archs"], list) and recorded["archs"]
    assert isinstance(recorded["built_at"], int)


# ------------------------- downloaded-vs-local-source rows (round 17)
#
# A downloaded asset was built from the RELEASE's source. Recording the bundled
# checkout's hash for it made every later resolve say "present" and silently
# ran the released binary after a local Swift edit. `source: "download"` is
# stale in a working checkout; a released install (no .git) still trusts it.

def _checkout(tmp_path, monkeypatch):
    """Mark the fake plugin directory as a git working checkout."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(overlay, "plugin_root", lambda: str(tmp_path))


def test_a_download_row_compiles_in_a_working_checkout(tmp_path, monkeypatch):
    row = {"source": "download", "version": "9.9.9",
           "archs": ["arm64"], "built_at": 1}
    exe, src, info = _download_layout(tmp_path, monkeypatch, buildinfo=row,
                                      executable=True)
    _checkout(tmp_path, monkeypatch)
    calls = _mock_http(monkeypatch, {})
    lookups, runs = _stub_compiler(monkeypatch, works=True)

    assert overlay.ensure_binary() == (True, "compiled")

    assert calls == [], "a checkout compiles its source instead of re-downloading"
    assert "swiftc" in lookups
    assert runs, "the local source must be rebuilt"
    refreshed = json.loads(info.read_text(encoding="utf-8"))
    assert refreshed["source"] == "compiled"
    assert refreshed["source_sha256"] == _sha(SOURCE)


def test_a_stale_compiled_row_also_compiles_in_a_working_checkout(
        tmp_path, monkeypatch):
    """A local edit changes the source hash: the checkout must rebuild, not
    install the release asset for one resolve and only compile on the next."""
    stale = {"source": "compiled", "source_sha256": "0" * 64,
             "archs": ["arm64"], "built_at": 1}
    exe, src, info = _download_layout(tmp_path, monkeypatch, buildinfo=stale,
                                      executable=True)
    _checkout(tmp_path, monkeypatch)
    calls = _mock_http(monkeypatch, {})
    lookups, runs = _stub_compiler(monkeypatch, works=True)

    assert overlay.ensure_binary() == (True, "compiled")

    assert calls == [], "the release asset must not mask the edited source"
    assert "swiftc" in lookups and runs
    assert json.loads(info.read_text(encoding="utf-8"))["source"] == "compiled"


def test_a_checkout_compile_failure_reports_compile_failed(
        tmp_path, monkeypatch):
    row = {"source": "download", "version": "9.9.9",
           "archs": ["arm64"], "built_at": 1}
    exe, _, _ = _download_layout(tmp_path, monkeypatch, buildinfo=row,
                                 executable=True)
    _checkout(tmp_path, monkeypatch)
    calls = _mock_http(monkeypatch, {})
    _stub_compiler(monkeypatch, works=False)

    ok, detail = overlay.ensure_binary()

    assert ok is False
    assert detail.startswith("compile_failed:")
    assert calls == [], "no download is attempted when the checkout compiles"


def test_a_download_row_stays_present_without_a_git_checkout(
        tmp_path, monkeypatch):
    row = {"source": "download", "version": "9.9.9",
           "archs": ["arm64"], "built_at": 1}
    exe, _, _ = _download_layout(tmp_path, monkeypatch, buildinfo=row,
                                 executable=True)
    # A released install has no .git: the verified asset stays in use, exactly
    # as before this round, and no compiler is needed.
    monkeypatch.setattr(overlay, "plugin_root", lambda: str(tmp_path))
    calls = _mock_http(monkeypatch, {})
    monkeypatch.setattr(overlay, "shutil", types.SimpleNamespace(
        which=lambda name: pytest.fail("a released install must not compile")))
    monkeypatch.setattr(overlay, "subprocess", types.SimpleNamespace(
        run=lambda *a, **k: pytest.fail("a released install must not compile")))

    assert overlay.ensure_binary() == (True, "present")
    assert calls == [], "a present asset must not touch the network"


def test_a_locally_compiled_matching_row_stays_present(tmp_path, monkeypatch):
    buildinfo = {"source": "compiled", "source_sha256": _sha(SOURCE),
                 "archs": ["arm64"], "built_at": 1}
    _layout(tmp_path, monkeypatch, buildinfo=buildinfo)
    _checkout(tmp_path, monkeypatch)

    assert overlay.ensure_binary() == (True, "present")


def test_a_digest_mismatch_removes_the_partial_file_and_falls_through(
        tmp_path, monkeypatch):
    # The blocker this pins: a re-uploaded asset carries its own matching
    # .sha256, so that asset agreeing with the payload proves nothing. Only the
    # digest committed in the tree may decide.
    evil = b"\xcf\xfa\xed\xfere-uploaded helper\n"
    exe, _, _ = _download_layout(tmp_path, monkeypatch)
    base = URL_TEMPLATE.format(version="9.9.9")
    _mock_http(monkeypatch, {
        base: evil,
        base + ".sha256": _sha(evil),
    })
    lookups, runs = _stub_compiler(monkeypatch, works=False)

    ok, detail = overlay.ensure_binary()

    assert ok is False
    assert detail.startswith("download_failed:")
    assert "sha256 mismatch" in detail
    assert not Path(str(exe) + ".download").exists(), "the partial file is removed"
    assert exe.read_bytes() != evil, "an unverified binary is never installed"
    # The mismatch falls through to the compile step, which also cannot rescue
    # this machine; the reported reason stays the primary (download) failure.
    assert "swiftc" in lookups
    assert runs, "the code must fall through to the compile step"
    assert not Path(str(exe) + ".new").exists()


def test_the_release_checksum_is_never_consulted(tmp_path, monkeypatch):
    # The release's own .sha256 travels beside the binary, so a re-upload can
    # replace both. It must not be fetched: the pinned digest is the only
    # decision, which means exactly one request -- the asset itself.
    exe, _, _ = _download_layout(tmp_path, monkeypatch)
    base = URL_TEMPLATE.format(version="9.9.9")
    calls = _mock_http(monkeypatch, {
        base: BINARY,
        base + ".sha256": _sha(b"some other binary entirely"),
    })
    monkeypatch.setattr(overlay, "shutil", types.SimpleNamespace(
        which=lambda name: None))
    monkeypatch.setattr(overlay, "subprocess", types.SimpleNamespace(
        run=lambda *a, **k: pytest.fail("a pinned helper must not compile")))

    assert overlay.ensure_binary() == (True, "downloaded")
    urls = [url for url, _ in calls]
    assert urls[0] == base, "the asset is fetched first"
    assert not any(url.endswith(".sha256") for url in urls), "no checksum sidecar request"
    assert exe.read_bytes() == BINARY


def test_a_pinned_helper_digest_is_a_lowercase_sha256():
    digest = overlay.EXPECTED_HELPER_SHA256

    assert len(digest) == 64
    assert digest == digest.lower()
    assert all(char in "0123456789abcdef" for char in digest)
    assert digest == (
        "45da2cf2f839420e9ba0caba2bd797c034a56c82c04614f27769219ae43730af")


def test_download_failure_with_nothing_to_compile_is_helper_unavailable(
        tmp_path, monkeypatch):
    _download_layout(tmp_path, monkeypatch, source=None)
    _mock_http(monkeypatch, {})  # neither the checksum nor the asset is reachable
    monkeypatch.setattr(overlay, "shutil", types.SimpleNamespace(
        which=lambda name: None))
    monkeypatch.setattr(overlay, "subprocess", types.SimpleNamespace(
        run=lambda *a, **k: pytest.fail("there is no source to compile")))

    assert overlay.ensure_binary() == (False, "helper-unavailable")


def test_the_download_does_not_depend_on_a_sidecar_asset(
        tmp_path, monkeypatch):
    # The pin is in the tree, so a release with no .sha256 beside it is still
    # installable -- and still verified against the pinned digest.
    exe, _, _ = _download_layout(tmp_path, monkeypatch)
    base = URL_TEMPLATE.format(version="9.9.9")
    calls = _mock_http(monkeypatch, {base: BINARY})
    _stub_compiler(monkeypatch, works=False)

    assert overlay.ensure_binary() == (True, "downloaded")

    urls = [url for url, _ in calls]
    assert urls[0] == base
    assert not any(url.endswith(".sha256") for url in urls), "the sidecar is never requested"
    assert exe.read_bytes() == BINARY, "the pinned digest verified the payload"


def test_butler_no_download_refuses_the_network_and_compiles(
        tmp_path, monkeypatch):
    exe, _, _ = _download_layout(tmp_path, monkeypatch)
    monkeypatch.setenv(overlay.NO_DOWNLOAD_ENV, "1")
    calls = _mock_http(monkeypatch, {})
    lookups, runs = _stub_compiler(monkeypatch, works=True)

    assert overlay.ensure_binary() == (True, "compiled")

    assert calls == [], "BUTLER_NO_DOWNLOAD must make no HTTP request at all"
    assert "swiftc" in lookups
    assert len(runs) == 1
    assert exe.read_bytes() == b"rebuilt companion"


# ------------------------------------------------------------- compile fallback

def test_hash_mismatch_attempts_a_rebuild_and_needs_swiftc(tmp_path, monkeypatch):
    buildinfo = {"source_sha256": "0" * 64, "archs": ["arm64"], "built_at": 1}
    _, _, _ = _layout(tmp_path, monkeypatch, buildinfo=buildinfo)
    lookups = []
    monkeypatch.setattr(overlay, "shutil", types.SimpleNamespace(
        which=lambda name: lookups.append(name) or None))

    # A mismatch with no download configured is the case that must go looking
    # for a compiler.
    assert overlay.ensure_binary() == (False, "swiftc_not_found")
    assert lookups == ["swiftc"]


def test_non_executable_binary_needs_a_rebuild(tmp_path, monkeypatch):
    buildinfo = {"source_sha256": _sha(SOURCE), "archs": ["arm64"], "built_at": 1}
    _layout(tmp_path, monkeypatch, buildinfo=buildinfo, executable=False)
    monkeypatch.setattr(overlay, "shutil", types.SimpleNamespace(
        which=lambda name: None))

    assert overlay.ensure_binary() == (False, "swiftc_not_found")


def test_successful_rebuild_swaps_atomically_and_refreshes_buildinfo(
        tmp_path, monkeypatch):
    buildinfo = {"source_sha256": "0" * 64, "archs": ["arm64"], "built_at": 1}
    exe, _, info = _layout(tmp_path, monkeypatch, buildinfo=buildinfo)
    seen = []

    def fake_run(args, **kwargs):
        seen.append(list(args))
        # swiftc writes the requested output; the real binary must not be the
        # file the compiler touches.
        Path(args[3]).write_bytes(b"rebuilt companion")
        Path(args[3]).chmod(0o755)

    monkeypatch.setattr(overlay, "shutil", types.SimpleNamespace(
        which=lambda name: "/usr/bin/swiftc" if name == "swiftc" else None))
    monkeypatch.setattr(overlay, "subprocess", types.SimpleNamespace(run=fake_run))

    assert overlay.ensure_binary() == (True, "compiled")
    assert seen[0][:3] == ["swiftc", "-O", "-o"]
    assert seen[0][3] == str(exe) + ".new"
    assert seen[0][4] == str(tmp_path / "ButlerOverlay.swift")
    assert exe.read_bytes() == b"rebuilt companion"
    refreshed = json.loads(info.read_text(encoding="utf-8"))
    assert refreshed["source_sha256"] == _sha(SOURCE)
    assert isinstance(refreshed["archs"], list) and refreshed["archs"]
    assert isinstance(refreshed["built_at"], int)

    # The rewritten sidecar makes the next run a no-op: no compiler lookup.
    monkeypatch.setattr(overlay, "shutil", types.SimpleNamespace(
        which=lambda name: pytest.fail("now-fresh binary must not compile")))
    assert overlay.ensure_binary() == (True, "present")


def test_failed_rebuild_leaves_the_working_binary_untouched(tmp_path, monkeypatch):
    buildinfo = {"source_sha256": "0" * 64, "archs": ["arm64"], "built_at": 1}
    exe, _, info = _layout(tmp_path, monkeypatch, buildinfo=buildinfo)
    before_binary = exe.read_bytes()
    before_info = info.read_text(encoding="utf-8")

    def exploding_run(args, **kwargs):
        Path(args[3]).write_bytes(b"half-written slice")
        raise RuntimeError("swiftc exploded")

    monkeypatch.setattr(overlay, "shutil", types.SimpleNamespace(
        which=lambda name: "/usr/bin/swiftc"))
    monkeypatch.setattr(overlay, "subprocess", types.SimpleNamespace(run=exploding_run))

    ok, detail = overlay.ensure_binary()

    assert ok is False
    assert detail.startswith("compile_failed:")
    assert exe.read_bytes() == before_binary
    assert info.read_text(encoding="utf-8") == before_info
    assert not Path(str(exe) + ".new").exists()


def test_nothing_available_reports_helper_unavailable(tmp_path, monkeypatch):
    _download_layout(tmp_path, monkeypatch, source=None)
    # The download is refused and the source is gone too, leaving nothing to
    # compile or install.
    monkeypatch.setenv(overlay.NO_DOWNLOAD_ENV, "1")
    calls = _mock_http(monkeypatch, {})
    monkeypatch.setattr(overlay, "shutil", types.SimpleNamespace(
        which=lambda name: None))
    monkeypatch.setattr(overlay, "subprocess", types.SimpleNamespace(
        run=lambda *a, **k: pytest.fail("there is no source to compile")))

    assert overlay.ensure_binary() == (False, "helper-unavailable")
    assert calls == []


# ------------------------------------------------------ repo disclosure surface

def test_no_machine_code_is_tracked():
    """Catalog rule 17: the tree carries source, not a signed/attested binary.

    Asking git (not the filesystem) keeps a developer's local build from
    turning this test red.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "butler_core/services"],
            capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    tracked = proc.stdout.split()

    assert "butler_core/services/ButlerOverlay" not in tracked
    assert "ButlerOverlay-universal.sha256" not in tracked
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "butler_core/services/ButlerOverlay" in ignore
    assert ".buildinfo" in ignore


def test_the_companion_url_template_names_this_repos_versioned_release():
    template = (ROOT / "butler_core" / "services" / ".companion_url").read_text(
        encoding="utf-8").strip()

    assert template == URL_TEMPLATE
    assert "{version}" in template
    assert "github.com/Intenzi/hermes-butler/releases/download/" in template


def test_download_url_substitutes_the_manifest_version():
    version = overlay.plugin_version()

    assert version
    assert overlay.download_url() == URL_TEMPLATE.format(version=version)


def test_the_release_workflow_publishes_an_attested_universal_helper():
    workflow = (ROOT / ".github" / "workflows" / "companion.yml").read_text(
        encoding="utf-8")

    # Triggered by a version tag (the release name) and by hand.
    assert "tags:" in workflow
    assert "'v*'" in workflow
    assert "workflow_dispatch:" in workflow
    assert "runs-on: macos-latest" in workflow
    # A release needs write access; the attestation needs the OIDC pair.
    for permission in ("contents: write", "id-token: write",
                       "attestations: write"):
        assert permission in workflow
    assert "bash scripts/build-companion.sh" in workflow
    # The release tag and the plugin.yaml version must agree, or the plugin's
    # download URL (v{version}) would point at a release that does not exist.
    assert "Check the tag matches plugin.yaml" in workflow
    assert '[ "$GITHUB_REF_NAME" != "v$version" ]' in workflow
    assert "lipo -archs" in workflow
    assert "arm64" in workflow and "x86_64" in workflow
    assert "shasum -a 256" in workflow
    assert "actions/attest-build-provenance@v2" in workflow
    assert 'gh release create "$GITHUB_REF_NAME" --generate-notes' in workflow
    for asset in ("ButlerOverlay-universal", "ButlerOverlay-universal.sha256",
                  "ButlerOverlay-universal.sha256"):
        assert asset in workflow


def test_the_workflow_checksum_covers_the_published_asset_copy():
    # The released file is a copy of the built binary; the .sha256 must be
    # computed from the same bytes the plugin verifies.
    workflow = (ROOT / ".github" / "workflows" / "companion.yml").read_text(
        encoding="utf-8")

    assert "cp butler_core/services/ButlerOverlay ButlerOverlay-universal" in workflow
    assert ("shasum -a 256 butler_core/services/ButlerOverlay > "
            "ButlerOverlay-universal.sha256") in workflow


def test_the_attested_asset_is_documented_as_universal():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "universal arm64+x86_64" in readme
    assert "attest-build-provenance" in readme
    assert "BUTLER_NO_DOWNLOAD=1" in readme


def test_build_script_keeps_the_dev_loop_fast():
    """The universal build is ~90 s because x86_64 codegen on an arm64 host is
    slow; a dev loop must not pay it. The script defaults to native, can skip a
    no-op rebuild, and keeps a per-arch module cache so compiles stay warm."""
    script = (ROOT / "scripts" / "build-companion.sh").read_text(encoding="utf-8")

    assert 'MODE="native"' in script, "native is the default"
    assert "--universal" in script and "--native" in script and "--force" in script
    assert "up to date for" in script, "a matching sidecar must short-circuit"
    assert "CACHE_ROOT" in script and "module-cache-$arch" in script, \
        "per-arch persistent module caches (a cold cache is 6x slower)"
    assert "Library/Caches/butler/companion" in script

    workflow = (ROOT / ".github" / "workflows" / "companion.yml").read_text(encoding="utf-8")
    assert "build-companion.sh --universal --force" in workflow, \
        "CI is the only caller that must produce a universal binary"
