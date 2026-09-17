from __future__ import annotations

from src.cleaning.normalizer import ToolNormalizer
from src.core.http_client import FetchResult
from src.extraction.official_page import OfficialFactsExtractor
from src.extraction.runner import OfficialFactsRunner, facts_key, load_facts
from src.models.enums import VerificationStatus


class Client:
    def __init__(self, result): self.result, self.calls = result, []
    def try_fetch(self, url, **kwargs): self.calls.append(url); return self.result


def tool():
    record = ToolNormalizer().from_raw({"name": "Example AI", "website": "https://example.ai", "discovery_sources": [{"name": "Directory", "kind": "directory"}]})
    record.verification.status = VerificationStatus.VERIFIED
    return record


def test_success_is_checkpointed_and_resumed_without_duplicate(tmp_path) -> None:
    page = "<html><title>Example AI</title><meta name='description' content='AI writing tool for teams.'><h2>Features</h2><ul><li>Generate text for marketing campaigns</li></ul></html>"
    client = Client(FetchResult(url="https://example.ai", final_url="https://example.ai", status=200, text=page))
    runner = OfficialFactsRunner(OfficialFactsExtractor(client=client), checkpoint_every=1)
    first = runner.run([tool()], interim_dir=tmp_path)
    second = runner.run([tool()], interim_dir=tmp_path)
    assert first["attempted"] == second["attempted"] == 1
    assert first["succeeded"] == 1
    assert len(client.calls) == 1
    assert facts_key("https://example.ai/") == facts_key("https://example.ai")
    assert load_facts(tmp_path / "official_facts.jsonl")["https://example.ai/"]["evidence"]


def test_http_failure_and_empty_page_are_persisted(tmp_path) -> None:
    failing = OfficialFactsRunner(OfficialFactsExtractor(client=Client(None)), checkpoint_every=1)
    report = failing.run([tool()], interim_dir=tmp_path)
    assert report["failed"] == 1
    empty_client = Client(FetchResult(url="https://empty.ai", final_url="https://empty.ai", status=200, text=""))
    empty = tool(); empty.website = "https://empty.ai"
    report = OfficialFactsRunner(OfficialFactsExtractor(client=empty_client), checkpoint_every=1).run([empty], interim_dir=tmp_path)
    assert report["empty"] == 1
