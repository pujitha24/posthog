from __future__ import annotations

import io
import time
import shlex
import base64
import hashlib
from collections.abc import Buffer
from typing import TYPE_CHECKING

from posthog.storage import object_storage

from products.tasks.backend.models import TaskRun

if TYPE_CHECKING:
    from products.tasks.backend.logic.services.sandbox import ExecutionStream, SandboxBase


def remaining_seconds(deadline: float | None, maximum: int) -> int:
    remaining = maximum if deadline is None else min(maximum, int(deadline - time.time()))
    if remaining <= 0:
        raise RuntimeError("Workspace recovery deadline expired")
    return remaining


class SandboxArchiveReader(io.RawIOBase):
    def __init__(self, execution: ExecutionStream, deadline: float) -> None:
        super().__init__()
        self.execution = execution
        self.output = iter(execution.iter_stdout())
        self.deadline = deadline
        self.encoded = ""
        self.decoded = b""
        self.size = 0
        self.partial = False
        self.finished = False

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer) -> int:
        view = memoryview(buffer).cast("B")
        if not view:
            return 0
        while not self.decoded and not self.finished:
            remaining_seconds(self.deadline, 240)
            chunk = next(self.output, None)
            if chunk is None:
                result = self.execution.wait()
                if result.exit_code not in {0, 1} or self.encoded:
                    raise RuntimeError("Unable to archive the retained workspace")
                self.partial = result.exit_code == 1
                self.finished = True
                break
            self.encoded += "".join(chunk.split())
            length = len(self.encoded) // 4 * 4
            self.decoded = base64.b64decode(self.encoded[:length], validate=True)
            self.encoded = self.encoded[length:]
        length = min(len(view), len(self.decoded))
        view[:length] = self.decoded[:length]
        self.decoded = self.decoded[length:]
        self.size += length
        return length


class SandboxRecoveryArchive:
    @staticmethod
    def storage_path(run: TaskRun, sandbox_id: str) -> str:
        generation = hashlib.sha256(sandbox_id.encode()).hexdigest()
        return f"{run.get_artifact_s3_prefix()}/recovery/{generation}/workspace.tar.gz"

    @classmethod
    def create(cls, run: TaskRun, sandbox: SandboxBase, *, deadline: float | None = None) -> None:
        if (run.state or {}).get("sandbox_recovery_archive_sandbox_id") == sandbox.id:
            return
        storage_path = cls.storage_path(run, sandbox.id)
        timeout = remaining_seconds(deadline, 240)
        command = "set -o pipefail; /usr/bin/tar -czf - -C /tmp/workspace . 2>/dev/null | /usr/bin/base64 -w 65536"
        execution = sandbox.execute_stream(
            f"/usr/bin/timeout --kill-after=5 {timeout} /bin/bash -c {shlex.quote(command)}",
            timeout_seconds=timeout + 10,
            isolated=True,
            buffer_stdout=False,
        )
        transfer_deadline = time.time() + timeout
        reader = SandboxArchiveReader(execution, min(deadline, transfer_deadline) if deadline else transfer_deadline)
        with io.BufferedReader(reader) as stream:
            object_storage.write_stream(storage_path, stream, extras={"Tagging": f"ttl_days=30&team_id={run.team_id}"})
        stored = object_storage.head_object(storage_path)
        if not reader.finished or reader.size == 0 or stored is None or stored.get("ContentLength") != reader.size:
            raise RuntimeError("Workspace recovery upload is incomplete")
        TaskRun.update_state_atomic(
            run.id,
            updates={
                "sandbox_recovery_archive_sandbox_id": sandbox.id,
                "sandbox_recovery_archive_partial": reader.partial,
            },
        )

    @classmethod
    def download_url(cls, run: TaskRun) -> str | None:
        state = run.state or {}
        sandbox_id = state.get("sandbox_recovery_archive_sandbox_id")
        if not isinstance(sandbox_id, str) or sandbox_id != state.get("sandbox_retained_for_recovery"):
            return None
        storage_path = cls.storage_path(run, sandbox_id)
        if object_storage.head_object(storage_path) is None:
            return None
        filename = (
            "workspace-recovery-partial.tar.gz"
            if state.get("sandbox_recovery_archive_partial")
            else "workspace-recovery.tar.gz"
        )
        return object_storage.get_presigned_url(
            storage_path,
            content_type="application/gzip",
            content_disposition=f'attachment; filename="{filename}"',
        )
