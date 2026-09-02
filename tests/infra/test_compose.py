"""docker-compose.yml -- the local Postgres that backs per-tenant schema isolation (B3).

The Docker daemon is not available on the development box, so these are static assertions
about the file. They are not a substitute for running it: the CI `postgres-ddl` job applies
the same init scripts to a real postgres:16 service container and proves the behaviour.
What this module can prove is that the *composition* is safe by construction -- no default
password, no interface but loopback, an init directory the container cannot write back to,
no privileged container -- because those are properties of the text, not of a running daemon.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker-compose.yml"

TEXT = COMPOSE.read_text(encoding="utf-8")
# The header comment discusses `privileged` and `ssl=off` in order to say they are not
# used, so text-level checks run over directives only, never over prose.
DIRECTIVES = "\n".join(line for line in TEXT.splitlines() if not line.lstrip().startswith("#"))
DOCUMENT = yaml.safe_load(TEXT)
SERVICES: dict[str, dict] = DOCUMENT["services"]


def _service(name: str = "postgres") -> dict:
    assert name in SERVICES, f"no {name} service in docker-compose.yml"
    return SERVICES[name]


def test_compose_file_parses_and_declares_a_project_name() -> None:
    assert DOCUMENT["name"] == "mizan", "an explicit project name keeps volumes off other projects"
    assert SERVICES, "no services declared"


def test_postgres_password_is_required_from_the_environment() -> None:
    """`:?` is the required-variable form: compose refuses to start when it is unset.

    `:-` would supply a default, and a default database password is a credential
    committed to the repository with extra steps.
    """
    value = _service()["environment"]["POSTGRES_PASSWORD"]
    assert isinstance(value, str)
    assert value.startswith("${POSTGRES_PASSWORD:?"), (
        f"POSTGRES_PASSWORD must use the ${{VAR:?message}} required form, got {value!r}"
    )
    assert ":-" not in value.split(":?", 1)[0], "no default may be supplied for the password"


def test_no_environment_value_is_a_literal_credential() -> None:
    for name, service in SERVICES.items():
        for key, value in (service.get("environment") or {}).items():
            if "PASSWORD" in key.upper() or "SECRET" in key.upper() or key.upper().endswith("KEY"):
                assert isinstance(value, str) and value.startswith("${"), (
                    f"{name}.{key} is a literal value in a committed file: {value!r}"
                )


def test_every_published_port_binds_to_loopback_only() -> None:
    for name, service in SERVICES.items():
        for published in service.get("ports", []):
            assert isinstance(published, str), (
                f"{name}: use the string short form so the interface is explicit"
            )
            assert published.startswith("127.0.0.1:"), (
                f"{name} publishes {published!r}; without an explicit interface Docker binds 0.0.0.0 "
                f"and punches through the host firewall"
            )


def test_the_init_directory_is_mounted_read_only() -> None:
    mounts = [m for m in _service().get("volumes", []) if isinstance(m, str)]
    init = [m for m in mounts if "docker-entrypoint-initdb.d" in m]
    assert init, "infra/postgres/init is not mounted into the container"
    for mount in init:
        assert mount.endswith(":ro"), (
            f"{mount!r} is writable: the DDL that enforces A2 and B3 must not be editable "
            f"from inside the container it constrains"
        )
        assert mount.startswith("./infra/postgres/init:"), mount
    assert (REPO_ROOT / "infra" / "postgres" / "init").is_dir()


def test_no_service_is_privileged_or_escalates() -> None:
    for name, service in SERVICES.items():
        assert not service.get("privileged"), f"{name} is privileged"
        assert not service.get("cap_add"), f"{name} adds capabilities: {service.get('cap_add')}"
        assert "no-new-privileges:true" in (service.get("security_opt") or []), (
            f"{name} does not set no-new-privileges"
        )
    assert "privileged" not in DIRECTIVES


def test_postgres_has_a_healthcheck() -> None:
    healthcheck = _service().get("healthcheck")
    assert healthcheck, "without a healthcheck `docker compose up --wait` returns before Postgres is ready"
    assert healthcheck["test"], "the healthcheck has no test"
    assert "pg_isready" in " ".join(healthcheck["test"])
    assert healthcheck.get("retries", 0) >= 3


def test_the_image_is_pinned_to_a_major_version() -> None:
    image = _service()["image"]
    assert image.startswith("postgres:"), image
    tag = image.split(":", 1)[1]
    assert tag != "latest" and tag[0].isdigit(), f"{image!r} is not pinned"


@pytest.mark.parametrize("substring", ["ssl=off", "trust", "POSTGRES_HOST_AUTH_METHOD"])
def test_authentication_is_never_disabled(substring: str) -> None:
    assert substring not in DIRECTIVES, f"{substring!r} would disable password authentication"


def test_scram_authentication_and_checksums_are_requested() -> None:
    args = _service()["environment"]["POSTGRES_INITDB_ARGS"]
    assert "--data-checksums" in args
    assert "scram-sha-256" in args


def test_data_lives_in_a_named_volume_not_a_bind_mount() -> None:
    mounts = [m for m in _service().get("volumes", []) if isinstance(m, str)]
    data = [m for m in mounts if m.endswith(":/var/lib/postgresql/data")]
    assert len(data) == 1, mounts
    volume = data[0].split(":", 1)[0]
    assert not volume.startswith("."), "the data directory is a bind mount into the working tree"
    assert volume in DOCUMENT["volumes"], f"{volume} is not declared under top-level volumes"
