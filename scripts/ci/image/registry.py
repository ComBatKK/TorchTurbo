"""Docker registry authentication, manifests, and alias promotion."""

from __future__ import annotations

import hashlib
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

ACCEPT = (
    "application/vnd.docker.distribution.manifest.v2+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json, "
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.oci.image.index.v1+json"
)
DOCKER_V2 = "application/vnd.docker.distribution.manifest.v2+json"
DOCKER_LIST_V2 = "application/vnd.docker.distribution.manifest.list.v2+json"


class RegistryError(RuntimeError):
    """Raised when registry state cannot be determined or changed safely."""


@dataclass(frozen=True)
class Manifest:
    reference: str
    digest: str
    media_type: str
    raw: dict[str, Any]


def verify_manifest(manifest: Manifest) -> None:
    if manifest.media_type not in {DOCKER_V2, DOCKER_LIST_V2}:
        raise RegistryError(
            "registry returned unsupported manifest media type "
            f"{manifest.media_type!r}; expected Docker v2s2"
        )


class Registry:
    def __init__(
        self,
        repository: str,
        *,
        token_fetcher: Callable[[str, str], str] | None = None,
        timeout: int = 30,
    ) -> None:
        self.repository = repository
        self.host = repository.split("/", 1)[0]
        self.repo_path = repository.split("/", 1)[1] if "/" in repository else repository
        self.timeout = timeout
        self._token_fetcher = token_fetcher or self._anonymous_token
        self._token: str | None = None

    def _anonymous_token(self, host: str, repo: str) -> str:
        url = (
            f"https://{host}/swr/auth/v2/registry/auth/"
            f"?service=dockyard&scope=repository:{repo}:pull"
        )
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                return str(json.loads(response.read().decode()).get("token", ""))
        except (urllib.error.URLError, ValueError, OSError):
            return ""

    def token(self) -> str:
        if self._token is None:
            self._token = self._token_fetcher(self.host, self.repo_path)
        return self._token

    def manifest(self, reference: str) -> Manifest | None:
        token = self.token()
        if not token:
            raise RegistryError(f"cannot obtain registry token for {self.repository}")
        encoded = urllib.parse.quote(reference, safe=":")
        request = urllib.request.Request(
            f"https://{self.host}/v2/{self.repo_path}/manifests/{encoded}",
            headers={"Authorization": f"Bearer {token}", "Accept": ACCEPT},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                raw = json.loads(body.decode() or "{}")
                digest = response.headers.get("Docker-Content-Digest", "")
                media_type = (
                    raw.get("mediaType")
                    or response.headers.get("Content-Type", "").split(";", 1)[0]
                )
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise RegistryError(
                f"registry manifest read failed for {reference}: HTTP {exc.code}"
            ) from exc
        except (urllib.error.URLError, ValueError, OSError) as exc:
            raise RegistryError(
                f"registry manifest read failed for {reference}: {exc}"
            ) from exc
        if not digest:
            digest = "sha256:" + hashlib.sha256(body).hexdigest()
        return Manifest(reference=reference, digest=digest, media_type=media_type, raw=raw)

    def require(self, reference: str) -> Manifest:
        manifest = self.manifest(reference)
        if manifest is None:
            raise RegistryError(f"registry reference does not exist: {reference}")
        return manifest

    def promote(
        self,
        alias: str,
        digest: str,
        *,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        if not digest.startswith("sha256:"):
            raise RegistryError(f"promote requires a sha256 digest, got {digest!r}")
        command = [
            "docker",
            "buildx",
            "imagetools",
            "create",
            "-t",
            f"{self.repository}:{alias}",
            f"{self.repository}@{digest}",
        ]
        result = run(command, capture_output=True, text=True)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RegistryError(f"promote failed: {detail}")
