"""Where a finished dump goes: local disk, S3, SFTP or FTP(S).

Every destination kind implements the same four operations, which is
all the runner and the restore path need:

    upload(local, name) -> uri     put one artifact there
    listing()           -> [Artifact]  what is already there, newest first
    download(name, local)          fetch one back, for restore
    delete(name)                   prune one, for retention

`open_target()` turns a stored Destination into the right object.
Failures come back as TargetError with the remote's own message —
"Access Denied" from a bucket is more useful than anything we could
paraphrase.

Dependencies: S3 needs the `s3` extra (boto3); SFTP needs `ssh`
(paramiko, already pulled in by SSH tunnelling). FTP is stdlib. A
missing one is reported as a plain "install this" message rather than
an import traceback, and only when that kind is actually used.
"""

from __future__ import annotations

import ftplib
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlide.backend.backups.jobs import FTP, LOCAL, S3, SFTP, Destination


class TargetError(Exception):
    pass


@dataclass
class Artifact:
    """One backup file sitting at a destination."""

    name: str
    size: int = 0
    modified: str = ""  # ISO, local time; "" when the remote won't say


class Target:
    """The interface every destination kind implements."""

    def __init__(self, destination: Destination) -> None:
        self.destination = destination

    def upload(self, local: Path, name: str) -> str:
        raise NotImplementedError

    def listing(self) -> list[Artifact]:
        raise NotImplementedError

    def download(self, name: str, local: Path) -> Path:
        raise NotImplementedError

    def delete(self, name: str) -> None:
        raise NotImplementedError

    def check(self) -> str:
        """Prove the destination works, for the editor's Test button.
        Returns a one-line summary; raises TargetError if it doesn't."""
        found = self.listing()
        return f"Reachable — {len(found)} file(s) already there."


# Local


class LocalTarget(Target):
    def _dir(self) -> Path:
        raw = self.destination.path or str(Path.home() / "sqlide-backups")
        path = Path(raw).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise TargetError(f"{path}: {exc}") from exc
        return path

    def upload(self, local: Path, name: str) -> str:
        target = self._dir() / name
        try:
            # Same filesystem or not — copy, then only remove the
            # temporary once it is safely on the other side.
            shutil.copy2(local, target)
        except OSError as exc:
            raise TargetError(f"{target}: {exc}") from exc
        return str(target)

    def listing(self) -> list[Artifact]:
        found = [
            Artifact(
                p.name,
                p.stat().st_size,
                datetime.fromtimestamp(p.stat().st_mtime).isoformat(
                    timespec="seconds"
                ),
            )
            for p in self._dir().iterdir()
            if p.is_file()
        ]
        return sorted(found, key=lambda a: a.modified, reverse=True)

    def download(self, name: str, local: Path) -> Path:
        source = self._dir() / name
        if not source.is_file():
            raise TargetError(f"No such backup: {source}")
        shutil.copy2(source, local)
        return local

    def delete(self, name: str) -> None:
        (self._dir() / name).unlink(missing_ok=True)


# S3 and S3-compatible


class S3Target(Target):
    def _client(self):
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - depends on env
            raise TargetError(
                'S3 destinations need boto3: pip install "sqlide[s3]"'
            ) from exc
        d = self.destination
        return boto3.client(
            "s3",
            endpoint_url=d.endpoint_url or None,
            region_name=d.region or None,
            aws_access_key_id=d.access_key or None,
            aws_secret_access_key=d.secret_key or None,
        )

    def _key(self, name: str) -> str:
        prefix = self.destination.path.strip("/")
        return f"{prefix}/{name}" if prefix else name

    def upload(self, local: Path, name: str) -> str:
        client = self._client()
        key = self._key(name)
        try:
            # upload_file multiparts large objects for us, which is
            # the whole reason S3 goes through boto3 rather than a
            # hand-rolled PUT.
            client.upload_file(str(local), self.destination.bucket, key)
        except Exception as exc:
            raise TargetError(_s3_message(exc)) from exc
        return f"s3://{self.destination.bucket}/{key}"

    def listing(self) -> list[Artifact]:
        client = self._client()
        prefix = self.destination.path.strip("/")
        kwargs = {"Bucket": self.destination.bucket}
        if prefix:
            kwargs["Prefix"] = prefix + "/"
        found: list[Artifact] = []
        try:
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(**kwargs):
                for obj in page.get("Contents", []):
                    found.append(
                        Artifact(
                            obj["Key"].rsplit("/", 1)[-1],
                            obj.get("Size", 0),
                            obj["LastModified"].isoformat(timespec="seconds"),
                        )
                    )
        except Exception as exc:
            raise TargetError(_s3_message(exc)) from exc
        return sorted(found, key=lambda a: a.modified, reverse=True)

    def download(self, name: str, local: Path) -> Path:
        try:
            self._client().download_file(
                self.destination.bucket, self._key(name), str(local)
            )
        except Exception as exc:
            raise TargetError(_s3_message(exc)) from exc
        return local

    def delete(self, name: str) -> None:
        try:
            self._client().delete_object(
                Bucket=self.destination.bucket, Key=self._key(name)
            )
        except Exception as exc:
            raise TargetError(_s3_message(exc)) from exc


