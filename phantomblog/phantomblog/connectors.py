"""Configured MCP adapters. Credentials remain in Phantombot's vault."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from string import Template
from urllib.request import HTTPRedirectHandler, Request

from .core import Invalid, atomic, build, contained, decode, digest, encoded, load, lock, render, slug, article_url

PROVIDERS = ("facebook", "instagram", "linkedin", "x", "mastodon", "bluesky", "youtube", "pinterest", "tiktok", "phantomchat", "deployment", "custom")
CAPABILITIES = {"deploy", "share", "message"}
VARIABLES = {"url", "title", "text", "language", "slug", "revision", "idempotency_key", "output_dir", "manifest", "request", "image_url", "social_image_url", "image_file"}


class ExternalFailure(RuntimeError):
    """An external operation failed or requires reconciliation; exit code 3."""


def validate_connections(connections):
    if not isinstance(connections, dict):
        raise Invalid("Connections must be an object")
    for name, connection in connections.items():
        slug(name)
        if not isinstance(connection, dict) or set(connection) != {"provider", "persona", "server", "capabilities"}:
            raise Invalid("Connection needs provider, persona, server and capabilities")
        if connection["provider"] not in PROVIDERS:
            raise Invalid("Unknown provider; use custom for additional providers")
        for field in ("persona", "server"):
            if not isinstance(connection[field], str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", connection[field]):
                raise Invalid(f"Invalid connection {field}")
        caps = connection["capabilities"]
        if not isinstance(caps, dict) or not caps or set(caps) - CAPABILITIES:
            raise Invalid("Unsupported connection capability")
        for capability, spec in caps.items():
            if not isinstance(spec, dict) or set(spec) != {"tool", "arguments", "successField", "receiptField"}:
                raise Invalid("Capability needs tool, arguments, successField and receiptField")
            if not isinstance(spec["tool"], str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,150}", spec["tool"]):
                raise Invalid("Invalid MCP tool name")
            if not isinstance(spec["arguments"], dict):
                raise Invalid("MCP arguments must be an object")
            for field in ("successField", "receiptField"):
                if not isinstance(spec[field], str) or not re.fullmatch(r"[a-zA-Z0-9_.-]+", spec[field]):
                    raise Invalid("Response fields must be dotted object paths")
                if re.search(r"token|secret|password|nsec|authorization", spec[field], re.I):
                    raise Invalid("Receipt fields cannot identify credentials")
            def walk(value):
                if isinstance(value, dict):
                    for k, v in value.items():
                        if re.search(r"token|secret|password|private.?key|nsec|authorization", k, re.I):
                            raise Invalid("Credentials belong in the MCP server vault configuration")
                        walk(v)
                elif isinstance(value, list):
                    for item in value:
                        walk(item)
                elif isinstance(value, str):
                    t = Template(value)
                    if not t.is_valid() or set(t.get_identifiers()) - VARIABLES:
                        raise Invalid("Unknown connector argument placeholder")
                    if re.search(r"\bnsec1|\bgh[pousr]_", value):
                        raise Invalid("Credentials cannot be stored in blog.json")
            walk(spec["arguments"])


class Runner:
    def __init__(self):
        self.executable = shutil.which("phantombot")

    def run(self, args):
        if not self.executable:
            raise ExternalFailure("Phantombot is not installed on this machine")
        if self.executable.lower().endswith((".cmd", ".bat")):
            raise ExternalFailure("Use a Phantombot executable runtime; batch wrappers are not supported for external adapter calls")
        try:
            # No shell interpolation. Never echo provider output: it can contain secrets.
            result = subprocess.run([self.executable, *args], capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExternalFailure("Phantombot call failed or timed out; reconcile before retry") from exc
        if result.returncode:
            raise ExternalFailure("Phantombot returned an error; inspect its private diagnostics")
        try:
            return json.loads(result.stdout)
        except ValueError as exc:
            raise ExternalFailure("Phantombot returned an unexpected response") from exc

    def call(self, connection, spec, arguments):
        return self.run(["mcp", "call", connection["server"], spec["tool"], "--persona", connection["persona"], "--args", json.dumps(arguments, ensure_ascii=False)])

    def capture(self, persona, message):
        if not self.executable or self.executable.lower().endswith((".cmd", ".bat")):
            return False
        try:
            r = subprocess.run([self.executable, "memory", "capture", message, "--persona", persona, "--tag", "decision"], capture_output=True, timeout=30, check=False)
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def describe(self, connection):
        return self.run(["mcp", "describe", connection["server"], "--persona", connection["persona"]])


def substitute(value, variables):
    if isinstance(value, dict):
        return {k: substitute(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, variables) for v in value]
    if isinstance(value, str):
        return Template(value).substitute(variables)
    return value


def field(value, path):
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            raise ExternalFailure("Provider response did not satisfy configured success contract")
        value = value[key]
    return value


def receipt(response, spec):
    if not isinstance(response, dict) or response.get("isError"):
        raise ExternalFailure("MCP tool reported an error")
    value = response.get("structuredContent")
    if value is None:
        text_blocks = [c.get("text", "") for c in response.get("content", []) if c.get("type") == "text"]
        try:
            value = json.loads("\n".join(text_blocks))
        except ValueError as exc:
            raise ExternalFailure("Connector requires a structured JSON success response") from exc
    if field(value, spec["successField"]) is not True:
        raise ExternalFailure("Provider did not confirm success")
    result = field(value, spec["receiptField"])
    if not isinstance(result, str) or not result or len(result) > 1000 or any(ord(c) < 32 for c in result):
        raise ExternalFailure("Provider did not return a valid receipt")
    return result


def connection_status(root, runner=None):
    model, _ = load(root)
    validate_connections(model["connections"])
    runner = runner or Runner()
    statuses = []
    for name, c in model["connections"].items():
        state = "configured"
        try:
            description = runner.describe(c)
            serialized = json.dumps(description)
            if all(spec["tool"] in serialized for spec in c["capabilities"].values()):
                state = "discovered"
            else:
                state = "tool-not-discovered"
        except ExternalFailure:
            state = "unavailable"
        statuses.append({"id": name, "provider": c["provider"], "state": state, "capabilities": sorted(c["capabilities"])})
    return statuses


def prepare(root, article_slug, language, deploy=None, shares=None):
    model, revision = load(root)
    outputs = render(root, model)
    if language not in ("en", "es"):
        raise Invalid("Language must be en or es")
    article = next((a for a in model["articles"] if a["slug"] == article_slug), None)
    if not article or article["status"] != "published":
        raise Invalid("Select a complete published article")
    ids = ([deploy] if deploy else []) + list(shares or [])
    if len(ids) != len(set(ids)):
        raise Invalid("Duplicate connection selection")
    for name in ids:
        c = model["connections"].get(name)
        if not c or ("deploy" if name == deploy else "share") not in c["capabilities"]:
            raise Invalid(f"Connection lacks requested capability: {name}")
    url = model["site"]["url"] + article_url(article, language)
    if article.get("mode", "generated") == "legacy":
        data = contained(root / "public", article_url(article, language).split("?")[0], True).read_bytes()
    else:
        data = outputs[article_url(article, language)]
    plan = {"revision": revision, "slug": article_slug, "language": language, "url": url, "title": article[language]["title"], "deploy": deploy, "shares": list(shares or []), "contentHash": digest(data), "buildHash": digest(encoded({n: digest(d) for n, d in sorted(outputs.items())})), "liveVerified": False}
    plan["approval"] = digest(encoded(plan))
    return plan


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ExternalFailure("Published URL redirected; check its configured canonical URL")


def verify_live(url, expected_hash):
    from urllib.request import build_opener
    try:
        with build_opener(NoRedirect()).open(Request(url, headers={"User-Agent": "PhantomBlog/0.1", "Cache-Control": "no-cache"}), timeout=20) as response:
            if response.status != 200 or "text/html" not in response.headers.get("Content-Type", ""):
                raise ExternalFailure("Published URL is not an HTML page")
            data = response.read(5_000_001)
            if len(data) > 5_000_000 or digest(data) != expected_hash:
                raise ExternalFailure("Published page does not match this generated revision; wait for deployment")
    except (OSError, ValueError) as exc:
        raise ExternalFailure("Could not confirm published URL") from exc
    return {"url": url, "contentHash": expected_hash, "liveVerified": True}


def execution_state(root):
    path = contained(root, ".phantomblog-publishing.json")
    if not path.exists():
        return {"version": 1, "operations": {}}
    state = decode(path.read_bytes())
    if not isinstance(state, dict) or set(state) != {"version", "operations"} or state["version"] != 1 or not isinstance(state["operations"], dict):
        raise Invalid("Invalid publishing ledger; reconcile before continuing")
    if any(not re.fullmatch(r"[0-9a-f]{64}", key) or not isinstance(value, dict) or value.get("state") not in ("pending", "uncertain", "complete", "retry-approved") for key, value in state["operations"].items()):
        raise Invalid("Malformed publishing operation; refuse external work")
    return state


def operation(root, connection, capability, variables, runner, key):
    state = execution_state(root)
    previous = state["operations"].get(key)
    if previous:
        if previous.get("state") == "complete":
            return previous
        if previous.get("state") != "retry-approved":
            raise ExternalFailure("Previous attempt has an uncertain result. Reconcile its receipt before retrying.")
    spec = connection["capabilities"][capability]
    arguments = substitute(spec["arguments"], variables)
    history = previous.get("reconciliations", []) if previous else []
    state["operations"][key] = {"state": "pending", "capability": capability, "reconciliations": history}
    path = contained(root, ".phantomblog-publishing.json")
    atomic(path, encoded(state))
    try:
        result = receipt(runner.call(connection, spec, arguments), spec)
    except (ExternalFailure, OSError) as exc:
        state["operations"][key]["state"] = "uncertain"
        atomic(path, encoded(state))
        raise ExternalFailure("External result uncertain; no automatic retry. Check the provider before reconciling.") from exc
    captured = runner.capture(connection["persona"], f"PhantomBlog {capability} completed for {variables['slug']} ({variables['language']}); revision {variables['revision']}.")
    state["operations"][key] = {"state": "complete", "capability": capability, "receipt": result, "memoryCaptured": captured, "reconciliations": history}
    atomic(path, encoded(state))
    return state["operations"][key]


def reconcile(root, key, confirmed_receipt=None, confirmed_not_sent=False):
    """Operator-only reconciliation after checking the provider's own history."""
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key) or type(confirmed_not_sent) is not bool or bool(confirmed_receipt) == bool(confirmed_not_sent):
        raise Invalid("Select exactly one confirmed receipt or confirmed-not-sent")
    if confirmed_receipt and (not isinstance(confirmed_receipt, str) or len(confirmed_receipt) > 1000 or any(ord(c) < 32 for c in confirmed_receipt)):
        raise Invalid("Use the provider's public receipt, never a credential")
    with lock(root):
        state = execution_state(root)
        operation = state["operations"].get(key)
        if not operation or operation["state"] not in ("pending", "uncertain"):
            raise Invalid("Only pending or uncertain operations can be reconciled")
        operation.setdefault("reconciliations", []).append({"previousState": operation["state"], "operatorConfirmed": "not-sent" if confirmed_not_sent else "complete"})
        if confirmed_receipt:
            operation.update(state="complete", receipt=confirmed_receipt, memoryCaptured=False)
        else:
            operation["state"] = "retry-approved"
        atomic(contained(root, ".phantomblog-publishing.json"), encoded(state))
        return {"operation": key, "result": operation}


