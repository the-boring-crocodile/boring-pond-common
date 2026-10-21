"""Provider-neutral tool definitions from the maintained admission contracts."""
from .batch_contracts import (ReadArgs, WriteArgs, PatchArgs, ReadBytesArgs,
    WriteBytesArgs, ListArgs, SearchArgs, ExecArgs)
FILE_MODELS = {'file_read': ReadArgs, 'file_write': WriteArgs, 'file_patch': PatchArgs,
    'file_read_bytes': ReadBytesArgs, 'file_write_bytes': WriteBytesArgs,
    'file_list': ListArgs, 'file_search': SearchArgs}
MODELS = {**FILE_MODELS, 'exec': ExecArgs}

DESCRIPTIONS = {
    'file_read': 'Read a bounded text line range from a named root. Returns the whole-file SHA-256 and encoding, BOM and newline format; follow next_line if truncated.',
    'file_write': 'Replace a text file using its current whole-file expected_sha256; null creates only an absent file. Preserve existing encoding, BOM and newlines unless explicitly changed. A stale refusal requires rereading.',
    'file_patch': 'Apply exact text replacements using the current whole-file expected_sha256. Each nonempty old_text must occur exactly once; edits must not overlap. Preserves encoding and BOM; reread on stale.',
    'file_read_bytes': 'Read a bounded byte range as base64, with the whole-file SHA-256. Offset and length count bytes, not characters.',
    'file_write_bytes': 'Write base64 data using the current whole-file expected_sha256; null creates only an absent file. Omit offset to replace the whole file; otherwise overwrite bytes at an offset no greater than its length, preserving the remainder.',
    'file_list': 'List a bounded directory in a configured named root; path defaults to its root. Reports truncation and skips inaccessible or disallowed entries.',
    'file_search': 'Search text recursively for a literal, nonempty single-line query in a named root. Search depth, files, bytes and results are bounded; inspect truncation before treating results as exhaustive.',
    'exec': 'Run a bounded contained shell command as this instance Unix identity. cwd must be within configured roots but is not a filesystem sandbox. Inspect exit_code and truncated output; cancellation waits for owned cleanup.',
}


def definitions():
    return [{'type': 'function', 'function': {'name': name,
        'description': DESCRIPTIONS[name],
        'parameters': model.model_json_schema()}} for name, model in MODELS.items()]
