"""Guards for project files: each one protects against a mistake that happened, or was found in review."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# These guards check the repository's own files. The images leave most of them out on
# purpose (no CI or compose files inside the container), so there the guards skip with a reason.
pytestmark = pytest.mark.skipif(
    not (ROOT / "Dockerfile").exists(),
    reason="repository files are not in this image; these guards run on the repository, locally and in CI",
)


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def pins(text):
    """{package: version} for every exact pin in a requirements-style text."""
    found = {}
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z0-9_.\[\]-]+)==([^\s;#\\]+)", line)
        if match:
            name = re.sub(r"\[.*\]", "", match.group(1)).lower().replace("_", "-")
            found[name] = match.group(2)
    return found


def test_database_healthcheck_asks_over_tcp():
    # During first-time setup a temporary server answers on the local socket only. A socket check
    # turned "healthy" too early; asking 127.0.0.1 waits for the real server.
    assert "pg_isready -h 127.0.0.1" in read("docker-compose.yml")


def test_example_settings_use_127_0_0_1_not_localhost():
    # On Windows "localhost" is tried over IPv6 first, and each connection waited out its
    # 10 s limit: the test suite took 154 s instead of 6.8 s.
    example = read(".env.example")
    assert "CDSE_DB_HOST=127.0.0.1" in example
    assert "localhost" not in example.replace('not "localhost"', "").replace('for "localhost"', "")


def test_every_package_in_the_locks_is_pinned_with_hashes():
    for lock in ("requirements.lock", "requirements-dev.lock"):
        text = read(lock)
        packages = [line for line in text.splitlines() if line and not line.startswith(("#", " "))]
        assert packages, lock
        for line in packages:
            assert "==" in line and line.rstrip().endswith("\\"), f"{lock}: {line}"
        assert text.count("--hash=sha256:") >= len(packages), lock


def test_locks_hold_every_direct_requirement_at_the_same_version():
    runtime, dev = pins(read("requirements.txt")), pins(read("requirements-dev.txt"))
    assert runtime and dev, "requirements.txt and requirements-dev.txt list the direct dependencies"
    for lock, direct in (("requirements.lock", runtime), ("requirements-dev.lock", {**runtime, **dev})):
        locked = pins(read(lock))
        for name, version in direct.items():
            assert locked.get(name) == version, f"{lock} {name}: wanted {version}, locked {locked.get(name)}"
    assert "pytest" not in pins(read("requirements.lock")), "the runtime image holds no test tools"


def test_docker_and_ci_install_with_hashes():
    assert "pip install --require-hashes -r requirements.lock" in read("Dockerfile")
    assert "pip install --require-hashes -r requirements-dev.lock" in read("Dockerfile")
    assert "pip install --require-hashes -r requirements-dev.lock" in read(".github/workflows/tests.yml")


def test_images_are_pinned_by_digest():
    for name in ("Dockerfile", "docker-compose.yml", ".github/workflows/tests.yml"):
        stages = set(re.findall(r"\bAS\s+(\w+)", read(name)))  # build stages of the Dockerfile itself
        for image in re.findall(r"(?:FROM|image:)\s+(\S+)", read(name)):
            if image.startswith("cdse-stac-client") or image in stages:
                continue  # our own image or stage, built here
            assert "@sha256:" in image, f"{name}: {image}"


def test_ci_actions_are_pinned_to_a_commit_and_the_job_only_reads():
    workflow = read(".github/workflows/tests.yml")
    for action in re.findall(r"uses:\s+(\S+)", workflow):
        assert re.search(r"@[0-9a-f]{40}$", action), action
    assert "permissions:\n  contents: read" in workflow
    assert "persist-credentials: false" in workflow and "timeout-minutes:" in workflow


def test_passwords_reach_the_containers_as_secrets_not_environment():
    compose = read("docker-compose.yml")
    assert "POSTGRES_PASSWORD:" not in compose.split("secrets:\n  postgres_password")[0]
    assert "CDSE_DB_PASSWORD_FILE: /run/secrets/cdse_db_password" in compose


def test_scripts_for_the_containers_keep_linux_line_ends():
    # Cloned on Windows, the database script got Windows line ends and the Linux container could not run it.
    assert "*.sh text eol=lf" in read(".gitattributes")
    assert b"\r\n" not in (ROOT / "docker" / "initdb" / "30_client_role.sh").read_bytes()


def test_image_builds_reuse_downloaded_packages():
    dockerfile = read("Dockerfile")
    assert dockerfile.count("--mount=type=cache,target=/root/.cache/pip") == 2
    assert "PIP_NO_CACHE_DIR=1" not in dockerfile


def test_readme_names_the_docker_versions_the_compose_file_needs():
    assert "Docker Engine 25" in read("README.md")
