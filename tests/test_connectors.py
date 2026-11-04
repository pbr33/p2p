import json

import httpx
import pytest

from p2p_agents.connectors import FileBundleConnector, RestConnector
from p2p_agents.settings import InstanceConfig, ObjectTypeConfig, PlatformConfig


def coupa_platform(page_size=2):
    return PlatformConfig(
        name="coupa", display_name="Coupa", connector="rest", page_size=page_size,
        objects={"tax_codes": ObjectTypeConfig(
            name="tax_codes", list_path="/api/tax_codes", update_path="/api/tax_codes/{id}",
            key="code", references={"country.code": "countries"})},
    )


def test_rest_oauth_pagination_create_update_and_retry(monkeypatch):
    monkeypatch.setenv("CID", "client")
    monkeypatch.setenv("CSECRET", "secret")
    rows = [{"id": i, "code": f"T{i}", "country": {"code": "GB"}} for i in range(3)]
    seen = {"token": 0, "429": 0, "posts": [], "puts": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            seen["token"] += 1
            assert b"grant_type=client_credentials" in request.content
            assert b"scope=core.common.read" in request.content
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer tok"
        if request.method == "GET":
            offset = int(request.url.params["offset"])
            if offset == 0 and seen["429"] == 0:
                seen["429"] += 1
                return httpx.Response(429, headers={"retry-after": "0"})
            return httpx.Response(200, json=rows[offset:offset + 2])
        if request.method == "POST":
            seen["posts"].append(json.loads(request.content))
            return httpx.Response(201, json={"id": 42})
        seen["puts"].append(request.url.path)
        return httpx.Response(200, json={})

    instance = InstanceConfig(name="coupa-sbx", platform="coupa", base_url="https://x.coupahost.com",
                              auth="oauth2_client_credentials", token_url="/oauth2/token",
                              scopes=["core.common.read"], client_id_env="CID", client_secret_env="CSECRET")
    conn = RestConnector(coupa_platform(), instance, transport=httpx.MockTransport(handler))
    snap = conn.extract()
    assert [i.key for i in snap.items] == ["T0", "T1", "T2"]
    assert snap.items[0].depends_on[0].model_dump() == {"object_type": "countries", "key": "GB", "path": "country.code"}
    assert seen["token"] == 1 and seen["429"] == 1

    cfg = conn.object_config("tax_codes")
    assert conn.create(cfg, {"code": "T9"}) == "42"
    conn.update(cfg, "5", {"code": "T5"})
    assert seen["puts"] == ["/api/tax_codes/5"]


def test_rest_missing_secret_is_a_clear_error():
    instance = InstanceConfig(name="i", platform="coupa", base_url="https://x", auth="bearer", token_env="NOPE_NOT_SET")
    conn = RestConnector(coupa_platform(), instance, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(ValueError, match="NOPE_NOT_SET is not set"):
        conn.extract()


def test_rest_path_vars():
    platform = PlatformConfig(name="readsoft", display_name="RSO", connector="rest", pagination="none",
                              objects={"document_types": ObjectTypeConfig(
                                  name="document_types", list_path="/documenttypes/rest/{org}", key="Name")})
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json=[{"Name": "Invoice"}])

    instance = InstanceConfig(name="rso", platform="readsoft", base_url="https://rso", path_vars={"org": "ORG1"})
    snap = RestConnector(platform, instance, transport=httpx.MockTransport(handler)).extract()
    assert paths == ["/documenttypes/rest/ORG1"] and snap.items[0].key == "Invoice"


def test_file_bundle_reads_nested_csv_and_writes_import_file(tmp_path):
    bundle, outbox = tmp_path / "in", tmp_path / "out"
    bundle.mkdir()
    (bundle / "cig_cross_references.csv").write_text(
        "Table,SourceValue,Target.Value,Comment\nUOM,EA,EACH,\nUOM,BX,BOX,boxes\n", encoding="utf-8")
    platform = PlatformConfig(name="ariba", display_name="Ariba", connector="file_bundle", objects={
        "cig_cross_references": ObjectTypeConfig(name="cig_cross_references", file="cig_cross_references.csv",
                                                 key="Table+SourceValue", references={"Table": "lookup_tables"})})
    instance = InstanceConfig(name="ariba-test", platform="ariba", bundle_dir=str(bundle), outbox_dir=str(outbox))
    conn = FileBundleConnector(platform, instance)
    snap = conn.extract()
    assert [i.key for i in snap.items] == ["UOM|EA", "UOM|BX"]
    assert snap.items[0].data == {"Table": "UOM", "SourceValue": "EA", "Target": {"Value": "EACH"}}
    assert snap.items[0].depends_on[0].key == "UOM"

    cfg = conn.object_config("cig_cross_references")
    conn.create(cfg, snap.items[0].data)
    [written] = conn.flush()
    lines = written.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "_action,SourceValue,Table,Target.Value"
    assert lines[1] == "create,EA,UOM,EACH"
