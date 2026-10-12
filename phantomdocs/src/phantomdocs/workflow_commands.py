"""Pure command construction from trusted installed tool descriptors.

No subprocess execution is provided. Templates cannot come from job payloads.
Only whole input/output argument substitutions are accepted, never shell text.
"""

from pathlib import Path

from .workflows import WorkflowError, validate_contract


def build_command(binding, artifact_role, input_path, output_path):
    validate_contract(binding, "tool")
    for path in (input_path, output_path):
        if (
            not str(path)
            or str(path).startswith("-")
            or any(ord(c) < 32 for c in str(path))
        ):
            raise WorkflowError("invalid transformation path argument")
    if Path(input_path).resolve() == Path(output_path).resolve():
        raise WorkflowError("transformation output must preserve its source")
    variants = binding.get("variants", {})
    if variants and artifact_role not in variants:
        raise WorkflowError("artifact role has no declared command variant")
    arguments = variants.get(artifact_role, binding.get("arguments"))
    if arguments is None:
        raise WorkflowError("tool has no declared transformation command interface")
    replacements = {"{input}": str(input_path), "{output}": str(output_path)}
    command = [binding["entrypoint"]]
    if binding.get("script"):
        command.append(binding["script"])
    for argument in arguments:
        if argument in replacements:
            command.append(replacements[argument])
        elif "{" in argument or "}" in argument:
            raise WorkflowError("only whole input/output placeholders are supported")
        else:
            command.append(argument)
    return command
