# boring-outcomes

The shared outcome vocabulary for the Pond family. Refused means nothing
started or changed; Failed means work started and may have had effects.
A code names the cause, independently of that status.

Outcome is their common exception base. as_data() is the only rendering:
one flat object with status, code, message and the extra detail fields.
Codes match [a-z][a-z0-9_]{0,63}; messages and detail keys are strings.
Details cannot replace those three reserved fields. Details must serialize
with json.dumps(..., allow_nan=False): cycles, non-finite numbers and
non-JSON objects are programming errors, raised during construction.
JSON-compatible tuples become arrays. Details are copied into JSON values;
rendering returns a fresh copy and validates any subsequent detail edits.

OUR_CODES is the closed shared list below. Local producers use it plus their
component's documented EXTRA_CODES, with no synonyms. Received component
outcomes remain valid when their code meets the wire format above; forwarding
does not relabel them. Source tests enforce the local vocabulary.

| Code | Cause |
|---|---|
| invalid_request | the request is malformed or unacceptable; change it |
| not_found | the named thing does not exist in this scope |
| forbidden | policy or permissions do not allow the operation |
| stale | the expected version or identity is no longer current |
| busy | a shared resource is occupied; retry later |
| locked | a lock prevents access to the resource |
| limit | a size, count or capacity limit was reached |
| not_admitted | the work could not be admitted before its deadline |
| unavailable | the component needed to answer is unavailable |
| timeout | the operation did not complete within its time limit |
| unsupported | the requested capability or format is not supported |
| unreadable | the source cannot be read or decoded |
| conflict | the requested change conflicts with current state |
| bad_reply | the downstream reply does not meet its contract |
| cleanup_unconfirmed | cleanup could not be confirmed |
| upstream_error | a downstream component reported an error |

Common's component additions are write_failed (a local save write failed after
the service answered) and foreground_interrupted (the execution owner's foreground
operation was interrupted). Their declarations and reviewed dynamic exceptions
live in [the Common source test](https://github.com/the-boring-crocodile/boring-pond-common/blob/main/scripts/test_family_vocabulary.py).

Each repository keeps one short test that scans src for literal codes in
Refused/Failed calls, accepting OUR_CODES plus documented component extras.
Dynamic calls have a short reviewed exception list. These are source tests;
the runtime ships no AST auditor. Wire constructors continue to accept
syntactically valid causes received from other components.
