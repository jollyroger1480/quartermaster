"""Text replies search only an allow-list of knowledge files (default services.md),
so a stranger's text can never pull other customers' order lines into the context.
Voice calls keep searching every file. Nothing here may reach the phone:
subprocess.run is a tripwire and the brain and the send are fakes."""
import pytest

from callscoot import sms, vault_rag

ORDER_LINE = "OrderID=11-22222-33333 | Buyer=sparkyjoe77 | Total=$45.10"
ORDER_ITEM = "Items: Carlson H2596 Rear Left Drum Adjuster Ford Ranger"
SERVICES = "Junk removal and scrap pickup in Yourtown. E-waste accepted."


@pytest.fixture
def vault(tmp_path, monkeypatch):
    def no_phone(*a, **kw):
        raise AssertionError("test tried to run a subprocess")

    monkeypatch.setattr(sms.subprocess, "run", no_phone)  # same module vault_rag uses
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "services.md").write_text(f"# Services\n\n{SERVICES}\n")
    (root / "ebay_orders.md").write_text(f"# ebay orders\n\n{ORDER_LINE}\n  {ORDER_ITEM}\n")
    (root / "square_orders.md").write_text(f"# square orders\n\n{ORDER_LINE}\n")
    # an order file added later must stay out of texts without any code change
    (root / "brand_new_orders.md").write_text(f"# new feed\n\n{ORDER_LINE}\n")
    graph = tmp_path / "graph"
    graph.mkdir()
    cfg = {"vault": {"root": str(root), "lanes": [], "extra_dirs": [],
                     "graphify_dir": "", "top_k": 6, "chunk_chars": 700},
           "sms": {"enabled": True, "stock_file": "/nonexistent/inventory.csv"}}
    return cfg


@pytest.fixture
def texter(vault, tmp_path, monkeypatch):
    monkeypatch.setattr(sms, "STATE_FILE", str(tmp_path / "state.json"))
    inbox, prompts, sent = [], [], []
    monkeypatch.setattr(sms, "_inbox", lambda: list(inbox))
    monkeypatch.setattr(sms, "_sent", lambda: [])
    monkeypatch.setattr(sms, "_is_contact", lambda addr: False)

    def fake_brain(cfg, messages):
        prompts.append(messages[0]["content"])
        return "Anne will follow up.", "fake"

    monkeypatch.setattr(sms, "llm_chat", fake_brain)
    monkeypatch.setattr(sms.notify, "send_call_alert", lambda *a, **kw: None)
    monkeypatch.setattr(sms, "_send_sms", lambda cfg, addr, text: sent.append(addr) or True)
    sms._save_state({"last_id": 0})
    return {"cfg": vault, "inbox": inbox, "prompts": prompts, "sent": sent}


def _ask(t, body):
    t["inbox"].append({"_id": str(len(t["inbox"]) + 1), "address": "+15705550100", "body": body})
    sms.poll(t["cfg"], log=lambda *_: None)
    return t["prompts"][-1]


def test_text_matching_order_line_gets_no_order_content(texter):
    prompt = _ask(texter, "is order 11-22222-33333 for sparkyjoe77 the carlson drum adjuster?")
    # the system prompt carries only the context, never the texter's own words
    for leak in ("sparkyjoe77", "OrderID", "Carlson", "45.10", "33333", "Buyer="):
        assert leak not in prompt
    assert texter["sent"] == ["+15705550100"]


def test_text_matching_services_still_gets_services(texter):
    prompt = _ask(texter, "do you do junk removal or scrap pickup?")
    assert SERVICES in prompt


def test_sms_context_never_lists_order_files_even_if_new(vault):
    ctx, sources = vault_rag.build_context(vault, "sparkyjoe77 Carlson junk removal",
                                           only=sms._context_files(vault))
    assert [s.rsplit("/", 1)[-1] for s in sources] == ["services.md"]
    assert "sparkyjoe77" not in ctx


def test_sms_allow_list_is_configurable_and_cannot_escape_root(vault, tmp_path):
    (tmp_path / "outside.md").write_text(ORDER_LINE)
    vault["sms"]["context_files"] = ["../outside.md", "services.md", "missing.md"]
    _ctx, sources = vault_rag.build_context(vault, "sparkyjoe77 junk removal",
                                            only=sms._context_files(vault))
    assert [s.rsplit("/", 1)[-1] for s in sources] == ["services.md"]


def test_sms_path_skips_graphify(vault, tmp_path):
    vault["vault"]["graphify_dir"] = str(tmp_path / "graph")
    # subprocess.run is a tripwire: a graphify call here would fail the test
    vault_rag.build_context(vault, "junk removal", only=["services.md"])


def test_call_path_still_searches_every_file(vault):
    ctx, sources = vault_rag.build_context(vault, "order for sparkyjoe77 junk removal",
                                           focus="sparkyjoe77")
    names = {s.rsplit("/", 1)[-1] for s in sources}
    assert {"ebay_orders.md", "square_orders.md", "services.md"} <= names
    assert "sparkyjoe77" in ctx
    assert vault_rag._vault_files(vault) == vault_rag._vault_files(vault, None)
