"""Background recorder and verified OneDrive uploader."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import aiohttp


DISK_RESERVE = int(os.environ.get("TVPROXY_RECORDING_RESERVE_BYTES", str(10 * 1024 ** 3)))
RECORDING_DIR = Path(os.environ.get("TVPROXY_RECORDING_DIR", "/var/lib/tv-proxy/recordings"))
RCLONE_REMOTE = os.environ.get("TVPROXY_ONEDRIVE_REMOTE", "")
INTERNAL_URL = os.environ.get("TVPROXY_INTERNAL_URL", "http://127.0.0.1:8081")
LIBRARY_UPLOAD_URL = os.environ.get("TVPROXY_LIBRARY_UPLOAD_URL", "").rstrip("/")
LIBRARY_UPLOAD_TOKEN = os.environ.get("TVPROXY_LIBRARY_UPLOAD_TOKEN", "")
LIBRARY_UPLOAD_CHUNK_BYTES = int(os.environ.get("TVPROXY_LIBRARY_UPLOAD_CHUNK_BYTES", str(16 * 1024 * 1024)))
logger = logging.getLogger("tvproxy.recorder")


def storage_status() -> dict:
    usage = shutil.disk_usage(RECORDING_DIR if RECORDING_DIR.exists() else "/")
    return {"path": str(RECORDING_DIR), "total_bytes": usage.total,
            "used_bytes": usage.used, "free_bytes": usage.free,
            "reserve_bytes": DISK_RESERVE,
            "recording_space_available": max(0, usage.free - DISK_RESERVE)}


def _safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return value[:100] or "recording"


class RecorderWorker:
    def __init__(self, stores: dict):
        self.stores = stores
        self.tasks: dict[int, asyncio.Task] = {}
        self.stopped = False

    async def run(self) -> None:
        RECORDING_DIR.mkdir(parents=True, exist_ok=True)
        # A process restart cannot safely resume an ffmpeg process from the old worker.
        for row in await self.stores["recordings"].list_all():
            if row["status"] == "recording":
                await self.stores["recordings"].update(
                    row["id"], status="failed", error="recorder restarted before completion"
                )
            elif row["status"] == "uploading" and row.get("local_path") and LIBRARY_UPLOAD_URL and LIBRARY_UPLOAD_TOKEN:
                self.tasks[row["id"]] = asyncio.create_task(self._upload_existing(row))
            elif row["status"] == "uploading":
                await self.stores["recordings"].update(row["id"], status="failed", error="recorder restarted before upload completion")
        while not self.stopped:
            for row in await self.stores["recordings"].due(time.time()):
                if row["id"] not in self.tasks:
                    self.tasks[row["id"]] = asyncio.create_task(self._record(row))
            self.tasks = {rid: task for rid, task in self.tasks.items() if not task.done()}
            await asyncio.sleep(5)

    async def stop(self) -> None:
        self.stopped = True
        for task in self.tasks.values():
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    async def _record(self, row: dict) -> None:
        store = self.stores["recordings"]
        recording_id = row["id"]
        viewer = await self.stores["viewers"].get(row["viewer_id"])
        channel = await self.stores["channels"].get(row["channel_id"])
        if not viewer or not channel or viewer.get("disabled"):
            await store.update(recording_id, status="failed", error="viewer or channel unavailable")
            return
        if storage_status()["free_bytes"] <= DISK_RESERVE:
            await store.update(recording_id, status="disk_rejected", error="10 GB disk reserve would be breached")
            return
        deadline = row["end_at"]
        stamp = datetime.fromtimestamp(row["start_at"], timezone.utc).strftime("%Y%m%d-%H%M%S")
        filename = f"{stamp}-{_safe_name(row['title'] or channel['display_name'])}-{recording_id}.ts"
        local_path = RECORDING_DIR / filename
        await store.update(recording_id, status="recording", local_path=str(local_path))
        url = f"{INTERNAL_URL.rstrip('/')}/live/{channel['slug']}.m3u8?token={viewer['token']}"
        chunks = []
        last_error = ""
        attempt = 0
        while time.time() < deadline:
            if storage_status()["free_bytes"] <= DISK_RESERVE:
                await store.update(recording_id, status="disk_rejected", error="10 GB disk reserve reached")
                return
            remaining = max(1, int(deadline - time.time()))
            chunk = local_path.with_suffix(f".part{attempt}.ts")
            attempt += 1
            process = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
                "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                "-i", url, "-map", "0", "-c", "copy", "-t", str(remaining),
                "-f", "mpegts", str(chunk),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            stderr_task = asyncio.create_task(process.stderr.read())
            try:
                await process.wait()
                stderr = (await stderr_task).decode("utf-8", "replace")[-500:]
                if chunk.exists() and chunk.stat().st_size:
                    chunks.append(chunk)
                last_error = stderr or f"ffmpeg exited with code {process.returncode}"
                if time.time() < deadline:
                    logger.warning("recording-retry id=%s attempt=%s returncode=%s remaining=%ss stderr=%s", recording_id, attempt, process.returncode, remaining, last_error)
                    await asyncio.sleep(5)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                if not stderr_task.done():
                    stderr_task.cancel()
                    await asyncio.gather(stderr_task, return_exceptions=True)
        if not chunks:
            await store.update(recording_id, status="failed", error=last_error or "ffmpeg produced no recording")
            return
        with open(local_path, "wb") as output:
            for chunk in chunks:
                with open(chunk, "rb") as part:
                    shutil.copyfileobj(part, output)
                chunk.unlink(missing_ok=True)
        if LIBRARY_UPLOAD_URL and LIBRARY_UPLOAD_TOKEN:
            await store.update(recording_id, status="uploading")
            await self._upload_with_retries(row | {"local_path": str(local_path), "remote_path": row.get("remote_path", "")}, local_path)
            await store.update(recording_id, status="completed", error="Uploaded to IPTV library")
            return
        if not RCLONE_REMOTE:
            # Local-only mode: keep the verified file available for web playback/download.
            await store.update(recording_id, status="completed", error="Stored locally; OneDrive is not configured")
            return
        await store.update(recording_id, status="uploading")
        remote = f"{RCLONE_REMOTE.rstrip('/')}/{datetime.now().year}/{datetime.now().month:02d}/{filename}"
        copy = await self._command("rclone", "copyto", str(local_path), remote, "--retries", "3", "--low-level-retries", "10")
        if copy.returncode != 0:
            await store.update(recording_id, status="upload_failed", error=copy.stderr[-500:])
            return
        verify = await self._command("rclone", "check", str(local_path), remote, "--one-way")
        if verify.returncode != 0:
            await store.update(recording_id, status="upload_failed", error="upload verification failed")
            return
        local_path.unlink(missing_ok=True)
        await store.update(recording_id, status="completed", remote_path=remote, local_path="")

    async def _upload_existing(self, row: dict) -> None:
        path = Path(row["local_path"])
        if not path.is_file():
            await self.stores["recordings"].update(row["id"], status="failed", error="local file missing for library upload")
            return
        try:
            await self._upload_with_retries(row, path)
            await self.stores["recordings"].update(row["id"], status="completed", error="Uploaded to IPTV library")
        except asyncio.CancelledError:
            raise

    async def _upload_with_retries(self, row: dict, path: Path) -> None:
        delay = 10
        while True:
            try:
                await self._upload_resumable(row, path)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("library-upload-retry id=%s error=%s retry_in=%ss", row["id"], exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)

    async def _upload_resumable(self, row: dict, path: Path) -> None:
        headers = {"Authorization": f"Bearer {LIBRARY_UPLOAD_TOKEN}", "Tus-Resumable": "1.0.0"}
        timeout = aiohttp.ClientTimeout(total=600, connect=30)
        async with aiohttp.ClientSession(timeout=timeout) as client:
            location = row.get("remote_path") or ""
            if location:
                async with client.head(location, headers=headers) as response:
                    if response.status == 404:
                        location = ""
                    elif response.status != 200:
                        raise RuntimeError(f"resume HEAD returned HTTP {response.status}")
            if not location:
                metadata = ", ".join(f"{key} {base64.b64encode(value.encode()).decode()}" for key, value in {
                    "filename": path.name, "filetype": "video/mp2t", "destination": "iptv",
                }.items())
                create_headers = {**headers, "Upload-Length": str(path.stat().st_size), "Upload-Mime-Type": "video/mp2t", "Upload-Metadata": metadata}
                async with client.post(LIBRARY_UPLOAD_URL, headers=create_headers) as response:
                    if response.status != 201:
                        raise RuntimeError(f"upload create returned HTTP {response.status}: {(await response.text())[-200:]}")
                    location = urljoin(LIBRARY_UPLOAD_URL + "/", response.headers["Location"])
                    row["remote_path"] = location
                    await self.stores["recordings"].update(row["id"], remote_path=location)
            async with client.head(location, headers=headers) as response:
                if response.status != 200:
                    raise RuntimeError(f"upload offset returned HTTP {response.status}")
                offset = int(response.headers.get("Upload-Offset", "0"))
            with path.open("rb") as source:
                source.seek(offset)
                while offset < path.stat().st_size:
                    chunk = source.read(LIBRARY_UPLOAD_CHUNK_BYTES)
                    if not chunk:
                        raise RuntimeError("local file ended before upload completed")
                    patch_headers = {**headers, "Upload-Offset": str(offset), "Content-Type": "application/offset+octet-stream"}
                    async with client.patch(location, data=chunk, headers=patch_headers) as response:
                        if response.status == 409:
                            raise RuntimeError("upload offset conflict")
                        if response.status != 204:
                            raise RuntimeError(f"upload chunk returned HTTP {response.status}")
                        offset = int(response.headers.get("Upload-Offset", str(offset + len(chunk))))
            async with client.post(f"{location}/finalize", headers=headers) as response:
                if response.status != 200:
                    raise RuntimeError(f"upload finalize returned HTTP {response.status}: {(await response.text())[-200:]}")

    async def _command(self, *args):
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        return type("Result", (), {"returncode": process.returncode, "stdout": stdout.decode("utf-8", "replace"), "stderr": stderr.decode("utf-8", "replace")})()


async def _main() -> None:
    from .stores import ChannelStore, ConnectionStore, RecordingStore, ViewerStore
    db_path = os.environ.get("TVPROXY_DB_PATH", "/var/lib/tv-proxy/mappings.db")
    stores = {"recordings": RecordingStore(db_path), "viewers": ViewerStore(db_path),
              "channels": ChannelStore(db_path), "connections": ConnectionStore(db_path)}
    for store in stores.values():
        store.init_schema()
    worker = RecorderWorker(stores)
    try:
        await worker.run()
    finally:
        await worker.stop()


if __name__ == "__main__":
    asyncio.run(_main())
