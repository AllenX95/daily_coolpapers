import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from daily_coolpapers import cache_manager, fulltext


VALID_PDF = b"%PDF-1.7\n" + (b"x" * 1100) + b"\n%%EOF"


class FakeResponse:
    def __init__(self, status_code, url, *, headers=None, chunks=()):
        self.status_code = status_code
        self.url = httpx.URL(url)
        self.headers = headers or {}
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", self.url)
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=request,
                response=httpx.Response(self.status_code, request=request),
            )

    def iter_bytes(self, chunk_size=1024):
        yield from self._chunks


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, url, headers=None):
        self.calls.append(url)
        return self.responses.pop(0)


class CacheSafetyTests(unittest.TestCase):
    def test_download_rejects_non_https_url_before_network(self):
        client = FakeClient([])
        with patch.object(cache_manager.httpx, "Client", return_value=client):
            with self.assertRaises(ValueError):
                cache_manager.download_pdf(
                    "2601.00001",
                    "http://arxiv.org/pdf/2601.00001",
                    retries=0,
                )
        self.assertEqual(client.calls, [])

    def test_download_rejects_redirect_to_untrusted_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(
                [
                    FakeResponse(
                        302,
                        "https://arxiv.org/pdf/2601.00001",
                        headers={"location": "https://evil.example/file.pdf"},
                    )
                ]
            )
            with (
                patch.object(cache_manager, "PDF_CACHE_DIR", Path(tmp)),
                patch.object(cache_manager, "ensure_directories", return_value=None),
                patch.object(cache_manager, "_pdf_client_kwargs", return_value={}),
                patch.object(cache_manager.httpx, "Client", return_value=client),
            ):
                with self.assertRaises(RuntimeError) as caught:
                    cache_manager.download_pdf(
                        "2601.00001",
                        "https://arxiv.org/pdf/2601.00001",
                        retries=0,
                    )

            self.assertIsInstance(caught.exception.__cause__, ValueError)
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(list(Path(tmp).glob("*.pdf")), [])

    def test_failed_download_preserves_existing_destination_and_cleans_tmp(self):
        def interrupted_chunks():
            yield b"%PDF-1.7\npartial"
            raise OSError("connection interrupted")

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            destination = cache_dir / "2601.00001.pdf"
            destination.write_bytes(b"old-cache")
            client = FakeClient(
                [
                    FakeResponse(
                        200,
                        "https://arxiv.org/pdf/2601.00001",
                        chunks=interrupted_chunks(),
                    )
                ]
            )
            with (
                patch.object(cache_manager, "PDF_CACHE_DIR", cache_dir),
                patch.object(cache_manager, "ensure_directories", return_value=None),
                patch.object(cache_manager, "_pdf_client_kwargs", return_value={}),
                patch.object(cache_manager.httpx, "Client", return_value=client),
            ):
                with self.assertRaises(RuntimeError):
                    cache_manager.download_pdf(
                        "2601.00001",
                        "https://arxiv.org/pdf/2601.00001",
                        retries=0,
                    )

            self.assertEqual(destination.read_bytes(), b"old-cache")
            self.assertEqual(list(cache_dir.glob("*.tmp")), [])

    def test_successful_download_is_published_only_after_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            destination = cache_dir / "2601.00001.pdf"

            def chunks():
                self.assertFalse(destination.exists())
                yield VALID_PDF

            client = FakeClient(
                [
                    FakeResponse(
                        200,
                        "https://arxiv.org/pdf/2601.00001",
                        chunks=chunks(),
                    )
                ]
            )
            with (
                patch.object(cache_manager, "PDF_CACHE_DIR", cache_dir),
                patch.object(cache_manager, "ensure_directories", return_value=None),
                patch.object(cache_manager, "_pdf_client_kwargs", return_value={}),
                patch.object(cache_manager.httpx, "Client", return_value=client),
            ):
                result = cache_manager.download_pdf(
                    "2601.00001",
                    "https://arxiv.org/pdf/2601.00001",
                    retries=0,
                )

            self.assertEqual(result, destination)
            self.assertEqual(destination.read_bytes(), VALID_PDF)
            self.assertEqual(list(cache_dir.glob("*.tmp")), [])

    def test_failed_markdown_replace_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            destination = cache_dir / "2601.00001.md"
            destination.write_text("old markdown", encoding="utf-8")
            paper = {
                "arxiv_id": "2601.00001",
                "pdf_url": "https://arxiv.org/pdf/2601.00001",
            }
            with (
                patch.object(fulltext, "markdown_path", return_value=destination),
                patch.object(fulltext, "download_pdf", return_value=cache_dir / "paper.pdf"),
                patch.object(fulltext, "convert_pdf_to_markdown", return_value="new markdown"),
                patch.object(cache_manager, "_replace_with_retries", side_effect=OSError("busy")),
            ):
                with self.assertRaises(OSError):
                    fulltext.ensure_markdown(paper, force=True)

            self.assertEqual(destination.read_text(encoding="utf-8"), "old markdown")
            self.assertEqual(list(cache_dir.glob("*.tmp")), [])

    def test_concurrent_markdown_requests_convert_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            paper = {
                "arxiv_id": "2601.00001",
                "pdf_url": "https://arxiv.org/pdf/2601.00001",
            }
            conversion_entered = threading.Event()
            release_conversion = threading.Event()
            conversion_count = 0
            count_lock = threading.Lock()
            results = []

            def convert(_path):
                nonlocal conversion_count
                with count_lock:
                    conversion_count += 1
                conversion_entered.set()
                if not release_conversion.wait(2):
                    raise TimeoutError("test did not release conversion")
                return "markdown"

            def ensure():
                results.append(fulltext.ensure_markdown(paper))

            with (
                patch.object(cache_manager, "MARKDOWN_CACHE_DIR", cache_dir),
                patch.object(cache_manager, "ensure_directories", return_value=None),
                patch.object(fulltext, "download_pdf", return_value=cache_dir / "paper.pdf"),
                patch.object(fulltext, "convert_pdf_to_markdown", side_effect=convert),
            ):
                first = threading.Thread(target=ensure)
                second = threading.Thread(target=ensure)
                first.start()
                self.assertTrue(conversion_entered.wait(2))
                second.start()
                release_conversion.set()
                first.join(2)
                second.join(2)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(conversion_count, 1)
            self.assertEqual(sorted(created for _path, created in results), [False, True])


if __name__ == "__main__":
    unittest.main()
