"""Small Immich API client using only the standard library.

Everything here reads, except upload, create_album and add_to_album, which only
run when uploading has been turned on."""

import json
import uuid
import time
import urllib.error
import urllib.request
from pathlib import Path


class ImmichError(RuntimeError):
    pass


class Immich:
    def __init__(self, base_url, key):
        self.base_url = base_url.rstrip("/")
        self._key = key

    def __repr__(self):
        return f"Immich({self.base_url!r})"

    def _request(self, method, path, body=None, retries=3, data=None, content_type=None, extra_headers=None):
        if body is not None:
            data, content_type = json.dumps(body).encode("utf-8"), "application/json"
        headers = {"x-api-key": self._key, "Accept": "application/json", **(extra_headers or {})}
        if content_type:
            headers["Content-Type"] = content_type
        url = self.base_url + path
        for attempt in range(retries):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as err:
                detail = err.read()[:300].decode("utf-8", "replace")
                if err.code >= 500 and attempt < retries - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise ImmichError(f"{method} {path} failed with {err.code}: {detail}") from None
            except urllib.error.URLError as err:
                if attempt < retries - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise ImmichError(f"{method} {path} failed: {err.reason}") from None

    def version(self):
        v = self._request("GET", "/api/server/version")
        return f"{v['major']}.{v['minor']}.{v['patch']}"

    def major_version(self):
        return int(self._request("GET", "/api/server/version")["major"])

    def upload(self, data, filename, created_at, sidecar=None, checksum=None, device_fields=False):
        """Add a new asset. Returns Immich's answer: its id and whether it was created or
        already there. device_fields adds the deviceId and deviceAssetId Immich 2 needs."""
        fields = {"fileCreatedAt": created_at, "fileModifiedAt": created_at, "filename": filename}
        if device_fields:
            fields.update(deviceId="fuseshot", deviceAssetId=f"fuseshot-{checksum or filename}")
        files = [("assetData", filename, data)]
        if sidecar:
            files.append(("sidecarData", filename.rsplit(".", 1)[0] + ".xmp", sidecar))
        body, content_type = multipart(fields, files)
        headers = {"x-immich-checksum": checksum} if checksum else None
        return self._request("POST", "/api/assets", data=body, content_type=content_type,
                             extra_headers=headers, retries=1)

    def albums(self):
        return self._request("GET", "/api/albums")

    def create_album(self, name, description=""):
        return self._request("POST", "/api/albums", {"albumName": name, "description": description})

    def add_to_album(self, album_id, asset_ids):
        return self._request("PUT", f"/api/albums/{album_id}/assets", {"ids": list(asset_ids)})

    def statistics(self):
        return self._request("GET", "/api/assets/statistics")

    def iter_assets(self, visibility, page_size=1000, updated_after=None):
        """Yield pages of assets (with exif) for one visibility value."""
        page = 1
        while True:
            body = {
                "page": page,
                "size": page_size,
                "withExif": True,
                "withPeople": True,
                "visibility": visibility,
                "order": "asc",
            }
            if updated_after:
                body["updatedAfter"] = updated_after
            result = self._request("POST", "/api/search/metadata", body)["assets"]
            items = result.get("items", [])
            if items:
                yield items
            next_page = result.get("nextPage")
            if not next_page or not items:
                break
            page = int(next_page)

    def asset_info(self, asset_id):
        return self._request("GET", f"/api/assets/{asset_id}")

    def download_original(self, asset_id, dest, chunk=1 << 20):
        """Stream an original file to dest. Returns bytes written."""
        dest = Path(dest)
        tmp = dest.with_suffix(dest.suffix + ".part")
        headers = {"x-api-key": self._key, "Accept": "application/octet-stream"}
        url = f"{self.base_url}/api/assets/{asset_id}/original"
        request = urllib.request.Request(url, headers=headers, method="GET")
        written = 0
        try:
            with urllib.request.urlopen(request, timeout=300) as response, open(tmp, "wb") as out:
                while True:
                    block = response.read(chunk)
                    if not block:
                        break
                    out.write(block)
                    written += len(block)
        except urllib.error.HTTPError as err:
            tmp.unlink(missing_ok=True)
            raise ImmichError(f"download {asset_id} failed with {err.code}") from None
        tmp.replace(dest)
        return written

    def original_head(self, asset_id, length=1 << 18):
        url = f"{self.base_url}/api/assets/{asset_id}/original"
        headers = {"x-api-key": self._key, "Range": f"bytes=0-{length - 1}"}
        request = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read(length)

    def thumbnail(self, asset_id, size="preview"):
        """Return the bytes of a generated preview (about 1440 px) or thumbnail."""
        headers = {"x-api-key": self._key, "Accept": "image/*"}
        url = f"{self.base_url}/api/assets/{asset_id}/thumbnail?size={size}"
        for attempt in range(3):
            request = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return response.read()
            except urllib.error.HTTPError as err:
                if err.code >= 500 and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise ImmichError(f"thumbnail {asset_id} failed with {err.code}") from None
            except urllib.error.URLError as err:
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise ImmichError(f"thumbnail {asset_id} failed: {err.reason}") from None


def multipart(fields, files):
    """A multipart/form-data body: fields is name -> text, files is (name, filename, bytes)."""
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, filename, data in files:
        head = (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n")
        parts += [head.encode(), data, b"\r\n"]
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
