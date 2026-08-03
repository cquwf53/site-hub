from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class Bookmark:
    title: str
    url: str
    folders: tuple[str, ...]


class NetscapeBookmarkParser(HTMLParser):
    """Parse the intentionally loose Netscape bookmark export format."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.bookmarks: list[Bookmark] = []
        self._folders: list[str] = []
        self._dl_pushed_folder: list[bool] = []
        self._pending_folder: str | None = None
        self._capture: str | None = None
        self._text: list[str] = []
        self._href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "h3":
            self._capture = "folder"; self._text = []
        elif tag == "a":
            self._capture = "bookmark"; self._text = []
            self._href = next((value for key, value in attrs if key.casefold() == "href"), None)
        elif tag == "dl":
            if self._pending_folder:
                self._folders.append(self._pending_folder)
                self._pending_folder = None
                self._dl_pushed_folder.append(True)
            else:
                self._dl_pushed_folder.append(False)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "h3" and self._capture == "folder":
            name = " ".join("".join(self._text).split()).strip()
            self._pending_folder = name[:200] if name else None
            self._capture = None; self._text = []
        elif tag == "a" and self._capture == "bookmark":
            title = " ".join("".join(self._text).split()).strip()
            if self._href:
                self.bookmarks.append(Bookmark(title[:200], self._href.strip(), tuple(self._folders)))
            self._capture = None; self._text = []; self._href = None
        elif tag == "dl" and self._dl_pushed_folder:
            if self._dl_pushed_folder.pop() and self._folders:
                self._folders.pop()

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._text.append(data)


def parse_bookmarks(content: bytes) -> list[Bookmark]:
    parser = NetscapeBookmarkParser()
    parser.feed(content.decode("utf-8-sig", errors="replace"))
    parser.close()
    return parser.bookmarks


class BookmarkImportService:
    def __init__(self, store: Any) -> None:
        self.store = store

    async def import_bytes(self, content: bytes) -> dict[str, int]:
        bookmarks = parse_bookmarks(content)
        existing_urls = set(await self.store.list_site_urls_for_import())
        folders = await self.store.list_folders()
        folder_index = {
            (str(row["parent_id"]) if row.get("parent_id") else None, str(row["name"]).casefold()): row
            for row in folders
        }
        folder_cache: dict[tuple[str, ...], str | None] = {(): None}
        result = {"total": len(bookmarks), "imported": 0, "duplicates": 0,
                  "invalid": 0, "folders_created": 0}

        async def ensure_folder(path: tuple[str, ...]) -> str | None:
            if path in folder_cache:
                return folder_cache[path]
            parent_path = path[:-1]
            parent_id = await ensure_folder(parent_path)
            key = (parent_id, path[-1].casefold())
            row = folder_index.get(key)
            if row is None:
                row = await self.store.create_folder({"name": path[-1], "parent_id": parent_id})
                folder_index[key] = row
                result["folders_created"] += 1
            identifier = str(row["id"])
            folder_cache[path] = identifier
            return identifier

        for bookmark in bookmarks:
            parts = urlsplit(bookmark.url)
            if (
                parts.scheme.casefold() not in {"http", "https"}
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or len(bookmark.url) > 2048
            ):
                result["invalid"] += 1
                continue
            if bookmark.url in existing_urls:
                result["duplicates"] += 1
                continue
            folder_id = await ensure_folder(bookmark.folders)
            name = bookmark.title or parts.hostname or bookmark.url
            await self.store.create_site({
                "name": name[:200], "url": bookmark.url, "folder_id": folder_id,
                "capabilities": ["bookmark"], "kind": "bookmark", "status": "active",
            })
            existing_urls.add(bookmark.url)
            result["imported"] += 1
        return result


__all__ = ["Bookmark", "BookmarkImportService", "NetscapeBookmarkParser", "parse_bookmarks"]
