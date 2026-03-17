# 6 Contradiction Patterns

When explaining findings, use these pattern descriptions:

| # | Name | Signal | Example |
|---|------|--------|---------|
| ① | **Asymmetric Defaults** | Input/output paths handle the same value differently | `parseBody()` caches result but `text()`/`json()` re-reads body |
| ② | **Semantic Mismatch** | Same name means different things in different modules | `setVersionHeader` uses header name X, `getVersionHeader` reads header name Y |
| ③ | **External Spec Divergence** | Implementation contradicts the spec it claims to follow | RFC requires constant-time comparison but `==` used in some auth paths |
| ④ | **Guard Non-Propagation** | Validation present in one path, missing in a parallel path | `findById` with ACL in service A, without ACL in OAuth2 callback |
| ⑤ | **Paired-Setting Override** | Independent-looking settings secretly interfere | Two config flags that silently override each other |
| ⑥ | **Lifecycle Ordering** | Execution order assumption breaks under specific code paths | Cache consumed before alternative reader attempts access |

## Detection Heuristics

- **①**: Look for symmetric operations (encode/decode, set/get, parse/serialize) where one side handles edge cases the other doesn't
- **②**: Search for identically-named variables/functions/constants across modules and verify they carry the same semantics
- **③**: Find spec references (RFC numbers, protocol names) in comments and verify implementation matches
- **④**: Find a validation/guard in one code path, then check all parallel paths for the same guard — **this is the most common pattern (61% of findings)**
- **⑤**: Find settings/config that affect the same behavior and verify they compose correctly
- **⑥**: Find resource acquisition/release pairs and verify ordering holds across all execution paths