def execute(root, plan, approval, runner=None, verifier=verify_live):
    fresh = prepare(root, plan["slug"], plan["language"], plan.get("deploy"), plan.get("shares"))
    if approval != fresh["approval"] or plan != fresh:
        raise Invalid("Approval is stale; review a fresh publishing plan")
    runner = runner or Runner()
    build(root)
    with lock(root):
        if load(root)[1] != plan["revision"]:
            raise Invalid("Source changed; review again")
        model, _ = load(root)
        article = next(a for a in model["articles"] if a["slug"] == plan["slug"])
        variables = {"url": plan["url"], "title": plan["title"], "text": plan["title"] + " " + plan["url"], "language": plan["language"], "slug": plan["slug"], "revision": plan["revision"], "output_dir": str((root / "public").resolve()), "manifest": str((root / "public" / "phantomblog-manifest.json").resolve()), "request": ""}
        variables.update(image_url=model["site"]["url"] + article["image"], social_image_url=model["site"]["url"] + article["socialImage"], image_file=str(contained(root / "public", article["image"], True)))
        result = {"plan": plan, "deployment": None, "verification": None, "shares": {}, "errors": {}}
        if plan["deploy"]:
            key = digest(encoded(["deploy", plan["deploy"], plan["buildHash"]]))
            variables["idempotency_key"] = key
            result["deployment"] = operation(root, model["connections"][plan["deploy"]], "deploy", variables, runner, key)
        result["verification"] = verifier(plan["url"], plan["contentHash"])
        if result["verification"].get("liveVerified") is not True:
            raise ExternalFailure("Live verification failed; sharing refused")
        for name in plan["shares"]:
            key = digest(encoded(["share", name, plan["url"], plan["contentHash"]]))
            variables["idempotency_key"] = key
            try:
                result["shares"][name] = operation(root, model["connections"][name], "share", variables, runner, key)
            except ExternalFailure as exc:
                result["errors"][name] = str(exc)
        return result


