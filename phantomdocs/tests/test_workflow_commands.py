from pathlib import Path

import pytest

from phantomdocs.workflow_commands import build_command
from phantomdocs.workflows import WorkflowError, read_json, validate_contract


def test_arguments_with_spaces_and_shell_tokens_remain_one_argument():
    binding = {
        "id": "transcriber",
        "version": "1.0.0",
        "entrypoint": "python",
        "script": "/installed/transcribe.py",
        "arguments": ["{input}", "--out", "{output}"],
    }
    assert build_command(
        binding, "recording", "/work/a b;$(secret).mp4", "/work/out.txt"
    ) == [
        "python",
        "/installed/transcribe.py",
        "/work/a b;$(secret).mp4",
        "--out",
        "/work/out.txt",
    ]


def test_unknown_placeholder_and_variant_fail_closed():
    binding = {
        "id": "brand",
        "version": "1",
        "entrypoint": "python",
        "variants": {"summary": ["{input}", "{output}", "--clase", "resumen"]},
    }
    with pytest.raises(WorkflowError, match="variant"):
        build_command(binding, "signed-contract", "in", "out")
    binding["variants"]["summary"] = ["--out={output}"]
    with pytest.raises(WorkflowError, match="placeholders"):
        build_command(binding, "summary", "in", "out")


@pytest.mark.parametrize(
    "source,target", [("--summarize", "out"), ("in", "--out"), ("same", "same")]
)
def test_paths_cannot_be_options_or_overwrite_originals(source, target):
    binding = {
        "id": "transform",
        "version": "1",
        "entrypoint": "python",
        "arguments": ["{input}", "{output}"],
    }
    with pytest.raises(WorkflowError):
        build_command(binding, "original", source, target)


def test_au_snapshot_descriptors_do_not_bind_the_upload_cleanup_orchestrator():
    bindings = read_json(
        Path(__file__).parents[1] / "examples/workflows/au-tool-bindings.json"
    )
    for binding in bindings.values():
        validate_contract(binding, "tool")
        assert "process_recording.py" not in str(binding)
        assert "post-meeting.sh" not in str(binding)
    command = build_command(
        bindings["documents.transcribe"], "recording", "in.mp4", "out.txt"
    )
    assert "--summarize" not in command
    assert command[-2:] == ["--out", "out.txt"]
    assert (
        build_command(bindings["documents.brand"], "summary", "in.md", "out.pdf")[-1]
        == "resumen"
    )