def _s3_message(exc: Exception) -> str:
    """botocore's exceptions carry the useful part in a response
    dict; unwrap it so the run log says "Access Denied" rather than
    the repr of a generated exception class."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error", {})
        code = error.get("Code", "")
        message = error.get("Message", "")
        if code or message:
            return f"S3: {message or code}".strip()
    return f"S3: {exc}"


# SFTP


class SftpTarget(Target):
    def _connect(self):
        try:
            import paramiko
        except ImportError as exc:  # pragma: no cover - depends on env
            raise TargetError(
                'SFTP destinations need paramiko: pip install "sqlide[ssh]"'
            ) from exc
        d = self.destination
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=d.host,
                port=d.default_port(),
                username=d.user or None,
                password=d.password or None,
                key_filename=d.key_path or None,
                look_for_keys=not d.password,
                allow_agent=not d.password,
                timeout=20,
            )
        except Exception as exc:
            raise TargetError(f"SFTP: {exc}") from exc
        return client

    def _remote(self, name: str) -> str:
        base = self.destination.path.rstrip("/")
        return f"{base}/{name}" if base else name

    def upload(self, local: Path, name: str) -> str:
        client = self._connect()
        try:
            sftp = client.open_sftp()
            self._mkdirs(sftp)
            sftp.put(str(local), self._remote(name))
        except TargetError:
            raise
        except Exception as exc:
            raise TargetError(f"SFTP: {exc}") from exc
        finally:
            client.close()
        return f"sftp://{self.destination.host}/{self._remote(name)}"

    def _mkdirs(self, sftp) -> None:
        """Create the destination directory if it isn't there. sftp
        has no mkdir -p, so walk the path a component at a time."""
        parts = [p for p in self.destination.path.strip("/").split("/") if p]
        prefix = "/" if self.destination.path.startswith("/") else ""
        for part in parts:
            prefix = f"{prefix.rstrip('/')}/{part}" if prefix else part
            try:
                sftp.stat(prefix)
            except IOError:
                sftp.mkdir(prefix)

    def listing(self) -> list[Artifact]:
        client = self._connect()
        try:
            sftp = client.open_sftp()
            path = self.destination.path.rstrip("/") or "."
            entries = sftp.listdir_attr(path)
        except Exception as exc:
            raise TargetError(f"SFTP: {exc}") from exc
        finally:
            client.close()
        found = [
            Artifact(
                e.filename,
                e.st_size or 0,
                datetime.fromtimestamp(e.st_mtime).isoformat(
                    timespec="seconds"
                )
                if e.st_mtime
                else "",
            )
            for e in entries
        ]
        return sorted(found, key=lambda a: a.modified, reverse=True)

    def download(self, name: str, local: Path) -> Path:
        client = self._connect()
        try:
            client.open_sftp().get(self._remote(name), str(local))
        except Exception as exc:
            raise TargetError(f"SFTP: {exc}") from exc
        finally:
            client.close()
        return local

    def delete(self, name: str) -> None:
        client = self._connect()
        try:
            client.open_sftp().remove(self._remote(name))
        except Exception as exc:
            raise TargetError(f"SFTP: {exc}") from exc
        finally:
            client.close()


# FTP / FTPS


class FtpTarget(Target):
    def _connect(self) -> ftplib.FTP:
        d = self.destination
        try:
            client: ftplib.FTP = (
                ftplib.FTP_TLS() if d.tls else ftplib.FTP()
            )
            client.connect(d.host, d.default_port(), timeout=20)
            client.login(d.user or "anonymous", d.password or "")
            if d.tls:
                # Encrypt the data channel too; login alone only
                # protects the credentials.
                client.prot_p()
            client.set_pasv(True)
            if d.path:
                self._chdir(client, d.path)
        except ftplib.all_errors as exc:
            raise TargetError(f"FTP: {exc}") from exc
        return client

    def _chdir(self, client: ftplib.FTP, path: str) -> None:
        for part in [p for p in path.strip("/").split("/") if p]:
            try:
                client.cwd(part)
            except ftplib.error_perm:
                client.mkd(part)
                client.cwd(part)

    def upload(self, local: Path, name: str) -> str:
        client = self._connect()
        try:
            with local.open("rb") as handle:
                client.storbinary(f"STOR {name}", handle)
        except ftplib.all_errors as exc:
            raise TargetError(f"FTP: {exc}") from exc
        finally:
            _quiet_quit(client)
        d = self.destination
        return f"ftp://{d.host}/{d.path.strip('/')}/{name}".replace("//", "/")

    def listing(self) -> list[Artifact]:
        client = self._connect()
        found: list[Artifact] = []
        try:
            # MLSD is the machine-readable listing; servers too old
            # for it fall back to bare names with no size or date.
            try:
                for name, facts in client.mlsd():
                    if facts.get("type") not in (None, "file"):
                        continue
                    found.append(
                        Artifact(
                            name,
                            int(facts.get("size", 0) or 0),
                            _ftp_time(facts.get("modify", "")),
                        )
                    )
            except ftplib.error_perm:
                found = [Artifact(n) for n in client.nlst()]
        except ftplib.all_errors as exc:
            raise TargetError(f"FTP: {exc}") from exc
        finally:
            _quiet_quit(client)
        return sorted(found, key=lambda a: a.modified, reverse=True)

    def download(self, name: str, local: Path) -> Path:
        client = self._connect()
        try:
            with local.open("wb") as handle:
                client.retrbinary(f"RETR {name}", handle.write)
        except ftplib.all_errors as exc:
            raise TargetError(f"FTP: {exc}") from exc
        finally:
            _quiet_quit(client)
        return local

    def delete(self, name: str) -> None:
        client = self._connect()
        try:
            client.delete(name)
        except ftplib.all_errors as exc:
            raise TargetError(f"FTP: {exc}") from exc
        finally:
            _quiet_quit(client)


def _ftp_time(stamp: str) -> str:
    """MLSD's modify fact (YYYYMMDDHHMMSS, UTC) as local ISO time."""
    if not stamp:
        return ""
    try:
        when = datetime.strptime(stamp[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return ""
    return when.isoformat(timespec="seconds")


def _quiet_quit(client: ftplib.FTP) -> None:
    """A server that drops the control connection on QUIT must not
    turn a finished upload into a failed run."""
    try:
        client.quit()
    except Exception:
        try:
            client.close()
        except Exception:
            pass


_TARGETS = {
    LOCAL: LocalTarget,
    S3: S3Target,
    SFTP: SftpTarget,
    FTP: FtpTarget,
}


def open_target(destination: Destination) -> Target:
    factory = _TARGETS.get(destination.kind)
    if factory is None:
        raise TargetError(f"Unknown destination kind: {destination.kind}")
    return factory(destination)


def missing_dependency(kind: str) -> str:
    """The extra a destination kind needs but doesn't have, or ""."""
    if kind == S3:
        try:
            import boto3  # noqa: F401
        except ImportError:
            return 'S3 needs boto3: pip install "sqlide[s3]"'
    if kind == SFTP:
        try:
            import paramiko  # noqa: F401
        except ImportError:
            return 'SFTP needs paramiko: pip install "sqlide[ssh]"'
    return ""