def message(root, connection_id, request, article_slug, revision, runner=None):
    model, current = load(root)
    if current != revision:
        raise Invalid("Article changed; reload before sending")
    render(root, model, include_drafts=True)
    c = model["connections"].get(connection_id)
    if not c or c["provider"] != "phantomchat" or "message" not in c["capabilities"]:
        raise Invalid("Configure a verified PhantomChat messaging MCP adapter first")
    if not isinstance(request, str) or not request.strip() or len(request) > 10_000:
        raise Invalid("Request must be nonempty text under 10 KB")
    a = next((a for a in model["articles"] if a["slug"] == article_slug), None)
    if not a:
        raise Invalid("Article not found")
    # Request text never authorizes changes or posting. The channel handles sender
    # provenance; dashboard suggestions are reviewed through the source CAS API.
    envelope = json.dumps({"type": "phantomblog-review-request-v1", "slug": article_slug, "revision": revision, "request": request, "context": {"enTitle": a["en"].get("title"), "esTitle": a["es"].get("title")}}, ensure_ascii=False)
    variables = {k: "" for k in VARIABLES}
    variables.update(request=envelope, text=envelope, slug=article_slug, revision=revision, idempotency_key=digest(envelope.encode()))
    with lock(root):
        if load(root)[1] != revision:
            raise Invalid("Source changed before sending")
        return operation(root, c, "message", variables, runner or Runner(), variables["idempotency_key"])
